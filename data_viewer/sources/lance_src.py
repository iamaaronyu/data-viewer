"""Lance datasets (directories with _versions/ manifests)."""
from __future__ import annotations

import os

os.environ.setdefault("LANCE_LOG", "error")

import lance  # noqa: E402
import pyarrow as pa  # noqa: E402

from ..util import decode_metadata, json_safe  # noqa: E402
from .base import Source, SourceError  # noqa: E402


class LanceSource(Source):
    format = "lance"
    label = "Lance"

    def __init__(self, path, params, engine):
        super().__init__(path, params, engine)
        version = self.params.get("version")
        if isinstance(version, str) and version.strip().isdigit():
            version = int(version)
        if version in ("", None):
            version = None
        try:
            self.ds = lance.dataset(str(self.path), version=version) if version is not None else lance.dataset(str(self.path))
        except Exception as e:  # pragma: no cover - depends on on-disk state
            raise SourceError(f"无法打开 Lance 数据集：{e}") from None
        self._schema = self.ds.schema

    def schema(self) -> pa.Schema:
        return self._schema

    def num_rows(self) -> int | None:
        if self._count is None:
            self._count = self.ds.count_rows()
        return self._count

    def read(self, offset, limit, columns=None):
        return self.ds.to_table(columns=columns, offset=int(offset), limit=int(limit))

    def take(self, indices, columns=None):
        return self.ds.take([int(i) for i in indices], columns=columns)

    def register(self, con, name):
        con.register(name, self.ds)

    def info(self):
        ds = self.ds
        out: dict = {
            "uri": ds.uri,
            "version": ds.version,
            "rows": self.num_rows(),
        }
        try:
            out["latest_version"] = ds.latest_version
        except Exception:
            pass
        try:
            out["data_storage_version"] = ds.data_storage_version
        except Exception:
            pass
        try:
            out["fragments"] = len(ds.get_fragments())
        except Exception:
            pass
        try:
            versions = ds.versions()
            out["versions"] = [
                {
                    "version": v.get("version"),
                    "timestamp": v.get("timestamp").isoformat(sep=" ", timespec="seconds") if v.get("timestamp") else None,
                    "rows": _int((v.get("metadata") or {}).get("total_rows")),
                    "size": _int((v.get("metadata") or {}).get("total_files_size")),
                }
                for v in reversed(versions[-500:])
            ]
            out["version_count"] = len(versions)
        except Exception as e:
            out["versions_error"] = str(e)
        try:
            tags = ds.tags.list()
            out["tags"] = [
                {"name": k, "version": getattr(v, "version", None) if not isinstance(v, dict) else v.get("version")}
                for k, v in sorted(tags.items())
            ]
        except Exception:
            out["tags"] = []
        try:
            idx = []
            for d in ds.describe_indices():
                idx.append({
                    "name": getattr(d, "name", None),
                    "type": getattr(d, "index_type", None) or getattr(d, "type", None),
                    "fields": list(getattr(d, "field_names", None) or getattr(d, "fields", None) or []),
                })
            out["indices"] = json_safe(idx)
        except Exception:
            try:
                out["indices"] = json_safe([
                    {"name": i.get("name"), "type": i.get("type"), "fields": i.get("fields")} for i in ds.list_indices()
                ])
            except Exception:
                out["indices"] = []
        out["schema_metadata"] = decode_metadata(self._schema.metadata)
        return out


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
