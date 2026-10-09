"""Builds assets/icon.png and assets/icon.ico from a transparent llama glyph.

The tile is drawn here rather than taken from the generated artwork, whose background
removal left ragged edges around the rounded square.

Usage: python assets/make_icon.py <glyph.png>
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

SIZE = 1024
SUPERSAMPLE = 4
MARGIN = 20
RADIUS_FRACTION = 0.21
GLYPH_HEIGHT_FRACTION = 0.64
TILE_TOP = (28, 30, 40)
TILE_BOTTOM = (10, 11, 15)
GLOW = (108, 140, 255)
ICO_SIZES = [(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)]
OUT = Path(__file__).resolve().parent


def rounded_mask(size: int, margin: int, radius: int) -> Image.Image:
    big = size * SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (margin * SUPERSAMPLE, margin * SUPERSAMPLE, big - margin * SUPERSAMPLE - 1, big - margin * SUPERSAMPLE - 1),
        radius=radius * SUPERSAMPLE, fill=255)
    return mask.resize((size, size), Image.LANCZOS)


def vertical_gradient(size: int, top: tuple, bottom: tuple) -> Image.Image:
    column = Image.new("RGB", (1, size))
    for y in range(size):
        t = y / (size - 1)
        column.putpixel((0, y), tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return column.resize((size, size))


def main(glyph_path: str) -> None:
    glyph = Image.open(glyph_path).convert("RGBA")
    glyph = glyph.crop(glyph.getchannel("A").point(lambda a: 255 if a > 16 else 0).getbbox())
    radius = round(SIZE * RADIUS_FRACTION)
    tile_mask = rounded_mask(SIZE, MARGIN, radius)

    target_h = round((SIZE - 2 * MARGIN) * GLYPH_HEIGHT_FRACTION)
    glyph = glyph.resize((round(glyph.width * target_h / glyph.height), target_h), Image.LANCZOS)
    pos = ((SIZE - glyph.width) // 2, (SIZE - glyph.height) // 2)

    icon = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    icon.paste(vertical_gradient(SIZE, TILE_TOP, TILE_BOTTOM), (0, 0), tile_mask)

    glyph_alpha = Image.new("L", (SIZE, SIZE), 0)
    glyph_alpha.paste(glyph.getchannel("A"), pos)
    glow_alpha = glyph_alpha.filter(ImageFilter.GaussianBlur(34)).point(lambda a: round(a * 0.55))
    glow = Image.new("RGBA", (SIZE, SIZE), GLOW + (0,))
    glow.putalpha(ImageChops.multiply(glow_alpha, tile_mask))
    icon.alpha_composite(glow)

    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    layer.paste(glyph, pos)
    icon.alpha_composite(layer)

    # Faint rim so the tile still separates from a dark taskbar.
    rim = ImageChops.subtract(tile_mask, rounded_mask(SIZE, MARGIN + 3, radius - 3)).point(lambda a: round(a * 0.09))
    rim_layer = Image.new("RGBA", (SIZE, SIZE), (255, 255, 255, 0))
    rim_layer.putalpha(rim)
    icon.alpha_composite(rim_layer)

    icon.save(OUT / "icon.png")
    icon.save(OUT / "icon.ico", sizes=ICO_SIZES)
    print(f"wrote {OUT / 'icon.png'} and {OUT / 'icon.ico'}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
