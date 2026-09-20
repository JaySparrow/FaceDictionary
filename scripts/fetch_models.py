"""Download and verify the ONNX models.

This is the only part of FaceDictionary that uses the network. It never touches
the vault or any personal data.

Usage:
    python scripts/fetch_models.py            # detector + ArcFace-MBF + SFace (~53 MB)
    python scripts/fetch_models.py --all      # also the optional ArcFace-R50 (~290 MB download)
    python scripts/fetch_models.py --verify   # only verify files already present
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from facedict.config import MODELS, ModelSpec, models_dir  # noqa: E402


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str) -> bytes:
    print(f"  downloading {url}")
    with urllib.request.urlopen(url, timeout=120) as resp:
        return resp.read()


def fetch(spec: ModelSpec, dest_dir: Path) -> None:
    dest = dest_dir / spec.filename
    if dest.exists() and sha256_file(dest) == spec.sha256:
        print(f"[ok]   {spec.name}: {dest.name} already present")
        return
    data = download(spec.url)
    if spec.zip_member:
        if spec.zip_sha256 and sha256_bytes(data) != spec.zip_sha256:
            raise SystemExit(f"[fail] {spec.name}: archive checksum mismatch")
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            member = next(n for n in zf.namelist() if n.endswith(spec.zip_member))
            data = zf.read(member)
    if sha256_bytes(data) != spec.sha256:
        raise SystemExit(f"[fail] {spec.name}: checksum mismatch, file discarded")
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    print(f"[ok]   {spec.name}: saved {dest.name} ({len(data) / 1e6:.1f} MB)")


def verify(dest_dir: Path) -> bool:
    ok = True
    for spec in MODELS.values():
        path = dest_dir / spec.filename
        if not path.exists():
            print(f"[miss] {spec.name}: {path.name}")
            ok = ok and spec.optional
        elif sha256_file(path) != spec.sha256:
            print(f"[fail] {spec.name}: checksum mismatch")
            ok = False
        else:
            print(f"[ok]   {spec.name}")
    return ok


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--verify", action="store_true", help="only verify existing files")
    parser.add_argument("--all", action="store_true", help="include optional models")
    args = parser.parse_args()
    dest_dir = models_dir()
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not args.verify:
        for spec in MODELS.values():
            if args.all or not spec.optional:
                fetch(spec, dest_dir)
    sys.exit(0 if verify(dest_dir) else 1)


if __name__ == "__main__":
    main()
