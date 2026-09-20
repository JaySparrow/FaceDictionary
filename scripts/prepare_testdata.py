"""Build the public test data set from LFW (Labeled Faces in the Wild).

Input:  testdata/lfw.tgz  (https://ndownloader.figshare.com/files/5976018)
Output: testdata/lfw_subset/<Person_Name>/*.jpg   ~60 people x up to 8 photos
        testdata/samples/                          edge cases (HEIC, EXIF rotation, two faces, no face, PNG+alpha)
"""

from __future__ import annotations

import random
import shutil
import subprocess
import sys
import tarfile
from collections import defaultdict
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
TESTDATA = ROOT / "testdata"
ARCHIVE = TESTDATA / "lfw.tgz"
SUBSET = TESTDATA / "lfw_subset"
SAMPLES = TESTDATA / "samples"

N_PEOPLE = 60
MIN_IMAGES = 6
MAX_IMAGES_POOL = 60     # skip the handful of people with hundreds of photos
PER_PERSON = 8
SEED = 20260919


def build_subset() -> None:
    with tarfile.open(ARCHIVE, "r:gz") as tar:
        members = [m for m in tar.getmembers() if m.isfile() and m.name.endswith(".jpg")]
        by_person: dict[str, list[tarfile.TarInfo]] = defaultdict(list)
        for m in members:
            by_person[Path(m.name).parent.name].append(m)
        pool = sorted(p for p, ms in by_person.items() if MIN_IMAGES <= len(ms) <= MAX_IMAGES_POOL)
        chosen = sorted(random.Random(SEED).sample(pool, N_PEOPLE))
        if SUBSET.exists():
            shutil.rmtree(SUBSET)
        for person in chosen:
            out_dir = SUBSET / person
            out_dir.mkdir(parents=True)
            for m in sorted(by_person[person], key=lambda m: m.name)[:PER_PERSON]:
                (out_dir / Path(m.name).name).write_bytes(tar.extractfile(m).read())
    n = sum(1 for _ in SUBSET.rglob("*.jpg"))
    print(f"subset: {len(chosen)} people, {n} photos -> {SUBSET}")


def build_samples() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    people = sorted(p for p in SUBSET.iterdir() if p.is_dir())
    a = sorted(people[0].glob("*.jpg"))[0]
    b = sorted(people[1].glob("*.jpg"))[0]
    img_a = Image.open(a).convert("RGB")
    img_b = Image.open(b).convert("RGB")

    # 1. EXIF rotation: pixels stored rotated, orientation tag says "rotate 90 CW to display"
    rotated = img_a.transpose(Image.Transpose.ROTATE_90)       # counter-clockwise
    exif = Image.Exif()
    exif[0x0112] = 6                                            # display needs 90 CW rotation
    rotated.save(SAMPLES / "exif_rotated.jpg", exif=exif.tobytes(), quality=95)

    # 2. Two faces side by side (larger face on the right)
    big = img_b.resize((int(img_b.width * 1.4), int(img_b.height * 1.4)))
    canvas = Image.new("RGB", (img_a.width + big.width, big.height), (128, 128, 128))
    canvas.paste(img_a, (0, (big.height - img_a.height) // 2))
    canvas.paste(big, (img_a.width, 0))
    canvas.save(SAMPLES / "two_faces.jpg", quality=95)

    # 3. No face: smooth noise
    rng = np.random.default_rng(0)
    noise = (rng.random((240, 320, 3)) * 255).astype(np.uint8)
    Image.fromarray(noise).resize((640, 480)).save(SAMPLES / "no_face.jpg", quality=90)

    # 4. PNG with alpha channel
    rgba = img_a.convert("RGBA")
    rgba.putalpha(255)
    rgba.save(SAMPLES / "alpha.png")

    # 5. HEIC: try pillow-heif's encoder, fall back to macOS sips
    heic = SAMPLES / "photo.heic"
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        img_a.save(heic, format="HEIF", quality=90)
    except Exception as exc:  # encoder not available in this build
        print(f"pillow-heif encode failed ({exc}); trying sips")
        subprocess.run(["sips", "-s", "format", "heic", str(a), "--out", str(heic)],
                       check=True, capture_output=True)
    (SAMPLES / "photo_source.txt").write_text(f"{a.relative_to(SUBSET)}\n")
    print(f"samples -> {SAMPLES}: {sorted(p.name for p in SAMPLES.iterdir())}")


def main() -> None:
    if not ARCHIVE.exists():
        sys.exit(f"missing {ARCHIVE}; download https://ndownloader.figshare.com/files/5976018 first")
    build_subset()
    build_samples()


if __name__ == "__main__":
    main()
