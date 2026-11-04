"""Small helpers shared across the backend."""
from __future__ import annotations

import datetime as _dt
import decimal
import math
from typing import Any

import numpy as np
import pyarrow as pa


def sql_str(value: str) -> str:
    """Quote a value as a SQL string literal."""
    return "'" + str(value).replace("'", "''") + "'"


def sql_ident(name: str) -> str:
    """Quote a value as a SQL identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def fetch_arrow(cursor) -> pa.Table:
    """Fetch the pending DuckDB result as a pyarrow Table (works across DuckDB versions)."""
    res = cursor.arrow()
    if isinstance(res, pa.Table):
        return res
    if hasattr(res, "read_all"):
        return res.read_all()
    return cursor.fetch_arrow_table()


def human_size(n: int | float | None) -> str:
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def json_safe(value: Any, depth: int = 0) -> Any:
    """Convert arbitrary Python / numpy values into JSON-serialisable data."""
    if depth > 30:
        return "…"
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        return value if abs(value) <= 2**53 - 1 else str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, np.generic):
        return json_safe(value.item(), depth + 1)
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist(), depth + 1)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        b = bytes(value)
        try:
            return b.decode("utf-8")
        except UnicodeDecodeError:
            return "0x" + b[:64].hex() + ("…" if len(b) > 64 else "")
    if isinstance(value, dict):
        return {str(k): json_safe(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v, depth + 1) for v in value]
    return str(value)


def as_array(col: pa.Array | pa.ChunkedArray) -> pa.Array:
    """Return a single contiguous Array for a (possibly chunked) column."""
    if isinstance(col, pa.ChunkedArray):
        if col.num_chunks == 0:
            return pa.array([], type=col.type)
        if col.num_chunks == 1:
            return col.chunk(0)
        return pa.concat_arrays(col.chunks)
    return col


def decode_metadata(meta: dict | None, limit: int = 20000) -> dict[str, str]:
    """Decode Arrow key/value metadata (bytes -> str), truncating huge values."""
    out: dict[str, str] = {}
    if not meta:
        return out
    for k, v in meta.items():
        ks = k.decode("utf-8", "replace") if isinstance(k, bytes) else str(k)
        vs = v.decode("utf-8", "replace") if isinstance(v, bytes) else str(v)
        if len(vs) > limit:
            vs = vs[:limit] + "…"
        out[ks] = vs
    return out
