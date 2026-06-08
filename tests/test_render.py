"""Tests for app.render + templates + CSS — old-Safari safety + correct links."""
import re

import pytest

from app.render import templates, TEMPLATES_DIR, STATIC_DIR

CSS = (STATIC_DIR / "app.css").read_text(encoding="utf-8")


def render(name: str, **ctx) -> str:
    return templates.env.get_template(name).render(**ctx)


# -- old-Safari safety: scan every template + the CSS ---------------------------

ALL_ASSETS = list(TEMPLATES_DIR.glob("*.html")) + [STATIC_DIR / "app.css"]


@pytest.mark.parametrize("path", ALL_ASSETS, ids=lambda p: p.name)
def test_no_javascript_or_grid_or_external_assets(path):
    text = path.read_text(encoding="utf-8")
    low = text.lower()
    assert "<script" not in low, f"{path.name} contains <script"
    assert "javascript:" not in low, f"{path.name} contains a javascript: URL"
    assert "onclick" not in low and "onload" not in low, f"{path.name} has an inline JS handler"
    assert "fetch(" not in low and "xmlhttprequest" not in low
    assert "display:grid" not in low.replace(" ", "") and "grid-template" not in low
    # No external/CDN assets or web fonts — everything is same-origin.
    assert "http://" not in low and "https://" not in low, f"{path.name} references an external URL"
    assert "@font-face" not in low and "//fonts." not in low


def test_base_has_viewport_and_single_local_css():
    html = render("home.html", kavita_ok=True, status_detail="", root_feed_url="/feed/x")
    assert '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
    assert html.count('rel="stylesheet"') == 1
    assert '/static/app.css' in html
    assert "<!DOCTYPE html>" in html


def test_feed_renders_nav_and_book_entries():
    entries = [
        {"is_nav": True, "title": "Libraries", "href": "/feed/abc"},
        {"is_nav": False, "title": "The Time Machine", "author": "H. G. Wells",
         "badge": "EPUB", "detail_url": "/book/def", "cover_url": "/cover/ghi"},
    ]
    html = render("feed.html", feed_title="Recently Added", entries=entries,
                  next_url="/feed/next", prev_url=None, search_url="/search")
    assert "Libraries" in html and 'href="/feed/abc"' in html
    assert 'href="/book/def"' in html
    assert 'src="/cover/ghi"' in html
    assert "badge-epub" in html
    assert 'href="/feed/next"' in html
    # No apiKey-bearing or upstream URLs leak into the page.
    assert "apiKey" not in html and "/api/opds/" not in html


def test_book_detail_download_link_is_extension_bearing():
    html = render("book.html", title="The Time Machine", author="H. G. Wells",
                  badge="EPUB", summary="A tale.", cover_url=None,
                  download_url="/download/xyz/The-Time-Machine.epub", back_url="/feed/abc")
    assert 'href="/download/xyz/The-Time-Machine.epub"' in html
    assert "Open in iBooks" in html
    assert 'href="/feed/abc"' in html


def test_book_detail_pdf_hint():
    html = render("book.html", title="Report", author="", badge="PDF", summary="",
                  cover_url=None, download_url="/download/p/report.pdf", back_url="/")
    assert "Open PDF" in html
    assert "Share" in html


def test_error_page():
    html = render("error.html", heading="Not found", message="No such book.")
    assert "Not found" in html and "No such book." in html


# -- touch-friendliness: every interactive target must be a comfortable size --

def _block(css: str, selector: str) -> str:
    """Return the declaration block for *selector* (first match)."""
    i = css.index(selector)
    return css[i:css.index("}", i)]


def _vpad(block: str) -> int:
    """Approximate vertical padding (top+bottom) from a `padding:` declaration."""
    m = re.search(r"padding:\s*([0-9]+)px", block)
    return int(m.group(1)) * 2 if m else 0


def test_touch_targets_are_large_enough():
    # Interactive elements need ~44px tap height. With ~23-28px line-height for
    # the font sizes used, that means >= ~12px vertical padding (or min-height).
    assert "-webkit-tap-highlight-color" in CSS, "themed tap feedback missing"
    # Primary + small buttons
    assert _vpad(_block(CSS, ".button {")) >= 28
    assert _vpad(_block(CSS, ".button.small {")) >= 26
    # Menu bar + back chips (were bare inline text before the touch pass)
    assert _vpad(_block(CSS, ".menubar a {")) >= 24
    assert _vpad(_block(CSS, ".back {")) >= 24
    # List rows (whole-row tap targets)
    assert _vpad(_block(CSS, ".navlink {")) >= 32
    book = _block(CSS, ".book {")
    assert _vpad(book) >= 28
    assert re.search(r"min-height:\s*(\d+)px", book) and int(re.search(r"min-height:\s*(\d+)px", book).group(1)) >= 60
    # Search controls (also >=16px font so iOS doesn't zoom on focus)
    assert _vpad(_block(CSS, ".search input[type=text]")) >= 24
    assert _vpad(_block(CSS, ".search input[type=submit]")) >= 24
