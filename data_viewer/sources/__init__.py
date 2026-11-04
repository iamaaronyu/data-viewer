"""Format detection and adapter factory."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from .base import Source, SourceError

FORMAT_LABELS = {
    "lance": "Lance",
    "parquet": "Parquet",
    "csv": "CSV",
    "json": "JSON",
    "arrow": "Arrow",
    "hdf5": "HDF5",
    "webdataset": "WebDataset",
    "rlds": "RLDS / TFRecord",
}

_FILE_SUFFIXES: list[tuple[tuple[str, ...], str]] = [
    ((".parquet", ".pq", ".parq"), "parquet"),
    ((".csv", ".tsv", ".psv", ".csv.gz", ".tsv.gz", ".csv.zst", ".tsv.zst"), "csv"),
    ((".jsonl", ".ndjson", ".json", ".jsonl.gz", ".ndjson.gz", ".json.gz", ".jsonl.zst", ".json.zst"), "json"),
    ((".arrow", ".feather", ".ipc", ".arrows", ".fea"), "arrow"),
    ((".h5", ".hdf5", ".he5", ".hdf", ".h5ad", ".nc", ".nc4"), "hdf5"),
    ((".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"), "webdataset"),
]


def detect_file(name: str) -> str | None:
    from ..embodied import is_rlds
    if is_rlds(name):
        return "rlds"
    lower = name.lower()
    for suffixes, fmt in _FILE_SUFFIXES:
        if lower.endswith(suffixes):
            return fmt
    return None


def is_lance_dir(p: Path) -> bool:
    try:
        return p.name.endswith(".lance") or (p / "_versions").is_dir()
    except OSError:
        return False


def detect_dir(p: Path, depth: int = 0) -> str | None:
    """Guess whether a directory is a dataset (Lance, Parquet/CSV/JSON set, WebDataset shards)."""
    if is_lance_dir(p):
        return "lance"
    counts: Counter = Counter()
    other = 0
    subdirs: list[Path] = []
    try:
        for i, child in enumerate(p.iterdir()):
            if i > 2000:
                break
            if child.name.startswith((".", "_")):
                continue
            if child.is_dir():
                subdirs.append(child)
                continue
            fmt = detect_file(child.name)
            if fmt:
                counts[fmt] += 1
            else:
                other += 1
    except OSError:
        return None
    kinds = set(counts)
    if kinds == {"webdataset"}:
        return "webdataset"
    if kinds == {"parquet"}:
        return "parquet"
    if not kinds and subdirs and depth < 3 and all("=" in d.name for d in subdirs[:50]):
        inner = detect_dir(subdirs[0], depth + 1)
        if inner == "parquet":
            return "parquet"
    if kinds == {"csv"} and counts["csv"] > 1:
        return "csv"
    if kinds == {"json"} and counts["json"] > 1:
        return "json"
    return None


def detect(path: Path) -> str | None:
    if path.is_dir():
        return detect_dir(path)
    return detect_file(path.name)


def open_source(path: Path, fmt: str | None, params: dict | None, engine) -> Source:
    path = Path(path)
    if not path.exists():
        raise SourceError(f"路径不存在：{path}")
    fmt = fmt or detect(path)
    if fmt is None:
        raise SourceError("无法识别的数据格式")
    if fmt == "lance":
        from .lance_src import LanceSource
        return LanceSource(path, params, engine)
    if fmt in ("parquet", "csv", "json"):
        from .duck_src import DuckSource
        return DuckSource(path, params, engine, fmt)
    if fmt == "arrow":
        from .arrow_src import ArrowSource
        return ArrowSource(path, params, engine)
    if fmt == "hdf5":
        from .hdf5_src import HDF5Source
        return HDF5Source(path, params, engine)
    if fmt == "webdataset":
        from .wds_src import WebDatasetSource
        return WebDatasetSource(path, params, engine)
    if fmt == "rlds":
        from .rlds_src import RLDSSource
        return RLDSSource(path, params, engine)
    raise SourceError(f"不支持的格式：{fmt}")


__all__ = ["Source", "SourceError", "FORMAT_LABELS", "detect", "detect_dir", "detect_file", "is_lance_dir", "open_source"]
