"""Shared fixtures. Every test runs against a throw-away vault in a temp directory."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from facedict import config  # noqa: E402

TESTDATA = ROOT / "testdata"
LFW_SUBSET = TESTDATA / "lfw_subset"
SAMPLES = TESTDATA / "samples"


@pytest.fixture(autouse=True)
def isolated_vault(tmp_path, monkeypatch) -> Path:
    vault = tmp_path / "vault.noindex"
    monkeypatch.setenv("FACEDICT_VAULT", str(vault))
    monkeypatch.setenv("FACEDICT_STATE", str(tmp_path / "state"))   # never touch the real database list
    assert config.vault_dir() == vault.resolve()
    assert config.vault_dir() != config.DEFAULT_VAULT_DIR
    return vault


def pytest_sessionstart(session):
    # Belt and braces: refuse to run if anything points the tests at the real vault.
    if os.environ.get("FACEDICT_VAULT") and Path(os.environ["FACEDICT_VAULT"]).expanduser().resolve() == config.DEFAULT_VAULT_DIR:
        raise pytest.UsageError("Tests must never run against the real vault")


def lfw_people(min_images: int = 2) -> dict[str, list[Path]]:
    """name -> sorted image paths from the LFW test subset (see scripts/prepare_testdata.py)."""
    if not LFW_SUBSET.exists():
        pytest.skip("LFW subset missing: run scripts/prepare_testdata.py")
    people = {}
    for d in sorted(p for p in LFW_SUBSET.iterdir() if p.is_dir()):
        imgs = sorted(d.glob("*.jpg"))
        if len(imgs) >= min_images:
            people[d.name] = imgs
    return people


@pytest.fixture(scope="session")
def models_available() -> None:
    missing = [s.filename for s in config.MODELS.values()
               if not s.optional and not (config.models_dir() / s.filename).exists()]
    if missing:
        pytest.skip(f"models missing ({missing}): run scripts/fetch_models.py")


def _tile(eng, path: Path):
    from facedict.engine import display_crop
    from facedict.imageio import decode
    img = decode(path.read_bytes())
    return display_crop(img, eng.detect(img)[0], margin=0.35)


def single_face_photos(people: dict[str, list[Path]], n: int, which: int = -1) -> list[str]:
    """Names of the first `n` people whose photo `which` shows no one else once cropped around the face
    (LFW photos often have bystanders at the edges)."""
    from facedict.engine import FaceEngine
    eng = FaceEngine()
    chosen = [name for name, imgs in people.items() if len(eng.detect(_tile(eng, imgs[which]))) == 1][:n]
    assert len(chosen) == n, "not enough single-face photos"
    return chosen


def group_photo(paths: list[Path], sizes: list[int], gap: int = 40) -> bytes:
    """A synthetic group photo: one face crop per path, left to right, each `sizes[i]` px tall.
    Crops are tight around each photo's main face, so the only faces are the ones we put there."""
    import cv2
    import numpy as np

    from facedict.engine import FaceEngine
    from facedict.imageio import encode_jpeg

    eng = FaceEngine()
    tiles = [cv2.resize(_tile(eng, p), (size, size), interpolation=cv2.INTER_AREA) for p, size in zip(paths, sizes)]
    h = max(sizes) + 2 * gap
    w = sum(sizes) + gap * (len(sizes) + 1)
    canvas = np.full((h, w, 3), 110, np.uint8)
    x = gap
    for t in tiles:
        y = (h - t.shape[0]) // 2
        canvas[y:y + t.shape[0], x:x + t.shape[1]] = t
        x += t.shape[1] + gap
    return encode_jpeg(canvas, 92)
