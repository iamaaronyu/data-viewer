"""Media detection (magic numbers), ndarray -> PNG and thumbnails."""
from __future__ import annotations

import io
import os
import struct
import zlib

import numpy as np

# Images that every mainstream browser can draw directly.
IMAGE_BROWSER = {
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
    "image/avif", "image/svg+xml", "image/x-icon",
}
# Images Pillow can convert to something displayable.
IMAGE_CONVERTIBLE = {"image/tiff"}
AUDIO_BROWSER = {"audio/wav", "audio/mpeg", "audio/ogg", "audio/flac", "audio/aac", "audio/mp4", "audio/webm"}
VIDEO_BROWSER = {"video/mp4", "video/webm", "video/quicktime", "video/ogg", "video/x-matroska"}

EXT_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
    ".webp": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".avif": "image/avif", ".svg": "image/svg+xml", ".ico": "image/x-icon",
    ".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime",
    ".mkv": "video/x-matroska", ".ogv": "video/ogg", ".avi": "video/x-msvideo",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg",
    ".oga": "audio/ogg", ".opus": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac",
    ".pdf": "application/pdf", ".npy": "application/x-npy",
    ".txt": "text/plain", ".md": "text/plain", ".log": "text/plain", ".yaml": "text/plain",
    ".yml": "text/plain", ".py": "text/plain", ".sh": "text/plain", ".toml": "text/plain",
    ".ini": "text/plain", ".cfg": "text/plain", ".xml": "text/plain", ".html": "text/plain",
}


def ext_mime(name: str) -> str | None:
    lower = name.lower()
    return EXT_MIME.get(os.path.splitext(lower)[1])


def category(mime: str | None) -> str | None:
    if not mime:
        return None
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    if mime == "application/pdf":
        return "pdf"
    if mime == "application/x-npy":
        return "npy"
    if mime.startswith("text/"):
        return "text"
    return None


