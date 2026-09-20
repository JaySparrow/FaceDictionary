"""In-memory image decoding/encoding. Decrypted images never touch the disk."""

from __future__ import annotations

import io
from pathlib import Path

import cv2
import numpy as np
import pillow_heif
from PIL import Image, ImageOps

pillow_heif.register_heif_opener()
Image.MAX_IMAGE_PIXELS = 150_000_000            # guard against decompression bombs

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp", ".bmp", ".tif", ".tiff"}


class ImageDecodeError(ValueError):
    pass


def is_supported(path: Path | str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def decode(data: bytes) -> np.ndarray:
    """Decode JPEG/PNG/HEIC/... bytes into an upright BGR uint8 array."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "PA") or (im.mode == "P" and "transparency" in im.info):
                im = im.convert("RGBA")
                background = Image.new("RGB", im.size, (255, 255, 255))
                background.paste(im, mask=im.getchannel("A"))
                im = background
            elif im.mode != "RGB":
                im = im.convert("RGB")
            rgb = np.asarray(im)
    except Exception as exc:
        raise ImageDecodeError(f"Unsupported or corrupt image: {exc}") from None
    return np.ascontiguousarray(rgb[:, :, ::-1])


def encode_jpeg(bgr: np.ndarray, quality: int = 88) -> bytes:
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, quality])
    if not ok:
        raise ValueError("JPEG encoding failed")
    return buf.tobytes()


def encode_png(bgr: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        raise ValueError("PNG encoding failed")
    return buf.tobytes()


def resize_max_side(bgr: np.ndarray, max_side: int) -> tuple[np.ndarray, float]:
    h, w = bgr.shape[:2]
    scale = min(1.0, max_side / max(h, w))
    if scale >= 1.0:
        return bgr, 1.0
    return cv2.resize(bgr, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA), scale
