"""Turn Arrow data into JSON-safe cells for the browser.

Plain values (numbers, strings, booleans, null, nested lists / objects) are sent as
JSON. Rich values are objects with a ``$t`` tag:

* ``bin``  – binary value: ``size``, ``mime``, optional inline base64 ``data``,
             ``text`` / ``hex`` previews, ``sub`` (path inside a nested value)
* ``str``  – truncated string: ``v`` (prefix) and ``len``
* ``vec``  – fixed-size numeric list (embedding): ``dim``, ``head``, ``norm``
* ``nd``   – n-dimensional array: ``shape``, ``dtype``, stats, optional PNG ``img``
* ``path`` – string pointing at a media file: ``v``, ``mime``, ``url``
* ``more`` – marker appended to truncated lists: ``n`` items omitted
"""
from __future__ import annotations

import base64
import datetime as _dt
import decimal
import json
import math
import re
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urlparse

import numpy as np
import pyarrow as pa

from . import media
from .util import as_array, decode_metadata

STR_LIMIT = 1000
LIST_LIMIT = 50
VEC_HEAD = 8
INLINE_IMAGE_LIMIT = 48 * 1024
INLINE_NESTED_IMAGE_LIMIT = 24 * 1024
THUMB_SIDE = 160
MAX_SAFE_INT = 2**53 - 1
MEDIA_PATH_RE = re.compile(
    r"\.(jpe?g|png|gif|webp|bmp|tiff?|avif|svg|mp4|webm|mov|m4v|mkv|ogv|wav|mp3|flac|ogg|oga|opus|m4a|aac)$",
    re.I,
)
_FRACTION_RE = re.compile(r"(\.\d*?)0+(?=$|[+\-Z ])")


@dataclass
class EncodeContext:
    # Maps a string that looks like a media path to {"url": ..., "mime": ...} (or None).
    path_resolver: Callable[[str], dict | None] | None = None


# ---------------------------------------------------------------------------
# type helpers
# ---------------------------------------------------------------------------

def is_tensor_type(t: pa.DataType) -> bool:
    return isinstance(t, pa.BaseExtensionType) and getattr(t, "extension_name", "") == "arrow.fixed_shape_tensor"


def kind_of(t: pa.DataType) -> str:
    if pa.types.is_dictionary(t):
        return kind_of(t.value_type)
    if isinstance(t, pa.BaseExtensionType):
        if is_tensor_type(t):
            return "tensor"
        return kind_of(t.storage_type)
    if pa.types.is_boolean(t):
        return "bool"
    if pa.types.is_integer(t):
        return "int"
    if pa.types.is_floating(t):
        return "float"
    if pa.types.is_decimal(t):
        return "decimal"
    if pa.types.is_string(t) or pa.types.is_large_string(t) or _is(t, "is_string_view"):
        return "string"
    if pa.types.is_binary(t) or pa.types.is_large_binary(t) or pa.types.is_fixed_size_binary(t) or _is(t, "is_binary_view"):
        return "binary"
    if pa.types.is_timestamp(t):
        return "timestamp"
    if pa.types.is_date(t):
        return "date"
    if pa.types.is_time(t):
        return "time"
    if pa.types.is_duration(t):
        return "duration"
    if pa.types.is_fixed_size_list(t) and (pa.types.is_floating(t.value_type) or pa.types.is_integer(t.value_type)):
        return "vector"
    if pa.types.is_list(t) or pa.types.is_large_list(t) or pa.types.is_fixed_size_list(t) \
            or _is(t, "is_list_view") or _is(t, "is_large_list_view"):
        return "list"
    if pa.types.is_struct(t):
        return "struct"
    if pa.types.is_map(t):
        return "map"
    if pa.types.is_null(t):
        return "null"
    return "other"


def _is(t: pa.DataType, fn: str) -> bool:
    f = getattr(pa.types, fn, None)
    return bool(f and f(t))


def type_str(t: pa.DataType) -> str:
    if is_tensor_type(t):
        return f"tensor<{t.value_type}>[{','.join(str(s) for s in t.shape)}]"
    if pa.types.is_fixed_size_list(t):
        return f"fixed_size_list<{type_str(t.value_type)}>[{t.list_size}]"
    if pa.types.is_list(t) or pa.types.is_large_list(t):
        prefix = "large_list" if pa.types.is_large_list(t) else "list"
        return f"{prefix}<{type_str(t.value_type)}>"
    if pa.types.is_dictionary(t):
        return f"dictionary<{type_str(t.value_type)}>"
    return str(t)


