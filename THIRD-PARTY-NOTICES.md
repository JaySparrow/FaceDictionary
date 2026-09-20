# Third-party components

FaceDictionary itself is licensed under the PolyForm Noncommercial License 1.0.0 (see `LICENSE`).
It builds on the following components, each under its own licence.

## Libraries

| Component | Licence | Notes |
|---|---|---|
| [PySide6 (Qt for Python)](https://doc.qt.io/qtforpython/) | LGPL-3.0 | Used unmodified as a shared library. Anyone who receives a packaged app may relink it against another build of Qt: install the dependencies listed in `environment.yml` and rebuild with `scripts/build_app.py`. |
| [OpenCV (opencv-python-headless)](https://opencv.org/) | Apache-2.0 | Face detection, alignment and the ONNX runtime used for recognition. |
| [NumPy](https://numpy.org/) | BSD-3-Clause | |
| [cryptography (PyCA)](https://cryptography.io/) | Apache-2.0 / BSD-3-Clause | AES-256-GCM. |
| [Pillow](https://python-pillow.org/) | MIT-CMU | Image decoding. |
| [pillow-heif](https://github.com/bigcat88/pillow_heif) | BSD-3-Clause (bundles libheif, LGPL-3.0) | HEIC/HEIF decoding. |
| [PyInstaller](https://pyinstaller.org/) | GPL-2.0 with a bootloader exception | Build tool only; the exception allows shipping the packaged app under this project's own licence. |
| [dmgbuild](https://github.com/dmgbuild/dmgbuild) | MIT | Build tool only. |

## Face models

| Model | Source | Licence |
|---|---|---|
| YuNet (`face_detection_yunet_2023mar.onnx`) | [OpenCV Zoo](https://github.com/opencv/opencv_zoo) | Apache-2.0 |
| SFace (`face_recognition_sface_2021dec.onnx`) | [OpenCV Zoo](https://github.com/opencv/opencv_zoo) | Apache-2.0 |
| ArcFace MobileFaceNet (`w600k_mbf.onnx`, from `buffalo_sc`) | [InsightFace](https://github.com/deepinsight/insightface) | **Non-commercial research use only** |
| ArcFace ResNet-50 (`w600k_r50.onnx`, from `buffalo_l`, optional) | [InsightFace](https://github.com/deepinsight/insightface) | **Non-commercial research use only** |

The models are **not** part of this repository; `scripts/fetch_models.py` downloads them from their
original sources and verifies their SHA-256.

If you share a built `.dmg` with other people, `python scripts/build_app.py --open-models` bundles only
the two Apache-2.0 models, so that no third party's non-commercial weights are redistributed.

## Test data

`scripts/prepare_testdata.py` and `scripts/build_demo.py` download *Labeled Faces in the Wild* (LFW),
a public research data set from the University of Massachusetts. It is used for tests and the demo
database only, is not redistributed here, and its images remain the property of their original
copyright holders.
