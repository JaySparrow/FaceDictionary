"""M2: detection, alignment and embeddings on public LFW images."""

from __future__ import annotations

import shutil
import time

import cv2
import numpy as np
import pytest

from facedict import config, imageio
from facedict.engine import FaceEngine, ModelIntegrityError, display_crop, draw_faces, thumbnail
from tests.conftest import SAMPLES, lfw_people


@pytest.fixture(scope="module")
def engine(models_available):
    return FaceEngine("arcface_mbf")


@pytest.fixture(scope="module")
def people():
    return lfw_people(min_images=4)


def load(path) -> np.ndarray:
    return imageio.decode(path.read_bytes())


def test_detects_centered_face(engine, people):
    img = load(next(iter(people.values()))[0])
    faces = engine.detect(img)
    assert faces
    x, y, w, h = faces[0].bbox
    cx, cy = x + w / 2, y + h / 2
    assert abs(cx - img.shape[1] / 2) < 40 and abs(cy - img.shape[0] / 2) < 40   # LFW faces are centered
    assert len(faces[0].landmarks) == 5


def test_two_faces_largest_first(engine):
    img = load(SAMPLES / "two_faces.jpg")
    faces = engine.detect(img)
    assert len(faces) == 2
    assert faces[0].area > faces[1].area
    assert faces[0].bbox[0] > faces[1].bbox[0]        # the enlarged face is on the right


def test_no_face(engine):
    assert engine.detect(load(SAMPLES / "no_face.jpg")) == []


def test_heic_and_rotated_inputs(engine):
    for name in ("photo.heic", "exif_rotated.jpg"):
        assert engine.detect(load(SAMPLES / name)), name


def test_tightly_cropped_face_is_still_found(engine, people):
    img = load(next(iter(people.values()))[1])
    face = engine.detect(img)[0]
    tight = cv2.resize(display_crop(img, face, margin=0.0), (112, 112))
    assert engine.detect(tight)                       # padding retry kicks in


def test_alignment_matches_opencv_reference(engine, people):
    sface_ref = cv2.FaceRecognizerSF.create(str(config.models_dir() / config.MODELS["sface"].filename), "")
    img = load(next(iter(people.values()))[0])
    face = engine.detect(img)[0]
    ours = engine.align(img, face)
    row = np.array(face.bbox + [c for pt in face.landmarks for c in pt] + [face.score], dtype=np.float32)
    ref = sface_ref.alignCrop(img, row)
    assert ours.shape == ref.shape == (112, 112, 3)
    assert np.abs(ours.astype(int) - ref.astype(int)).mean() < 3


@pytest.mark.parametrize("model", ["arcface_mbf", "sface"])
def test_same_person_scores_higher_than_different_people(models_available, people, model):
    eng = FaceEngine(model)
    names = list(people)[:12]
    embs = {}
    for n in names:
        embs[n] = []
        for p in people[n][:3]:
            faces, e = eng.analyze(load(p))
            assert e is not None and e.shape == (eng.dim,)
            assert np.linalg.norm(e) == pytest.approx(1.0, abs=1e-4)
            embs[n].append(e)
    same = [float(a @ b) for n in names for i, a in enumerate(embs[n]) for b in embs[n][i + 1:]]
    diff = [float(embs[a][0] @ embs[b][0]) for i, a in enumerate(names) for b in names[i + 1:]]
    assert np.mean(same) > np.mean(diff) + 0.25, (np.mean(same), np.mean(diff))


def test_query_latency(engine, people):
    img = load(next(iter(people.values()))[0])
    engine.analyze(img)
    t = time.perf_counter()
    for _ in range(10):
        engine.analyze(img)
    assert (time.perf_counter() - t) / 10 < 0.05       # < 50 ms per photo


def test_tampered_model_is_refused(tmp_path):
    for spec in (config.MODELS["yunet"], config.MODELS["arcface_mbf"]):
        src = config.models_dir() / spec.filename
        if not src.exists():
            pytest.skip("models missing")
        shutil.copy(src, tmp_path / spec.filename)
    blob = bytearray((tmp_path / config.MODELS["yunet"].filename).read_bytes())
    blob[1000] ^= 0xFF
    (tmp_path / config.MODELS["yunet"].filename).write_bytes(bytes(blob))
    with pytest.raises(ModelIntegrityError):
        FaceEngine("arcface_mbf", models_path=tmp_path)


def test_display_helpers(engine, people):
    img = load(next(iter(people.values()))[0])
    face = engine.detect(img)[0]
    crop = display_crop(img, face)
    assert crop.shape[0] == crop.shape[1] > face.bbox[2]
    # a face touching the image border still gives a full square crop
    border = img[:, int(face.bbox[0] + face.bbox[2] / 2):].copy()
    bface = engine.detect(border)
    if bface:
        c2 = display_crop(border, bface[0])
        assert c2.shape[0] == c2.shape[1]
    assert thumbnail(img, face, 96).shape == (96, 96, 3)
    assert thumbnail(img, None, 64).shape == (64, 64, 3)
    assert draw_faces(img, [face]).shape == img.shape


def test_small_faces_in_a_big_group_photo_are_found(engine, people):
    """Two-scale detection: ~37 px faces in a 1920 px photo (the 640 px pass alone finds about half)."""
    from facedict.engine import display_crop
    tiles = []
    for imgs in people.values():
        img = load(imgs[0])
        tiles.append(cv2.resize(display_crop(img, engine.detect(img)[0], margin=0.3), (60, 60)))
    cols, rows, gap = 24, 12, 20
    canvas = np.full((rows * 80, cols * 80, 3), 90, np.uint8)
    for i in range(cols * rows):
        r, c = divmod(i, cols)
        canvas[r * 80 + gap // 2:r * 80 + gap // 2 + 60, c * 80 + gap // 2:c * 80 + gap // 2 + 60] = tiles[i % len(tiles)]
    faces = engine.detect(canvas)
    assert 0.97 * cols * rows <= len(faces) <= 1.03 * cols * rows
    # a close-up (face much larger than 300 px) is still found thanks to the coarse pass
    big = cv2.resize(display_crop(load(next(iter(people.values()))[0]), engine.detect(
        load(next(iter(people.values()))[0]))[0], margin=0.2), (1600, 1600))
    assert len(engine.detect(big)) == 1


def test_nms_and_reading_order():
    from facedict.engine import non_max_suppression, reading_order
    from facedict.models import Face

    def f(x, y, s=50, score=0.9):
        return Face([x, y, s, s], [[0, 0]] * 5, score)

    dup = [f(100, 100, score=0.95), f(104, 102, score=0.9), f(300, 100)]
    kept = non_max_suppression(dup)
    assert len(kept) == 2 and kept[0].score == 0.95
    # two rows, given in scrambled order -> top row left to right, then bottom row
    grid = [f(300, 210), f(100, 20), f(200, 25), f(100, 200), f(300, 15), f(200, 205)]
    ordered = [grid[i].bbox[:2] for i in reading_order(grid)]
    assert ordered == [[100, 20], [200, 25], [300, 15], [100, 200], [200, 205], [300, 210]]
    assert reading_order([]) == []
