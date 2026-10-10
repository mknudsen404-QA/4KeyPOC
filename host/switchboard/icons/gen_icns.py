#!/usr/bin/env python3
"""Build settings.icns / update.icns from the raw source art in src/.

The raw PNGs (src/*-raw.png, 1024x1024, Codex-generated — see
docs/design for the prompt used) don't reliably leave enough margin for
macOS's own automatic icon mask: a gear/arrow that touches the edge of
the canvas gets clipped by the rounded-squircle mask every icon gets,
regardless of app. This script re-centers each one inside a safe margin
before building the .icns, so that's a property of the build step, not
something to re-ask Codex for by hand-tuning a prompt.

Requires macOS (`sips` and `iconutil` are both bundled, no extra
install). Needs Pillow for the crop/resize/recenter step:
    pip install Pillow

Regenerate after replacing a *-raw.png:
    python3 host/switchboard/icons/gen_icns.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Apple's own guidance: keep the glyph inside roughly the center 80% of
# the canvas so the automatic rounded-squircle mask (applied to every
# macOS app icon, not something this project controls) never clips it.
CANVAS = 1024
SAFE_CONTENT = 820

# (raw source, built .icns name)
ICONS = [
    ("settings-raw.png", "settings.icns"),
    ("update-raw.png", "update.icns"),
]

# Sizes iconutil expects inside a .iconset — every one of these, at
# exactly this filename, or it refuses to build.
ICONSET_SIZES = (16, 32, 128, 256, 512)


def pad_and_center(src: Path, dst: Path) -> None:
    from PIL import Image

    img = Image.open(src).convert("RGBA")
    bbox = img.getbbox()
    if bbox is None:
        raise ValueError(f"{src} is fully transparent — nothing to center")
    cropped = img.crop(bbox)
    scale = SAFE_CONTENT / max(cropped.size)
    new_size = (round(cropped.width * scale), round(cropped.height * scale))
    resized = cropped.resize(new_size, Image.LANCZOS)
    canvas = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    x = (CANVAS - new_size[0]) // 2
    y = (CANVAS - new_size[1]) // 2
    canvas.paste(resized, (x, y), resized)
    canvas.save(dst)


def build_icns(padded_png: Path, icns_out: Path) -> None:
    iconset = icns_out.with_suffix(".iconset")
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir()
    try:
        for size in ICONSET_SIZES:
            subprocess.run(
                ["sips", "-z", str(size), str(size), str(padded_png), "--out", str(iconset / f"icon_{size}x{size}.png")],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                [
                    "sips",
                    "-z",
                    str(size * 2),
                    str(size * 2),
                    str(padded_png),
                    "--out",
                    str(iconset / f"icon_{size}x{size}@2x.png"),
                ],
                check=True,
                capture_output=True,
            )
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns_out)], check=True, capture_output=True)
    finally:
        shutil.rmtree(iconset, ignore_errors=True)


def main() -> int:
    if sys.platform != "darwin":
        print("This only runs on macOS (needs sips/iconutil).", file=sys.stderr)
        return 1
    for raw_name, icns_name in ICONS:
        raw = HERE / "src" / raw_name
        with tempfile.TemporaryDirectory() as tmp:
            padded = Path(tmp) / raw_name.replace("-raw.png", "-padded.png")
            pad_and_center(raw, padded)
            build_icns(padded, HERE / icns_name)
        print(f"wrote {HERE / icns_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
