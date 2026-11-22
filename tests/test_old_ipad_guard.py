"""Guard: RetroShelf must keep working on iOS 5.1.1-12 Safari (owner hard
constraint, 2026-11-21). No JavaScript of any kind and no CSS old Safari
cannot render, in the templates, the stylesheet, the static assets, or the
HTML the app actually serves.
"""
import pathlib
import re

import pytest

from tests.test_app import ENV, make_client, make_handler

ROOT = pathlib.Path(__file__).resolve().parent.parent
TEMPLATES = sorted((ROOT / "app" / "templates").glob("*.html"))
STYLESHEETS = sorted((ROOT / "app" / "static").rglob("*.css"))

HTML_BANNED = {
    "<script": re.compile(r"<\s*script\b", re.I),
    "on*= event attribute": re.compile(r"<[^>]*\son[a-z]+\s*=", re.I | re.S),
    "javascript: URL": re.compile(r"javascript\s*:", re.I),
    "<svg> or <canvas>": re.compile(r"<\s*(svg|canvas)\b", re.I),
}
CSS_BANNED = {
    "CSS grid": re.compile(r"display\s*:\s*(inline-)?grid|\bgrid-(template|area|column|row|gap)", re.I),
    "custom properties": re.compile(r"var\(\s*--|(^|[;{\s])--[a-z0-9-]+\s*:", re.I),
    "modern flexbox": re.compile(r"display\s*:\s*(inline-)?flex\b|(^|[;{\s])flex(-[a-z]+)?\s*:", re.I),
    "position: sticky": re.compile(r"position\s*:\s*(-webkit-)?sticky", re.I),
    ":is/:has/:where": re.compile(r":(is|has|where)\(", re.I),
    "gap": re.compile(r"(^|[;{\s])(row-|column-)?gap\s*:", re.I),
    "object-fit": re.compile(r"object-(fit|position)\s*:", re.I),
    "vw/vh units": re.compile(r"\d(\.\d+)?\s*(vw|vh|vmin|vmax)\b", re.I),
}
BANNED_ASSET_SUFFIXES = {".webp", ".avif", ".svg", ".js", ".mjs"}


def _strip_css_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _css_violations(css: str) -> list[str]:
    css = _strip_css_comments(css)
    return [name for name, rx in CSS_BANNED.items() if rx.search(css)]


def _html_violations(html: str) -> list[str]:
    found = [name for name, rx in HTML_BANNED.items() if rx.search(html)]
    for block in re.findall(r"<style[^>]*>(.*?)</style>", html, re.S | re.I):
        found += _css_violations(block)
    for attr in re.findall(r'\sstyle\s*=\s*"([^"]*)"', html, re.I):
        found += _css_violations(attr)
    return found


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_template_has_no_js_or_banned_css(path):
    assert _html_violations(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("path", STYLESHEETS, ids=lambda p: p.name)
def test_stylesheet_has_no_banned_css(path):
    assert _css_violations(path.read_text(encoding="utf-8")) == []


def test_no_modern_or_script_assets_are_shipped():
    bad = [p.name for p in (ROOT / "app" / "static").rglob("*")
           if p.suffix.lower() in BANNED_ASSET_SUFFIXES]
    assert bad == []


def test_the_guard_itself_catches_each_banned_feature():
    for html in ("<script>x</script>", '<a onclick="x()">', '<a href="javascript:x">',
                 "<svg></svg>", '<p style="display:flex">'):
        assert _html_violations(html), html
    for css in ("a{display:grid}", "a{color:var(--x)}", ":root{--x:1px}", "a{flex:1}",
                "a{position:sticky}", "a:is(b){}", "a{gap:2px}", "img{object-fit:cover}",
                "a{width:50vw}", "a{display:inline-flex}"):
        assert _css_violations(css), css
    for ok in ("a{display:-webkit-box}", "a{float:left}", "a{display:inline-block}",
               "/* no grid here */ a{margin:0}", "a{-webkit-text-size-adjust:100%}"):
        assert not _css_violations(ok), ok


def _book_id(client) -> str | None:
    home = client.get("/").text
    fid = re.search(r'/feed/([\w\-.]+)', home).group(1)
    for f2 in re.findall(r'/feed/([\w\-.]+)"', client.get(f"/feed/{fid}").text):
        m = re.search(r'/book/([\w\-.]+)"', client.get(f"/feed/{f2}").text)
        if m:
            return m.group(1)
    return None


def test_served_pages_have_no_js_or_banned_css():
    with make_client(make_handler()) as client:
        tok = client.app.state.ids.site_token
        client.get(f"/shelves/create?name=Guard&t={tok}", follow_redirects=False)
        pages = ["/", "/help", "/list", "/shelves", "/status", "/search",
                 "/search?q=time", "/opds", "/opds/reading-list"]
        bid = _book_id(client)
        assert bid
        pages += [f"/book/{bid}"]
        fid = re.search(r'/feed/([\w\-.]+)', client.get("/").text).group(1)
        pages += [f"/feed/{fid}", f"/feed/{client.app.state.ids.encode('x')}"]  # + an error page
        sid = re.search(r'/shelf/([0-9a-f]+)"', client.get("/shelves").text).group(1)
        pages += [f"/shelf/{sid}"]
        for page in pages:
            r = client.get(page)
            assert _html_violations(r.text) == [], page


def test_account_pages_have_no_js_or_banned_css():
    from tests.test_auth_routes import _make_second_account
    from tests.test_auth_routes import make_client as accounts_client
    from tests.test_auth_routes import register_admin
    with accounts_client() as client:
        for page in ("/setup", "/login"):
            assert _html_violations(client.get(page).text) == [], page
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        for page in ("/account", "/profiles", "/admin", "/"):
            assert _html_violations(client.get(page).text) == [], page


def test_reader_pages_have_no_js_or_banned_css(tmp_path):
    from tests.test_reader_routes import _bid
    from tests.test_reader_routes import make_client as reader_client
    from tests.test_reader_routes import make_handler as reader_handler
    from tests.test_reader_shelve import make_epub
    handler, _calls = reader_handler(make_epub(chapters=2))
    with reader_client(handler, str(tmp_path)) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)
        for page in (f"/read/{bid}/0/1", f"/read/{bid}/1/1", f"/read/{bid}/toc",
                     f"/read/{bid}/find?q=Chapter", f"/read/{bid}/bookmarks",
                     f"/read/{bid}/9/1"):
            assert _html_violations(client.get(page).text) == [], page
