"""Build the desktop icon: a white outline brain on a teal tile.

The brain is drawn here, so the icon holds no third-party artwork or logos. At 16-24 px it is drawn with fewer
folds and a thicker line, so it can still be read.

    python scripts/make_icon.py                      # rewrite assets/icon/nmosd_extractor.ico and .png
    python scripts/make_icon.py --preview sheet.png  # also write a preview of every size

Needs Pillow, which comes with the `review` extra (Streamlit depends on it).
"""

from __future__ import annotations

import argparse
import itertools
import math
from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[1]
ICON_DIR = REPO / "assets" / "icon"
TEAL = (0, 156, 166)
WHITE = (255, 255, 255)
ICO_SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)  # what Windows asks for across display scalings
SMALL = 24  # at or below this size the brain is drawn with fewer folds and a thicker line
SS = 4  # supersampling for the tile outline

# Folds of the left hemisphere on a 1000-unit grid (midline x = 500), mirrored for the right one.
FOLDS = (
    ((300, 172), (300, 235), (345, 268), (405, 258)),  # upper hook from the first notch
    ((170, 378), (245, 372), (300, 420), (292, 495)),  # central C
    ((300, 838), (300, 772), (345, 742), (405, 752)),  # lower hook from the last notch
    ((500, 590), (450, 592), (405, 618), (398, 668)),  # short stem off the midline
)


# ---------------------------------------------------------------------------------------------- brain
def _bezier(p0, p1, p2, p3, n: int = 80) -> list[tuple[float, float]]:
    pts = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        pts.append(tuple(u**3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t**3 * d
                         for a, b, c, d in zip(p0, p1, p2, p3, strict=True)))
    return pts


def _stroke(draw: ImageDraw.ImageDraw, pts, width: float) -> None:
    """Round-brush stroke: stamp discs along the resampled path (wide PIL lines leave seams at joints)."""
    r = width / 2
    step = max(1.0, width / 40)
    dense = [pts[0]]
    for (x0, y0), (x1, y1) in itertools.pairwise(pts):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / step))
        dense += [(x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n) for i in range(1, n + 1)]
    for x, y in dense:
        draw.ellipse((x - r, y - r, x + r, y + r), fill=255)


def brain_mask(px: int = 2048, stroke: float = 0.06, folds: tuple = FOLDS) -> Image.Image:
    """Top-down outline brain as an 'L' mask cropped to its ink."""
    k = px / 1000
    img = Image.new("L", (px, px), 0)
    draw = ImageDraw.Draw(img)
    width = stroke * px
    cx, cy, a, b = 500, 505, 345, 410
    bumps, amp = 5, 0.075
    for side in (1, -1):  # 1 = left hemisphere, -1 = right
        outline = []
        for i in range(601):
            phi = math.pi * i / 600  # 0 = top of the midline, pi = bottom
            theta = -math.pi / 2 - side * phi
            s = 1 + amp * abs(math.sin(bumps * phi)) ** 0.75  # scalloped edge: one bump per gyrus
            outline.append(((cx + a * math.cos(theta) * s) * k, (cy + b * math.sin(theta) * s) * k))
        _stroke(draw, outline, width)
        for fold in folds:
            _stroke(draw, [((cx - (cx - x) * side) * k, y * k) for x, y in _bezier(*fold)], width)
    _stroke(draw, [(cx * k, (cy - b) * k), (cx * k, (cy + b) * k)], width)  # midline
    return img.crop(img.getbbox())


# ---------------------------------------------------------------------------------------------- composition
def _tile(size: int, inset: float = 0.03, radius: float = 0.2) -> Image.Image:
    big = size * SS
    mask = Image.new("L", (big, big), 0)
    i = round(big * inset)
    ImageDraw.Draw(mask).rounded_rectangle((i, i, big - i - 1, big - i - 1), radius=round(big * radius), fill=255)
    return mask.resize((size, size), Image.Resampling.LANCZOS)


def _fill(canvas: Image.Image, mask: Image.Image, colour) -> None:
    layer = Image.new("RGBA", canvas.size, (*colour, 255))
    layer.putalpha(mask)
    canvas.alpha_composite(layer)


def _place(canvas: Image.Image, mask: Image.Image, colour, centre, box) -> None:
    scale = min(box[0] / mask.width, box[1] / mask.height)
    m = mask.resize((max(1, round(mask.width * scale)), max(1, round(mask.height * scale))),
                    Image.Resampling.LANCZOS)
    layer = Image.new("RGBA", m.size, (*colour, 255))
    layer.putalpha(m)
    canvas.alpha_composite(layer, (round(centre[0] - m.width / 2), round(centre[1] - m.height / 2)))


def render(size: int, brain: Image.Image, brain_small: Image.Image) -> Image.Image:
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    _fill(canvas, _tile(size), TEAL)
    if size <= SMALL:
        _place(canvas, brain_small, WHITE, (size / 2, size / 2), (round(size * 0.72), round(size * 0.72)))
    else:
        _place(canvas, brain, WHITE, (size / 2, size / 2), (round(size * 0.66), round(size * 0.66)))
    return canvas


def build(sizes: tuple[int, ...] = ICO_SIZES) -> dict[int, Image.Image]:
    brain = brain_mask()
    brain_small = brain_mask(stroke=0.09, folds=FOLDS[1:2])
    return {s: render(s, brain, brain_small) for s in sizes}


def write_preview(icons: dict[int, Image.Image], out: Path) -> None:
    pad, zoom = 16, 3
    width = pad + sum(s + pad for s in icons) + pad
    sheet = Image.new("RGB", (max(width, 2 * pad + 3 * 32 * zoom), 2 * (256 + 2 * pad) + 32 * zoom + pad),
                      (236, 240, 245))
    ImageDraw.Draw(sheet).rectangle((0, 256 + 2 * pad, sheet.width, 2 * (256 + 2 * pad)), fill=(18, 40, 77))
    for row in range(2):
        x = pad
        for s, im in icons.items():
            sheet.paste(im, (x, row * (256 + 2 * pad) + pad + (256 - s) // 2), im)
            x += s + pad
    # small sizes enlarged with hard pixels, to judge them
    x = pad
    for s in (16, 24, 32):
        big = icons[s].resize((s * zoom, s * zoom), Image.Resampling.NEAREST)
        sheet.paste(big, (x, 2 * (256 + 2 * pad) + pad // 2), big)
        x += s * zoom + pad
    sheet.save(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-dir", type=Path, default=ICON_DIR)
    ap.add_argument("--preview", type=Path, help="also write a preview sheet of every size here")
    args = ap.parse_args(argv)

    icons = build()
    args.out_dir.mkdir(parents=True, exist_ok=True)
    largest = icons[max(ICO_SIZES)]
    ico = args.out_dir / "nmosd_extractor.ico"
    # append_images supplies each size as drawn here, instead of Pillow shrinking the 256 px image
    largest.save(ico, format="ICO", sizes=[(s, s) for s in ICO_SIZES],
                 append_images=[icons[s] for s in ICO_SIZES if s != max(ICO_SIZES)])
    largest.save(args.out_dir / "nmosd_extractor.png")
    print(f"Wrote {ico.relative_to(REPO) if ico.is_relative_to(REPO) else ico} ({len(ICO_SIZES)} sizes)")
    if args.preview:
        write_preview(icons, args.preview)
        print(f"Wrote {args.preview}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
