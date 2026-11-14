"""Build FaceDictionary.app and a .dmg installer.

    conda activate facedict-build
    python scripts/build_app.py                 # app + dmg, with every installed model
    python scripts/build_app.py --open-models   # only the Apache-2.0 models (for sharing the dmg)
    python scripts/build_app.py --app-only

The app is self-contained: it carries Python, Qt, OpenCV and the face models, so a machine that
runs it needs nothing installed. Your databases stay in ~/Library/Application Support/FaceDictionary.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from facedict import __version__ as VERSION  # noqa: E402
from facedict.config import MODELS  # noqa: E402
OPEN_LICENCE_MODELS = ("yunet", "sface")          # Apache-2.0; the ArcFace weights are non-commercial


def run(command: list[str], **kwargs) -> None:
    print("  $", " ".join(str(c) for c in command))
    subprocess.run(command, check=True, **kwargs)


def folder_size(path: Path) -> str:
    total = sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())
    return f"{total / 1e6:.0f} MB"


def pick_models(open_only: bool, with_optional: bool) -> list[str]:
    names = []
    for spec in MODELS.values():
        if open_only and spec.name not in OPEN_LICENCE_MODELS:
            continue
        if spec.optional and not with_optional:          # the big ResNet-50 stays out by default
            continue
        path = ROOT / "models" / spec.filename
        if path.exists():
            names.append(spec.filename)
        elif not spec.optional:
            sys.exit(f"Missing {path}. Run scripts/fetch_models.py first.")
    return names


def build_app(models: list[str]) -> Path:
    print("Building FaceDictionary.app …")
    run([sys.executable, str(ROOT / "packaging" / "make_icon.py")])
    env = dict(os.environ, FACEDICT_VERSION=VERSION, FACEDICT_BUNDLE_MODELS=os.pathsep.join(models))
    for folder in (ROOT / "build", ROOT / "dist"):
        shutil.rmtree(folder, ignore_errors=True)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--distpath", str(ROOT / "dist"),
         "--workpath", str(ROOT / "build"), str(ROOT / "packaging" / "FaceDictionary.spec")], env=env)
    app = ROOT / "dist" / "FaceDictionary.app"
    if not app.exists():
        sys.exit("PyInstaller did not produce the app bundle")
    run(["codesign", "--force", "--deep", "--sign", "-", str(app)])          # ad-hoc: required on Apple silicon
    run(["codesign", "--verify", "--deep", "--strict", str(app)])
    print(f"  app: {app}  ({folder_size(app)})")
    return app


def build_dmg(app: Path) -> Path:
    print("Building the disk image …")
    dmg = ROOT / "dist" / f"FaceDictionary-{VERSION}.dmg"
    dmg.unlink(missing_ok=True)
    env = dict(os.environ, FACEDICT_APP=str(app), FACEDICT_GUIDE=str(ROOT / "docs" / "get-started.md"))
    run([sys.executable, "-m", "dmgbuild", "-s", str(ROOT / "packaging" / "dmg_settings.py"),
         "FaceDictionary", str(dmg)], env=env)
    print(f"  dmg: {dmg}  ({dmg.stat().st_size / 1e6:.0f} MB)")
    return dmg


def smoke_test(app: Path) -> None:
    """The bundled binary also runs the command line, so the build can check itself."""
    print("Checking the bundle …")
    binary = app / "Contents" / "MacOS" / "FaceDictionary"
    vault = Path(os.environ.get("TMPDIR", "/tmp")) / f"facedict-build-check-{int(time.time())}.noindex"
    result = subprocess.run([str(binary), "--vault", str(vault), "verify", "--password-stdin"],
                            input="x\n", capture_output=True, text=True, timeout=180)
    shutil.rmtree(vault, ignore_errors=True)
    if "No vault at" not in (result.stdout + result.stderr):
        sys.exit(f"The bundled app did not start properly:\n{result.stdout}\n{result.stderr}")
    print("  the bundled app runs and finds no database at a fresh path, as expected")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app-only", action="store_true", help="skip the disk image")
    parser.add_argument("--open-models", action="store_true",
                        help="bundle only the Apache-2.0 models (YuNet + SFace)")
    parser.add_argument("--with-r50", action="store_true",
                        help="also bundle the optional 174 MB ArcFace ResNet-50 model")
    args = parser.parse_args()

    models = pick_models(args.open_models, args.with_r50)
    print(f"Models in the bundle: {', '.join(models)}")
    app = build_app(models)
    smoke_test(app)
    if not args.app_only:
        build_dmg(app)
    shutil.rmtree(ROOT / "build", ignore_errors=True)                 # intermediates: the app has it all
    shutil.rmtree(ROOT / "dist" / "FaceDictionary", ignore_errors=True)
    print("Done. Drag the app from the disk image into Applications.")


if __name__ == "__main__":
    main()
