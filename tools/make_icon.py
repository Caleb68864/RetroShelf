#!/usr/bin/env python3
"""Generate RetroShelf's apple-touch-icon — a 180x180 amber-on-black bookshelf.

Deterministic, dependency-light (Pillow only, no web/external fonts), so the
icon is a same-origin static asset per the iPad build constraints. Re-run to
regenerate:

    .venv/bin/python tools/make_icon.py

Writes ``app/static/icons/apple-touch-icon-180.png`` (baseline PNG, 180x180).
Old iOS Safari rounds the corners itself, so we render a full-bleed square.
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

SIZE = 180
BG = (10, 10, 10)        # near-black terminal
AMBER = (255, 176, 0)    # phosphor amber (matches the default theme)

OUT = os.path.join(os.path.dirname(__file__), os.pardir,
                   "app", "static", "icons", "apple-touch-icon-180.png")


def render() -> Image.Image:
    """Render the 180x180 icon: an amber bookshelf on black."""
    img = Image.new("RGB", (SIZE, SIZE), BG)
    d = ImageDraw.Draw(img)

    # Outer amber frame (a CRT-ish border).
    d.rectangle([8, 8, SIZE - 9, SIZE - 9], outline=AMBER, width=4)

    # Two shelves of books. Each book is an amber-outlined spine; a couple are
    # tilted/filled to read clearly at home-screen size.
    shelves = (46, 104)          # top y of each shelf row
    shelf_h = 50
    for sy in shelves:
        # shelf baseline
        d.line([22, sy + shelf_h, SIZE - 22, sy + shelf_h], fill=AMBER, width=3)
        x = 28
        widths = (16, 12, 18, 14, 16, 12)
        for i, bw in enumerate(widths):
            top = sy + (6 if i % 2 else 2)
            if i % 3 == 0:
                d.rectangle([x, top, x + bw, sy + shelf_h - 2], fill=AMBER)
            else:
                d.rectangle([x, top, x + bw, sy + shelf_h - 2], outline=AMBER, width=2)
            x += bw + 6
    return img


def main() -> None:
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    img = render()
    # Baseline PNG (universally safe on old Safari).
    img.save(os.path.normpath(OUT), format="PNG")
    print(f"wrote {os.path.normpath(OUT)} ({img.size[0]}x{img.size[1]})")


if __name__ == "__main__":
    main()
