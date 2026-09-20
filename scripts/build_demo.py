"""Build a demo vault from public LFW photos (no personal data involved).

    python scripts/build_demo.py                      # -> demo/vault.noindex (password: demo-password)
    python scripts/build_demo.py --group-photo-only   # only (re)make demo/queries/group_photo.jpg

Creates:
  demo/vault.noindex/     encrypted demo vault, 150 people, up to 6 photos each
  demo/queries/           held-out photos NOT in the vault, to drag into the app
  demo/queries/ANSWERS.txt which person each query shows
Then open it with ./run_demo.command
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from facedict.service import FaceDictionaryService  # noqa: E402

LFW = ROOT / "testdata" / "lfw_full" / "lfw"
DEMO = ROOT / "demo"
PASSWORD = "demo-password"
N_PEOPLE = 150
PER_PERSON = 6
N_QUERIES = 40
SEED = 7


def make_group_photo(queries: Path) -> None:
    """Compose a group photo from held-out query photos (people who are in the demo library),
    at different sizes, and append who is who to ANSWERS.txt."""
    import cv2
    import numpy as np

    from facedict.engine import FaceEngine, display_crop
    from facedict.imageio import decode, encode_jpeg

    eng = FaceEngine()
    answers = [line.split("\t") for line in (queries / "ANSWERS.txt").read_text("utf-8").splitlines()
               if line and not line.startswith("group_photo")]
    sizes = [260, 300, 250, 380, 360]                   # back row, then the (larger) front row
    tiles, names = [], []
    for fname, name in answers[2:]:                      # skip the two searches pre-run in the demo
        if not fname.endswith(".jpg"):
            continue
        img = decode((queries / fname).read_bytes())
        crop = display_crop(img, eng.detect(img)[0], margin=0.4)
        if len(eng.detect(crop)) != 1:                   # skip photos with bystanders at the edges
            continue
        size = sizes[len(tiles)]
        tiles.append(cv2.resize(crop, (size, size), interpolation=cv2.INTER_CUBIC))
        names.append(name)
        if len(tiles) == len(sizes):
            break
    # two rows like a real group photo: three people at the back, two in front (bigger)
    gap = 50
    back, front = tiles[:3], tiles[3:]
    w = max(sum(t.shape[1] for t in back) + gap * 4, sum(t.shape[1] for t in front) + gap * 3)
    back_h = max(t.shape[0] for t in back)
    h = gap + back_h + front[0].shape[0] // 2 + max(t.shape[0] for t in front) // 2 + gap
    canvas = np.full((h, w, 3), (205, 200, 190), np.uint8)
    for row, y0 in ((back, gap), (front, gap + back_h // 2 + 40)):
        x = (w - sum(t.shape[1] for t in row) - gap * (len(row) - 1)) // 2
        for t in row:
            yy = min(y0, h - t.shape[0])
            canvas[yy:yy + t.shape[0], x:x + t.shape[1]] = t
            x += t.shape[1] + gap
    (queries / "group_photo.jpg").write_bytes(encode_jpeg(canvas, 90))
    lines = [line for line in (queries / "ANSWERS.txt").read_text("utf-8").splitlines()
             if line and not line.startswith("group_photo")]
    lines.append("group_photo.jpg\tback row, left to right: " + ", ".join(names[:3])
                 + "; front row: " + ", ".join(names[3:]))
    (queries / "ANSWERS.txt").write_text("\n".join(lines) + "\n", "utf-8")
    print(f"group photo: {len(names)} faces -> {queries / 'group_photo.jpg'}")


def main() -> None:
    if "--group-photo-only" in sys.argv:
        make_group_photo(DEMO / "queries")
        return
    if not LFW.exists():
        sys.exit("Missing testdata/lfw_full/lfw - extract testdata/lfw.tgz there first.")
    vault = DEMO / "vault.noindex"
    if vault.exists():
        shutil.rmtree(vault)
    queries = DEMO / "queries"
    if queries.exists():
        shutil.rmtree(queries)
    staging = DEMO / ".staging"
    if staging.exists():
        shutil.rmtree(staging)
    queries.mkdir(parents=True)

    rng = random.Random(SEED)
    pool = sorted(d for d in LFW.iterdir() if d.is_dir() and 7 <= len(list(d.glob("*.jpg"))) <= 80)
    people = sorted(rng.sample(pool, N_PEOPLE))
    query_people = set(rng.sample(people, N_QUERIES))
    answers = []
    for person in people:
        photos = sorted(person.glob("*.jpg"))
        held_out = photos[-1]
        library = photos[:PER_PERSON]
        d = staging / person.name
        d.mkdir(parents=True)
        for p in library:
            shutil.copy(p, d / p.name)
        name = person.name.replace("_", " ")
        (d / "entry.json").write_text(json.dumps({
            "name": name, "tags": ["demo", "LFW"],
            "notes": "Demo entry built from the public Labeled Faces in the Wild dataset.",
            "source": "LFW", "photos in LFW": str(len(photos))}, ensure_ascii=False, indent=2), "utf-8")
        if person in query_people:
            answers.append((held_out, name))

    rng.shuffle(answers)
    lines = []
    for i, (src, name) in enumerate(answers, 1):
        dest = queries / f"query_{i:02d}.jpg"
        shutil.copy(src, dest)
        lines.append(f"{dest.name}\t{name}")
    # one query as HEIC, like a photo from an iPhone. Encoded with macOS `sips`: encoding HEIC with
    # pillow-heif in a process that has OpenCV loaded crashes (clashing native HEVC libraries).
    heic_src, heic_name = answers[-1]                 # not one of the pre-run searches below
    subprocess.run(["sips", "-s", "format", "heic", str(heic_src), "--out", str(queries / "query_heic.heic")],
                   check=True, capture_output=True)
    lines.append(f"query_heic.heic\t{heic_name}")
    (queries / "ANSWERS.txt").write_text("\n".join(lines) + "\n", "utf-8")
    make_group_photo(queries)

    svc = FaceDictionaryService(vault)
    svc.create_vault(PASSWORD)
    report = svc.import_folder(staging, progress=lambda d, t: print(f"\r  importing {d}/{t}", end="", flush=True))
    print()
    print(f"entries: {report.entries_created}, photos: {report.photos_added}, no face: {report.no_face}, "
          f"several faces: {report.multi_face}, errors: {len(report.errors)}")
    # a few searches so the History page is not empty; confirm the first one
    first = svc.query((queries / "query_01.jpg").read_bytes())
    if first.matches:
        svc.confirm(first.history.id, first.matches[0].entry_id)
    svc.query((queries / "query_02.jpg").read_bytes())
    svc.lock()
    shutil.rmtree(staging)
    print(f"demo vault: {vault}\npassword:   {PASSWORD}\nqueries:    {queries}")


if __name__ == "__main__":
    main()