def field_meta(f: pa.Field, key: str) -> str | None:
    if not f.metadata:
        return None
    v = f.metadata.get(key.encode())
    return v.decode("utf-8", "replace") if v is not None else None


def describe_field(f: pa.Field) -> dict:
    t = f.type
    k = kind_of(t)
    if field_meta(f, "dv:kind") == "json" and k == "string":
        k = "json"
    d: dict[str, Any] = {"name": f.name, "type": type_str(t), "kind": k, "nullable": f.nullable}
    if k == "vector":
        d["dim"] = t.list_size
    if k == "tensor":
        d["shape"] = list(t.shape)
        d["dtype"] = str(t.value_type)
    meta = {key: v for key, v in decode_metadata(f.metadata, 2000).items() if not key.startswith("dv:")}
    if meta:
        d["metadata"] = meta
    note = field_meta(f, "dv:note")
    if note:
        d["note"] = note
    return d


def field_tree(f: pa.Field) -> dict:
    """Recursive schema description used by the schema tab."""
    d = describe_field(f)
    t = f.type
    if isinstance(t, pa.BaseExtensionType) and not is_tensor_type(t):
        d["extension"] = getattr(t, "extension_name", str(t))
        t = t.storage_type
    children: list[pa.Field] = []
    if pa.types.is_struct(t):
        children = [t.field(i) for i in range(t.num_fields)]
    elif pa.types.is_map(t):
        children = [t.key_field, t.item_field]
    elif pa.types.is_list(t) or pa.types.is_large_list(t) or pa.types.is_fixed_size_list(t) \
            or _is(t, "is_list_view") or _is(t, "is_large_list_view"):
        children = [t.value_field]
    if children:
        d["children"] = [field_tree(c) for c in children]
    return d


# ---------------------------------------------------------------------------
# scalar encoders
# ---------------------------------------------------------------------------

def num(x: Any) -> Any:
    """JSON-safe number with ~6 significant digits for floats."""
    if x is None:
        return None
    if isinstance(x, (np.integer, int)) and not isinstance(x, bool):
        x = int(x)
        return x if -MAX_SAFE_INT <= x <= MAX_SAFE_INT else str(x)
    try:
        f = float(x)
    except (TypeError, ValueError):
        return str(x)
    if math.isnan(f):
        return "NaN"
    if math.isinf(f):
        return "Infinity" if f > 0 else "-Infinity"
    return float(f"{f:.6g}")


def _enc_int(v):
    if v is None:
        return None
    return v if -MAX_SAFE_INT <= v <= MAX_SAFE_INT else str(v)


def _enc_float(v):
    if v is None:
        return None
    f = float(v)
    if math.isnan(f):
        return "NaN"
    if math.isinf(f):
        return "Infinity" if f > 0 else "-Infinity"
    return f


def _trim_fraction(s: str) -> str:
    s = _FRACTION_RE.sub(lambda m: m.group(1) if len(m.group(1)) > 1 else "", s)
    return s


def enc_bytes(b: bytes, inline_limit: int = INLINE_IMAGE_LIMIT, sub: list | None = None) -> dict:
    n = len(b)
    d: dict[str, Any] = {"$t": "bin", "size": n}
    if sub is not None:
        d["sub"] = sub
    mime = media.sniff_mime(b)
    if mime:
        d["mime"] = mime
        if mime in media.IMAGE_BROWSER and n <= inline_limit:
            d["data"] = base64.b64encode(b).decode("ascii")
        elif mime == "application/x-npy" and n <= 16 * 1024 * 1024:
            arr = media.load_npy(b)
            if arr is not None:
                d["nd"] = nd_summary(arr, thumb_side=96)
        return d
    text = media.looks_like_text(b[:4096])
    if text is not None and n > 0:
        d["text"] = text[:300]
    else:
        d["hex"] = bytes(b[:24]).hex(" ")
    return d


def nd_summary(a: np.ndarray, thumb_side: int = THUMB_SIDE) -> dict:
    a = np.asarray(a)
    d: dict[str, Any] = {"$t": "nd", "shape": list(a.shape), "dtype": str(a.dtype)}
    if a.size and a.dtype.kind in "biuf":
        vals = a
        if a.dtype.kind == "f":
            vals = a[np.isfinite(a)]
        if vals.size:
            d["min"] = num(vals.min())
            d["max"] = num(vals.max())
            d["mean"] = num(vals.astype(np.float64).mean())
    if a.dtype.kind in "biuf" and media.image_like(a.shape):
        png = media.array_to_png(a, max_side=thumb_side)
        if png:
            d["img"] = base64.b64encode(png).decode("ascii")
    elif a.size <= 16:
        d["values"] = py_to_json(a.tolist())
    else:
        d["head"] = [num(x) for x in a.ravel()[:VEC_HEAD].tolist()]
    return d


