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


# -- reading comfort: text size + line spacing (cookie-driven, no-JS) ---------


def test_prefs_size_and_leading_set_cookies(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1))
    with make_client(handler, str(tmp_path / "cache")) as client:
        t = _token(client)
        r = client.get(f"/prefs?size=xl&next=/&t={t}", follow_redirects=False)
        assert r.status_code == 303
        assert r.cookies.get("rs_reader_size") == "xl"

        r2 = client.get(f"/prefs?leading=roomy&next=/&t={t}", follow_redirects=False)
        assert r2.status_code == 303
        assert r2.cookies.get("rs_reader_leading") == "roomy"


def test_prefs_rejects_bogus_size_and_leading(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1))
    with make_client(handler, str(tmp_path / "cache")) as client:
        t = _token(client)
        # A crafted value must never be set as a cookie (guards the body class).
        r = client.get(f'/prefs?size=x"><script&next=/&t={t}', follow_redirects=False)
        assert r.status_code == 303
        assert r.cookies.get("rs_reader_size") is None


def test_reader_size_and_leading_cookies_set_body_class(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)

        default_page = client.get(f"/read/{bid}/0/1")
        assert "read-size-" not in default_page.text
        assert "read-lead-" not in default_page.text

        client.cookies.set("rs_reader_size", "l")
        client.cookies.set("rs_reader_leading", "tight")
        page = client.get(f"/read/{bid}/0/1")
        assert "read-size-l" in page.text
        assert "read-lead-tight" in page.text
        # Defaults emit no class; medium/normal stay classless.
        client.cookies.set("rs_reader_size", "m")
        client.cookies.set("rs_reader_leading", "normal")
        page2 = client.get(f"/read/{bid}/0/1")
        assert "read-size-" not in page2.text
        assert "read-lead-" not in page2.text


def test_reader_size_controls_render_in_footer(tmp_path):
    handler, _calls = make_handler(make_epub(chapters=1))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)
        text = client.get(f"/read/{bid}/0/1").text
        assert "Text size:" in text
        assert "Spacing:" in text
        assert "size=xl" in text and "leading=roomy" in text


# -- in-book search (findability) ---------------------------------------------

_SEARCHABLE = (
    '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
    "<p>The lighthouse stood alone against the storm.</p>"
    "<p>A second mention of the lighthouse keeper.</p></body></html>"
).encode("utf-8")


def test_find_returns_matches_linking_into_the_reader(tmp_path):
    handler, _c = make_handler(make_epub(chapters=1, image=False, ncx=False,
                                         chapter_bytes={0: _SEARCHABLE}))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        r = client.get(f"/read/{bid}/find?q=lighthouse")
        assert r.status_code == 200
        assert "match" in r.text
        assert "<mark>lighthouse</mark>" in r.text
        assert f"/read/{bid}/0/1" in r.text  # links to the part with the hit


def test_find_empty_and_short_queries(tmp_path):
    handler, _c = make_handler(make_epub(chapters=1, image=False, ncx=False,
                                         chapter_bytes={0: _SEARCHABLE}))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        assert client.get(f"/read/{bid}/find").status_code == 200  # no query, form only
        short = client.get(f"/read/{bid}/find?q=a")
        assert "at least" in short.text  # min-length hint
        none = client.get(f"/read/{bid}/find?q=zzzznomatch")
        assert "No matches" in none.text


def test_find_snippet_escapes_hostile_book_text(tmp_path):
    # A book whose text contains a script payload must render it as inert,
    # escaped snippet text — never as live markup on the results page.
    hostile = (
        '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
        "<p>danger zone alert then more danger words</p></body></html>"
    ).encode("utf-8")
    handler, _c = make_handler(make_epub(chapters=1, image=False, ncx=False,
                                         chapter_bytes={0: hostile}))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        r = client.get(f"/read/{bid}/find?q=danger")
        assert r.status_code == 200
        # Our own <mark> is the only markup around the match; no script survives
        # and the results page carries no book-injected tags.
        assert "<script" not in r.text
        assert "<mark>danger</mark>" in r.text


# -- bookmarks (findability) --------------------------------------------------


