"""WebDataset: tar shards in which consecutive files sharing a key form one sample.

``000123.jpg`` + ``000123.json`` + ``000123.cls`` -> one row with columns
``__key__``, ``__shard__``, ``jpg``, ``json``, ``cls``.
"""
from __future__ import annotations

import bisect
import bz2
import gzip
import lzma
import os
import posixpath
import tarfile
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pyarrow as pa

from .base import Source, SourceError

SHARD_SUFFIXES = (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")
TEXT_EXTS = {
    "txt", "text", "caption", "cap", "cls", "cls2", "class", "label", "labels", "index", "idx", "id",
    "key", "md", "csv", "tsv", "yaml", "yml", "xml", "html", "htm", "url", "uri", "vtt", "srt", "prompt",
}
JSON_EXTS = {"json", "jsn"}
SQL_ROW_LIMIT = 2_000_000
SQL_TEXT_BUDGET = 512 * 1024 * 1024


def is_shard(name: str) -> bool:
    return name.lower().endswith(SHARD_SUFFIXES) and not name.startswith(".")


def ext_kind(ext: str) -> str:
    last = ext.rsplit(".", 1)[-1]
    if last in JSON_EXTS:
        return "json"
    if last in TEXT_EXTS:
        return "text"
    return "binary"


def _compression(path: Path) -> str | None:
    with open(path, "rb") as f:
        head = f.read(6)
    if head[:2] == b"\x1f\x8b":
        return "gz"
    if head[:3] == b"BZh":
        return "bz2"
    if head[:6] == b"\xfd7zXZ\x00":
        return "xz"
    return None


@dataclass
class Shard:
    path: Path
    size: int
    compression: str | None
    samples: list | None = None  # [(key, {ext: (offset, size)})]
    error: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class WebDatasetSource(Source):
    format = "webdataset"
    label = "WebDataset"

    def __init__(self, path, params, engine):
        super().__init__(path, params, engine)
        if self.path.is_dir():
            files = sorted(p for p in self.path.iterdir() if p.is_file() and is_shard(p.name))
        else:
            files = [self.path]
        if not files:
            raise SourceError("目录中没有找到 .tar 分片")
        self.shards = [Shard(p, p.stat().st_size, _compression(p)) for p in files]
        self._cum = [0]  # cumulative sample counts over the indexed prefix
        self._exts: list[str] = []
        self._prefix_lock = threading.Lock()
        self._sql: pa.Table | None = None
        self._sql_note: str | None = None
        self._index_upto(0)
        if not self.shards[0].samples and self.shards[0].error:
            raise SourceError(f"无法读取分片 {self.shards[0].path.name}：{self.shards[0].error}")
        if len(self.shards) > 1:
            threading.Thread(target=self._index_all, name="wds-index", daemon=True).start()

    # ------------------------------------------------------------------
    # indexing
    # ------------------------------------------------------------------
    def _index_shard(self, shard: Shard):
        with shard.lock:
            if shard.samples is not None:
                return
            samples: list = []
            try:
                with tarfile.open(shard.path, "r:*") as tf:
                    cur_key, cur = None, None
                    count = 0
                    while True:
                        m = tf.next()
                        if m is None:
                            break
                        count += 1
                        if count % 2000 == 0:
                            tf.members.clear()  # keep memory flat on big shards
                        if not m.isfile():
                            continue
                        d, base = posixpath.split(m.name)
                        if base.startswith(".") or "." not in base:
                            continue
                        prefix, ext = base.split(".", 1)
                        key = f"{d}/{prefix}" if d else prefix
                        if key != cur_key:
                            cur = {}
                            samples.append((key, cur))
                            cur_key = key
                        cur[ext.lower()] = (m.offset_data, m.size)
            except Exception as e:
                shard.error = str(e)
            shard.samples = samples

    def _advance_prefix(self):
        with self._prefix_lock:
            k = len(self._cum) - 1
            while k < len(self.shards) and self.shards[k].samples is not None:
                for _, members in self.shards[k].samples[:2000]:
                    for ext in members:
                        if ext not in self._exts:
                            self._exts.append(ext)
                self._cum.append(self._cum[-1] + len(self.shards[k].samples))
                k += 1

    def _index_upto(self, row: int):
        """Index shards in order until ``row`` is covered (or all shards are indexed)."""
        while self._cum[-1] <= row and len(self._cum) - 1 < len(self.shards):
            self._index_shard(self.shards[len(self._cum) - 1])
            self._advance_prefix()

    def _index_all(self):
        for s in self.shards:
            self._index_shard(s)
            self._advance_prefix()

    @property
    def fully_indexed(self) -> bool:
        return len(self._cum) - 1 == len(self.shards)

    def _locate(self, i: int) -> tuple[int, int]:
        k = bisect.bisect_right(self._cum, i) - 1
        return k, i - self._cum[k]

    # ------------------------------------------------------------------
    # schema & reading
    # ------------------------------------------------------------------
    def _field(self, name: str) -> pa.Field:
        if name in ("__key__", "__shard__"):
            return pa.field(name, pa.string())
        kind = ext_kind(name)
        if kind == "json":
            return pa.field(name, pa.string(), metadata={"dv:kind": "json"})
        if kind == "text":
            return pa.field(name, pa.string())
        return pa.field(name, pa.large_binary())

    def column_names(self) -> list[str]:
        return ["__key__", "__shard__"] + list(self._exts)

    def schema(self):
        return pa.schema([self._field(n) for n in self.column_names()])

    def num_rows(self):
        return self._cum[-1] if self.fully_indexed else None

    def count_status(self):
        done = len(self._cum) - 1
        return {
            "count": self._cum[-1],
            "exact": self.fully_indexed,
            "pending": not self.fully_indexed,
            "progress": f"{done}/{len(self.shards)} 个分片已索引",
        }

    def count_rows(self):
        self._index_all()
        return self._cum[-1]

    def _open(self, shard: Shard):
        if shard.compression == "gz":
            return gzip.open(shard.path, "rb")
        if shard.compression == "bz2":
            return bz2.open(shard.path, "rb")
        if shard.compression == "xz":
            return lzma.open(shard.path, "rb")
        return open(shard.path, "rb")

    def _read_members(self, shard: Shard, wanted: list[tuple[int, int]]) -> dict[int, bytes]:
        out: dict[int, bytes] = {}
        if not wanted:
            return out
        with self._open(shard) as f:
            for off, size in sorted(set(wanted)):
                f.seek(off)
                out[off] = f.read(size)
        return out

    def _rows(self, indices: list[int], columns: list[str]) -> pa.Table:
        located = [self._locate(i) for i in indices]
        exts = [c for c in columns if c not in ("__key__", "__shard__")]
        by_shard: dict[int, list] = {}
        for k, j in located:
            members = self.shards[k].samples[j][1]
            for e in exts:
                if e in members:
                    by_shard.setdefault(k, []).append(members[e])
        content = {k: self._read_members(self.shards[k], w) for k, w in by_shard.items()}
        arrays = []
        for c in columns:
            vals = []
            for k, j in located:
                key, members = self.shards[k].samples[j]
                if c == "__key__":
                    vals.append(key)
                elif c == "__shard__":
                    vals.append(self.shards[k].path.name)
                elif c in members:
                    raw = content[k][members[c][0]]
                    vals.append(raw.decode("utf-8", "replace") if ext_kind(c) != "binary" else raw)
                else:
                    vals.append(None)
            arrays.append(pa.array(vals, type=self._field(c).type))
        return pa.Table.from_arrays(arrays, schema=pa.schema([self._field(c) for c in columns]))

    def read(self, offset, limit, columns=None):
        offset, limit = int(offset), int(limit)
        self._index_upto(offset + limit - 1)
        total = self._cum[-1]
        a, b = min(offset, total), min(offset + limit, total)
        return self._rows(list(range(a, b)), columns or self.column_names())

    def take(self, indices, columns=None):
        idx = [int(i) for i in indices]
        if idx:
            self._index_upto(max(idx))
        idx = [i for i in idx if i < self._cum[-1]]
        return self._rows(idx, columns or self.column_names())

    # ------------------------------------------------------------------
    # SQL
    # ------------------------------------------------------------------
    def lazy_columns(self):
        return {e: "字节数" for e in self._exts if ext_kind(e) == "binary"}

    def _sql_table(self) -> pa.Table:
        if self._sql is not None:
            return self._sql
        self._index_all()
        total = self._cum[-1]
        n = min(total, SQL_ROW_LIMIT)
        exts = list(self._exts)
        text_exts = [e for e in exts if ext_kind(e) != "binary"]
        cols: dict[str, list] = {"__key__": [], "__shard__": []}
        for e in exts:
            cols[e] = []
        budget = SQL_TEXT_BUDGET
        rows = 0
        for k, shard in enumerate(self.shards):
            if rows >= n:
                break
            samples = shard.samples[: n - rows]
            wanted = [m[e] for _, m in samples for e in text_exts if e in m]
            content = self._read_members(shard, wanted) if budget > 0 else {}
            budget -= sum(len(v) for v in content.values())
            for key, m in samples:
                cols["__key__"].append(key)
                cols["__shard__"].append(shard.path.name)
                for e in exts:
                    if e not in m:
                        cols[e].append(None)
                    elif ext_kind(e) == "binary":
                        cols[e].append(m[e][1])
                    else:
                        raw = content.get(m[e][0])
                        cols[e].append(raw.decode("utf-8", "replace") if raw is not None else None)
            rows += len(samples)
            if budget <= 0 and not self._sql_note:
                self._sql_note = "文本字段超过 512 MB，之后样本的文本内容在 SQL 中为空"
        if n < total:
            self._sql_note = f"样本太多，SQL 只载入了前 {n:,} 个（共 {total:,} 个）"
        arrays = {"__index__": pa.array(np.arange(rows, dtype=np.int64))}
        arrays["__key__"] = pa.array(cols["__key__"], pa.string())
        arrays["__shard__"] = pa.array(cols["__shard__"], pa.string())
        for e in exts:
            arrays[e] = pa.array(cols[e], pa.int64() if ext_kind(e) == "binary" else pa.string())
        self._sql = pa.table(arrays)
        return self._sql

    def register(self, con, name):
        con.register(name, self._sql_table())

    def warnings(self):
        out = [f"分片 {s.path.name} 读取出错：{s.error}" for s in self.shards if s.error][:10]
        if self._sql_note:
            out.append(self._sql_note)
        return out

    # ------------------------------------------------------------------
    def info(self):
        shards = []
        field_count: dict[str, int] = {}
        field_bytes: dict[str, int] = {}
        for s in self.shards[:2000]:
            shards.append({
                "name": s.path.name, "size": s.size, "compression": s.compression,
                "samples": len(s.samples) if s.samples is not None else None, "error": s.error,
            })
            if s.samples is not None:
                for _, m in s.samples:
                    for e, (_, size) in m.items():
                        field_count[e] = field_count.get(e, 0) + 1
                        field_bytes[e] = field_bytes.get(e, 0) + size
        first = self.shards[0].samples or []
        return {
            "shards": shards,
            "shard_count": len(self.shards),
            "indexed_shards": len(self._cum) - 1,
            "samples_indexed": self._cum[-1],
            "fully_indexed": self.fully_indexed,
            "total_size": sum(s.size for s in self.shards),
            "fields": [{"ext": e, "kind": ext_kind(e), "count": field_count.get(e, 0), "bytes": field_bytes.get(e, 0)}
                       for e in self._exts],
            "example_keys": [k for k, _ in first[:8]],
        }
