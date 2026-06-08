"""Tests for SS-02 — Add-to-Home-Screen web-app meta + apple-touch-icon.

The home-screen install affordance is pure `<head>` markup + a same-origin PNG
icon (no JS, no external assets), per the iOS 5-12 build constraints.
"""
import pathlib

from PIL import Image

from tests.test_app import make_client, make_handler

ICON = pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "icons" / "apple-touch-icon-180.png"


def test_head_advertises_home_screen_webapp():
    with make_client(make_handler()) as client:
        html = client.get("/").text
    assert 'name="apple-mobile-web-app-capable"' in html
    assert 'name="apple-mobile-web-app-status-bar-style"' in html
    assert 'name="apple-mobile-web-app-title"' in html
    # The icon link must point at a same-origin /static asset.
    assert 'rel="apple-touch-icon"' in html
    assert "/static/icons/apple-touch-icon-180.png" in html


def test_apple_touch_icon_is_valid_180_png():
    assert ICON.exists(), f"icon missing at {ICON}"
    img = Image.open(ICON)
    img.verify()                      # raises if the PNG is corrupt
    img = Image.open(ICON)            # reopen (verify() leaves the file unusable)
    assert img.format == "PNG"
    assert img.size == (180, 180)
