"""Integration tests for the in-browser EPUB reader routes (SS-04).

Kavita is mocked at the httpx transport layer, and the EPUB fixture is built
in-memory with :func:`tests.test_reader_shelve.make_epub` — no committed
binary EPUB fixtures, mirroring ``tests/test_app.py``'s harness style.
"""
from __future__ import annotations

import json
import os
import tempfile
from contextlib import asynccontextmanager

import httpx
from fastapi.testclient import TestClient

from app.config import load_config
from app.ids import IdCodec
from app.kavita import KavitaClient
from app.main import FeedCache, create_app
from app.store import Store
from tests.test_reader_shelve import make_epub

BOOK_URL = "http://kavita:5000/api/download/9001/book.epub"
PDF_URL = "http://kavita:5000/api/download/9002/book.pdf"

ENV = {
    "KAVITA_OPDS_URL": "http://kavita:5000/api/opds/SECRETKEY",
    "BRIDGE_ID_SECRET": "test-secret",
}

# A chapter with many modest paragraphs, long enough that a "small" split
# (6000 target chars) produces several parts while "whole" produces one.
_BIG_PARAGRAPH = "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 20
_BIG_CHAPTER = (
    '<?xml version="1.0"?>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml"><body>\n'
    + "".join(f"<p>{_BIG_PARAGRAPH}</p>\n" for _ in range(20))
    + "</body></html>\n"
).encode("utf-8")

# A chapter that sanitizes to exactly one block, so viewing its only part
# lands on the manifest's true final block (percent_of's 100% condition).
_ONE_BLOCK_CHAPTER = (
    '<?xml version="1.0"?>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
    "<p>The only block in this chapter.</p>"
    "</body></html>\n"
).encode("utf-8")


def _test_store() -> Store:
    """A fresh, isolated Store backed by a unique temp file (no cross-test bleed)."""
    return Store(os.path.join(tempfile.mkdtemp(), "state.json"))


def make_handler(epub_bytes: bytes, calls: list | None = None):
    """Build a mock-transport handler serving *epub_bytes* at ``BOOK_URL``.

    :param calls: Optional list; a request's path is appended to it every
        time the handler is invoked, so tests can assert on upstream call
        counts.
    """
    calls = calls if calls is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/download/9001/book.epub":
            async def _epub_stream():
                yield epub_bytes
            return httpx.Response(200, content=_epub_stream())
        if request.url.path == "/api/download/9002/book.pdf":
            async def _pdf_stream():
                yield b"%PDF-1.4 fake"
            return httpx.Response(200, content=_pdf_stream())
        return httpx.Response(404, text="nope")

    return handler, calls


def make_client(handler, cache_dir: str, extra_env: dict | None = None) -> TestClient:
    cfg = load_config({**ENV, **(extra_env or {}), "CACHE_DIR": cache_dir})
    app = create_app(cfg)
    transport = httpx.MockTransport(handler)

    def _override_lifespan():
        @asynccontextmanager
        async def ls(a):
            http = httpx.AsyncClient(
                transport=transport,
                timeout=httpx.Timeout(connect=5, read=None, write=None, pool=5),
            )
            a.state.http = http
            a.state.kavita = KavitaClient(cfg, http)
            a.state.ids = IdCodec(cfg.bridge_id_secret)
            a.state.cache = FeedCache(cfg.cache_feeds_seconds)
            a.state.store = _test_store()
            try:
                yield
            finally:
                await http.aclose()

        return ls

    app.router.lifespan_context = _override_lifespan()
    return TestClient(app)


def _bid(client: TestClient, *, url: str = BOOK_URL, media: str = "application/epub+zip",
         title: str = "Test Book", author: str = "Test Author") -> str:
    """Encode a book bridge id the same shape ``/book/{bid}`` mints."""
    rec = {"u": url, "m": media, "t": title, "a": author, "s": "", "c": None}
    return client.app.state.ids.encode(json.dumps(rec, separators=(",", ":")))


