"""Tests for in-browser HTML/text book reading (the ``shelve_html_book`` path).

Every fixture is a synthetic HTML document built in-test — no committed
binaries. Kavita is mocked at the httpx transport layer (real
:class:`~app.kavita.KavitaClient`, so the genuine SSRF guard runs), mirroring
``tests/test_reader_routes.py``'s harness. The critical case is the sanitizer
seam: hostile HTML must reach served pages with no surviving markup.
"""
from __future__ import annotations

import io
import json

import httpx
from PIL import Image

from app.config import load_config
from app.kavita import KavitaClient
from app.reader import Manifest, load_chapter, shelve_html_book
from tests.test_reader_routes import ENV, make_client

ORIGIN = "http://kavita:5000"
HTML_URL = f"{ORIGIN}/api/html/book"
IMG_PATH = "/api/html/img/cover.jpg"
IMG_URL = f"{ORIGIN}{IMG_PATH}"


def _jpeg(width: int = 1400, height: int = 20) -> bytes:
    """A small JPEG wider than the reader's 1024px cap (so it downscales)."""
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (40, 120, 200)).save(buf, format="JPEG")
    return buf.getvalue()


# A benign multi-heading document: an h1 chapter with enough paragraphs to
# paginate, an image referenced by a relative src, an h2 sub-section, and a
# second h1 chapter — so the split produces three chapters and a real ToC.
_LONG_PARA = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 20
_BENIGN_HTML = (
    "<!DOCTYPE html>\n<html><head><title>Ignored Head Title</title>"
    "<style>body{color:red}</style></head><body>\n"
    "<h1>Chapter One</h1>\n"
    + "".join(f"<p>{_LONG_PARA}</p>\n" for _ in range(12))
    + '<img src="img/cover.jpg" alt="a cover">\n'
    "<h2>Section A</h2>\n<p>Section A body text.</p>\n"
    "<h1>Chapter Two</h1>\n<p>The second chapter body.</p>\n"
    "</body></html>\n"
).encode("utf-8")

# Hostile document: script/style/iframe elements, event-handler + style
# attributes, a javascript: link, and a foreign-origin image. Nothing here may
# survive into served HTML, and the foreign image must never be fetched.
_HOSTILE_HTML = (
    "<html><body>\n"
    "<h1>Hostile Chapter</h1>\n"
    "<script>alert('xss')</script>\n"
    "<style>.x{background:url(javascript:evil)}</style>\n"
    '<p onclick="steal()" style="color:red">Visible paragraph '
    '<a href="javascript:evil()">clickme</a></p>\n'
    '<iframe src="http://evil.test/frame"></iframe>\n'
    '<img src="http://evil.test/x.png" onerror="pwn()">\n'
    "<p>Safe trailing paragraph.</p>\n"
    "</body></html>\n"
).encode("utf-8")

# A document that sanitizes to nothing readable (head + script only).
_EMPTY_HTML = (
    "<html><head><title>t</title></head><body><script>x=1</script></body></html>"
).encode("utf-8")


def make_html_handler(html_bytes: bytes, image_bytes: bytes | None = None,
                      calls: list | None = None):
    """Build a mock-transport handler serving *html_bytes* at :data:`HTML_URL`.

    Records ``(host, path)`` for every request in *calls* so tests can assert a
    foreign-origin image was never fetched.
    """
    calls = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.url.host, request.url.path))
        if request.url.host == "kavita" and request.url.path == "/api/html/book":
            async def _html():
                yield html_bytes
            return httpx.Response(200, content=_html(),
                                  headers={"content-type": "text/html; charset=utf-8"})
        if request.url.host == "kavita" and request.url.path == IMG_PATH and image_bytes:
            async def _img():
                yield image_bytes
            return httpx.Response(200, content=_img(),
                                  headers={"content-type": "image/jpeg"})
        return httpx.Response(404, text="nope")

    return handler, calls


def _html_bid(client, *, url: str = HTML_URL, media: str = "text/html",
              title: str = "Web Book", author: str = "A Author") -> str:
    rec = {"u": url, "m": media, "t": title, "a": author, "s": "", "c": None}
    return client.app.state.ids.encode(json.dumps(rec, separators=(",", ":")))


