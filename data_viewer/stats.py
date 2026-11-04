"""Column statistics computed with DuckDB over the current view."""
from __future__ import annotations

from collections import Counter

import pyarrow as pa

from . import media
from .encode import kind_of, type_str
from .engine import Engine, QueryError, validate_select
from .util import json_safe, sql_ident

NUMERIC = {"int", "float", "decimal"}
TOPK = 20


def _from(stmt: str, sample: int | None) -> str:
    if sample:
        return f"(SELECT * FROM ({stmt}) AS __s LIMIT {int(sample)}) AS __b"
    return f"({stmt}) AS __b"


def _column_exprs(f: pa.Field, exact: bool = False) -> tuple[str, list[tuple[str, str]]]:
    k = kind_of(f.type)
    c = sql_ident(f.name)
    distinct = f"count(DISTINCT {c})" if exact else f"approx_count_distinct({c})"
    items = [("nonnull", f"count({c})")]
    if k in ("int", "decimal"):
        x = f"{c}::DOUBLE"
        items += [
            ("distinct", distinct),
            ("min", f"min({x})"), ("max", f"max({x})"), ("mean", f"avg({x})"), ("std", f"stddev_samp({x})"),
            ("q", f"approx_quantile({x}, [0.25, 0.5, 0.75])"),
        ]
    elif k == "float":
        x = f"{c}::DOUBLE"
        fin = f"FILTER (WHERE isfinite({x}))"
        items += [
            ("distinct", distinct),
            ("nan", f"count(*) FILTER (WHERE isnan({x}))"), ("inf", f"count(*) FILTER (WHERE isinf({x}))"),
            ("min", f"min({x}) {fin}"), ("max", f"max({x}) {fin}"), ("mean", f"avg({x}) {fin}"),
            ("std", f"stddev_samp({x}) {fin}"), ("q", f"approx_quantile({x}, [0.25, 0.5, 0.75]) {fin}"),
        ]
    elif k == "bool":
        items += [("true", f"count(*) FILTER (WHERE {c})")]
    elif k == "string":
        items += [
            ("distinct", distinct),
            ("len_min", f"min(length({c}))"), ("len_max", f"max(length({c}))"), ("len_mean", f"avg(length({c}))"),
        ]
    elif k == "binary":
        items += [
            ("size_min", f"min(octet_length({c}))"), ("size_max", f"max(octet_length({c}))"),
            ("size_mean", f"avg(octet_length({c}))"), ("size_sum", f"sum(octet_length({c}))"),
        ]
    elif k in ("timestamp", "date", "time"):
        items += [("distinct", distinct), ("min", f"min({c})::VARCHAR"), ("max", f"max({c})::VARCHAR")]
    elif k in ("list", "vector", "tensor"):
        items += [("len_min", f"min(len({c}))"), ("len_max", f"max(len({c}))"), ("len_mean", f"avg(len({c}))")]
    return k, items


EXACT_DISTINCT_MAX = 1_000_000


def summary(engine: Engine, src, sql: str, sample: int | None = None, timeout: float = 60.0,
            known_rows: int | None = None) -> dict:
    """One pass over the view. Distinct counts are exact when the view is known to be small
    (a sample, or a source with at most EXACT_DISTINCT_MAX rows), else HyperLogLog estimates."""
    stmt, _ = validate_select(sql)
    exact = bool(sample and sample <= EXACT_DISTINCT_MAX) or (known_rows is not None and known_rows <= EXACT_DISTINCT_MAX)
    with engine.session(src) as cur:
        schema = engine.query(cur, f"SELECT * FROM ({stmt}) AS __q LIMIT 0", timeout).schema
        frm = _from(stmt, sample)
        plans = []
        exprs = ["count(*) AS __n"]
        for i, f in enumerate(schema):
            k, items = _column_exprs(f, exact)
            keys = [(name, f"c{i}_{name}") for name, _ in items]
            plans.append((f, k, keys, items))
            exprs += [f"{e} AS {sql_ident(key)}" for (name, e), (_, key) in zip(items, keys)]
        try:
            row = engine.query(cur, f"SELECT {', '.join(exprs)} FROM {frm}", timeout).to_pylist()[0]
            errors: dict[str, str] = {}
        except QueryError as e:
            if "超时" in str(e):
                raise
            # Fall back to one query per column so one odd type does not break everything.
            row, errors = {"__n": None}, {}
            for f, k, keys, items in plans:
                cols = [f"{e} AS {sql_ident(key)}" for (name, e), (_, key) in zip(items, keys)]
                try:
                    r = engine.query(cur, f"SELECT count(*) AS __n, {', '.join(cols)} FROM {frm}", timeout).to_pylist()[0]
                    row.update(r)
                except QueryError as ce:
                    errors[f.name] = str(ce)
    n = row.get("__n")
    columns = []
    for f, k, keys, _ in plans:
        d = {"name": f.name, "kind": k, "type": type_str(f.type)}
        for name, key in keys:
            d[name] = row.get(key)
        if d.get("nonnull") is not None and n is not None:
            d["nulls"] = n - d["nonnull"]
        if f.name in errors:
            d["error"] = errors[f.name]
        columns.append(json_safe(d))
    return {"rows": n, "sample": sample, "distinct_exact": exact, "columns": columns}


