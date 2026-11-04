import io
import json

import pytest
from PIL import Image

DATASETS = ["images.lance", "table.parquet", "partitioned", "people.csv", "sales.tsv", "events.jsonl", "records.json",
            "signals.arrow", "signals.feather", "signals.arrows", "robot.h5", "wds", "hf_images.parquet"]


def cell(client, page, row, col, **extra):
    url = page["cell_url"].replace("{row}", str(row)).replace("{col}", col).replace("{abs}", str(page["offset"] + row))
    for k, v in extra.items():
        url += f"&{k}={v if isinstance(v, (str, int)) else json.dumps(v)}"
    return client.get(url)


def test_config_and_listing(client):
    cfg = client.get("/api/config").json()
    assert cfg["initial"] is None and "lance" in cfg["formats"]
    listing = client.get("/api/fs/list").json()
    names = {e["name"]: e for e in listing["entries"]}
    assert names["images.lance"]["format"] == "lance" and names["images.lance"]["dir"]
    assert names["robot.h5"]["format"] == "hdf5"
    assert client.get("/api/fs/list", params={"path": "wds"}).json()["dir_format"] == "webdataset"


@pytest.mark.parametrize("path", DATASETS)
def test_every_dataset_end_to_end(client, open_source, path):
    d = open_source(path)
    sid = d["id"]
    page = client.post(f"/api/s/{sid}/rows", json={"offset": 0, "limit": 20}).json()
    assert page["mode"] == "browse" and len(page["rows"]) > 0
    json.dumps(page, allow_nan=False)  # strictly valid JSON
    sql = client.post(f"/api/s/{sid}/sql", json={"sql": "SELECT * FROM data", "offset": 1, "limit": 3})
    assert sql.status_code == 200, sql.text
    json.dumps(sql.json(), allow_nan=False)
    assert client.post(f"/api/s/{sid}/count", json={}).json()["exact"] in (True, False)
    stats = client.post(f"/api/s/{sid}/stats", json={}).json()
    assert stats["rows"] > 0 and all("error" not in c for c in stats["columns"])
    for c in d["columns"]:
        r = client.post(f"/api/s/{sid}/stats/column", json={"column": c["name"]})
        assert r.status_code == 200, (c, r.text)
    assert client.get(f"/api/s/{sid}/info").status_code == 200
    assert client.get(f"/api/s/{sid}/schema").status_code == 200
    for j, col in enumerate(page["columns"]):
        r = cell(client, page, 0, col["name"], **({"format": "png"} if col["kind"] == "tensor" else {}))
        assert r.status_code == 200, (col, r.text)


def test_filter_sort_and_count(client, open_source):
    sid = open_source("table.parquet")["id"]
    r = client.post(f"/api/s/{sid}/rows", json={"offset": 0, "limit": 5, "where": "city = '北京'",
                                                 "order": [{"column": "id", "dir": "desc"}]}).json()
    assert r["mode"] == "query" and [row[2] for row in r["rows"]] == ["北京"] * 5
    ids = [row[0] for row in r["rows"]]
    assert ids == sorted(ids, reverse=True)
    n = client.post(f"/api/s/{sid}/count", json={"where": "city = '北京'"}).json()["count"]
    assert n == len([i for i in range(20000) if i % 7 == 0])
    bad = client.post(f"/api/s/{sid}/rows", json={"where": "nope_column > 1"})
    assert bad.status_code == 400 and "nope_column" in bad.json()["error"]
    inj = client.post(f"/api/s/{sid}/rows", json={"where": "1=1); DROP TABLE x; --"})
    assert inj.status_code == 400


@pytest.mark.parametrize("sql", [
    "COPY data TO '/tmp/x.csv'", "CREATE TABLE t AS SELECT 1", "INSTALL httpfs", "ATTACH 'x.db'",
    "SET threads=1", "SELECT 1; SELECT 2", "CALL pragma_version()", "SELECT * FROM query('CREATE TABLE z(a INT)')",
])
def test_sql_guard_rejects(client, open_source, sql):
    sid = open_source("people.csv")["id"]
    r = client.post(f"/api/s/{sid}/sql", json={"sql": sql})
    assert r.status_code == 400


@pytest.mark.parametrize("sql", ["DESCRIBE data", "SUMMARIZE data", "PRAGMA version", "EXPLAIN SELECT * FROM data",
                                 "WITH t AS (SELECT * FROM data) SELECT count(*) FROM t", "FROM data LIMIT 3"])
def test_sql_guard_allows_read_only(client, open_source, sql):
    sid = open_source("people.csv")["id"]
    r = client.post(f"/api/s/{sid}/sql", json={"sql": sql})
    assert r.status_code == 200, r.text


def test_sql_paging_consistent_with_order(client, open_source):
    sid = open_source("table.parquet")["id"]
    q = "SELECT id FROM data ORDER BY score DESC NULLS LAST, id"
    p1 = client.post(f"/api/s/{sid}/sql", json={"sql": q, "offset": 0, "limit": 30}).json()
    p2 = client.post(f"/api/s/{sid}/sql", json={"sql": q, "offset": 30, "limit": 30}).json()
    both = client.post(f"/api/s/{sid}/sql", json={"sql": q, "offset": 0, "limit": 60}).json()
    assert [r[0] for r in p1["rows"] + p2["rows"]] == [r[0] for r in both["rows"]]
    assert p1["has_more"] is True