async def _shelve(cache_dir: str, handler, record: dict) -> Manifest:
    """Shelve *record* through a real KavitaClient over *handler*'s transport."""
    cfg = load_config({**ENV, "CACHE_DIR": cache_dir})
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        timeout=httpx.Timeout(connect=5, read=None, write=None, pool=5),
    ) as http:
        kc = KavitaClient(cfg, http)
        return await shelve_html_book(kc, record, cache_dir)


def _record(url: str = HTML_URL, media: str = "text/html") -> dict:
    return {"u": url, "m": media, "t": "Web Book", "a": "A Author"}


# -- shelving: manifest shape, chapters, ToC, image --------------------------


async def test_shelve_html_produces_manifest_and_chapters(tmp_path):
    cache_dir = str(tmp_path / "cache")
    handler, _calls = make_html_handler(_BENIGN_HTML, _jpeg())
    manifest = await _shelve(cache_dir, handler, _record())

    assert isinstance(manifest, Manifest)
    assert manifest.version == 2
    assert manifest.title == "Web Book"
    # h1 "Chapter One", h2 "Section A", h1 "Chapter Two" -> three chapters.
    assert [c.title for c in manifest.chapters] == ["Chapter One", "Section A", "Chapter Two"]
    # ToC carries heading depth (h1 -> 0, h2 -> 1).
    assert [(d, i) for d, _t, i in manifest.toc] == [(0, 0), (1, 1), (0, 2)]
    assert manifest.total_chars == sum(c.chars for c in manifest.chapters)
    # The relative <img src> was fetched, guarded, downscaled and stored.
    assert manifest.images == 1
    blocks = load_chapter(cache_dir, manifest.book_key, 0)
    assert any("{IMG:0}" in b for b in blocks)


async def test_no_headings_is_single_chapter(tmp_path):
    cache_dir = str(tmp_path / "cache")
    html = b"<html><body><p>Just one flat paragraph.</p><p>And another.</p></body></html>"
    handler, _calls = make_html_handler(html)
    manifest = await _shelve(cache_dir, handler, _record())
    assert len(manifest.chapters) == 1
    assert manifest.chapters[0].title == "Web Book"  # falls back to record title


async def test_plain_text_shelves_via_escaped_fallback(tmp_path):
    cache_dir = str(tmp_path / "cache")
    text = b"First paragraph line.\n\nSecond paragraph after a blank line."
    handler, _calls = make_html_handler(text)
    manifest = await _shelve(cache_dir, handler, _record(media="text/plain"))
    assert len(manifest.chapters) == 1
    blocks = load_chapter(cache_dir, manifest.book_key, 0)
    assert any("First paragraph line." in b for b in blocks)
    assert any("Second paragraph" in b for b in blocks)
    # Plain text is escaped, never treated as markup.
    assert all(b.startswith("<p>") for b in blocks)


async def test_messy_html_normalizes_not_escaped(tmp_path):
    # Unclosed <p>, a bare <br>, and HTML entities — the tag-soup a real
    # "Read online" page has. It must normalize to real blocks, never fall
    # back to rendering the raw source as escaped text.
    html = (
        "<html><body>\n<h1>Real Book</h1>\n"
        "<p>Para one with a break<br>and more text\n"
        "<p>Para two, still open\n"
        "<p>Entities: caf&eacute; &amp; sugar &mdash; done\n"
        "</body></html>\n"
    ).encode("utf-8")
    cache_dir = str(tmp_path / "cache")
    handler, _calls = make_html_handler(html)
    manifest = await _shelve(cache_dir, handler, _record())
    blocks = load_chapter(cache_dir, manifest.book_key, 0)
    joined = "".join(blocks)
    # The unclosed paragraphs became three separate <p> blocks (not one nest).
    assert joined.count("<p>") >= 3
    assert "<br" in joined                 # bare <br> preserved as an element
    assert "café" in joined and "sugar" in joined and "—" in joined  # entities decoded
    assert "&lt;p&gt;" not in joined       # not the escaped-source fallback


# -- the critical security test: sanitizer seam holds on hostile HTML --------