def _histogram(engine, cur, frm, x, cond, bins, integer, timeout):
    t = engine.query(cur, f"SELECT min({x}) AS mn, max({x}) AS mx, count({x}) AS n FROM {frm} WHERE {cond}", timeout)
    mn, mx, n = t.to_pylist()[0].values()
    if mn is None or not n:
        return None
    mn, mx = float(mn), float(mx)
    if integer and mx - mn + 1 <= 2 * bins:
        rows = engine.query(cur, f"SELECT {x} AS v, count(*) AS n FROM {frm} WHERE {cond} GROUP BY v ORDER BY v", timeout).to_pylist()
        return {"type": "discrete", "values": [r["v"] for r in rows], "counts": [r["n"] for r in rows], "min": mn, "max": mx}
    if mx == mn:
        return {"type": "bins", "edges": [mn, mx], "counts": [n], "min": mn, "max": mx}
    width = (mx - mn) / bins
    q = (f"SELECT LEAST(CAST(FLOOR(({x} - {mn!r}) / {width!r}) AS BIGINT), {bins - 1}) AS b, count(*) AS n "
         f"FROM {frm} WHERE {cond} GROUP BY b ORDER BY b")
    counts = [0] * bins
    for r in engine.query(cur, q, timeout).to_pylist():
        b = int(r["b"])
        counts[max(0, min(bins - 1, b))] += r["n"]
    return {"type": "bins", "edges": [mn + i * width for i in range(bins + 1)], "counts": counts, "min": mn, "max": mx}


def _topk(engine, cur, frm, c, timeout):
    q = (f"SELECT CASE WHEN v IS NULL THEN NULL ELSE left(CAST(v AS VARCHAR), 200) END AS v, n FROM "
         f"(SELECT {c} AS v, count(*) AS n FROM {frm} GROUP BY {c} ORDER BY n DESC, v NULLS LAST LIMIT {TOPK})")
    rows = engine.query(cur, q, timeout).to_pylist()
    return [{"value": r["v"], "count": r["n"]} for r in rows]


def column_detail(engine: Engine, src, sql: str, column: str, sample: int | None = None,
                  bins: int = 30, timeout: float = 60.0) -> dict:
    stmt, _ = validate_select(sql)
    with engine.session(src) as cur:
        schema = engine.query(cur, f"SELECT * FROM ({stmt}) AS __q LIMIT 0", timeout).schema
        idx = schema.get_field_index(column)
        if idx < 0:
            raise QueryError(f"没有这一列：{column}")
        f = schema.field(idx)
        k = kind_of(f.type)
        c = sql_ident(column)
        frm = _from(stmt, sample)
        out: dict = {"name": column, "kind": k, "type": type_str(f.type)}
        if k in NUMERIC:
            x = f"{c}::DOUBLE"
            out["histogram"] = _histogram(engine, cur, frm, x, f"isfinite({x})", bins, k == "int", timeout)
            out["top"] = _topk(engine, cur, frm, c, timeout)
        elif k in ("timestamp", "date"):
            x = f"epoch({c})"
            out["histogram"] = _histogram(engine, cur, frm, x, f"{c} IS NOT NULL", bins, False, timeout)
            out["temporal"] = True
            out["top"] = _topk(engine, cur, frm, c, timeout)
        elif k in ("string", "bool", "time"):
            out["top"] = _topk(engine, cur, frm, c, timeout)
            if k == "string":
                out["lengths"] = _histogram(engine, cur, frm, f"length({c})", f"{c} IS NOT NULL", bins, True, timeout)
        elif k == "binary":
            out["lengths"] = _histogram(engine, cur, frm, f"octet_length({c})", f"{c} IS NOT NULL", bins, True, timeout)
            t = engine.query(cur, f"SELECT {c} AS v FROM {frm} WHERE {c} IS NOT NULL LIMIT 200", timeout)
            mimes = Counter(media.sniff_mime(v) or "未知" for v in t.column(0).to_pylist())
            out["mime"] = [{"value": m, "count": n} for m, n in mimes.most_common()]
            out["mime_sampled"] = t.num_rows
        elif k in ("list", "tensor"):
            out["lengths"] = _histogram(engine, cur, frm, f"len({c})", f"{c} IS NOT NULL", bins, True, timeout)
        elif k == "vector":
            norm = f"sqrt(list_sum(list_transform(CAST({c} AS DOUBLE[]), lambda v: v * v)))"
            out["norms"] = _histogram(engine, cur, frm, norm, f"{c} IS NOT NULL", bins, False, timeout)
    return json_safe(out)
