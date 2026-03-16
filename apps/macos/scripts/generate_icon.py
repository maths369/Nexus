#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


ICON_SIZES = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]


def rounded_card_mask(size: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    draw = ImageDraw.Draw(mask)
    inset = int(size * 0.08)
    radius = int(size * 0.17)
    draw.rounded_rectangle(
        (inset, inset, size - inset - 1, size - inset - 1),
        radius=radius,
        fill=255,
    )
    return mask


def render_master(source_path: Path, size: int = 1024) -> Image.Image:
    source = Image.open(source_path).convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    source.putalpha(rounded_card_mask(size))
    return source


def write_iconset(source_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    master = render_master(source_path)
    for name, size in ICON_SIZES:
        master.resize((size, size), Image.Resampling.LANCZOS).save(output_dir / name)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    source_path = root / "Resources" / "NexusMac.source.png"
    if not source_path.exists():
        raise FileNotFoundError(f"Missing source image: {source_path}")

    iconset_dir = root / "Resources" / "NexusMac.iconset"
    write_iconset(source_path, iconset_dir)
    print(iconset_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
