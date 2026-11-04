"""Generate a small demo dataset in every supported format (``data-viewer --demo``)."""
from __future__ import annotations

import datetime as dt
import decimal
import io
import json
import math
import tarfile
import wave
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.feather as feather
import pyarrow.parquet as pq

from .media import array_to_png

SHAPES = ["circle", "square", "triangle"]
CITIES = ["北京", "上海", "深圳", "杭州", "成都", "Berlin", "Paris"]


def _shape_image(rng: np.random.Generator, kind: int, size: int = 64) -> np.ndarray:
    img = np.zeros((size, size, 3), dtype=np.uint8)
    img[:] = rng.integers(0, 60, 3)
    color = rng.integers(120, 256, 3)
    yy, xx = np.mgrid[0:size, 0:size]
    cx, cy = rng.integers(size // 3, 2 * size // 3, 2)
    r = rng.integers(size // 6, size // 3)
    if kind == 0:
        mask = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    elif kind == 1:
        mask = (abs(xx - cx) <= r) & (abs(yy - cy) <= r)
    else:
        mask = (yy - cy + r >= 0) & (yy - cy - r <= 0) & (abs(xx - cx) <= (yy - cy + r) / 2)
    img[mask] = color
    return img


def _wav(freq: float, seconds: float = 0.6, rate: int = 16000) -> bytes:
    t = np.arange(int(seconds * rate)) / rate
    env = np.minimum(1, np.minimum(t, seconds - t) * 20)
    pcm = (0.4 * np.sin(2 * math.pi * freq * t) * env * 32767).astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def _jpeg(arr: np.ndarray) -> bytes:
    try:
        from PIL import Image
        out = io.BytesIO()
        Image.fromarray(arr).save(out, format="JPEG", quality=85)
        return out.getvalue()
    except Exception:
        return array_to_png(arr) or b""


def make_demo(root: Path, force: bool = False) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    marker = root / ".demo-complete"
    if marker.exists() and not force:
        return root
    rng = np.random.default_rng(7)

    # ---- shared media files referenced by path columns ------------------
    media_dir = root / "media"
    media_dir.mkdir(exist_ok=True)
    for i in range(12):
        (media_dir / f"img_{i:03d}.png").write_bytes(array_to_png(_shape_image(rng, i % 3, 96)))
    (media_dir / "tone_440.wav").write_bytes(_wav(440))
    (media_dir / "README.txt").write_text("这些文件被 table.parquet 的 image_path 列引用。\n", encoding="utf-8")

    # ---- Lance ------------------------------------------------------------
    import lance

    n = 300
    labels = rng.integers(0, 3, n)
    images = [array_to_png(_shape_image(rng, int(k))) for k in labels]
    emb = rng.normal(size=(n, 64)).astype(np.float32)
    emb /= np.linalg.norm(emb, axis=1, keepdims=True)
    base = dt.datetime(2026, 1, 1, 8, 0, 0)
    lance_table = pa.table({
        "id": pa.array(np.arange(n), pa.int64()),
        "image": pa.array(images, pa.large_binary()),
        "label": pa.array(labels.astype(np.int32)),
        "label_name": pa.array([SHAPES[k] for k in labels]),
        "caption": pa.array([f"一个{['圆形', '方形', '三角形'][k]}，编号 {i}" for i, k in enumerate(labels)]),
        "embedding": pa.FixedSizeListArray.from_arrays(pa.array(emb.ravel()), 64),
        "created_at": pa.array([base + dt.timedelta(minutes=37 * i) for i in range(n)], pa.timestamp("us")),
        "tags": pa.array([[SHAPES[k], "demo"] + (["hard"] if i % 7 == 0 else []) for i, k in enumerate(labels)]),
        "meta": pa.array([{"width": 64, "height": 64, "source": "synthetic"} for _ in range(n)]),
    })
    lance_path = root / "images.lance"
    if not lance_path.exists():
        lance.write_dataset(lance_table.slice(0, 200), str(lance_path))
        ds = lance.write_dataset(lance_table.slice(200), str(lance_path), mode="append")
        try:
            ds.tags.create("first-200", 1)
        except Exception:
            pass

    # ---- Parquet ----------------------------------------------------------
    m = 20000
    scores = rng.normal(70, 12, m)
    scores[rng.random(m) < 0.03] = np.nan
    score_arr = pa.array(scores, mask=rng.random(m) < 0.02)
    long_note = "这是一段很长的备注。" * 160
    pq_table = pa.table({
        "id": pa.array(np.arange(m), pa.int64()),
        "name": pa.array([f"user_{i:05d}" for i in range(m)]),
        "city": pa.array([CITIES[i % len(CITIES)] for i in range(m)]).dictionary_encode(),
        "score": score_arr,
        "amount": pa.array([decimal.Decimal(f"{v:.2f}") for v in rng.uniform(0, 5000, m)], pa.decimal128(10, 2)),
        "active": pa.array(rng.random(m) < 0.6),
        "birthday": pa.array([dt.date(1970, 1, 1) + dt.timedelta(days=int(d)) for d in rng.integers(3000, 15000, m)]),
        "event_time": pa.array([dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=int(s))
                                for s in rng.integers(0, 86400 * 30, m)], pa.timestamp("ms", tz="UTC")),
        "latency": pa.array(rng.integers(1, 5000, m), pa.duration("ms")),
        "tags": pa.array([[CITIES[(i + j) % 7] for j in range(i % 4)] for i in range(m)]),
        "point": pa.array([{"x": float(x), "y": float(y)} for x, y in rng.normal(size=(m, 2))]),
        "attrs": pa.array([[("clicks", int(i % 13)), ("views", int(i % 101))] for i in range(m)],
                          pa.map_(pa.string(), pa.int32())),
        "big": pa.array([2**60 + i for i in range(m)], pa.uint64()),
        "notes": pa.array([long_note if i % 500 == 0 else (None if i % 11 == 0 else f"备注 {i}") for i in range(m)]),
        "image_path": pa.array([f"media/img_{i % 12:03d}.png" for i in range(m)]),
    })
    pq.write_table(pq_table, root / "table.parquet", row_group_size=4096)

    part_root = root / "partitioned"
    for year in (2025, 2026):
        for month in (1, 2, 3):
            d = part_root / f"year={year}" / f"month={month}"
            d.mkdir(parents=True, exist_ok=True)
            k = 500
            pq.write_table(pa.table({
                "order_id": pa.array(np.arange(k) + year * 100000 + month * 1000),
                "amount": pa.array(rng.gamma(2.0, 50.0, k)),
                "city": pa.array([CITIES[i % 7] for i in range(k)]),
            }), d / "part-0.parquet")

    # ---- CSV / TSV --------------------------------------------------------
    with open(root / "people.csv", "w", encoding="utf-8") as f:
        f.write("id,name,age,city,signup_date,score\n")
        for i in range(1000):
            f.write(f"{i},用户{i},{18 + i % 60},{CITIES[i % 7]},2025-{1 + i % 12:02d}-{1 + i % 28:02d},{rng.normal(60, 15):.3f}\n")
    with open(root / "sales.tsv", "w", encoding="utf-8") as f:
        f.write("sku\tqty\tprice\tnote\n")
        for i in range(300):
            f.write(f"SKU-{i:04d}\t{1 + i % 9}\t{rng.uniform(1, 99):.2f}\t{'含, 逗号' if i % 5 == 0 else ''}\n")

    # ---- JSON / JSONL -----------------------------------------------------
    with open(root / "events.jsonl", "w", encoding="utf-8") as f:
        for i in range(500):
            f.write(json.dumps({
                "event_id": i, "type": ["click", "view", "purchase"][i % 3], "ts": f"2026-05-{1 + i % 30:02d}T12:{i % 60:02d}:00Z",
                "user": {"id": i % 50, "name": f"u{i % 50}", "vip": i % 9 == 0},
                "items": [{"sku": f"SKU-{(i + j) % 300:04d}", "qty": 1 + j} for j in range(i % 4)],
                "props": {"utm": "newsletter"} if i % 4 == 0 else {},
            }, ensure_ascii=False) + "\n")
    with open(root / "records.json", "w", encoding="utf-8") as f:
        json.dump([{"id": i, "title": f"记录 {i}", "values": [round(float(v), 3) for v in rng.normal(size=5)]} for i in range(120)],
                  f, ensure_ascii=False)

    # ---- Arrow / Feather --------------------------------------------------
    arrow_table = pa.table({
        "t": pa.array(np.arange(5000) * 0.01),
        "sensor": pa.array([f"s{i % 4}" for i in range(5000)]),
        "value": pa.array(np.sin(np.arange(5000) / 50) + rng.normal(0, 0.05, 5000)),
        "window": pa.array([list(np.round(rng.normal(size=3), 3)) for _ in range(5000)]),
    })
    with pa.OSFile(str(root / "signals.arrow"), "wb") as sink:
        with pa.ipc.new_file(sink, arrow_table.schema) as w:
            for batch in arrow_table.to_batches(max_chunksize=1000):
                w.write_batch(batch)
    feather.write_feather(arrow_table, root / "signals.feather", compression="zstd")
    with pa.OSFile(str(root / "signals.arrows"), "wb") as sink:
        with pa.ipc.new_stream(sink, arrow_table.schema) as w:
            w.write_table(arrow_table)

    # ---- HDF5 ---------------------------------------------------------------
    import h5py

    with h5py.File(root / "robot.h5", "w") as f:
        f.attrs["description"] = "演示：机器人轨迹数据（两个 episode）"
        f.attrs["created"] = "2026-11-04"
        for e in range(2):
            T = 60
            g = f.create_group(f"episodes/ep_{e:03d}")
            g.attrs["success"] = bool(e == 0)
            frames = np.zeros((T, 48, 64, 3), dtype=np.uint8)
            for t in range(T):
                frames[t, :, :] = (20, 30, 40)
                x = 4 + (t * 50) // T
                frames[t, 16:32, x:x + 12] = (255, 140 + e * 60, 40)
            obs = g.create_group("obs")
            obs.create_dataset("image", data=frames, chunks=(1, 48, 64, 3), compression="gzip")
            obs.create_dataset("state", data=rng.normal(size=(T, 7)).astype(np.float32))
            g.create_dataset("action", data=rng.normal(size=(T, 7)).astype(np.float32))
            g.create_dataset("reward", data=np.linspace(0, 1, T).astype(np.float32))
            g.create_dataset("done", data=np.arange(T) == T - 1)
        rec = np.zeros(200, dtype=[("id", "<i4"), ("name", "S16"), ("value", "<f8"), ("pos", "<f4", (3,))])
        rec["id"] = np.arange(200)
        rec["name"] = [f"item-{i}".encode() for i in range(200)]
        rec["value"] = rng.normal(size=200)
        rec["pos"] = rng.normal(size=(200, 3))
        f.create_dataset("table", data=rec)
        f.create_dataset("embeddings", data=rng.normal(size=(100, 128)).astype(np.float32))
        meta = f.create_group("meta")
        meta.create_dataset("names", data=[f"关节_{i}" for i in range(7)], dtype=h5py.string_dtype())
        meta.create_dataset("config", data=json.dumps({"fps": 10, "robot": "arm-7dof"}), dtype=h5py.string_dtype())
        enc = meta.create_dataset("thumbnails", (12,), dtype=h5py.vlen_dtype(np.uint8))
        for i in range(12):
            enc[i] = np.frombuffer(array_to_png(_shape_image(rng, i % 3, 48)), dtype=np.uint8)

    # ---- WebDataset --------------------------------------------------------
    wds = root / "wds"
    wds.mkdir(exist_ok=True)
    idx = 0
    for s in range(2):
        with tarfile.open(wds / f"shard-{s:06d}.tar", "w") as tar:
            for _ in range(40):
                k = int(rng.integers(0, 3))
                key = f"sample{idx:06d}"
                files = {
                    f"{key}.jpg" if idx % 2 else f"{key}.png":
                        _jpeg(_shape_image(rng, k, 128)) if idx % 2 else array_to_png(_shape_image(rng, k, 128)),
                    f"{key}.txt": f"a {SHAPES[k]} on a dark background ({idx})".encode(),
                    f"{key}.cls": str(k).encode(),
                    f"{key}.json": json.dumps({"shape": SHAPES[k], "score": round(float(rng.random()), 3), "index": idx}).encode(),
                }
                if idx % 5 == 0:
                    files[f"{key}.wav"] = _wav(220 + 110 * k)
                if idx % 4 == 0:
                    buf = io.BytesIO()
                    np.save(buf, rng.normal(size=(4, 4)).astype(np.float32))
                    files[f"{key}.npy"] = buf.getvalue()
                for name, data in files.items():
                    info = tarfile.TarInfo(name)
                    info.size = len(data)
                    info.mtime = 1_780_000_000
                    tar.addfile(info, io.BytesIO(data))
                idx += 1

    marker.write_text("ok")
    return root