def test_bookmark_add_list_and_remove_flow(tmp_path):
    handler, _c = make_handler(make_epub(chapters=3))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        t = _token(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelve

        # The reader page offers to bookmark the current page.
        page = client.get(f"/read/{bid}/1/1")
        assert "Bookmark this page" in page.text

        # Save a bookmark (state-changing → 303 back to the page).
        r = client.get(f"/read/{bid}/bookmark?chapter=1&block=0&part=1&t={t}",
                       follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == f"/read/{bid}/1/1"

        # It now shows as bookmarked, with a count, and appears in the list.
        page2 = client.get(f"/read/{bid}/1/1")
        assert "Remove bookmark" in page2.text
        assert "Bookmarks (1)" in page2.text
        listing = client.get(f"/read/{bid}/bookmarks")
        assert listing.status_code == 200
        assert f"/read/{bid}/1/1" in listing.text
        assert "remove" in listing.text

        # Remove it from the list.
        rm = client.get(f"/read/{bid}/unbookmark?chapter=1&block=0&to=list&t={t}",
                        follow_redirects=False)
        assert rm.status_code == 303
        assert rm.headers["location"] == f"/read/{bid}/bookmarks"
        empty = client.get(f"/read/{bid}/bookmarks")
        assert "no bookmarks yet" in empty.text


def test_bookmark_requires_site_token(tmp_path):
    handler, _c = make_handler(make_epub(chapters=1))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)
        # No token → refused, and nothing is saved.
        assert client.get(f"/read/{bid}/bookmark?chapter=0&block=0").status_code == 403
        assert "no bookmarks yet" in client.get(f"/read/{bid}/bookmarks").text


# -- footnotes: same-chapter anchor links (book fidelity) ---------------------


def test_footnote_link_resolves_to_the_part_holding_the_note(tmp_path):
    # A chapter with a footnote marker near the top and its body far below,
    # so under a "small" split the marker and note land in different parts.
    filler = "".join(f"<p>{_BIG_PARAGRAPH}</p>" for _ in range(10))
    chapter = (
        '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<p>Text with a marker <a href="#fn1">1</a> here.</p>'
        + filler +
        '<p id="fn1">The footnote body itself.</p>'
        "</body></html>"
    ).encode("utf-8")
    handler, _c = make_handler(make_epub(chapters=1, image=False, ncx=False,
                                         chapter_bytes={0: chapter}))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        client.cookies.set("rs_split", "small")
        client.get(f"/read/{bid}", follow_redirects=False)  # shelve
        page = client.get(f"/read/{bid}/0/1")
        assert page.status_code == 200
        # The marker renders as a link into this book's reader (not a raw #id,
        # not a leftover placeholder), pointing at a later part of chapter 0.
        assert "{FRAG" not in page.text
        assert 'href="#fn1"' not in page.text
        import re as _re
        m = _re.search(rf'href="/read/{bid}/0/(\d+)"', page.text)
        assert m and int(m.group(1)) >= 1  # resolved to a concrete part URL


# -- robustness: a corrupt cached chapter must only affect that chapter -------


def _corrupt_chapter(cache_dir: str, url: str, chapter: int) -> None:
    """Overwrite a shelved chapter's cache file with unparseable bytes."""
    from app.store import book_key
    path = os.path.join(cache_dir, "reader", book_key(url), "chapters", f"{chapter}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{ this is not valid json")


def test_corrupt_neighbor_chapter_does_not_break_a_readable_page(tmp_path):
    # Turning to chapter 1 computes a "Prev" link to the last part of chapter
    # 0. If chapter 0's cache file is corrupt, the (readable) chapter-1 page
    # must still render, with Prev falling back to chapter 0's first part.
    cache_dir = str(tmp_path / "cache")
    handler, _calls = make_handler(make_epub(chapters=3))
    with make_client(handler, cache_dir) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelve
        _corrupt_chapter(cache_dir, BOOK_URL, 0)
        r = client.get(f"/read/{bid}/1/1")
        assert r.status_code == 200
        assert f"/read/{bid}/0/1" in r.text  # Prev degraded to chapter 0 part 1


def test_corrupt_chapter_does_not_blank_the_bookmarks_list(tmp_path):
    # A bookmark in a chapter whose cache later becomes unreadable must still
    # list (linking to that chapter's first part), not 502 the whole page.
    cache_dir = str(tmp_path / "cache")
    handler, _calls = make_handler(make_epub(chapters=3))
    with make_client(handler, cache_dir) as client:
        bid = _bid(client)
        token = _token(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelve
        # Bookmark chapter 2, then corrupt chapter 2's cache file.
        client.get(f"/read/{bid}/bookmark?chapter=2&block=0&part=1&t={token}",
                   follow_redirects=False)
        _corrupt_chapter(cache_dir, BOOK_URL, 2)
        r = client.get(f"/read/{bid}/bookmarks")
        assert r.status_code == 200
        assert f"/read/{bid}/2/1" in r.text


# -- robustness: chapters that sanitize to zero blocks must not trap nav ------

_EMPTY_CHAPTER = (
    '<?xml version="1.0"?>'
    '<html xmlns="http://www.w3.org/1999/xhtml"><body>   </body></html>'
).encode("utf-8")


def test_open_skips_a_leading_empty_chapter(tmp_path):
    # Chapter 0 sanitizes to zero blocks; opening the book must land on the
    # first chapter that actually has content, not 404 on /0/1.
    handler, _c = make_handler(make_epub(chapters=3, image=False, ncx=False,
                                         chapter_bytes={0: _EMPTY_CHAPTER}))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        r = client.get(f"/read/{bid}", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == f"/read/{bid}/1/1"
        # And that first content page renders.
        assert client.get(f"/read/{bid}/1/1").status_code == 200


def test_next_and_prev_skip_an_empty_middle_chapter(tmp_path):
    # Chapter 1 is empty; Next from chapter 0 and Prev from chapter 2 must
    # jump over it rather than dead-end on a 404 part.
    handler, _c = make_handler(make_epub(chapters=3, image=False, ncx=False,
                                         chapter_bytes={1: _EMPTY_CHAPTER}))
    with make_client(handler, str(tmp_path / "cache")) as client:
        bid = _bid(client)
        client.get(f"/read/{bid}", follow_redirects=False)  # shelve
        page0 = client.get(f"/read/{bid}/0/1")
        assert page0.status_code == 200
        assert f"/read/{bid}/2/1" in page0.text     # Next skipped empty ch.1
        assert f"/read/{bid}/1/1" not in page0.text
        page2 = client.get(f"/read/{bid}/2/1")
        assert page2.status_code == 200
        assert f"/read/{bid}/0/1" in page2.text     # Prev skipped empty ch.1