async def test_hostile_html_has_no_surviving_markup(tmp_path):
    cache_dir = str(tmp_path / "cache")
    handler, calls = make_html_handler(_HOSTILE_HTML)
    manifest = await _shelve(cache_dir, handler, _record())

    served = "".join(
        "".join(load_chapter(cache_dir, manifest.book_key, i))
        for i in range(len(manifest.chapters))
    )
    # Benign text survives (proving we did not just drop everything).
    assert "Visible paragraph" in served
    assert "Safe trailing paragraph." in served
    assert "clickme" in served  # javascript: link unwrapped to its text
    # Nothing hostile survives the sanitizer seam.
    for forbidden in ("<script", "<iframe", "<style", "onclick", "onerror",
                      "style=", "javascript:", "evil.test"):
        assert forbidden not in served, forbidden
    # The foreign-origin image was refused by the SSRF guard, never fetched.
    assert not any(host == "evil.test" for host, _path in calls)
    assert manifest.images == 0


async def test_same_origin_image_fetched_foreign_dropped(tmp_path):
    # One same-origin relative image plus one foreign absolute image.
    html = (
        "<html><body><h1>C</h1>"
        '<img src="img/cover.jpg" alt="ok">'
        '<img src="http://evil.test/x.png" alt="bad">'
        "<p>Body.</p></body></html>"
    ).encode("utf-8")
    cache_dir = str(tmp_path / "cache")
    handler, calls = make_html_handler(html, _jpeg())
    manifest = await _shelve(cache_dir, handler, _record())
    assert manifest.images == 1  # only the same-origin image
    assert any(path == IMG_PATH for _host, path in calls)      # it was fetched
    assert not any(host == "evil.test" for host, _path in calls)  # foreign never touched


# -- friendly failures (never a raw 500) -------------------------------------


async def test_oversized_html_raises_reader_error(tmp_path, monkeypatch):
    import app.reader as reader
    monkeypatch.setattr(reader, "MAX_HTML_BYTES", 64)  # smaller than the doc
    cache_dir = str(tmp_path / "cache")
    handler, _calls = make_html_handler(_BENIGN_HTML)
    from app.errors import ReaderError
    try:
        await _shelve(cache_dir, handler, _record())
        raised = False
    except ReaderError:
        raised = True
    assert raised


async def test_no_readable_text_raises_reader_error(tmp_path):
    cache_dir = str(tmp_path / "cache")
    handler, _calls = make_html_handler(_EMPTY_HTML)
    from app.errors import ReaderError
    try:
        await _shelve(cache_dir, handler, _record())
        raised = False
    except ReaderError as exc:
        raised = "no readable text" in str(exc)
    assert raised


# -- routes: book page, read flow, pagination, search, bookmarks -------------


def test_book_page_offers_read_here_and_no_download(tmp_path):
    handler, _calls = make_html_handler(_BENIGN_HTML, _jpeg())
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _html_bid(client)
        r = client.get(f"/book/{bid}")
        assert r.status_code == 200
        assert f"/read/{bid}" in r.text          # "Read here" reader button
        assert "Open in iBooks" not in r.text     # HTML has no iBooks download
        assert "badge-html" in r.text             # HTML format badge


def test_first_open_shelves_and_serves_a_part(tmp_path):
    handler, calls = make_html_handler(_BENIGN_HTML, _jpeg())
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _html_bid(client)
        r = client.get(f"/read/{bid}", follow_redirects=False)
        assert r.status_code == 303
        assert calls  # shelving fetched the upstream document
        part = client.get(r.headers["location"])
        assert part.status_code == 200
        assert "Chapter One" in part.text
        for forbidden in ("<script", "onclick", "onerror"):
            assert forbidden not in part.text


def test_toc_lists_all_chapters(tmp_path):
    handler, _calls = make_html_handler(_BENIGN_HTML, _jpeg())
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _html_bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelve
        toc = client.get(f"/read/{bid}/toc")
        assert toc.status_code == 200
        for title in ("Chapter One", "Section A", "Chapter Two"):
            assert title in toc.text


