import base64
import datetime as dt
import decimal
import io
import math

import numpy as np
import pyarrow as pa
import pytest
from PIL import Image

from data_viewer import encode, media


@pytest.mark.parametrize("head,mime", [
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "image/png"),
    (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
    (b"GIF89a" + b"\x00" * 8, "image/gif"),
    (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
    (b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav"),
    (b"\x00\x00\x00\x18ftypisom\x00\x00", "video/mp4"),
    (b"\x00\x00\x00\x18ftypavif\x00\x00", "image/avif"),
    (b"\x00\x00\x00\x18ftypM4A \x00\x00", "audio/mp4"),
    (b"\x1a\x45\xdf\xa3" + b"\x00" * 20 + b"webm", "video/webm"),
    (b"OggS" + b"\x00" * 20, "audio/ogg"),
    (b"fLaC" + b"\x00" * 8, "audio/flac"),
    (b"ID3\x03" + b"\x00" * 8, "audio/mpeg"),
    (b"\xff\xfb\x90\x64" + b"\x00" * 8, "audio/mpeg"),
    (b"\xff\xf1\x50\x80" + b"\x00" * 8, "audio/aac"),
    (b"%PDF-1.7" + b"\x00" * 8, "application/pdf"),
    (b"\x93NUMPY\x01\x00", "application/x-npy"),
    (b"II*\x00" + b"\x00" * 8, "image/tiff"),
    (b"BM\x00\x00\x00\x00\x00\x00\x00\x00\x36\x00\x00\x00", "image/bmp"),
    (b"<svg xmlns='http://www.w3.org/2000/svg'></svg>", "image/svg+xml"),
])
def test_sniff_known_types(head, mime):
    assert media.sniff_mime(head) == mime


@pytest.mark.parametrize("data", [b"", b"abc", b"hello world, plain text", bytes(range(16)), b"\xff\xff\xff\xff\xff"])
def test_sniff_rejects_non_media(data):
    assert media.sniff_mime(data) is None


def test_looks_like_text():
    assert media.looks_like_text("中文 text\n".encode()) == "中文 text\n"
    assert media.looks_like_text(b"\x00\x01\x02binary") is None
    # A multibyte character cut at the end of the window is still text.
    assert media.looks_like_text("汉字".encode()[:-1]) is not None


@pytest.mark.parametrize("shape,dtype", [((20, 30), np.uint8), ((20, 30, 3), np.uint8), ((20, 30, 4), np.uint8),
                                         ((3, 20, 30), np.uint8), ((20, 30), np.float32), ((20, 30, 1), np.bool_)])
def test_array_to_png_decodes(shape, dtype):
    rng = np.random.default_rng(0)
    a = (rng.random(shape) * (255 if dtype == np.uint8 else 1)).astype(dtype)
    png = media.array_to_png(a)
    assert png is not None
    with Image.open(io.BytesIO(png)) as im:
        assert im.size == (30, 20)


def test_array_to_png_downscales_and_rejects():
    big = np.zeros((1000, 500), dtype=np.uint8)
    with Image.open(io.BytesIO(media.array_to_png(big, max_side=100))) as im:
        assert max(im.size) <= 100
    assert media.array_to_png(np.zeros((5, 5, 7))) is None
    assert media.array_to_png(np.zeros(10)) is None


def test_thumbnail_returns_webp():
    buf = io.BytesIO()
    Image.new("RGB", (800, 400), (200, 10, 10)).save(buf, format="JPEG")
    data, mime = media.thumbnail(buf.getvalue(), 128)
    assert mime == "image/webp"
    with Image.open(io.BytesIO(data)) as im:
        assert max(im.size) <= 128


def _enc(arr, field=None):
    f = field or pa.field("c", arr.type)
    return encode.encode_array(arr, f)


def test_encode_scalars():
    assert _enc(pa.array([1, None, 2**60], pa.int64())) == [1, None, str(2**60)]
    out = _enc(pa.array([1.5, float("nan"), float("inf"), -float("inf"), None]))
    assert out == [1.5, "NaN", "Infinity", "-Infinity", None]
    assert _enc(pa.array([decimal.Decimal("1.20")], pa.decimal128(5, 2))) == ["1.20"]
    assert _enc(pa.array([True, None])) == [True, None]
    assert _enc(pa.array([dt.date(2024, 1, 2)])) == ["2024-01-02"]
    ts = _enc(pa.array([dt.datetime(2024, 1, 2, 3, 4, 5)], pa.timestamp("ns", tz="UTC")))
    assert ts[0].startswith("2024-01-02 03:04:05") and "000000" not in ts[0]
    assert _enc(pa.array([1500], pa.duration("ms"))) == ["1500 ms"]
    assert _enc(pa.array(["a", "b", "a"]).dictionary_encode()) == ["a", "b", "a"]
    assert _enc(pa.array([None, None], pa.null())) == [None, None]


def test_encode_long_string_truncates():
    out = _enc(pa.array(["x" * 5000]))
    assert out[0]["$t"] == "str" and out[0]["len"] == 5000 and len(out[0]["v"]) == encode.STR_LIMIT


def test_encode_binary_inline_image_and_blob():
    png = media.array_to_png(np.zeros((16, 16), dtype=np.uint8))
    out = _enc(pa.array([png, b"\x00\x01\x02", "text".encode()], pa.binary()))
    assert out[0]["mime"] == "image/png" and base64.b64decode(out[0]["data"]) == png
    assert out[1]["$t"] == "bin" and "hex" in out[1]
    assert out[2]["text"] == "text"


def test_encode_large_image_is_not_inlined():
    rng = np.random.default_rng(0)
    png = media.array_to_png(rng.integers(0, 255, (300, 300, 3), dtype=np.uint8))
    assert len(png) > encode.INLINE_IMAGE_LIMIT
    out = _enc(pa.array([png], pa.large_binary()))
    assert out[0]["mime"] == "image/png" and "data" not in out[0]


def test_encode_vectors_and_slices():
    vals = np.arange(30, dtype=np.float32)
    arr = pa.FixedSizeListArray.from_arrays(pa.array(vals), 10)
    out = _enc(arr.slice(1, 2))
    assert out[0]["dim"] == 10 and out[0]["head"][0] == 10.0
    assert math.isclose(out[1]["norm"], float(np.linalg.norm(vals[20:30])), rel_tol=1e-4)


def test_encode_tensor_cells():
    nd = np.zeros((2, 16, 16, 3), dtype=np.uint8)
    out = _enc(pa.FixedShapeTensorArray.from_numpy_ndarray(nd))
    assert out[0]["$t"] == "nd" and out[0]["shape"] == [16, 16, 3] and "img" in out[0]


def test_encode_nested_binary_gets_sub_path():
    png = media.array_to_png(np.zeros((8, 8), dtype=np.uint8))
    arr = pa.array([{"img": {"bytes": png}, "tags": list(range(80))}])
    cell = _enc(arr)[0]
    assert cell["img"]["bytes"]["sub"] == ["img", "bytes"]
    assert cell["tags"][-1] == {"$t": "more", "n": 80 - encode.LIST_LIMIT}


def test_encode_map_and_json_kind():
    m = pa.array([[("a", 1), ("b", 2)]], pa.map_(pa.string(), pa.int32()))
    assert _enc(m) == [{"a": 1, "b": 2}]
    f = pa.field("j", pa.string(), metadata={"dv:kind": "json"})
    assert encode.encode_array(pa.array(['{"x": [1, 2]}', "not json"]), f) == [{"x": [1, 2]}, "not json"]
    assert encode.describe_field(f)["kind"] == "json"


def test_navigate_and_full_json():
    value = {"a": [{"b": b"\x89PNG\r\n\x1a\nrest"}]}
    assert encode.navigate(value, ["a", 0, "b"]).startswith(b"\x89PNG")
    full = encode.full_json("struct", value)
    assert full["a"][0]["b"]["sub"] == ["a", 0, "b"] and "data" not in full["a"][0]["b"]
