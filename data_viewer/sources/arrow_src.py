"""Arrow IPC (file / stream) and Feather files, memory-mapped."""
from __future__ import annotations

import os

import pyarrow as pa
import pyarrow.feather as feather

from ..util import decode_metadata
from .base import Source, SourceError


class ArrowSource(Source):
    format = "arrow"
    label = "Arrow"

    def __init__(self, path, params, engine):
        super().__init__(path, params, engine)
        self.kind = None
        self.batches = None
        errors = []
        try:
            source = pa.memory_map(str(self.path), "r")
            reader = pa.ipc.open_file(source)
            self.table = reader.read_all()
            self.batches = reader.num_record_batches
            self.kind = "Arrow IPC 文件格式（Feather V2）"
        except Exception as e:
            errors.append(str(e))
            try:
                source = pa.memory_map(str(self.path), "r")
                reader = pa.ipc.open_stream(source)
                batches = list(reader)
                self.table = pa.Table.from_batches(batches, schema=reader.schema)
                self.batches = len(batches)
                self.kind = "Arrow IPC 流格式"
            except Exception as e2:
                errors.append(str(e2))
                try:
                    self.table = feather.read_table(str(self.path), memory_map=True)
                    self.kind = "Feather V1"
                except Exception as e3:
                    errors.append(str(e3))
                    raise SourceError("无法读取 Arrow/Feather 文件：" + errors[-1]) from None
        self._count = self.table.num_rows

    def schema(self):
        return self.table.schema

    def read(self, offset, limit, columns=None):
        t = self.table.select(columns) if columns else self.table
        return t.slice(int(offset), int(limit))

    def take(self, indices, columns=None):
        t = self.table.select(columns) if columns else self.table
        return t.take(pa.array([int(i) for i in indices], type=pa.int64()))

    def register(self, con, name):
        con.register(name, self.table)

    def info(self):
        return {
            "layout": self.kind,
            "record_batches": self.batches,
            "rows": self.table.num_rows,
            "in_memory_bytes": self.table.nbytes,
            "file_size": os.path.getsize(self.path),
            "schema_metadata": decode_metadata(self.table.schema.metadata),
        }
