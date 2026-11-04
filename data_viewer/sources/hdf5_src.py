"""HDF5 files: a group or a dataset is presented as a table.

* dataset view: rows run along axis 0. Compound fields become columns; a 2-D
  dataset with few columns is split into scalar columns, wider ones become a
  vector column; 3-D and higher become a tensor column (rendered as images when
  the per-row shape looks like one).
* group view: the group's child datasets that share the same length along axis 0
  become the columns of one table (handy for episode / trajectory layouts).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from dataclasses import field as dc_field

import h5py
import numpy as np
import pyarrow as pa

from ..util import json_safe
from .base import Source, SourceError

SPLIT_MAX = 16
SQL_BUDGET = 1 << 30
SQL_CHUNK = 65536
TREE_LIMIT = 5000
LAZY_MODES = {"tensor", "binary", "vlen"}


@dataclass
class Col:
    name: str
    dset: h5py.Dataset
    mode: str
    dtype: np.dtype
    field: str | None = None
    index: int | None = None
    shape: tuple = ()
    enum: dict = dc_field(default_factory=dict)

    def describe(self) -> str:
        if self.mode in ("tensor", "vector"):
            return f"{self.dtype}[{'×'.join(str(s) for s in self.shape)}]"
        if self.mode == "binary":
            return "bytes"
        if self.mode == "vlen":
            return f"vlen<{self.dtype}>"
        return str(self.dtype)


def _arrow_dtype(dt: np.dtype) -> pa.DataType:
    if dt.kind == "b":
        return pa.bool_()
    return pa.from_numpy_dtype(dt.newbyteorder("=") if dt.byteorder not in ("=", "|") else dt)


def _native(x: np.ndarray) -> np.ndarray:
    if x.dtype.byteorder not in ("=", "|"):
        return x.astype(x.dtype.newbyteorder("="))
    return x


def _dtype_str(dt: np.dtype) -> str:
    if dt.names:
        inner = ", ".join(f"{n}: {_dtype_str(dt.fields[n][0])}" for n in dt.names[:12])
        return "compound{" + inner + (", …" if len(dt.names) > 12 else "") + "}"
    if h5py.check_string_dtype(dt) is not None:
        return "string"
    vl = h5py.check_vlen_dtype(dt)
    if vl is not None:
        return f"vlen<{vl}>"
    if h5py.check_enum_dtype(dt) is not None:
        return f"enum<{dt}>"
    if h5py.check_ref_dtype(dt) is not None:
        return "reference"
    if dt.subdtype:
        base, shape = dt.subdtype
        return f"{_dtype_str(base)}[{'×'.join(str(s) for s in shape)}]"
    return str(dt)


def _preview_attr(v):
    if isinstance(v, np.ndarray):
        if v.size > 20:
            return json_safe(v.ravel()[:20].tolist()) + ["…"]
        return json_safe(v.tolist())
    if isinstance(v, (bytes, np.bytes_)):
        return bytes(v).decode("utf-8", "replace")[:2000]
    if isinstance(v, str):
        return v[:2000]
    if isinstance(v, h5py.Reference):
        return "<reference>"
    return json_safe(v)


def _attrs(obj, limit=40):
    out = []
    try:
        for i, (k, v) in enumerate(obj.attrs.items()):
            if i >= limit:
                out.append({"name": "…", "value": f"还有 {len(obj.attrs) - limit} 个属性"})
                break
            try:
                out.append({"name": k, "value": _preview_attr(v)})
            except Exception as e:  # unreadable attribute types
                out.append({"name": k, "value": f"<无法读取：{e}>"})
    except Exception:
        pass
    return out


class HDF5Source(Source):
    format = "hdf5"
    label = "HDF5"

    def __init__(self, path, params, engine):
        super().__init__(path, params, engine)
        try:
            self.f = h5py.File(str(self.path), "r", locking=False)
        except Exception as e:
            raise SourceError(f"无法打开 HDF5 文件：{e}") from None
        node = self.params.get("node") or self._default_node()
        if node is None:
            self.node = "/"
            self.cols: list[Col] = []
            self.rows = 0
            self.skipped: list[str] = []
            self.node_kind = "group"
        else:
            self._build_view(node)
        self._sql: pa.Table | None = None
        self._sql_rows_note: str | None = None

    @property
    def name(self) -> str:
        return self.path.name if self.node in ("/", "") else f"{self.path.name} · {self.node}"

    # ------------------------------------------------------------------
    # view planning
    # ------------------------------------------------------------------
    def _default_node(self) -> str | None:
        """First group (breadth-first) whose datasets line up into a table, else the first dataset."""
        queue = [self.f]
        seen = 0
        first_dataset = None
        while queue and seen < TREE_LIMIT:
            g = queue.pop(0)
            seen += 1
            lengths: Counter = Counter()
            for _, obj in g.items():
                if isinstance(obj, h5py.Dataset) and obj.shape is not None and len(obj.shape) >= 1:
                    lengths[obj.shape[0]] += 1
                    if first_dataset is None:
                        first_dataset = obj.name
                elif isinstance(obj, h5py.Group):
                    queue.append(obj)
            if lengths and max(lengths.values()) >= 2:
                return g.name
        return first_dataset

    def _build_view(self, node: str):
        if node not in self.f:
            raise SourceError(f"HDF5 中不存在节点：{node}")
        obj = self.f[node]
        self.node = obj.name
        self.skipped = []
        if isinstance(obj, h5py.Dataset):
            self.node_kind = "dataset"
            if obj.shape is None:
                raise SourceError("这个数据集是空的（null dataspace）")
            base = obj.name.rsplit("/", 1)[-1] or "value"
            self.cols = self._plan(obj, None if obj.dtype.names else base)
            self.rows = obj.shape[0] if len(obj.shape) >= 1 else 1
        else:
            self.node_kind = "group"
            children = [(k, v) for k, v in obj.items() if isinstance(v, h5py.Dataset)]
            usable = [(k, v) for k, v in children if v.shape is not None and len(v.shape) >= 1]
            usable_names = {k for k, _ in usable}
            for k, _ in children:
                if k not in usable_names:
                    self.skipped.append(f"{k}（标量或空数据集）")
            if not usable:
                self.cols, self.rows = [], 0
                return
            lengths = Counter(v.shape[0] for _, v in usable)
            common = max(lengths.items(), key=lambda kv: (kv[1], kv[0]))[0]
            self.cols = []
            for k, v in usable:
                if v.shape[0] != common:
                    self.skipped.append(f"{k}（长度 {v.shape[0]}，与 {common} 不一致）")
                    continue
                self.cols.extend(self._plan(v, k, prefix_fields=True))
            self.rows = common
        names = Counter(c.name for c in self.cols)
        if any(n > 1 for n in names.values()):  # make duplicate names unique
            seen: Counter = Counter()
            for c in self.cols:
                if names[c.name] > 1:
                    seen[c.name] += 1
                    c.name = f"{c.name}#{seen[c.name]}"

    def _plan(self, dset: h5py.Dataset, name: str | None, prefix_fields: bool = False) -> list[Col]:
        dt = dset.dtype
        row_shape = tuple(dset.shape[1:]) if len(dset.shape) >= 1 else ()
        if dt.names:
            out = []
            for fname in dt.names:
                fdt = dt.fields[fname][0]
                cname = f"{name}.{fname}" if (prefix_fields and name) else fname
                out.extend(self._plan_element(cname, dset, fdt, row_shape, fname))
            return out
        return self._plan_element(name or "value", dset, dt, row_shape, None)

    def _plan_element(self, name, dset, dt, row_shape, fname) -> list[Col]:
        if dt.subdtype:
            base, sub = dt.subdtype
            dt, row_shape = base, tuple(row_shape) + tuple(sub)
        mk = lambda mode, **kw: Col(name=name, dset=dset, mode=mode, dtype=dt, field=fname, shape=tuple(row_shape), **kw)  # noqa: E731
        if h5py.check_string_dtype(dt) is not None:
            return [mk("string" if not row_shape else "repr")]
        enum = h5py.check_enum_dtype(dt)
        if enum is not None and not row_shape:
            return [mk("enum", enum={int(v): k for k, v in enum.items()})]
        vl = h5py.check_vlen_dtype(dt)
        if vl is not None:
            if row_shape:
                return [mk("repr")]
            if np.dtype(vl).kind in "iu" and np.dtype(vl).itemsize == 1:
                return [mk("binary")]
            return [Col(name=name, dset=dset, mode="vlen", dtype=np.dtype(vl), field=fname)]
        if h5py.check_ref_dtype(dt) is not None:
            return [mk("ref" if not row_shape else "repr")]
        if dt.kind == "V" and not dt.names:
            return [mk("binary" if not row_shape else "repr")]
        if dt.kind in "biuf":
            if not row_shape:
                return [mk("scalar")]
            if len(row_shape) == 1 and row_shape[0] <= SPLIT_MAX:
                return [Col(name=f"{name}[{i}]", dset=dset, mode="split", dtype=dt, field=fname, index=i, shape=row_shape)
                        for i in range(row_shape[0])]
            if len(row_shape) == 1:
                return [mk("vector")]
            return [mk("tensor")]
        return [mk("repr")]

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def _arrow_type(self, c: Col) -> pa.DataType:
        if c.mode in ("scalar", "split"):
            return _arrow_dtype(c.dtype)
        if c.mode == "vector":
            return pa.list_(_arrow_dtype(c.dtype), c.shape[0])
        if c.mode == "tensor":
            vt = pa.uint8() if c.dtype.kind == "b" else _arrow_dtype(c.dtype)
            return pa.fixed_shape_tensor(vt, list(c.shape))
        if c.mode == "binary":
            return pa.large_binary()
        if c.mode == "vlen":
            return pa.list_(_arrow_dtype(c.dtype))
        return pa.string()

    def schema(self) -> pa.Schema:
        return pa.schema([self._field(c) for c in self.cols])

    def _field(self, c: Col) -> pa.Field:
        meta = {"dv:note": f"HDF5 {c.dset.name}" + (f" · 字段 {c.field}" if c.field else "")}
        return pa.field(c.name, self._arrow_type(c), metadata=meta)

    def num_rows(self):
        return self.rows

    def _block(self, dset: h5py.Dataset, a: int, b: int, string: bool = False):
        if len(dset.shape) == 0:
            v = dset.asstr(errors="replace")[()] if string else dset[()]
            return np.array([v], dtype=object if string else None)
        if string:
            return dset.asstr(errors="replace")[a:b]
        return dset[a:b]

    def _convert(self, c: Col, block) -> pa.Array:
        x = block[c.field] if c.field is not None else block
        t = self._arrow_type(c)
        if c.mode == "scalar":
            return pa.array(_native(np.ascontiguousarray(x)), type=t)
        if c.mode == "split":
            return pa.array(_native(np.ascontiguousarray(x[:, c.index])), type=t)
        if c.mode == "vector":
            x = _native(np.ascontiguousarray(x))
            n = x.shape[0]
            return pa.FixedSizeListArray.from_arrays(pa.array(x.reshape(n * c.shape[0]), type=t.value_type), c.shape[0])
        if c.mode == "tensor":
            x = np.ascontiguousarray(_native(np.asarray(x)))
            if x.dtype.kind == "b":
                x = x.astype(np.uint8)
            if x.shape[0] == 0:
                return pa.ExtensionArray.from_storage(t, pa.array([], type=t.storage_type))
            return pa.FixedShapeTensorArray.from_numpy_ndarray(x)
        if c.mode == "string":
            vals = []
            for v in x.tolist() if isinstance(x, np.ndarray) else list(x):
                if isinstance(v, bytes):
                    v = v.decode("utf-8", "replace")
                vals.append(v)
            return pa.array(vals, type=pa.string())
        if c.mode == "enum":
            return pa.array([c.enum.get(int(v), str(v)) for v in np.asarray(x).ravel().tolist()], type=pa.string())
        if c.mode == "binary":
            return pa.array([None if v is None else (v.tobytes() if hasattr(v, "tobytes") else bytes(v)) for v in x], type=t)
        if c.mode == "vlen":
            return pa.array([None if v is None else np.asarray(v).tolist() for v in x], type=t)
        if c.mode == "ref":
            out = []
            for r in x:
                try:
                    out.append(self.f[r].name if r else None)
                except Exception:
                    out.append("<reference>")
            return pa.array(out, type=pa.string())
        vals = []
        for v in (x if isinstance(x, np.ndarray) else np.asarray(x)):
            if isinstance(v, np.ndarray):
                v = v.tolist()
            elif isinstance(v, bytes):
                v = v.decode("utf-8", "replace")
            vals.append(None if v is None else str(v))
        return pa.array(vals, type=pa.string())

    def read(self, offset, limit, columns=None):
        offset, limit = int(offset), int(limit)
        cols = self.cols if not columns else [c for c in self.cols if c.name in set(columns)]
        if columns:
            order = {n: i for i, n in enumerate(columns)}
            cols = sorted(cols, key=lambda c: order.get(c.name, 0))
        a = max(0, min(offset, self.rows))
        b = max(a, min(self.rows, offset + limit))
        blocks: dict = {}
        arrays, fields = [], []
        for c in cols:
            key = (c.dset.name, c.mode == "string" and c.field is None)
            if key not in blocks:
                blocks[key] = self._block(c.dset, a, b, string=key[1])
            arrays.append(self._convert(c, blocks[key]))
            fields.append(self._field(c))
        return pa.Table.from_arrays(arrays, schema=pa.schema(fields))

    # ------------------------------------------------------------------
    # SQL
    # ------------------------------------------------------------------
    def lazy_columns(self):
        return {c.name: c.describe() for c in self.cols if c.mode in LAZY_MODES}

    def _row_bytes(self, c: Col) -> int:
        if c.mode in ("scalar", "split"):
            return c.dtype.itemsize
        if c.mode == "vector":
            return c.dtype.itemsize * c.shape[0]
        return 32

    def _sql_table(self) -> pa.Table:
        if self._sql is not None:
            return self._sql
        lazy = self.lazy_columns()
        cheap = [c for c in self.cols if c.name not in lazy]
        row_bytes = max(1, sum(self._row_bytes(c) for c in cheap))
        n = min(self.rows, max(1, SQL_BUDGET // row_bytes))
        if n < self.rows:
            self._sql_rows_note = f"该视图较大，SQL 只载入了前 {n:,} 行（共 {self.rows:,} 行）"
        parts = []
        names = [c.name for c in cheap]
        for start in range(0, n, SQL_CHUNK):
            parts.append(self.read(start, min(SQL_CHUNK, n - start), names) if names else None)
        arrays = {"__index__": pa.array(np.arange(n, dtype=np.int64))}
        if names:
            t = pa.concat_tables([p for p in parts if p is not None]) if parts else self.read(0, 0, names)
            for name in names:
                col = t.column(name)
                if isinstance(col.type, pa.BaseExtensionType):
                    col = pa.chunked_array([ch.storage for ch in col.chunks], type=col.type.storage_type)
                arrays[name] = col
        order = ["__index__"] + [c.name for c in self.cols]
        out = {}
        for name in order:
            if name in arrays:
                out[name] = arrays[name]
            elif name in lazy:
                out[name] = pa.DictionaryArray.from_arrays(pa.array(np.zeros(n, dtype=np.int32)), pa.array([lazy[name]])).cast(pa.string())
        self._sql = pa.table(out)
        return self._sql

    def register(self, con, name):
        con.register(name, self._sql_table())

    def warnings(self):
        out = []
        if self.skipped:
            out.append("以下数据集没有放进表格：" + "；".join(self.skipped[:20]))
        if self._sql_rows_note:
            out.append(self._sql_rows_note)
        return out

    # ------------------------------------------------------------------
    # info
    # ------------------------------------------------------------------
    def info(self):
        nodes = []
        truncated = False

        def add(name, obj):
            nonlocal truncated
            if len(nodes) >= TREE_LIMIT:
                truncated = True
                return True
            nodes.append(self._node_info(obj))
            return None

        nodes.append(self._node_info(self.f))
        self.f.visititems(add)
        return {
            "node": self.node,
            "node_kind": self.node_kind,
            "rows": self.rows,
            "columns": [{"name": c.name, "dataset": c.dset.name, "field": c.field, "mode": c.mode, "shape": list(c.shape),
                         "dtype": str(c.dtype)} for c in self.cols],
            "skipped": self.skipped,
            "tree": nodes,
            "tree_truncated": truncated,
            "file_attrs": _attrs(self.f),
            "libver": list(self.f.libver) if hasattr(self.f, "libver") else None,
        }

    def _node_info(self, obj) -> dict:
        if isinstance(obj, h5py.Dataset):
            d = {
                "path": obj.name,
                "type": "dataset",
                "shape": list(obj.shape) if obj.shape is not None else None,
                "dtype": _dtype_str(obj.dtype),
                "chunks": list(obj.chunks) if obj.chunks else None,
                "compression": obj.compression,
                "compression_opts": json_safe(obj.compression_opts),
                "nbytes": int(obj.size * obj.dtype.itemsize) if obj.shape is not None else 0,
                "attrs": _attrs(obj, 20),
            }
            try:
                d["storage"] = int(obj.id.get_storage_size())
            except Exception:
                pass
            d["tabular"] = obj.shape is not None
            return d
        n_children = len(obj)
        return {
            "path": obj.name,
            "type": "group",
            "children": n_children,
            "attrs": _attrs(obj, 20),
            "tabular": any(isinstance(v, h5py.Dataset) for v in list(obj.values())[:2000]),
        }

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass
