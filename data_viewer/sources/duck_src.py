"""Formats DuckDB reads natively: Parquet, CSV/TSV, JSON/JSONL (files or directories)."""
from __future__ import annotations

import bisect
import json
import os
import threading
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ..util import decode_metadata, json_safe, sql_ident, sql_str
from .base import Source, SourceError, empty_like

SUFFIXES = {
    "parquet": (".parquet", ".pq", ".parq"),
    "csv": (".csv", ".tsv", ".csv.gz", ".tsv.gz", ".csv.zst", ".tsv.zst", ".psv"),
    "json": (".json", ".jsonl", ".ndjson", ".json.gz", ".jsonl.gz", ".ndjson.gz", ".jsonl.zst", ".json.zst"),
}
MAX_LISTED_FILES = 2000


def list_data_files(root: Path, fmt: str, limit: int = 200_000) -> list[Path]:
    suffixes = SUFFIXES[fmt]
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith((".", "_")))
        for fn in sorted(filenames):
            if fn.startswith((".", "_")):
                continue
            if fn.lower().endswith(suffixes):
                out.append(Path(dirpath) / fn)
                if len(out) >= limit:
                    return out
    return out


class DuckSource(Source):
    def __init__(self, path, params, engine, fmt: str):
        super().__init__(path, params, engine)
        self.format = fmt
        self.label = {"parquet": "Parquet", "csv": "CSV", "json": "JSON"}[fmt]
        self.is_dir = self.path.is_dir()
        self.files = list_data_files(self.path, fmt) if self.is_dir else [self.path]
        if not self.files:
            raise SourceError(f"目录中没有找到 {self.label} 文件")
        self.scan = self._scan_sql()
        self._schema: pa.Schema | None = None
        self._pf: pq.ParquetFile | None = None
        self._rg_starts: list[int] = []
        self._pf_lock = threading.Lock()
        if self._single_parquet:
            try:
                self._pf = pq.ParquetFile(str(self.path))
            except Exception as e:
                raise SourceError(f"无法读取 Parquet 文件：{e}") from None
            md = self._pf.metadata
            starts, acc = [], 0
            for i in range(md.num_row_groups):
                starts.append(acc)
                acc += md.row_group(i).num_rows
            self._rg_starts = starts
            self._count = md.num_rows
            self._schema = self._pf.schema_arrow
        else:
            # Bind the scan once so format problems surface when the file is opened.
            self.schema()

    @property
    def _single_parquet(self) -> bool:
        return self.format == "parquet" and not self.is_dir

    # ------------------------------------------------------------------
    def _target_sql(self) -> str:
        if not self.is_dir:
            return sql_str(str(self.path))
        if len(self.files) > MAX_LISTED_FILES:
            ext = SUFFIXES[self.format][0]
            return sql_str(str(self.path / "**" / f"*{ext}"))
        return "[" + ", ".join(sql_str(str(f)) for f in self.files) + "]"

    def _scan_sql(self) -> str:
        target = self._target_sql()
        multi = self.is_dir
        if self.format == "parquet":
            opts = ", hive_partitioning = true, union_by_name = true" if multi else ""
            return f"read_parquet({target}{opts})"
        if self.format == "csv":
            opts = ", union_by_name = true" if multi else ""
            if self.params.get("all_varchar"):
                opts += ", all_varchar = true"
            return f"read_csv({target}, auto_detect = true{opts})"
        opts = ", union_by_name = true" if multi else ""
        return f"read_json_auto({target}, maximum_object_size = 536870912{opts})"

    # ------------------------------------------------------------------
    def schema(self) -> pa.Schema:
        if self._schema is None:
            with self.engine.session(self) as cur:
                self._schema = self.engine.query(cur, "SELECT * FROM data LIMIT 0", timeout=120).schema
        return self._schema

    def register(self, con, name):
        con.execute(f"CREATE OR REPLACE TEMP VIEW {sql_ident(name)} AS SELECT * FROM {self.scan}")

    def num_rows(self):
        return self._count

    def count_status(self):
        if self._count is None and self.format == "parquet":
            self.count_rows()  # parquet counts come from file footers: cheap
        return super().count_status()

    def read(self, offset, limit, columns=None):
        if self._single_parquet:
            return self._read_parquet(int(offset), int(limit), columns)
        cols = ", ".join(sql_ident(c) for c in columns) if columns else "*"
        with self.engine.session(self) as cur:
            return self.engine.query(cur, f"SELECT {cols} FROM data LIMIT {int(limit)} OFFSET {int(offset)}", timeout=300)

    def _read_parquet(self, offset: int, limit: int, columns):
        pf = self._pf
        assert pf is not None
        total = pf.metadata.num_rows
        if offset >= total or limit <= 0:
            return empty_like(self.schema(), columns)
        end = min(total, offset + limit)
        starts = self._rg_starts
        first = bisect.bisect_right(starts, offset) - 1
        groups = []
        i = first
        while i < len(starts) and starts[i] < end:
            groups.append(i)
            i += 1
        skip = offset - starts[first]
        need = end - offset
        batches = []
        with self._pf_lock:
            for batch in pf.iter_batches(batch_size=max(256, min(need + skip, 8192)), row_groups=groups,
                                         columns=columns, use_threads=True):
                if skip >= batch.num_rows:
                    skip -= batch.num_rows
                    continue
                if skip:
                    batch = batch.slice(skip)
                    skip = 0
                if batch.num_rows > need:
                    batch = batch.slice(0, need)
                batches.append(batch)
                need -= batch.num_rows
                if need <= 0:
                    break
        if not batches:
            return empty_like(self.schema(), columns)
        return pa.Table.from_batches(batches)

    def take(self, indices, columns=None):
        if self._single_parquet:
            parts = [self._read_parquet(int(i), 1, columns) for i in indices]
            return pa.concat_tables(parts) if parts else empty_like(self.schema(), columns)
        return super().take(indices, columns)

    # ------------------------------------------------------------------
    def info(self):
        out: dict = {"files": len(self.files), "scan": self.scan}
        rel = lambda p: str(Path(p).relative_to(self.path)) if self.is_dir else Path(p).name  # noqa: E731
        listing = []
        for f in self.files[:500]:
            try:
                size = os.path.getsize(f)
            except OSError:
                size = None
            listing.append({"path": rel(f), "size": size})
        out["file_list"] = listing
        out["total_size"] = sum(x["size"] or 0 for x in listing) if len(self.files) <= 500 else None
        if self.format == "parquet":
            out.update(self._parquet_info())
        elif self.format == "csv":
            out.update(self._csv_info())
        else:
            out.update(self._json_info())
        return out

    def _parquet_info(self):
        path = self.files[0]
        try:
            pf = self._pf if self._single_parquet else pq.ParquetFile(str(path))
        except Exception as e:
            return {"parquet_error": str(e)}
        md = pf.metadata
        d: dict = {
            "inspected_file": path.name,
            "num_rows": md.num_rows,
            "num_row_groups": md.num_row_groups,
            "num_columns": md.num_columns,
            "created_by": md.created_by,
            "format_version": md.format_version,
            "footer_size": md.serialized_size,
        }
        columns = {}
        groups = []
        for rg in range(min(md.num_row_groups, 2000)):
            g = md.row_group(rg)
            comp = 0
            for ci in range(g.num_columns):
                c = g.column(ci)
                entry = columns.setdefault(c.path_in_schema, {
                    "column": c.path_in_schema, "physical_type": c.physical_type,
                    "compression": c.compression, "encodings": sorted(set(c.encodings)),
                    "compressed": 0, "uncompressed": 0,
                })
                entry["compressed"] += c.total_compressed_size
                entry["uncompressed"] += c.total_uncompressed_size
                comp += c.total_compressed_size
            if rg < 300:
                groups.append({"index": rg, "rows": g.num_rows, "uncompressed": g.total_byte_size, "compressed": comp})
        d["columns"] = list(columns.values())
        d["row_groups"] = groups
        kv = decode_metadata(md.metadata)
        kv.pop("ARROW:schema", None)
        d["key_value_metadata"] = {k: _maybe_json(v) for k, v in kv.items()}
        if self.is_dir:
            parts = set()
            for f in self.files[:2000]:
                for seg in Path(f).relative_to(self.path).parts[:-1]:
                    if "=" in seg:
                        parts.add(seg.split("=", 1)[0])
            d["partition_columns"] = sorted(parts)
        return d

    def _csv_info(self):
        try:
            with self.engine.session() as cur:
                t = self.engine.query(cur, f"SELECT * FROM sniff_csv({sql_str(str(self.files[0]))})", timeout=60)
            row = t.to_pylist()[0] if t.num_rows else {}
            keep = ["Delimiter", "Quote", "Escape", "NewLineDelimiter", "SkipRows", "HasHeader", "DateFormat", "TimestampFormat"]
            sniff = {k: row.get(k) for k in keep if k in row}
            if "Columns" in row:
                sniff["Columns"] = row["Columns"]
            return {"dialect": json_safe(sniff), "all_varchar": bool(self.params.get("all_varchar"))}
        except Exception as e:
            return {"dialect_error": str(e)}

    def _json_info(self):
        f = self.files[0]
        kind = "unknown"
        try:
            if not str(f).lower().endswith((".gz", ".zst")):
                with open(f, "rb") as fh:
                    head = fh.read(4096).lstrip()
                if head.startswith(b"["):
                    kind = "JSON 数组"
                elif head.startswith(b"{"):
                    nl = head.find(b"\n")
                    kind = "JSON Lines（每行一个对象）" if nl != -1 and head[nl:].lstrip().startswith(b"{") else "JSON 对象"
        except OSError:
            pass
        return {"layout": kind}


def _maybe_json(v: str):
    s = v.strip()
    if s[:1] in "{[":
        try:
            return json.loads(s)
        except ValueError:
            return v
    return v