def py_to_json(v: Any, depth: int = 0, sub: list | None = None, limits: bool = True) -> Any:
    """Recursively convert Python values (from ``to_pylist``) into JSON-safe cells."""
    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, str):
        if limits and len(v) > STR_LIMIT:
            return {"$t": "str", "v": v[:STR_LIMIT], "len": len(v)}
        return v
    if isinstance(v, int):
        return _enc_int(v)
    if isinstance(v, float):
        return _enc_float(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        # In full (limits=False) mode binary values are summarised; the client fetches
        # their content separately through the ``sub`` path.
        return enc_bytes(bytes(v), INLINE_NESTED_IMAGE_LIMIT if limits else 0, sub=list(sub or []))
    if isinstance(v, decimal.Decimal):
        return str(v)
    if isinstance(v, _dt.datetime):
        return _trim_fraction(v.isoformat(sep=" "))
    if isinstance(v, (_dt.date, _dt.time)):
        return v.isoformat()
    if isinstance(v, _dt.timedelta):
        return str(v)
    if isinstance(v, np.ndarray):
        return nd_summary(v) if limits else v.tolist()
    if isinstance(v, np.generic):
        return py_to_json(v.item(), depth, sub, limits)
    if depth > 24:
        return "…"
    path = list(sub or [])
    if isinstance(v, dict):
        return {str(k): py_to_json(x, depth + 1, path + [k], limits) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        items = list(v)
        if limits and len(items) > LIST_LIMIT:
            out = [py_to_json(x, depth + 1, path + [i], limits) for i, x in enumerate(items[:LIST_LIMIT])]
            out.append({"$t": "more", "n": len(items) - LIST_LIMIT})
            return out
        return [py_to_json(x, depth + 1, path + [i], limits) for i, x in enumerate(items)]
    return str(v)


def _map_to_json(v, limits=True):
    if v is None:
        return None
    pairs = list(v)
    shown = pairs[:LIST_LIMIT] if limits else pairs
    if all(isinstance(k, (str, int)) for k, _ in shown):
        out = {str(k): py_to_json(x, 1, [i, 1], limits) for i, (k, x) in enumerate(shown)}
        if limits and len(pairs) > LIST_LIMIT:
            out["…"] = {"$t": "more", "n": len(pairs) - LIST_LIMIT}
        return out
    return [[py_to_json(k, 1, None, limits), py_to_json(x, 1, [i, 1], limits)] for i, (k, x) in enumerate(shown)]


def _enc_json_text(s: str | None):
    if s is None:
        return None
    if len(s) <= 2_000_000:
        try:
            obj = json.loads(s)
            if isinstance(obj, (dict, list)):
                return py_to_json(obj)
        except ValueError:
            pass
    return py_to_json(s)


# ---------------------------------------------------------------------------
# column encoders
# ---------------------------------------------------------------------------

def _temporal_strings(arr: pa.Array) -> list:
    try:
        return [None if s is None else _trim_fraction(s) for s in arr.cast(pa.string()).to_pylist()]
    except Exception:
        try:
            return [None if v is None else str(v) for v in arr.to_pylist()]
        except Exception:
            return [None if v is None else str(v) for v in arr.cast(pa.int64()).to_pylist()]


def _enc_strings(values: list, ctx: EncodeContext | None) -> list:
    resolver = ctx.path_resolver if ctx else None
    out = []
    for s in values:
        if s is None:
            out.append(None)
            continue
        if resolver and len(s) < 2048:
            target = urlparse(s).path if s.startswith(("http://", "https://")) else s
            if MEDIA_PATH_RE.search(target):
                info = resolver(s)
                if info:
                    out.append({"$t": "path", "v": s, **info})
                    continue
        out.append({"$t": "str", "v": s[:STR_LIMIT], "len": len(s)} if len(s) > STR_LIMIT else s)
    return out


def _enc_vectors(arr: pa.Array) -> list:
    dim = arr.type.list_size
    n = len(arr)
    try:
        values = arr.values.slice(arr.offset * dim, n * dim)
        mat = values.to_numpy(zero_copy_only=False).reshape(n, dim)
    except Exception:
        return [py_to_json(v) for v in arr.to_pylist()]
    valid = arr.is_valid().to_numpy(zero_copy_only=False)
    is_float = mat.dtype.kind == "f"
    f64 = mat.astype(np.float64, copy=False)
    norms = np.sqrt(np.nansum(f64 * f64, axis=1)) if dim else np.zeros(n)
    out = []
    for i in range(n):
        if not valid[i]:
            out.append(None)
            continue
        head = [num(x) if is_float else _enc_int(int(x)) for x in mat[i, :VEC_HEAD].tolist()]
        out.append({"$t": "vec", "dim": dim, "head": head, "norm": num(norms[i])})
    return out


def _enc_tensors(arr: pa.Array) -> list:
    try:
        nd = arr.to_numpy_ndarray()
    except Exception:
        return [py_to_json(v) for v in arr.storage.to_pylist()]
    valid = arr.is_valid().to_numpy(zero_copy_only=False)
    return [nd_summary(nd[i]) if valid[i] else None for i in range(len(arr))]


def encode_array(col: pa.Array | pa.ChunkedArray, f: pa.Field, ctx: EncodeContext | None = None) -> list:
    arr = as_array(col)
    t = arr.type
    if pa.types.is_dictionary(t):
        arr = arr.dictionary_decode()
        t = arr.type
    if isinstance(t, pa.BaseExtensionType):
        if is_tensor_type(t):
            return _enc_tensors(arr)
        arr = arr.storage
        t = arr.type
    k = kind_of(t)
    if k == "string" and field_meta(f, "dv:kind") == "json":
        return [_enc_json_text(s) for s in arr.to_pylist()]
    if k == "int":
        return [_enc_int(v) for v in arr.to_pylist()]
    if k == "float":
        return [_enc_float(v) for v in arr.to_pylist()]
    if k == "bool":
        return arr.to_pylist()
    if k == "decimal":
        return arr.cast(pa.string()).to_pylist()
    if k in ("timestamp", "date", "time"):
        return _temporal_strings(arr)
    if k == "duration":
        unit = t.unit
        return [None if v is None else f"{v} {unit}" for v in arr.cast(pa.int64()).to_pylist()]
    if k == "string":
        return _enc_strings(arr.to_pylist(), ctx)
    if k == "binary":
        return [None if v is None else enc_bytes(v) for v in arr.to_pylist()]
    if k == "vector":
        return _enc_vectors(arr)
    if k == "null":
        return [None] * len(arr)
    if k == "map":
        return [_map_to_json(v) for v in arr.to_pylist()]
    return [py_to_json(v) for v in arr.to_pylist()]


def encode_table(table: pa.Table, ctx: EncodeContext | None = None) -> tuple[list[dict], list[list]]:
    columns = [describe_field(f) for f in table.schema]
    encoded = [encode_array(table.column(i), f, ctx) for i, f in enumerate(table.schema)]
    rows = [list(r) for r in zip(*encoded)] if encoded else [[] for _ in range(table.num_rows)]
    return columns, rows


# ---------------------------------------------------------------------------
# full-value extraction for the cell endpoint
# ---------------------------------------------------------------------------

def scalar_value(arr: pa.Array, f: pa.Field):
    """Return (kind, python value) for a one-element array, keeping ndarrays for tensors."""
    t = arr.type
    if pa.types.is_dictionary(t):
        arr = arr.dictionary_decode()
        t = arr.type
    if is_tensor_type(t):
        if not arr.is_valid()[0].as_py():
            return "null", None
        return "tensor", arr.to_numpy_ndarray()[0]
    if isinstance(t, pa.BaseExtensionType):
        arr = arr.storage
        t = arr.type
    k = kind_of(t)
    if k == "string" and field_meta(f, "dv:kind") == "json":
        k = "json"
    if k in ("timestamp", "date", "time"):
        return k, _temporal_strings(arr)[0]
    return k, arr.to_pylist()[0]


def full_json(kind: str, value: Any) -> Any:
    """JSON for the complete value of a cell (no truncation, binaries summarised)."""
    if kind == "map":
        return _map_to_json(value, limits=False)
    if kind == "json" and isinstance(value, str):
        try:
            return py_to_json(json.loads(value), limits=False)
        except ValueError:
            return value
    return py_to_json(value, limits=False)


def navigate(value: Any, sub: list) -> Any:
    """Follow a path of keys / indices into a nested Python value."""
    cur = value
    for step in sub:
        if isinstance(cur, dict):
            if step in cur:
                cur = cur[step]
            elif str(step) in cur:
                cur = cur[str(step)]
            else:
                raise KeyError(step)
        elif isinstance(cur, (list, tuple)):
            cur = cur[int(step)]
        else:
            raise KeyError(step)
    return cur