def _token(client: TestClient) -> str:
    return client.app.state.ids.site_token


# -- happy path: first open shelves, redirects, serves a part ----------------


def test_first_open_shelves_and_redirects_to_a_part(tmp_path):
    handler, calls = make_handler(make_epub(chapters=3))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        r = client.get(f"/read/{bid}", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == f"/read/{bid}/0/1"
        assert calls  # shelving fetched the upstream book


def test_part_page_has_text_prev_next_and_no_script_or_inline_handlers(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=3))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        r = client.get(f"/read/{bid}/0/1")
        assert r.status_code == 200
        text = r.text
        assert "Body text for chapter 0" in text
        assert "<script" not in text
        # No event-handler attributes survived the sanitizer into the page.
        # (The old `" on" ... or "onclick=" ...` form was a tautology — the
        # right operand was always true, so it could never fail. [SS-04 review])
        low = text.lower()
        assert "onclick=" not in low
        assert "onerror=" not in low
        assert "onload=" not in low
        assert 'style="' not in text
        # single-part chapter: no Prev (chapter 0), Next -> chapter 1
        assert "/read/{}/1/1".format(bid) in text


def test_second_part_request_makes_zero_upstream_calls(tmp_path):
    handler, calls = make_handler(make_epub(chapters=3))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelves
        n_calls_after_shelve = len(calls)
        r1 = client.get(f"/read/{bid}/0/1")
        r2 = client.get(f"/read/{bid}/1/1")
        assert r1.status_code == 200
        assert r2.status_code == 200
        assert len(calls) == n_calls_after_shelve  # no re-fetch of upstream


def test_toc_lists_chapters_and_links_into_reader(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=3))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)
        r = client.get(f"/read/{bid}/toc")
        assert r.status_code == 200
        assert "| safe" not in r.text  # sanity: no raw jinja leaked
        assert f"/read/{bid}/0/1" in r.text
        assert f"/read/{bid}/1/1" in r.text
        assert f"/read/{bid}/2/1" in r.text


# -- split sizes and resume ---------------------------------------------------


def test_split_small_vs_whole_yield_different_part_counts(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1, chapter_bytes={0: _BIG_CHAPTER}))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelves

        client.cookies.set("rs_split", "whole")
        whole_text = client.get(f"/read/{bid}/0/1").text
        assert "part 1 of 1" in whole_text
        r = client.get(f"/read/{bid}/0/2")
        assert r.status_code == 404  # only one part exists under "whole"

        client.cookies.set("rs_split", "small")
        small_text = client.get(f"/read/{bid}/0/1").text
        assert "part 1 of" in small_text
        assert "part 1 of 1" not in small_text  # more than one part now
        r2 = client.get(f"/read/{bid}/0/2")
        assert r2.status_code == 200


def test_resume_position_survives_a_split_size_change(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1, chapter_bytes={0: _BIG_CHAPTER}))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelves

        client.cookies.set("rs_split", "small")
        client.get(f"/read/{bid}/0/2")  # move position into part 2

        # Switch to "whole": there is only ever 1 part, so resume lands there.
        client.cookies.set("rs_split", "whole")
        r = client.get(f"/read/{bid}", follow_redirects=False)
        assert r.headers["location"] == f"/read/{bid}/0/1"

        # Switch back to "small": resume should land back in part 2, since
        # the stored block index is unchanged and part_containing regroups
        # the same blocks the same way.
        client.cookies.set("rs_split", "small")
        r2 = client.get(f"/read/{bid}", follow_redirects=False)
        assert r2.headers["location"] == f"/read/{bid}/0/2"


# -- friendly errors -----------------------------------------------------


def test_pdf_record_bid_is_friendly_404(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client, url=PDF_URL, media="application/pdf")
        r = client.get(f"/read/{bid}")
        assert r.status_code == 404
        assert "Only EPUB books" in r.text