def test_lazy_columns_resolved_in_sql(client, open_source):
    d = open_source("wds")
    page = client.post(f"/api/s/{d['id']}/sql", json={"sql": "SELECT __index__, png FROM data WHERE __index__ = 0"}).json()
    assert page["columns"][1]["lazy"] is True and page["rows"][0][1]["mime"] == "image/png"
    r = cell(client, page, 0, "png")
    assert r.headers["content-type"] == "image/png"
    h5 = open_source("robot.h5", params={"node": "/episodes/ep_000/obs"})
    page = client.post(f"/api/s/{h5['id']}/sql", json={"sql": "SELECT image, __index__ FROM data WHERE __index__ = 3"}).json()
    assert page["rows"][0][0]["$t"] == "nd"
    png = cell(client, page, 0, "image", format="png")
    with Image.open(io.BytesIO(png.content)) as im:
        assert im.size == (64, 48)


def test_cell_media_range_and_thumb(client, open_source):
    sid = open_source("wds")["id"]
    page = client.post(f"/api/s/{sid}/rows", json={"offset": 0, "limit": 1}).json()
    wav = cell(client, page, 0, "wav")
    assert wav.headers["content-type"] == "audio/wav" and wav.headers["accept-ranges"] == "bytes"
    part = client.get(page["cell_url"].replace("{row}", "0").replace("{col}", "wav").replace("{abs}", "0"), headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100 and part.headers["content-range"].endswith(f"/{len(wav.content)}")
    npy = cell(client, page, 0, "npy").json()
    assert npy["shape"] == [4, 4] and len(npy["data"]) == 4
    page = client.post(f"/api/s/{sid}/rows", json={"offset": 1, "limit": 1}).json()
    thumb = cell(client, page, 0, "jpg", thumb=32)
    assert thumb.headers["content-type"] == "image/webp"


def test_nested_binary_sub_path(client, open_source):
    sid = open_source("hf_images.parquet")["id"]
    page = client.post(f"/api/s/{sid}/rows", json={"offset": 0, "limit": 2}).json()
    small, big = page["rows"][0][1], page["rows"][1][1]
    assert small["bytes"]["data"] and small["bytes"]["sub"] == ["bytes"]
    assert "data" not in big["bytes"] and big["bytes"]["mime"] == "image/png"
    r = cell(client, page, 1, "image", sub=["bytes"])
    assert r.headers["content-type"] == "image/png"
    with Image.open(io.BytesIO(r.content)) as im:
        assert im.size == (256, 256)
    assert cell(client, page, 1, "image", sub=["nope"]).status_code == 404


def test_path_column_media_and_fs_raw(client, open_source):
    sid = open_source("table.parquet")["id"]
    page = client.post(f"/api/s/{sid}/rows", json={"offset": 0, "limit": 1, "columns": ["image_path"]}).json()
    v = page["rows"][0][0]
    assert v["$t"] == "path" and v["mime"] == "image/png"
    r = client.get(v["url"] + "&thumb=48")
    assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
    text = client.get("/api/fs/text", params={"path": "media/README.txt"}).json()
    assert "image_path" in text["text"]


def test_stats_exact_distinct_and_sample(client, open_source):
    sid = open_source("people.csv")["id"]
    client.post(f"/api/s/{sid}/count", json={})  # makes the row count known
    s = client.post(f"/api/s/{sid}/stats", json={}).json()
    by = {c["name"]: c for c in s["columns"]}
    assert s["distinct_exact"] is True and by["id"]["distinct"] == 1000 and by["city"]["distinct"] == 7
    s2 = client.post(f"/api/s/{sid}/stats", json={"sample": 100}).json()
    assert s2["rows"] == 100
    col = client.post(f"/api/s/{sid}/stats/column", json={"column": "age"}).json()
    assert col["histogram"]["type"] == "discrete" and sum(col["histogram"]["counts"]) == 1000


def test_security_checks(client, open_source):
    sid = open_source("people.csv")["id"]
    assert client.get("/api/config", headers={"host": "evil.example:8765"}).status_code == 403
    r = client.post(f"/api/s/{sid}/sql", json={"sql": "SELECT 1"}, headers={"origin": "http://evil.example"})
    assert r.status_code == 403
    r = client.post(f"/api/s/{sid}/sql", content="sql=SELECT 1", headers={"content-type": "application/x-www-form-urlencoded"})
    assert r.status_code == 415
    assert client.get("/api/fs/list", params={"path": ".."}).status_code == 403
    assert client.get("/api/fs/raw", params={"path": "/etc/hosts"}).status_code == 403
    assert client.post("/api/open", json={"path": "../.."}).status_code == 403
    ok = client.post(f"/api/s/{sid}/sql", json={"sql": "SELECT 1"}, headers={"origin": "http://127.0.0.1:8765"})
    assert ok.status_code == 200


def test_token_mode(demo_dir):
    from fastapi.testclient import TestClient

    from data_viewer.server import AppConfig, create_app

    app = create_app(AppConfig(root=demo_dir, host="0.0.0.0", token="s3cret"))
    with TestClient(app, base_url="http://192.168.1.20:8765") as c:
        assert c.get("/api/config").status_code == 401
        r = c.get("/?token=s3cret", follow_redirects=False)
        assert r.status_code in (302, 307) and "dv_token" in r.headers.get("set-cookie", "")
        assert c.get("/api/config", headers={"x-dv-token": "s3cret"}).status_code == 200
