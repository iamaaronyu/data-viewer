import random

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from data_viewer.sources import SourceError, detect, detect_dir, open_source

ALL = [
    ("images.lance", "lance", 300),
    ("table.parquet", "parquet", 20000),
    ("partitioned", "parquet", 3000),
    ("people.csv", "csv", 1000),
    ("sales.tsv", "csv", 300),
    ("events.jsonl", "json", 500),
    ("records.json", "json", 120),
    ("signals.arrow", "arrow", 5000),
    ("signals.feather", "arrow", 5000),
    ("signals.arrows", "arrow", 5000),
    ("robot.h5", "hdf5", 60),
    ("wds", "webdataset", 80),
]


@pytest.mark.parametrize("name,fmt,rows", ALL)
def test_detect_open_read_count(demo_dir, engine, name, fmt, rows):
    path = demo_dir / name
    assert detect(path) == fmt
    src = open_source(path, None, {}, engine)
    assert src.format == fmt
    schema = src.schema()
    assert len(schema) > 0
    page = src.read(0, 7)
    assert page.num_rows == min(7, rows)
    assert [f.name for f in page.schema] == [f.name for f in schema]
    assert src.count_rows() == rows
    # deep page and past-the-end page
    assert src.read(rows - 3, 10).num_rows == 3
    assert src.read(rows + 10, 10).num_rows == 0
    # SQL sees the same number of rows
    with engine.session(src) as cur:
        n = engine.query(cur, "SELECT count(*) FROM data").column(0)[0].as_py()
    assert n == rows
    src.close()


def test_parquet_rowgroup_paging_matches_full_read(demo_dir, engine):
    src = open_source(demo_dir / "table.parquet", None, {}, engine)
    full = pq.read_table(demo_dir / "table.parquet", columns=["id", "name", "score"])
    rng = random.Random(3)
    for _ in range(25):
        off = rng.randrange(0, 20000)
        lim = rng.choice([1, 7, 50, 333, 5000])
        got = src.read(off, lim, ["id", "name", "score"])
        want = full.slice(off, lim)
        assert got.column("id").to_pylist() == want.column("id").to_pylist()
        assert got.column("name").to_pylist() == want.column("name").to_pylist()
        as_f = lambda c: np.array([np.nan if v is None else v for v in c.to_pylist()], dtype=float)  # noqa: E731
        assert np.array_equal(as_f(got.column("score")), as_f(want.column("score")), equal_nan=True)
    picked = src.take([19999, 0, 4096, 4095], ["id"])
    assert picked.column("id").to_pylist() == [19999, 0, 4096, 4095]


def test_partitioned_parquet_has_partition_columns(demo_dir, engine):
    src = open_source(demo_dir / "partitioned", None, {}, engine)
    names = src.schema().names
    assert "year" in names and "month" in names
    info = src.info()
    assert info["partition_columns"] == ["month", "year"]


def test_lance_versions_and_tags(demo_dir, engine):
    latest = open_source(demo_dir / "images.lance", None, {}, engine)
    info = latest.info()
    assert info["version"] == 2 and info["latest_version"] == 2
    assert {t["name"] for t in info["tags"]} == {"first-200"}
    v1 = open_source(demo_dir / "images.lance", None, {"version": "1"}, engine)
    assert v1.count_rows() == 200
    tagged = open_source(demo_dir / "images.lance", None, {"version": "first-200"}, engine)
    assert tagged.count_rows() == 200
    assert latest.take([299, 5], ["id"]).column("id").to_pylist() == [299, 5]


def test_csv_all_varchar_param(demo_dir, engine):
    src = open_source(demo_dir / "people.csv", None, {"all_varchar": True}, engine)
    assert all(pa.types.is_string(f.type) for f in src.schema())


def test_hdf5_views(demo_dir, engine):
    p = demo_dir / "robot.h5"
    default = open_source(p, None, {}, engine)
    assert default.node == "/episodes/ep_000" and default.node_kind == "group"
    obs = open_source(p, None, {"node": "/episodes/ep_000/obs"}, engine)
    t = obs.read(5, 3)
    img = t.column("image")
    assert img.type.extension_name == "arrow.fixed_shape_tensor"
    assert img.chunk(0).to_numpy_ndarray().shape == (3, 48, 64, 3)
    assert obs.lazy_columns() == {"image": "uint8[48×64×3]"}
    table = open_source(p, None, {"node": "/table"}, engine)
    assert table.schema().names == ["id", "name", "value", "pos[0]", "pos[1]", "pos[2]"]
    assert table.read(3, 1).column("name").to_pylist() == ["item-3"]
    thumbs = open_source(p, None, {"node": "/meta/thumbnails"}, engine)
    assert thumbs.read(0, 1).column(0).to_pylist()[0][:8] == b"\x89PNG\r\n\x1a\n"
    emb = open_source(p, None, {"node": "/embeddings"}, engine)
    assert pa.types.is_fixed_size_list(emb.schema().field(0).type)
    with engine.session(obs) as cur:
        r = engine.query(cur, "SELECT __index__, image FROM data WHERE __index__ = 7").to_pylist()
    assert r == [{"__index__": 7, "image": "uint8[48×64×3]"}]
    tree = default.info()["tree"]
    assert any(n["path"] == "/episodes/ep_001/obs/image" for n in tree)
    with pytest.raises(SourceError):
        open_source(p, None, {"node": "/nope"}, engine)


def test_webdataset_samples_and_lazy_sql(demo_dir, engine):
    src = open_source(demo_dir / "wds", None, {}, engine)
    assert src.count_rows() == 80
    cols = src.schema().names
    assert cols[:2] == ["__key__", "__shard__"] and {"png", "jpg", "txt", "cls", "json", "wav", "npy"} <= set(cols)
    row = src.read(41, 1).to_pylist()[0]
    assert row["__key__"] == "sample000041" and row["__shard__"] == "shard-000001.tar"
    assert row["jpg"][:3] == b"\xff\xd8\xff" and row["png"] is None
    with engine.session(src) as cur:
        t = engine.query(cur, "SELECT __index__, png, wav FROM data WHERE __index__ IN (0, 1)")
    assert t.column("png").to_pylist()[0] > 0 and t.column("png").to_pylist()[1] is None
    real = src.take([1, 0], ["png"])
    assert real.column("png").to_pylist()[0] is None and real.column("png").to_pylist()[1][:4] == b"\x89PNG"


def test_detect_dir_kinds(demo_dir):
    assert detect_dir(demo_dir / "images.lance") == "lance"
    assert detect_dir(demo_dir / "wds") == "webdataset"
    assert detect_dir(demo_dir / "partitioned") == "parquet"
    assert detect_dir(demo_dir / "media") is None


def test_arrow_stream_and_feather_contents_match(demo_dir, engine):
    tables = [open_source(demo_dir / n, None, {}, engine).read(100, 5) for n in ("signals.arrow", "signals.feather", "signals.arrows")]
    assert tables[0].to_pylist() == tables[1].to_pylist() == tables[2].to_pylist()
    assert np.isfinite(tables[0].column("value").to_numpy()).all()
