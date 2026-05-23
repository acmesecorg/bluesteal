#!/usr/bin/env python3
"""Generate the Bluesteal icon at arbitrary sizes.

The design mirrors the (now corrected) Android adaptive icon foreground:
- 108x108 base viewport
- Background fill #0E1A2B
- Light-blue circle (#4FC3F7) at center, radius 26
- Two triangle "arrows" cut out (showing the dark background through):
  - Top:    apex (54,33), base (46,49)-(62,49)
  - Bottom: apex (54,75), base (46,59)-(62,59)
"""
from PIL import Image, ImageDraw
import os
import sys

BG = (0x0E, 0x1A, 0x2B, 255)
FG = (0x4F, 0xC3, 0xF7, 255)
VP = 108.0

CIRCLE_C = (54, 54)
CIRCLE_R = 26
TRI_TOP = [(54, 33), (62, 49), (46, 49)]
TRI_BOT = [(54, 75), (46, 59), (62, 59)]


def render(size: int, opaque_bg: bool = True) -> Image.Image:
    """Render the icon at `size`x`size`. Anti-aliased via 4x supersampling."""
    ss = 4
    n = size * ss
    s = n / VP
    bg = BG if opaque_bg else (0, 0, 0, 0)
    img = Image.new("RGBA", (n, n), bg)
    draw = ImageDraw.Draw(img)
    cx, cy = CIRCLE_C[0] * s, CIRCLE_C[1] * s
    r = CIRCLE_R * s
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=FG)
    # Cut out the arrows by drawing them in the background color.
    cutout = BG if opaque_bg else (0, 0, 0, 0)
    draw.polygon([(x * s, y * s) for x, y in TRI_TOP], fill=cutout)
    draw.polygon([(x * s, y * s) for x, y in TRI_BOT], fill=cutout)
    return img.resize((size, size), Image.LANCZOS)


def write_png(path: str, size: int, opaque_bg: bool = True):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    render(size, opaque_bg=opaque_bg).save(path, "PNG")
    print(f"wrote {path} ({size}x{size})")


def write_ico(path: str, sizes):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    biggest = max(sizes)
    base = render(biggest, opaque_bg=True)
    base.save(path, format="ICO", sizes=[(s, s) for s in sizes])
    print(f"wrote {path} (sizes {sizes})")


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: gen_bluesteal_icon.py <png|ico> <out> [size|sizes]")
        sys.exit(1)
    kind = sys.argv[1]
    out = sys.argv[2]
    if kind == "png":
        write_png(out, int(sys.argv[3]))
    elif kind == "ico":
        sizes = [int(x) for x in sys.argv[3].split(",")]
        write_ico(out, sizes)
    else:
        print("unknown kind:", kind)
        sys.exit(1)
