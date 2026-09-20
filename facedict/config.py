"""Static configuration: paths, model registry, tunables."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def bundled() -> bool:
    """True when running from the packaged FaceDictionary.app."""
    return bool(getattr(sys, "frozen", False))


def resource_dir() -> Path:
    """Where the app's own read-only files (the models) live."""
    if bundled():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return PROJECT_ROOT

# The app's state folder and the default database inside it (not synced by iCloud, not indexed by Spotlight).
DEFAULT_STATE_DIR = Path.home() / "Library" / "Application Support" / "FaceDictionary"
DEFAULT_VAULT_DIR = DEFAULT_STATE_DIR / "vault.noindex"

AUTO_LOCK_SECONDS = 5 * 60
DETECT_MAX_SIDE = 640          # YuNet works best on faces of ~10-300 px ...
DETECT_MAX_SIDE_LARGE = 1280   # ... so big photos get a second, finer pass for small faces (group photos)
DETECT_SCORE_THRESHOLD = 0.8
DETECT_RETRY_THRESHOLD = 0.5
DISPLAY_CROP_MARGIN = 0.25
THUMB_SIZE = 192
TOP_K = 5


def state_dir() -> Path:
    """Where the app keeps its own small settings (the list of databases). FACEDICT_STATE overrides it."""
    override = os.environ.get("FACEDICT_STATE")
    return Path(override).expanduser().resolve() if override else DEFAULT_STATE_DIR


def vault_dir() -> Path:
    """Database to use when none is chosen; FACEDICT_VAULT overrides it (tests and the demo)."""
    override = os.environ.get("FACEDICT_VAULT")
    if override:
        return Path(override).expanduser().resolve()
    from .databases import last_used
    return last_used() or DEFAULT_VAULT_DIR


def models_dir() -> Path:
    override = os.environ.get("FACEDICT_MODELS")
    return Path(override).expanduser().resolve() if override else resource_dir() / "models"


@dataclass(frozen=True)
class ModelSpec:
    name: str
    filename: str
    sha256: str
    url: str
    zip_member: str | None = None      # file inside a zip archive at `url`
    zip_sha256: str | None = None
    kind: str = "recognizer"           # "detector" | "recognizer"
    dim: int = 0
    low_confidence: float = 0.3        # cosine score below which a match is flagged
    label: str = ""
    optional: bool = False


MODELS: dict[str, ModelSpec] = {
    "yunet": ModelSpec(
        name="yunet",
        filename="face_detection_yunet_2023mar.onnx",
        sha256="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        url="https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        kind="detector",
        label="YuNet (OpenCV Zoo)",
    ),
    "arcface_mbf": ModelSpec(
        name="arcface_mbf",
        filename="w600k_mbf.onnx",
        sha256="9cc6e4a75f0e2bf0b1aed94578f144d15175f357bdc05e815e5c4a02b319eb4f",
        url="https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_sc.zip",
        zip_member="w600k_mbf.onnx",
        zip_sha256="57d31b56b6ffa911c8a73cfc1707c73cab76efe7f13b675a05223bf42de47c72",
        dim=512,
        low_confidence=0.3,
        label="ArcFace MobileFaceNet (InsightFace buffalo_sc)",
    ),
    "sface": ModelSpec(
        name="sface",
        filename="face_recognition_sface_2021dec.onnx",
        sha256="0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        url="https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        dim=128,
        low_confidence=0.363,
        label="SFace (OpenCV Zoo)",
    ),
    "arcface_r50": ModelSpec(
        name="arcface_r50",
        filename="w600k_r50.onnx",
        sha256="4c06341c33c2ca1f86781dab0e829f88ad5b64be9fba56e56bc9ebdefc619e43",
        url="https://github.com/deepinsight/insightface/releases/download/v0.7/buffalo_l.zip",
        zip_member="w600k_r50.onnx",
        zip_sha256="80ffe37d8a5940d59a7384c201a2a38d4741f2f3c51eef46ebb28218a7b0ca2f",
        dim=512,
        low_confidence=0.3,
        label="ArcFace ResNet-50 (InsightFace buffalo_l)",
        optional=True,
    ),
}

DEFAULT_RECOGNIZER = "arcface_mbf"


def recognizer_names() -> list[str]:
    return [m.name for m in MODELS.values() if m.kind == "recognizer"]
