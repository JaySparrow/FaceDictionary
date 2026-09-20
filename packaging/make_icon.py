"""Draw the app icon (a face inside a magnifier) and turn it into FaceDictionary.icns."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SIZES = [16, 32, 64, 128, 256, 512, 1024]
BACKGROUND = (38, 48, 66)
FACE = (243, 240, 234)
GLASS = (96, 165, 250)


def draw_icon(size: int = 1024) -> Image.Image:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    s = size / 1024

    d.rounded_rectangle([0, 0, size, size], radius=int(225 * s), fill=BACKGROUND)
    # a simple face: head and shoulders
    d.ellipse([int(300 * s), int(215 * s), int(640 * s), int(555 * s)], fill=FACE)
    d.pieslice([int(210 * s), int(545 * s), int(730 * s), int(1000 * s)], start=180, end=360, fill=FACE)
    # the magnifier that looks at it
    ring = [int(430 * s), int(430 * s), int(880 * s), int(880 * s)]
    d.ellipse(ring, outline=GLASS, width=int(58 * s))
    d.line([int(835 * s), int(835 * s), int(960 * s), int(960 * s)], fill=GLASS, width=int(70 * s))
    return img


def main() -> None:
    iconset = HERE / "FaceDictionary.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)
    master = draw_icon(1024)
    for size in SIZES:
        master.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
        if size <= 512:
            master.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
    icns = HERE / "FaceDictionary.icns"
    result = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)], capture_output=True, text=True)
    shutil.rmtree(iconset)
    if result.returncode != 0:
        sys.exit(f"iconutil failed: {result.stderr}")
    print(f"icon: {icns} ({icns.stat().st_size / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
