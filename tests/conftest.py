import io
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

os.environ.setdefault("LANCE_LOG", "error")

from data_viewer.demo import _shape_image, make_demo  # noqa: E402
from data_viewer.engine import Engine  # noqa: E402
from data_viewer.media import array_to_png  # noqa: E402


@pytest.fixture(scope="session")
def demo_dir(tmp_path_factory) -> Path:
    root = make_demo(tmp_path_factory.mktemp("demo"))
    # A Hugging Face style image column: struct<bytes: binary, path: string>.
    import numpy as np

    rng = np.random.default_rng(1)
    big = io.BytesIO()
    try:
        from PIL import Image

        Image.fromarray(rng.integers(0, 255, (256, 256, 3), dtype=np.uint8)).save(big, format="PNG")
    except Exception:  # pragma: no cover
        big.write(array_to_png(rng.integers(0, 255, (256, 256, 3), dtype=np.uint8)))
    small = array_to_png(_shape_image(rng, 0, 32))
    pq.write_table(pa.table({
        "id": [0, 1],
        "image": pa.array([{"bytes": small, "path": "a.png"}, {"bytes": big.getvalue(), "path": "b.png"}],
                          pa.struct([("bytes", pa.binary()), ("path", pa.string())])),
    }), root / "hf_images.parquet")
    return root


@pytest.fixture(scope="session")
def engine() -> Engine:
    return Engine()


@pytest.fixture(scope="session")
def client(demo_dir):
    import warnings

    warnings.filterwarnings("ignore", message=".*httpx.*")
    from fastapi.testclient import TestClient

    from data_viewer.server import AppConfig, create_app

    app = create_app(AppConfig(root=demo_dir))
    with TestClient(app, base_url="http://127.0.0.1:8765") as c:
        yield c


@pytest.fixture(scope="session")
def open_source(client):
    cache = {}

    def _open(path, fmt=None, params=None):
        key = (path, fmt, repr(params))
        if key not in cache:
            r = client.post("/api/open", json={"path": path, "format": fmt, "params": params or {}})
            assert r.status_code == 200, r.text
            cache[key] = r.json()
        return cache[key]

    return _open
