"""Face detection (YuNet), 5-point alignment and embedding (ArcFace / SFace), all on CPU via OpenCV."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path

import cv2
import numpy as np

from . import config
from .imageio import resize_max_side
from .models import Face

# Standard 112x112 ArcFace template (image-left eye, image-right eye, nose, mouth corners);
# YuNet reports landmarks in the same order.
ARCFACE_TEMPLATE = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                             [41.5493, 92.3655], [70.7299, 92.2041]], dtype=np.float64)
ALIGNED_SIZE = 112

_verified: set[tuple[str, str]] = set()


class ModelIntegrityError(RuntimeError):
    pass


def _check_model(path: Path, sha256: str) -> None:
    key = (str(path), sha256)
    if key in _verified:
        return
    if not path.is_file():
        raise ModelIntegrityError(f"Model file missing: {path.name}. Run scripts/fetch_models.py")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if h.hexdigest() != sha256:
        raise ModelIntegrityError(f"Model file failed its integrity check: {path.name}")
    _verified.add(key)


def similarity_transform(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """Least-squares similarity transform (Umeyama, no reflection) mapping src points onto dst. Returns 2x3."""
    src = np.asarray(src, dtype=np.float64)
    dst = np.asarray(dst, dtype=np.float64)
    src_mean, dst_mean = src.mean(axis=0), dst.mean(axis=0)
    src_c, dst_c = src - src_mean, dst - dst_mean
    cov = dst_c.T @ src_c / len(src)
    u, s, vt = np.linalg.svd(cov)
    d = np.ones(2)
    if np.linalg.det(u) * np.linalg.det(vt) < 0:
        d[-1] = -1
    rot = u @ np.diag(d) @ vt
    scale = (s * d).sum() / ((src_c ** 2).sum() / len(src))
    t = dst_mean - scale * rot @ src_mean
    return np.hstack([scale * rot, t[:, None]])


def _normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(v))
    return v / n if n > 0 else v


class _ArcFace:
    def __init__(self, path: Path):
        self.net = cv2.dnn.readNet(str(path))

    def embed(self, aligned: np.ndarray) -> np.ndarray:
        blob = cv2.dnn.blobFromImage(aligned, 1.0 / 127.5, (ALIGNED_SIZE, ALIGNED_SIZE),
                                     (127.5, 127.5, 127.5), swapRB=True)
        self.net.setInput(blob)
        return self.net.forward()


class _SFace:
    def __init__(self, path: Path):
        self.rec = cv2.FaceRecognizerSF.create(str(path), "")

    def embed(self, aligned: np.ndarray) -> np.ndarray:
        return self.rec.feature(aligned)


class FaceEngine:
    """Thread-safe wrapper around the detector and one recognizer."""

    def __init__(self, recognizer: str = config.DEFAULT_RECOGNIZER, models_path: Path | None = None):
        cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_ERROR)
        root = Path(models_path) if models_path else config.models_dir()
        det_spec = config.MODELS["yunet"]
        rec_spec = config.MODELS[recognizer]
        if rec_spec.kind != "recognizer":
            raise ValueError(f"{recognizer} is not a recognizer")
        for spec in (det_spec, rec_spec):
            _check_model(root / spec.filename, spec.sha256)
        self.spec = rec_spec
        self._lock = threading.Lock()
        self._detector = cv2.FaceDetectorYN.create(str(root / det_spec.filename), "", (320, 320),
                                                   config.DETECT_SCORE_THRESHOLD, 0.3, 5000)
        path = root / rec_spec.filename
        self._recognizer = _SFace(path) if recognizer == "sface" else _ArcFace(path)

    @property
    def model_name(self) -> str:
        return self.spec.name

    @property
    def dim(self) -> int:
        return self.spec.dim

    # --- detection -------------------------------------------------------------------

    def detect(self, bgr: np.ndarray) -> list[Face]:
        """All faces, largest first.

        Large photos are scanned at two resolutions (big faces at the coarse one, small faces in
        group photos at the fine one) and the results merged. If nothing is found, retries with a
        lower threshold and then with padding (tightly cropped faces need some context)."""
        sides = [config.DETECT_MAX_SIDE]
        if max(bgr.shape[:2]) > config.DETECT_MAX_SIDE * 1.25:
            sides.append(config.DETECT_MAX_SIDE_LARGE)
        faces = []
        for side in sides:
            faces += self._detect_once(bgr, config.DETECT_SCORE_THRESHOLD, 0.0, side)
        faces = non_max_suppression(faces)
        if faces:
            return sorted(faces, key=lambda f: f.area, reverse=True)
        for threshold, pad in ((config.DETECT_RETRY_THRESHOLD, 0.0), (config.DETECT_RETRY_THRESHOLD, 0.35)):
            faces = self._detect_once(bgr, threshold, pad, config.DETECT_MAX_SIDE)
            if faces:
                return faces
        return []

    def _detect_once(self, bgr: np.ndarray, threshold: float, pad_ratio: float, max_side: int) -> list[Face]:
        offset = 0
        img = bgr
        if pad_ratio:
            offset = int(round(max(bgr.shape[:2]) * pad_ratio))
            img = cv2.copyMakeBorder(bgr, offset, offset, offset, offset, cv2.BORDER_CONSTANT,
                                     value=(127, 127, 127))
        small, scale = resize_max_side(img, max_side)
        with self._lock:
            self._detector.setInputSize((small.shape[1], small.shape[0]))
            self._detector.setScoreThreshold(threshold)
            _, rows = self._detector.detect(small)
        if rows is None:
            return []
        faces = []
        for row in rows:
            vals = row[:14].astype(np.float64) / scale - offset
            x, y = vals[0], vals[1]
            w, h = row[2] / scale, row[3] / scale
            faces.append(Face(bbox=[float(x), float(y), float(w), float(h)],
                              landmarks=vals[4:14].reshape(5, 2).tolist(), score=float(row[14])))
        return sorted(faces, key=lambda f: f.area, reverse=True)

    # --- recognition ------------------------------------------------------------------

    @staticmethod
    def align(bgr: np.ndarray, face: Face) -> np.ndarray:
        m = similarity_transform(np.array(face.landmarks), ARCFACE_TEMPLATE)
        return cv2.warpAffine(bgr, m, (ALIGNED_SIZE, ALIGNED_SIZE), flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_CONSTANT, borderValue=(0, 0, 0))

    def embed(self, bgr: np.ndarray, face: Face) -> np.ndarray:
        return self.embed_aligned(self.align(bgr, face))

    def embed_aligned(self, aligned: np.ndarray) -> np.ndarray:
        with self._lock:
            return _normalize(self._recognizer.embed(aligned))

    def analyze(self, bgr: np.ndarray) -> tuple[list[Face], np.ndarray | None]:
        faces = self.detect(bgr)
        return faces, (self.embed(bgr, faces[0]) if faces else None)


def iou(a: Face, b: Face) -> float:
    ax, ay, aw, ah = a.bbox
    bx, by, bw, bh = b.bbox
    iw = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    ih = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = iw * ih
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def non_max_suppression(faces: list[Face], threshold: float = 0.4) -> list[Face]:
    """Drop duplicate detections of the same face (keeps the most confident one)."""
    kept: list[Face] = []
    for f in sorted(faces, key=lambda f: f.score, reverse=True):
        if all(iou(f, k) < threshold for k in kept):
            kept.append(f)
    return kept


def reading_order(faces: list[Face]) -> list[int]:
    """Indices of `faces` sorted top-to-bottom in rows, then left-to-right (how people number a group photo)."""
    if not faces:
        return []
    band = max(1.0, float(np.median([f.bbox[3] for f in faces])))
    centers = [(f.bbox[0] + f.bbox[2] / 2, f.bbox[1] + f.bbox[3] / 2) for f in faces]
    by_y = sorted(range(len(faces)), key=lambda i: centers[i][1])
    rows: list[list[int]] = []
    for i in by_y:                                   # start a new row when a face sits clearly lower
        if rows and centers[i][1] - np.mean([centers[j][1] for j in rows[-1]]) < band * 0.6:
            rows[-1].append(i)
        else:
            rows.append([i])
    return [i for row in rows for i in sorted(row, key=lambda j: centers[j][0])]


# --- display helpers -------------------------------------------------------------------

def display_crop(bgr: np.ndarray, face: Face | None, margin: float = config.DISPLAY_CROP_MARGIN) -> np.ndarray:
    """Square crop around the face with a margin, padded with edge pixels if it leaves the image.
    Without a face, a centered square crop of the whole image."""
    h, w = bgr.shape[:2]
    if face is None:
        side = min(h, w)
        cx, cy = w / 2, h / 2
    else:
        x, y, fw, fh = face.bbox
        side = max(fw, fh) * (1 + 2 * margin)
        cx, cy = x + fw / 2, y + fh / 2
    x0, y0 = int(round(cx - side / 2)), int(round(cy - side / 2))
    s = max(1, int(round(side)))
    pad_l, pad_t = max(0, -x0), max(0, -y0)
    pad_r, pad_b = max(0, x0 + s - w), max(0, y0 + s - h)
    if any((pad_l, pad_t, pad_r, pad_b)):
        bgr = cv2.copyMakeBorder(bgr, pad_t, pad_b, pad_l, pad_r, cv2.BORDER_REPLICATE)
        x0, y0 = x0 + pad_l, y0 + pad_t
    return bgr[y0:y0 + s, x0:x0 + s].copy()


def thumbnail(bgr: np.ndarray, face: Face | None, size: int = config.THUMB_SIZE) -> np.ndarray:
    crop = display_crop(bgr, face)
    interp = cv2.INTER_AREA if crop.shape[0] > size else cv2.INTER_CUBIC
    return cv2.resize(crop, (size, size), interpolation=interp)


def draw_faces(bgr: np.ndarray, faces: list[Face], primary: int = 0) -> np.ndarray:
    out = bgr.copy()
    thickness = max(2, round(max(out.shape[:2]) / 300))
    for i, face in enumerate(faces):
        x, y, w, h = (int(round(v)) for v in face.bbox)
        color = (80, 200, 80) if i == primary else (160, 160, 160)
        cv2.rectangle(out, (x, y), (x + w, y + h), color, thickness)
        if i == primary:
            for lx, ly in face.landmarks:
                cv2.circle(out, (int(round(lx)), int(round(ly))), thickness + 1, (60, 170, 255), -1)
    return out
