"""M2: in-memory decoding of JPEG / PNG / HEIC with EXIF orientation."""

from __future__ import annotations

import numpy as np
import pytest

from facedict import imageio
from tests.conftest import LFW_SUBSET, SAMPLES


def source_image() -> bytes:
    if not (SAMPLES / "photo_source.txt").exists():
        pytest.skip("samples missing: run scripts/prepare_testdata.py")
    rel = (SAMPLES / "photo_source.txt").read_text().strip()
    return (LFW_SUBSET / rel).read_bytes()


def mad(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.abs(a.astype(np.int16) - b.astype(np.int16)).mean())


def test_decode_jpeg_to_bgr():
    img = imageio.decode(source_image())
    assert img.dtype == np.uint8 and img.ndim == 3 and img.shape[2] == 3
    assert img.flags["C_CONTIGUOUS"]


def test_decode_heic_matches_source():
    ref = imageio.decode(source_image())
    heic = imageio.decode((SAMPLES / "photo.heic").read_bytes())
    assert heic.shape == ref.shape
    assert mad(heic, ref) < 8                      # lossy re-encode, same content


def test_exif_orientation_is_applied():
    ref = imageio.decode(source_image())
    rotated = imageio.decode((SAMPLES / "exif_rotated.jpg").read_bytes())
    assert rotated.shape == ref.shape
    assert mad(rotated, ref) < 8
    assert mad(np.rot90(rotated), ref) > 20        # sanity: orientation matters for this image


def test_png_with_alpha_becomes_bgr():
    img = imageio.decode((SAMPLES / "alpha.png").read_bytes())
    assert img.shape[2] == 3


def test_garbage_is_rejected():
    with pytest.raises(imageio.ImageDecodeError):
        imageio.decode(b"not an image at all")
    with pytest.raises(imageio.ImageDecodeError):
        imageio.decode(source_image()[:200])       # truncated


def test_encode_roundtrip_and_resize():
    img = imageio.decode(source_image())
    assert mad(imageio.decode(imageio.encode_jpeg(img, 95)), img) < 4
    assert np.array_equal(imageio.decode(imageio.encode_png(img)), img)
    small, scale = imageio.resize_max_side(img, 100)
    assert max(small.shape[:2]) == 100 and scale == pytest.approx(100 / max(img.shape[:2]))
    same, s1 = imageio.resize_max_side(img, 10_000)
    assert same is img and s1 == 1.0


def test_supported_extensions():
    assert imageio.is_supported("a/B.HEIC") and imageio.is_supported("x.jpeg")
    assert not imageio.is_supported("notes.txt")
