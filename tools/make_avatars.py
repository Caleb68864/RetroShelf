#!/usr/bin/env python3
"""Generate RetroShelf's built-in profile avatars: simple shapes in each
profile colour, as tiny baseline PNGs (no fonts, no SVG, no JS), so they
render on iOS 5.1.1 Safari. Re-run to regenerate:

    .venv/bin/python tools/make_avatars.py

Writes ``app/static/avatars/{shape}-{colour}.png`` (48x48). The shape and
colour lists must match ``AVATAR_SHAPES`` and ``PROFILE_COLORS`` in
``app/store.py``.
"""
from __future__ import annotations

import math
import os
import sys

from PIL import Image, ImageDraw

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
from app.store import AVATAR_SHAPES, PROFILE_COLORS  # noqa: E402

SIZE = 48
BG = (10, 10, 10)
COLOURS = {
    "amber": (255, 176, 0), "green": (78, 228, 78), "cyan": (70, 208, 208),
    "white": (235, 235, 235), "magenta": (230, 90, 230), "blue": (90, 140, 255),
}
OUT = os.path.join(os.path.dirname(__file__), os.pardir, "app", "static", "avatars")


def _star(cx, cy, r_out, r_in, points=5):
    pts = []
    for i in range(points * 2):
        r = r_out if i % 2 == 0 else r_in
        a = math.pi / 2 + i * math.pi / points
        pts.append((cx + r * math.cos(a), cy - r * math.sin(a)))
    return pts


def render(shape: str, rgb: tuple[int, int, int]) -> Image.Image:
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)
    c, r = SIZE / 2, SIZE * 0.38
    box = (c - r, c - r, c + r, c + r)
    if shape == "circle":
        d.ellipse(box, fill=rgb)
    elif shape == "square":
        d.rectangle((c - r * 0.85, c - r * 0.85, c + r * 0.85, c + r * 0.85), fill=rgb)
    elif shape == "triangle":
        d.polygon([(c, c - r), (c + r, c + r * 0.8), (c - r, c + r * 0.8)], fill=rgb)
    elif shape == "diamond":
        d.polygon([(c, c - r), (c + r, c), (c, c + r), (c - r, c)], fill=rgb)
    elif shape == "star":
        d.polygon(_star(c, c, r, r * 0.45), fill=rgb)
    elif shape == "moon":
        d.ellipse(box, fill=rgb)
        d.ellipse((c - r * 0.45, c - r * 1.05, c + r * 1.4, c + r * 0.8), fill=BG)
    elif shape == "heart":
        d.ellipse((c - r, c - r * 0.75, c, c + r * 0.25), fill=rgb)
        d.ellipse((c, c - r * 0.75, c + r, c + r * 0.25), fill=rgb)
        d.polygon([(c - r * 0.97, c - r * 0.1), (c + r * 0.97, c - r * 0.1), (c, c + r)], fill=rgb)
    elif shape == "book":
        d.rectangle((c - r, c - r * 0.7, c - 2, c + r * 0.8), fill=rgb)
        d.rectangle((c + 2, c - r * 0.7, c + r, c + r * 0.8), fill=rgb)
    else:
        raise ValueError(shape)
    return img


def main() -> None:
    os.makedirs(OUT, exist_ok=True)
    for shape in AVATAR_SHAPES:
        for colour in PROFILE_COLORS:
            render(shape, COLOURS[colour]).convert("P", palette=Image.ADAPTIVE, colors=8).save(
                os.path.join(OUT, f"{shape}-{colour}.png"), format="PNG", optimize=True)


if __name__ == "__main__":
    main()
