"""FastAPI application: file browsing, data pages, SQL, statistics and cell content."""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import secrets
import threading
import time
import traceback
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import numpy as np
import pyarrow as pa
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, encode, media, stats
from .engine import Engine, QueryError, browse_sql
from .sources import FORMAT_LABELS, Source, SourceError, detect, detect_dir, detect_file, is_lance_dir, open_source
from .util import decode_metadata, json_safe

log = logging.getLogger("data_viewer")
STATIC_DIR = Path(os.environ.get("DATA_VIEWER_STATIC_DIR", Path(__file__).parent / "static"))
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}
MAX_PAGE = 1000


@dataclass
class AppConfig:
    root: Path
    initial: Path | None = None
    host: str = "127.0.0.1"
    token: str | None = None
    threads: int | None = None
    memory_limit: str | None = None
    query_timeout: float = 120.0
    extra_hosts: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# registries
# ---------------------------------------------------------------------------

class Registry:
    def __init__(self, engine: Engine, max_open: int = 24):
        self.engine = engine
        self.max_open = max_open
        self._items: OrderedDict[str, Source] = OrderedDict()
        self._lock = threading.Lock()

    @staticmethod
    def key(path: Path, fmt: str | None, params: dict) -> str:
        raw = json.dumps([str(path), fmt, params], sort_keys=True, default=str)
        return hashlib.sha1(raw.encode()).hexdigest()[:12]

    def open(self, path: Path, fmt: str | None, params: dict) -> Source:
        fmt = fmt or detect(path)
        sid = self.key(path, fmt, params)
        with self._lock:
            if sid in self._items:
                self._items.move_to_end(sid)
                return self._items[sid]
        src = open_source(path, fmt, params, self.engine)
        src.id = sid  # type: ignore[attr-defined]
        with self._lock:
            existing = self._items.get(sid)
            if existing is not None:
                src.close()
                return existing
            self._items[sid] = src
            while len(self._items) > self.max_open:
                _, old = self._items.popitem(last=False)
                old.close()
        return src

    def get(self, sid: str) -> Source:
        with self._lock:
            src = self._items.get(sid)
            if src is None:
                raise HTTPException(404, "数据源已关闭，请重新打开")
            self._items.move_to_end(sid)
            return src


@dataclass
class CachedPage:
    source_id: str
    table: pa.Table
    abs_base: int | None


class PageCache:
    def __init__(self, max_items: int = 128, max_bytes: int = 1 << 30):
        self.max_items = max_items
        self.max_bytes = max_bytes
        self._items: OrderedDict[str, CachedPage] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def put(self, source_id: str, table: pa.Table, abs_base: int | None) -> str:
        pid = uuid.uuid4().hex[:16]
        size = table.nbytes
        with self._lock:
            self._items[pid] = CachedPage(source_id, table, abs_base)
            self._bytes += size
            while self._items and (len(self._items) > self.max_items or self._bytes > self.max_bytes):
                _, old = self._items.popitem(last=False)
                self._bytes -= old.table.nbytes
        return pid

    def get(self, pid: str) -> CachedPage | None:
        with self._lock:
            page = self._items.get(pid)
            if page is not None:
                self._items.move_to_end(pid)
            return page


# ---------------------------------------------------------------------------
# request models
# ---------------------------------------------------------------------------

class OpenReq(BaseModel):
    path: str
    format: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class RowsReq(BaseModel):
    offset: int = 0
    limit: int = 50
    columns: list[str] | None = None
    where: str | None = None
    order: list[dict[str, str]] | None = None


class SqlReq(BaseModel):
    sql: str
    offset: int = 0
    limit: int = 50


class CountReq(BaseModel):
    where: str | None = None
    sql: str | None = None


class StatsReq(BaseModel):
    where: str | None = None
    sql: str | None = None
    sample: int | None = None
    column: str | None = None


# ---------------------------------------------------------------------------
# app factory
# ---------------------------------------------------------------------------

