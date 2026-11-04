"""DuckDB query engine: per-request cursors, read-only validation, timeouts, paging."""
from __future__ import annotations

import threading
import os
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator

import duckdb
import pyarrow as pa

from .util import fetch_arrow, sql_ident

if TYPE_CHECKING:  # pragma: no cover
    from .sources.base import Source

TABLE_NAME = "data"
_ALLOWED = {duckdb.StatementType.SELECT, duckdb.StatementType.EXPLAIN}


class QueryError(Exception):
    """A user-facing query problem (bad SQL, timeout, forbidden statement...)."""


def validate_select(sql: str) -> tuple[str, bool]:
    """Ensure ``sql`` is exactly one read-only statement. Returns (statement, is_explain)."""
    if not sql or not sql.strip():
        raise QueryError("SQL 为空")
    try:
        stmts = duckdb.extract_statements(sql)
    except duckdb.Error as e:
        raise QueryError(f"SQL 解析失败：{e}") from None
    if not stmts:
        raise QueryError("SQL 为空")
    if len(stmts) > 1:
        raise QueryError("一次只能执行一条语句")
    st = stmts[0]
    if st.type not in _ALLOWED:
        raise QueryError("只允许只读查询（SELECT / WITH / FROM / DESCRIBE / SUMMARIZE / EXPLAIN）")
    return st.query.strip().rstrip(";").strip(), st.type == duckdb.StatementType.EXPLAIN


class Engine:
    def __init__(self, threads: int | None = None, memory_limit: str | None = None):
        config: dict[str, object] = {"python_enable_replacements": False, "TimeZone": "UTC"}
        if os.environ.get("DATA_VIEWER_DUCKDB_EXTENSIONS"):
            config["extension_directory"] = os.environ["DATA_VIEWER_DUCKDB_EXTENSIONS"]
        if threads:
            config["threads"] = threads
        if memory_limit:
            config["memory_limit"] = memory_limit
        self.db = duckdb.connect(":memory:", config=config)
        self._lock = threading.Lock()

    def cursor(self) -> duckdb.DuckDBPyConnection:
        with self._lock:
            return self.db.cursor()

    @contextmanager
    def session(self, source: "Source | None" = None) -> Iterator[duckdb.DuckDBPyConnection]:
        """A fresh cursor where ``data`` refers to ``source``."""
        cur = self.cursor()
        try:
            if source is not None:
                source.register(cur, TABLE_NAME)
            yield cur
        finally:
            try:
                cur.close()
            except Exception:
                pass

    @staticmethod
    def query(cur: duckdb.DuckDBPyConnection, sql: str, timeout: float = 60.0, params=None) -> pa.Table:
        timer = threading.Timer(timeout, cur.interrupt)
        timer.daemon = True
        timer.start()
        try:
            if params is None:
                cur.execute(sql)
            else:
                cur.execute(sql, params)
            return fetch_arrow(cur)
        except duckdb.InterruptException:
            raise QueryError(f"查询超时（超过 {timeout:.0f} 秒），可以加上过滤条件或 LIMIT 后重试") from None
        except duckdb.Error as e:
            raise QueryError(_clean_error(e)) from None
        finally:
            timer.cancel()

    # ------------------------------------------------------------------
    def page(self, source: "Source", sql: str, offset: int, limit: int, timeout: float = 60.0) -> tuple[pa.Table, bool, float]:
        """Run a read-only query and return one page: (table, has_more, seconds)."""
        stmt, is_explain = validate_select(sql)
        t0 = time.perf_counter()
        with self.session(source) as cur:
            if is_explain:
                table = self.query(cur, stmt, timeout)
                return table.slice(offset, limit), offset + limit < table.num_rows, time.perf_counter() - t0
            wrapped = f"SELECT * FROM ({stmt}) AS __q LIMIT {int(limit) + 1} OFFSET {int(offset)}"
            try:
                table = self.query(cur, wrapped, timeout)
            except QueryError as e:
                if "Parser Error" not in str(e) and "syntax error" not in str(e).lower():
                    raise
                # Statements such as PRAGMA cannot be used as a subquery: run them as-is.
                table = self.query(cur, stmt, timeout).slice(offset, limit + 1)
        has_more = table.num_rows > limit
        return table.slice(0, limit), has_more, time.perf_counter() - t0

    def count(self, source: "Source", sql: str, timeout: float = 60.0) -> int:
        stmt, is_explain = validate_select(sql)
        with self.session(source) as cur:
            if is_explain:
                return self.query(cur, stmt, timeout).num_rows
            t = self.query(cur, f"SELECT count(*) AS n FROM ({stmt}) AS __q", timeout)
        return int(t.column(0)[0].as_py())

    def schema(self, source: "Source", sql: str, timeout: float = 60.0) -> pa.Schema:
        stmt, is_explain = validate_select(sql)
        with self.session(source) as cur:
            if is_explain:
                return self.query(cur, stmt, timeout).schema
            return self.query(cur, f"SELECT * FROM ({stmt}) AS __q LIMIT 0", timeout).schema


def browse_sql(columns: list[str] | None, where: str | None, order: list[dict] | None) -> str:
    cols = ", ".join(sql_ident(c) for c in columns) if columns else "*"
    sql = f"SELECT {cols} FROM {TABLE_NAME}"
    if where and where.strip():
        sql += f" WHERE ({where.strip().rstrip(';')})"
    if order:
        parts = []
        for o in order:
            direction = "DESC" if str(o.get("dir", "asc")).lower() == "desc" else "ASC"
            parts.append(f"{sql_ident(o['column'])} {direction} NULLS LAST")
        sql += " ORDER BY " + ", ".join(parts)
    return sql


def _clean_error(e: Exception) -> str:
    msg = str(e).strip()
    # DuckDB prefixes messages with the error class, keep them but trim noise.
    return msg[:4000]