def test_drm_fixture_returns_502_reader_error_page(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1, encryption=True))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        r = client.get(f"/read/{bid}")
        assert r.status_code == 502
        assert "iBooks" in r.text


def test_out_of_range_chapter_and_part_are_404(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=2))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelves
        assert client.get(f"/read/{bid}/99/1").status_code == 404
        assert client.get(f"/read/{bid}/0/99").status_code == 404


# -- images ----------------------------------------------------------------


def test_image_route_serves_shelved_image(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1, image=True))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelves
        r = client.get(f"/read/{bid}/img/0")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/")
        assert r.headers["cache-control"] == "private, max-age=86400"


def test_missing_image_index_returns_404_gif(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1, image=True))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelves
        r = client.get(f"/read/{bid}/img/99")
        assert r.status_code == 404
        assert r.headers["content-type"] == "image/gif"


# -- access-key gate -----------------------------------------------------


def test_access_key_required_for_reader_route(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1))
    with make_client(handler, str(tmp_path / "cache"),
                      extra_env={"BRIDGE_ACCESS_KEY": "letmein"}) as client:
        bid = _bid(client)
        r = client.get(f"/read/{bid}")
        assert r.status_code == 403


# -- prefs cookies (split=/reader=) ---------------------------------------


def test_prefs_split_and_reader_theme_cookies(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1))
    with make_client(handler, str(tmp_path / "cache")) as client:
        t = _token(client)
        r = client.get(f"/prefs?split=large&next=/&t={t}", follow_redirects=False)
        assert r.status_code == 303
        assert r.cookies.get("rs_split") == "large"

        r2 = client.get(f"/prefs?reader=phosphor&next=/&t={t}", follow_redirects=False)
        assert r2.status_code == 303
        assert r2.cookies.get("rs_reader_theme") == "phosphor"


def test_prefs_split_requires_site_token(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1))
    with make_client(handler, str(tmp_path / "cache")) as client:
        r = client.get("/prefs?split=large&next=/")
        assert r.status_code == 403


# -- UI integration: book/home pages + reader themes (SS-05) -----------------


def test_book_page_epub_has_read_link_and_first_open_hint(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        r = client.get(f"/book/{bid}")
        assert r.status_code == 200
        assert f"/read/{bid}" in r.text
        assert "Read here" in r.text
        assert "first open takes a moment" in r.text


def test_book_page_pdf_has_no_read_link(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client, url=PDF_URL, media="application/pdf")
        r = client.get(f"/book/{bid}")
        assert r.status_code == 200
        assert "/read/" not in r.text


def test_book_and_home_show_continue_reading_after_a_part_view(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=3))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=True)  # shelves + reads Ch1/Part1

        book_page = client.get(f"/book/{bid}")
        assert "Continue reading" in book_page.text
        assert "Ch. 1" in book_page.text

        home_page = client.get("/")
        assert "Currently Reading" in home_page.text
        assert "Test Book" in home_page.text


def test_book_page_shows_finished_after_last_part(tmp_path):
    handler, _calls = make_handler(
        make_epub(chapters=1, image=False, chapter_bytes={0: _ONE_BLOCK_CHAPTER})
    )
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        # Single chapter with a single block: its only part is both the last
        # part of the chapter and the manifest's final block, so it records
        # 100% ("finished").
        client.get(f"/read/{bid}", follow_redirects=True)
        r = client.get(f"/book/{bid}")
        assert "Read again" in r.text
        assert "finished" in r.text


def test_reader_theme_phosphor_cookie_sets_body_class(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)

        default_page = client.get(f"/read/{bid}/0/1")
        assert "reader-phosphor" not in default_page.text

        client.cookies.set("rs_reader_theme", "phosphor")
        phosphor_page = client.get(f"/read/{bid}/0/1")
        assert "reader-phosphor" in phosphor_page.text


def test_reader_honors_large_print_cookie(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)
        client.cookies.set("rs_big", "1")
        r = client.get(f"/read/{bid}/0/1")
        assert '<body class="' in r.text
        assert " big" in r.text