def create_app(config: AppConfig) -> FastAPI:
    root = config.root.resolve()
    engine = Engine(threads=config.threads, memory_limit=config.memory_limit)
    registry = Registry(engine)
    pages = PageCache()
    app = FastAPI(title="Data Viewer", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.engine = engine
    app.state.registry = registry

    allowed_hosts = set(LOOPBACK) | {h.lower() for h in config.extra_hosts}
    # Binding to a specific address: only accept that name (defeats DNS rebinding).
    # Binding to all interfaces: hosts vary, so access is protected by the token instead.
    check_host = config.host not in ("0.0.0.0", "::", "")
    if check_host:
        allowed_hosts.add(config.host.lower())

    # -- security --------------------------------------------------------
    @app.middleware("http")
    async def guard(request: Request, call_next):
        host_header = request.headers.get("host", "")
        hostname = host_header.rsplit(":", 1)[0] if not host_header.startswith("[") else host_header.split("]")[0] + "]"
        if check_host and hostname.lower() not in allowed_hosts:
            return PlainTextResponse("Forbidden: unexpected Host header", status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlparse(origin).netloc != host_header:
                return PlainTextResponse("Forbidden: cross-origin request", status_code=403)
            if not request.headers.get("content-type", "").startswith("application/json"):
                return PlainTextResponse("Unsupported content type", status_code=415)
        if config.token:
            supplied = request.query_params.get("token")
            cookie = request.cookies.get("dv_token")
            header = request.headers.get("x-dv-token")
            if supplied and secrets.compare_digest(supplied, config.token):
                resp = RedirectResponse(request.url.path or "/") if request.url.path == "/" else await call_next(request)
                resp.set_cookie("dv_token", config.token, httponly=True, samesite="strict")
                return resp
            if not any(v and secrets.compare_digest(v, config.token) for v in (cookie, header)):
                return PlainTextResponse("需要访问令牌：请使用启动时打印的完整链接打开", status_code=401)
        return await call_next(request)

    # -- errors -----------------------------------------------------------
    @app.exception_handler(QueryError)
    async def _query_error(_, exc: QueryError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(SourceError)
    async def _source_error(_, exc: SourceError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(HTTPException)
    async def _http_error(_, exc: HTTPException):
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    @app.exception_handler(Exception)
    async def _unexpected(_, exc: Exception):
        log.error("unhandled error: %s", traceback.format_exc())
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)

    # -- path helpers -----------------------------------------------------
    def resolve(rel: str | None) -> Path:
        p = Path(rel or ".")
        p = (p if p.is_absolute() else root / p).resolve()
        if p != root and root not in p.parents:
            raise HTTPException(403, "只能访问根目录以内的文件")
        if not p.exists():
            raise HTTPException(404, f"路径不存在：{rel}")
        return p

    def rel(p: Path) -> str:
        p = p.resolve()
        return "" if p == root else p.relative_to(root).as_posix()

    def within_root(p: Path) -> bool:
        return p == root or root in p.parents

    def path_resolver(src: Source):
        base = src.base_dir().resolve()
        candidates_base = [base, base.parent, root]
        memo: dict[str, dict | None] = {}

        def resolve_media(s: str):
            if s in memo:
                return memo[s]
            result = None
            if s.startswith(("http://", "https://")):
                result = {"url": s, "mime": media.ext_mime(urlparse(s).path), "remote": True}
            else:
                raw = s[7:] if s.startswith("file://") else s
                p = Path(raw).expanduser()
                for cand in ([p] if p.is_absolute() else [b / p for b in candidates_base]):
                    try:
                        cand = cand.resolve()
                    except (OSError, RuntimeError):
                        continue
                    if within_root(cand) and cand.is_file():
                        result = {"url": "/api/fs/raw?path=" + quote(rel(cand)), "mime": media.ext_mime(cand.name)}
                        break
            memo[s] = result
            return result

        return resolve_media

    # -- descriptors ------------------------------------------------------
    def descriptor(src: Source) -> dict:
        schema = src.schema()
        d = {
            "id": src.id,  # type: ignore[attr-defined]
            "name": src.name,
            "path": rel(src.path),
            "format": src.format,
            "label": src.label,
            "params": src.params,
            "columns": [encode.describe_field(f) for f in schema],
            "count": src.count_status(),
            "lazy": src.lazy_columns(),
            "warnings": src.warnings(),
        }
        if src.format == "lance":
            d["lance"] = {"version": src.ds.version, "latest_version": _safe(lambda: src.ds.latest_version)}  # type: ignore[attr-defined]
        if src.format == "hdf5":
            d["hdf5"] = {"node": src.node, "node_kind": src.node_kind}  # type: ignore[attr-defined]
        from .embodied import supports_trajectory
        d["views"] = ["trajectory"] if supports_trajectory(src.path,src.format,schema.names) else []
        return d

    def make_page(src: Source, table: pa.Table, mode: str, offset: int, has_more: bool, elapsed: float,
                  sql: str | None = None) -> dict:
        abs_base = offset if mode == "browse" else None
        pid = pages.put(src.id, table, abs_base)  # type: ignore[attr-defined]
        lazy = src.lazy_columns()
        enc_table = table
        lazy_cols: list[str] = []
        if mode == "query" and "__index__" in table.column_names:
            lazy_cols = [c for c in table.column_names if c in lazy]
            if lazy_cols:
                idx = table.column("__index__").to_pylist()
                if all(i is not None for i in idx):
                    real = src.take(idx, lazy_cols)
                    for c in lazy_cols:
                        pos = enc_table.column_names.index(c)
                        enc_table = enc_table.set_column(pos, real.schema.field(c), real.column(c))
                else:
                    lazy_cols = []
        columns, rows = encode.encode_table(enc_table, encode.EncodeContext(path_resolver=path_resolver(src)))
        for col in columns:
            if col["name"] in lazy_cols:
                col["lazy"] = True
        cell_url = f"/api/p/{pid}/cell?row={{row}}&col={{col}}"
        if mode == "browse":  # lets the server fall back to the source if the page left the cache
            cell_url += f"&sid={src.id}&abs={{abs}}"  # type: ignore[attr-defined]
        return {
            "page_id": pid,
            "mode": mode,
            "offset": offset,
            "columns": columns,
            "rows": rows,
            "has_more": has_more,
            "elapsed": round(elapsed, 4),
            "cell_url": cell_url,
            "sql": sql,
            "index_col": "__index__" if "__index__" in table.column_names else None,
            "warnings": src.warnings(),
        }

    def clamp(limit: int) -> int:
        return max(1, min(MAX_PAGE, int(limit)))

    # -- routes: meta & files ---------------------------------------------
    @app.get("/api/config")
    def api_config():
        initial = None
        if config.initial is not None:
            initial = {"path": rel(config.initial), "format": detect(config.initial)}
        return {"root": str(root), "root_name": root.name or str(root), "version": __version__,
                "initial": initial, "formats": FORMAT_LABELS}

    @app.get("/api/fs/list")
    def fs_list(path: str = "", hidden: bool = False):
        p = resolve(path)
        if not p.is_dir():
            raise HTTPException(400, "不是目录")
        entries = []
        truncated = False
        try:
            children = sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower()))
        except PermissionError:
            raise HTTPException(403, "没有权限读取这个目录") from None
        for child in children:
            if not hidden and child.name.startswith("."):
                continue
            if len(entries) >= 3000:
                truncated = True
                break
            try:
                st = child.stat()
                is_dir = child.is_dir()
            except OSError:
                continue
            fmt = ("lance" if is_lance_dir(child) else None) if is_dir else detect_file(child.name)
            entries.append({
                "name": child.name, "path": rel(child), "dir": is_dir,
                "size": None if is_dir else st.st_size, "mtime": st.st_mtime, "format": fmt,
                "media": None if is_dir else media.category(media.ext_mime(child.name)),
            })
        return {
            "path": rel(p), "name": p.name or str(p), "parent": None if p == root else rel(p.parent),
            "dir_format": detect_dir(p), "entries": entries, "truncated": truncated,
        }

    @app.get("/api/fs/raw")
    def fs_raw(path: str, thumb: int | None = Query(None, ge=16, le=4096)):
        p = resolve(path)
        if not p.is_file():
            raise HTTPException(400, "不是文件")
        mime = media.ext_mime(p.name) or mimetypes.guess_type(p.name)[0] or "application/octet-stream"
        if thumb and media.category(mime) == "image" and p.stat().st_size < 256 * 1024 * 1024:
            small = media.thumbnail(p.read_bytes(), thumb)
            if small:
                return Response(small[0], media_type=small[1], headers={"Cache-Control": "private, max-age=300"})
        if mime == "image/tiff":
            conv = media.to_browser_image(p.read_bytes()) if p.stat().st_size < 256 * 1024 * 1024 else None
            if conv:
                return Response(conv[0], media_type=conv[1])
        if mime.startswith("text/"):
            mime += "; charset=utf-8"
        return FileResponse(p, media_type=mime, headers={"Cache-Control": "private, max-age=60"})

    @app.get("/api/fs/text")
    def fs_text(path: str, limit: int = 200_000):
        p = resolve(path)
        with open(p, "rb") as fh:
            data = fh.read(limit + 1)
        text = media.looks_like_text(data[:4096])
        if text is None:
            raise HTTPException(400, "不是文本文件")
        return {"text": data[:limit].decode("utf-8", "replace"), "truncated": len(data) > limit, "size": p.stat().st_size}

    # -- routes: sources --------------------------------------------------
    @app.post("/api/open")
    def api_open(req: OpenReq):
        p = resolve(req.path)
        params = {k: v for k, v in (req.params or {}).items() if v not in (None, "")}
        src = registry.open(p, req.format, params)
        return descriptor(src)

    @app.get("/api/s/{sid}")
    def api_source(sid: str):
        return descriptor(registry.get(sid))

    @app.get("/api/s/{sid}/info")
    def api_info(sid: str):
        src = registry.get(sid)
        return json_safe(src.info())

    @app.get("/api/s/{sid}/schema")
    def api_schema(sid: str):
        src = registry.get(sid)
        schema = src.schema()
        meta = decode_metadata(schema.metadata)
        parsed = {}
        for k, v in meta.items():
            s = v.strip()
            if s[:1] in "{[":
                try:
                    parsed[k] = json.loads(s)
                    continue
                except ValueError:
                    pass
            parsed[k] = v
        return {"fields": [encode.field_tree(f) for f in schema], "metadata": json_safe(parsed), "lazy": src.lazy_columns()}

    @app.post("/api/s/{sid}/rows")
    def api_rows(sid: str, req: RowsReq):
        src = registry.get(sid)
        limit, offset = clamp(req.limit), max(0, int(req.offset))
        if not (req.where and req.where.strip()) and not req.order:
            t0 = time.perf_counter()
            table = src.read(offset, limit + 1, req.columns)
            has_more = table.num_rows > limit
            table = table.slice(0, limit)
            return JSONResponse(make_page(src, table, "browse", offset, has_more, time.perf_counter() - t0))
        sql = browse_sql(req.columns, req.where, req.order)
        table, has_more, elapsed = engine.page(src, sql, offset, limit, config.query_timeout)
        return JSONResponse(make_page(src, table, "query", offset, has_more, elapsed, sql))

    @app.post("/api/s/{sid}/sql")
    def api_sql(sid: str, req: SqlReq):
        src = registry.get(sid)
        limit, offset = clamp(req.limit), max(0, int(req.offset))
        table, has_more, elapsed = engine.page(src, req.sql, offset, limit, config.query_timeout)
        return JSONResponse(make_page(src, table, "query", offset, has_more, elapsed, req.sql))

    @app.post("/api/s/{sid}/count")
    def api_count(sid: str, req: CountReq):
        src = registry.get(sid)
        if req.sql:
            return {"count": engine.count(src, req.sql, config.query_timeout), "exact": True, "pending": False}
        if req.where and req.where.strip():
            sql = browse_sql(None, req.where, None)
            return {"count": engine.count(src, sql, config.query_timeout), "exact": True, "pending": False}
        status = src.count_status()
        if status["pending"] and src.format in ("csv", "json"):
            src.count_rows()
            status = src.count_status()
        return status

    def stats_sql(req: StatsReq) -> str:
        if req.sql:
            return req.sql
        return browse_sql(None, req.where, None)

    @app.post("/api/s/{sid}/stats")
    def api_stats(sid: str, req: StatsReq):
        src = registry.get(sid)
        sample = int(req.sample) if req.sample else None
        unfiltered = not req.sql and not (req.where and req.where.strip())
        known = src.num_rows() if unfiltered else None
        return stats.summary(engine, src, stats_sql(req), sample, config.query_timeout, known_rows=known)

    @app.post("/api/s/{sid}/stats/column")
    def api_column_stats(sid: str, req: StatsReq):
        src = registry.get(sid)
        if not req.column:
            raise HTTPException(400, "缺少列名")
        sample = int(req.sample) if req.sample else None
        return stats.column_detail(engine, src, stats_sql(req), req.column, sample, timeout=config.query_timeout)

    # -- routes: cell content ---------------------------------------------
    def cell_from_table(table: pa.Table, row: int, col: str):
        if col not in table.column_names:
            raise HTTPException(404, f"没有这一列：{col}")
        if row < 0 or row >= table.num_rows:
            raise HTTPException(404, "行号超出范围")
        f = table.schema.field(col)
        return encode.scalar_value(table.column(col).slice(row, 1).combine_chunks(), f)

    @app.get("/api/p/{pid}/cell")
    def api_page_cell(request: Request, pid: str, row: int, col: str, sub: str | None = None,
                      thumb: int | None = Query(None, ge=16, le=4096), format: str | None = None,
                      sid: str | None = None, abs: int | None = None):
        page = pages.get(pid)
        if page is None:
            if sid is None or abs is None:
                raise HTTPException(410, "页面缓存已过期，请刷新")
            src = registry.get(sid)
            kind, value = cell_from_table(src.read(abs, 1, [col]), 0, col)
            return cell_response(request, kind, value, sub, thumb, format)
        src = registry.get(page.source_id)
        table = page.table
        lazy = src.lazy_columns()
        if col in lazy and "__index__" in table.column_names and page.abs_base is None:
            index = table.column("__index__")[row].as_py()
            kind, value = cell_from_table(src.take([index], [col]), 0, col)
        else:
            kind, value = cell_from_table(table, row, col)
        return cell_response(request, kind, value, sub, thumb, format)

    @app.get("/api/s/{sid}/cell")
    def api_source_cell(request: Request, sid: str, row: int, col: str, sub: str | None = None,
                        thumb: int | None = Query(None, ge=16, le=4096), format: str | None = None):
        src = registry.get(sid)
        kind, value = cell_from_table(src.read(row, 1, [col]), 0, col)
        return cell_response(request, kind, value, sub, thumb, format)

    def cell_response(request: Request, kind: str, value: Any, sub: str | None, thumb: int | None, fmt: str | None):
        if sub:
            try:
                value = encode.navigate(value, json.loads(sub))
            except (KeyError, IndexError, ValueError, TypeError):
                raise HTTPException(404, "嵌套路径不存在") from None
            kind = "binary" if isinstance(value, (bytes, bytearray, memoryview)) else "nested"
        if value is None:
            return JSONResponse(None)
        if isinstance(value, (bytes, bytearray, memoryview)):
            data = bytes(value)
            mime = media.sniff_mime(data)
            if mime and thumb and media.category(mime) == "image":
                small = media.thumbnail(data, thumb)
                if small:
                    return Response(small[0], media_type=small[1], headers={"Cache-Control": "private, max-age=300"})
            if mime in media.IMAGE_CONVERTIBLE or mime == "image/heic":
                conv = media.to_browser_image(data)
                if conv:
                    return Response(conv[0], media_type=conv[1])
            if mime == "application/x-npy":
                arr = media.load_npy(data, max_bytes=512 * 1024 * 1024)
                if arr is not None:
                    return ndarray_response(arr, thumb, fmt)
            if fmt == "text":
                return PlainTextResponse(data.decode("utf-8", "replace"))
            return bytes_response(request, data, mime or "application/octet-stream")
        if isinstance(value, np.ndarray):
            return ndarray_response(value, thumb, fmt)
        if isinstance(value, str) and kind in ("string", "timestamp", "date", "time", "decimal"):
            return PlainTextResponse(value)
        return JSONResponse(json_safe(encode.full_json(kind, value)))

    def ndarray_response(arr: np.ndarray, thumb: int | None, fmt: str | None):
        if fmt == "png":
            png = media.array_to_png(arr, max_side=thumb or 2048)
            if png is None:
                raise HTTPException(400, "这个数组不像图像")
            return Response(png, media_type="image/png", headers={"Cache-Control": "private, max-age=300"})
        info = encode.nd_summary(arr, thumb_side=64)
        info.pop("img", None)
        if arr.size <= 250_000:
            info["data"] = json_safe(arr.tolist())
        else:
            info["too_large"] = True
        return JSONResponse(info)

    from .embodied import router as embodied_router
    app.include_router(embodied_router(root))

    # -- static UI ---------------------------------------------------------
    if STATIC_DIR.is_dir():
        app.mount("/static", RevalidatingStatic(directory=STATIC_DIR), name="static")

        @app.get("/")
        def index():
            return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    return app


class RevalidatingStatic(StaticFiles):
    """Static files that the browser must revalidate (cheap 304s), so upgrades show up at once."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache"
        return resp


def bytes_response(request: Request, data: bytes, mime: str) -> Response:
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "private, max-age=300"}
    rng = request.headers.get("range")
    size = len(data)
    if rng and rng.startswith("bytes=") and size:
        try:
            first = rng[6:].split(",")[0].strip()
            start_s, end_s = first.split("-", 1)
            if start_s == "":
                start, end = max(0, size - int(end_s)), size - 1
            else:
                start = int(start_s)
                end = min(int(end_s), size - 1) if end_s else size - 1
            if start > end or start >= size:
                return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
            headers["Content-Range"] = f"bytes {start}-{end}/{size}"
            return Response(data[start:end + 1], status_code=206, media_type=mime, headers=headers)
        except ValueError:
            pass
    return Response(data, media_type=mime, headers=headers)


def _safe(fn):
    try:
        return fn()
    except Exception:
        return None
