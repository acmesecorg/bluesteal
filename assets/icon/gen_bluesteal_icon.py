#!/usr/bin/env python3
"""Generate the Bluesteal icon at arbitrary sizes.

108x108 viewport. Layers (bottom to top):
- Full-bleed background       #7ED6E8 (light cyan)
- Outer ring                  #F0F8FF, center (54,54), radius 38
- Inner medium-blue disc      #1BA7E3, center (54,54), radius 31
- Top arrow (apex up)         #F0F8FF, (54,33)-(62,49)-(46,49)
- Bottom arrow (apex down)    #F0F8FF, (54,75)-(46,59)-(62,59)

Android composes the same design via its adaptive-icon background +
foreground layers (see app/src/main/res/...).
"""
from PIL import Image, ImageDraw
import os
import sys

BG = (0x7E, 0xD6, 0xE8, 255)
RING = (0xF0, 0xF8, 0xFF, 255)
INNER = (0x1B, 0xA7, 0xE3, 255)
VP = 108.0

CENTER = (54, 54)
OUTER_R = 38
INNER_R = 31
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
    cx, cy = CENTER[0] * s, CENTER[1] * s
    r_out, r_in = OUTER_R * s, INNER_R * s
    draw.ellipse((cx - r_out, cy - r_out, cx + r_out, cy + r_out), fill=RING)
    draw.ellipse((cx - r_in, cy - r_in, cx + r_in, cy + r_in), fill=INNER)
    draw.polygon([(x * s, y * s) for x, y in TRI_TOP], fill=RING)
    draw.polygon([(x * s, y * s) for x, y in TRI_BOT], fill=RING)
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