def test_split_size_changes_part_count(tmp_path):
    handler, _calls = make_html_handler(_BENIGN_HTML, _jpeg())
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _html_bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelve

        # "whole" collapses chapter 0 into a single part, so part 2 is 404.
        client.cookies.set("rs_split", "whole")
        assert client.get(f"/read/{bid}/0/1").status_code == 200
        assert client.get(f"/read/{bid}/0/2").status_code == 404

        # "small" (6000 chars) splits the long chapter, so part 2 exists.
        client.cookies.set("rs_split", "small")
        assert client.get(f"/read/{bid}/0/2").status_code == 200


def test_search_finds_text_in_html_book(tmp_path):
    handler, _calls = make_html_handler(_BENIGN_HTML, _jpeg())
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _html_bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelve
        r = client.get(f"/read/{bid}/find", params={"q": "second chapter"})
        assert r.status_code == 200
        assert f"/read/{bid}/" in r.text  # a hit links into the book


def test_bookmark_roundtrip_on_html_book(tmp_path):
    handler, _calls = make_html_handler(_BENIGN_HTML, _jpeg())
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _html_bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelve
        token = client.app.state.ids.site_token
        add = client.get(f"/read/{bid}/bookmark",
                         params={"chapter": 0, "block": 0, "part": 1, "t": token},
                         follow_redirects=False)
        assert add.status_code == 303
        marks = client.get(f"/read/{bid}/bookmarks")
        assert marks.status_code == 200
        assert "Chapter One" in marks.text


def test_reading_an_html_part_populates_home_shelf(tmp_path):
    handler, _calls = make_html_handler(_BENIGN_HTML, _jpeg())
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _html_bid(client)
        # Read a part: this records a server-side reading position.
        r = client.get(f"/read/{bid}", follow_redirects=False)
        client.get(r.headers["location"])
        # The home "Currently Reading" shelf now lists the book, and its link
        # decodes back to a readable HTML record (dispatches, not a 404).
        home = client.get("/")
        assert "Web Book" in home.text
        assert "/read/" in home.text


def test_image_route_serves_shelved_html_image(tmp_path):
    handler, _calls = make_html_handler(_BENIGN_HTML, _jpeg())
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _html_bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelve
        img = client.get(f"/read/{bid}/img/0")
        assert img.status_code == 200
        assert img.headers["content-type"].startswith("image/")


# -- normalizer hardening: depth caps and end-tag handling --------------------


async def test_deep_div_nest_content_survives(tmp_path):
    # 3000 nested <div>s: past the normalizer's depth cap the wrappers are
    # unwrapped (text keeps flowing at the cap depth), so re-serializing and
    # sanitizing the tree never recurses past Python's limit — and because the
    # cap equals the sanitizer's render cap, the deep text still renders.
    cache_dir = str(tmp_path / "cache")
    depth = 3000
    page = "<html><body>" + "<div>" * depth + "deep text" + "</div>" * depth + "</body></html>"
    handler, _calls = make_html_handler(page.encode("utf-8"), b"")
    manifest = await _shelve(cache_dir, handler, _record())
    texts = []
    for i in range(len(manifest.chapters)):
        texts.extend(load_chapter(cache_dir, manifest.book_key, i))
    assert any("deep text" in b for b in texts)


def test_normalizer_stray_and_mismatched_end_tags():
    from app.reader import _normalize_html
    # A stray </b> with nothing open is ignored; <b><i>x</b> closes both.
    xhtml, _srcs = _normalize_html("</b><p>a</p><b><i>x</b>tail")
    assert xhtml.startswith("<body>")
    assert xhtml.endswith("</body>")
    assert "<p>a</p>" in xhtml
    # Balanced: every <i>/<b> opened is closed before </body>.
    assert xhtml.count("<b>") == xhtml.count("</b>")
    assert xhtml.count("<i>") == xhtml.count("</i>")


def test_normalizer_many_end_tags_stay_fast():
    # Regression guard for the O(depth) per-end-tag stack scan: thousands of
    # nested opens followed by as many closes must normalize promptly.
    import time
    from app.reader import _normalize_html
    n = 20_000
    source = "<b>" * n + "x" + "</b>" * n
    start = time.monotonic()
    xhtml, _srcs = _normalize_html(source)
    elapsed = time.monotonic() - start
    assert "x" in xhtml
    assert elapsed < 5.0  # quadratic behaviour took minutes here