def sniff_mime(b: bytes | None) -> str | None:
    """Detect common media types from leading bytes."""
    if not b or len(b) < 4:
        return None
    h = bytes(b[:64])
    if h.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if h.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if h[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if h[:4] == b"RIFF" and len(h) >= 12:
        kind = h[8:12]
        if kind == b"WEBP":
            return "image/webp"
        if kind == b"WAVE":
            return "audio/wav"
        if kind == b"AVI ":
            return "video/x-msvideo"
    if h[:4] in (b"II*\x00", b"MM\x00*"):
        return "image/tiff"
    if len(h) >= 12 and h[4:8] == b"ftyp":
        brand = h[8:12]
        if brand in (b"avif", b"avis"):
            return "image/avif"
        if brand in (b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1"):
            return "image/heic"
        if brand in (b"M4A ", b"M4B ", b"M4P "):
            return "audio/mp4"
        if brand == b"qt  ":
            return "video/quicktime"
        return "video/mp4"
    if h[:4] == b"\x1a\x45\xdf\xa3":
        return "video/webm" if b"webm" in bytes(b[:256]) else "video/x-matroska"
    if h[:4] == b"OggS":
        return "video/ogg" if (b"theora" in bytes(b[:256]) or b"\x80theora" in bytes(b[:256])) else "audio/ogg"
    if h[:4] == b"fLaC":
        return "audio/flac"
    if h[:3] == b"ID3":
        return "audio/mpeg"
    if h[0] == 0xFF and len(h) >= 3:
        b1, b2 = h[1], h[2]
        if (b1 & 0xF6) == 0xF0:  # ADTS AAC: 12-bit sync, layer 00
            return "audio/aac"
        # MPEG audio frame: 11-bit sync, valid version, layer, bitrate and sample rate.
        if (b1 & 0xE0) == 0xE0 and (b1 & 0x18) != 0x08 and (b1 & 0x06) != 0 \
                and (b2 & 0xF0) not in (0x00, 0xF0) and (b2 & 0x0C) != 0x0C:
            return "audio/mpeg"
    if h[:2] == b"BM" and len(h) >= 14 and h[6:10] == b"\x00\x00\x00\x00":
        return "image/bmp"
    if h[:4] == b"%PDF":
        return "application/pdf"
    if h[:6] == b"\x93NUMPY":
        return "application/x-npy"
    if h[:4] == b"\x00\x00\x01\x00" and len(h) >= 6 and h[4] > 0:
        return "image/x-icon"
    stripped = h.lstrip()
    if stripped.startswith(b"<svg") or (stripped.startswith(b"<?xml") and b"<svg" in bytes(b[:512])):
        return "image/svg+xml"
    return None


def looks_like_text(b: bytes) -> str | None:
    """Return a decoded prefix if the bytes look like UTF-8 text, else None."""
    if not b:
        return ""
    head = bytes(b[:4096])
    if b"\x00" in head:
        return None
    for cut in range(0, 4):
        try:
            text = head[: len(head) - cut].decode("utf-8")
            break
        except UnicodeDecodeError:
            continue
    else:
        return None
    ctrl = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\n\r\f")
    if text and ctrl / len(text) > 0.05:
        return None
    return text


# ---------------------------------------------------------------------------
# ndarray helpers
# ---------------------------------------------------------------------------

def to_hwc(a: np.ndarray) -> np.ndarray | None:
    """Return an image-shaped view (H, W) or (H, W, C) or None if not image-like."""
    if a.ndim == 2:
        h, w = a.shape
        return a if min(h, w) >= 8 else None
    if a.ndim == 3:
        h, w, c = a.shape
        if c in (1, 3, 4) and min(h, w) >= 8:
            return a[:, :, 0] if c == 1 else a
        if a.shape[0] in (1, 3, 4) and min(a.shape[1], a.shape[2]) >= 8:  # CHW -> HWC
            t = np.transpose(a, (1, 2, 0))
            return t[:, :, 0] if t.shape[2] == 1 else t
    return None


def image_like(shape) -> bool:
    shape = tuple(shape)
    if len(shape) == 2:
        return min(shape) >= 8
    if len(shape) == 3:
        return (shape[2] in (1, 3, 4) and min(shape[:2]) >= 8) or (shape[0] in (1, 3, 4) and min(shape[1:]) >= 8)
    return False


def _to_uint8(a: np.ndarray) -> np.ndarray:
    if a.dtype == np.uint8:
        return a
    if a.dtype == np.bool_:
        return a.astype(np.uint8) * 255
    f = a.astype(np.float64)
    finite = np.isfinite(f)
    if not finite.any():
        return np.zeros(a.shape, dtype=np.uint8)
    lo = float(f[finite].min())
    hi = float(f[finite].max())
    if a.dtype.kind == "f" and lo >= 0.0 and hi <= 1.0:
        lo, hi = 0.0, 1.0
    elif a.dtype.kind in "iu" and lo >= 0 and hi <= 255:
        lo, hi = 0.0, 255.0
    span = hi - lo if hi > lo else 1.0
    out = np.where(finite, (f - lo) / span * 255.0, 0.0)
    return np.clip(out, 0, 255).astype(np.uint8)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def array_to_png(a: np.ndarray, max_side: int | None = None) -> bytes | None:
    """Encode an image-like ndarray as PNG (no Pillow needed). Returns None if not image-like."""
    img = to_hwc(np.asarray(a))
    if img is None:
        return None
    if max_side:
        step = int(np.ceil(max(img.shape[0], img.shape[1]) / max_side))
        if step > 1:
            img = img[::step, ::step]
    img = _to_uint8(np.ascontiguousarray(img))
    h, w = img.shape[:2]
    if img.ndim == 2:
        color = 0
    elif img.shape[2] == 3:
        color = 2
    else:
        color = 6
    rows = img.reshape(h, -1)
    raw = np.concatenate([np.zeros((h, 1), dtype=np.uint8), rows], axis=1).tobytes()
    ihdr = struct.pack(">IIBBBBB", w, h, 8, color, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", zlib.compress(raw, 6)) + _png_chunk(b"IEND", b"")


def load_npy(b: bytes, max_bytes: int = 64 * 1024 * 1024) -> np.ndarray | None:
    if len(b) > max_bytes:
        return None
    try:
        return np.load(io.BytesIO(b), allow_pickle=False)
    except Exception:
        return None


def thumbnail(data: bytes, side: int) -> tuple[bytes, str] | None:
    """Downscale an encoded image with Pillow. Returns (bytes, mime) or None."""
    try:
        from PIL import Image
    except Exception:  # pragma: no cover - pillow is a dependency, but stay robust
        return None
    try:
        with Image.open(io.BytesIO(data)) as im:
            im.draft("RGB", (side, side))  # fast JPEG downscale
            im.thumbnail((side, side))
            if im.mode not in ("RGB", "RGBA", "L", "LA"):
                im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            out = io.BytesIO()
            im.save(out, format="WEBP", quality=82)
            return out.getvalue(), "image/webp"
    except Exception:
        return None


def to_browser_image(data: bytes) -> tuple[bytes, str] | None:
    """Convert an image the browser cannot draw (e.g. TIFF) to PNG."""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            if im.mode not in ("RGB", "RGBA", "L", "LA"):
                im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            out = io.BytesIO()
            im.save(out, format="PNG")
            return out.getvalue(), "image/png"
    except Exception:
        return None
