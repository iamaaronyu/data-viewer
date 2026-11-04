"""Common interface implemented by every format adapter."""
from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa

if TYPE_CHECKING:  # pragma: no cover
    import duckdb

    from ..engine import Engine


class SourceError(Exception):
    """The data could not be opened or read."""


class Source:
    """A tabular view over some data on disk.

    ``read`` is the fast browsing path (offset/limit without filters). Filtering,
    sorting, SQL and statistics go through DuckDB, where ``register`` makes the
    data available as the table ``data``.
    """

    format: str = ""
    label: str = ""

    def __init__(self, path: Path, params: dict | None, engine: "Engine"):
        self.path = Path(path)
        self.params = dict(params or {})
        self.engine = engine
        self._count: int | None = None
        self._count_lock = threading.Lock()

    # ---- identity -------------------------------------------------------
    @property
    def name(self) -> str:
        return self.path.name or str(self.path)

    def base_dir(self) -> Path:
        """Directory used to resolve relative media paths found in string columns."""
        return self.path if self.path.is_dir() else self.path.parent

    # ---- required -------------------------------------------------------
    def schema(self) -> pa.Schema:
        raise NotImplementedError

    def read(self, offset: int, limit: int, columns: list[str] | None = None) -> pa.Table:
        raise NotImplementedError

    def register(self, con: "duckdb.DuckDBPyConnection", name: str) -> None:
        raise NotImplementedError

    # ---- optional -------------------------------------------------------
    def num_rows(self) -> int | None:
        """Row count if it is cheap to know, else None."""
        return self._count

    def count_rows(self) -> int:
        """Exact row count (may be slow)."""
        if self._count is None:
            with self._count_lock:
                if self._count is None:
                    self._count = self._count_slow()
        return self._count

    def _count_slow(self) -> int:
        with self.engine.session(self) as cur:
            t = self.engine.query(cur, "SELECT count(*) FROM data", timeout=600)
        return int(t.column(0)[0].as_py())

    def count_status(self) -> dict:
        """Row count for the UI: {count, exact, pending}."""
        n = self.num_rows()
        return {"count": n, "exact": n is not None, "pending": n is None}

    def take(self, indices: list[int], columns: list[str] | None = None) -> pa.Table:
        parts = [self.read(int(i), 1, columns) for i in indices]
        if not parts:
            return self.schema().empty_table() if columns is None else pa.table({c: [] for c in columns})
        return pa.concat_tables(parts)

    def lazy_columns(self) -> dict[str, str]:
        """Columns that SQL sees as light-weight proxies (name -> description).

        Their real values are fetched through ``take`` using the ``__index__`` column.
        """
        return {}

    def info(self) -> dict[str, Any]:
        return {}

    def warnings(self) -> list[str]:
        return []

    def close(self) -> None:
        pass


def empty_like(schema: pa.Schema, columns: list[str] | None) -> pa.Table:
    if columns:
        schema = pa.schema([schema.field(c) for c in columns])
    return schema.empty_table()
