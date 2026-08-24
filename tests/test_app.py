"""Integration tests for app.main — routes, wiring, error handling, no key leak.

Kavita is mocked at the httpx transport layer so no real server is needed.
"""
import pathlib

import httpx
from fastapi.testclient import TestClient

import os
import tempfile

from app.config import load_config
from app.ids import IdCodec
from app.kavita import KavitaClient
from app.main import create_app
from app.store import Store


def _token(client) -> str:
    """The site token that authorises /star, /unstar and /prefs. [SS-15]"""
    return client.app.state.ids.site_token


def _test_store() -> Store:
    """A fresh, isolated Store backed by a unique temp file (no cross-test bleed)."""
    return Store(os.path.join(tempfile.mkdtemp(), "state.json"))

FIX = pathlib.Path(__file__).parent / "fixtures"
ROOT_XML = (FIX / "opds_root.xml").read_text(encoding="utf-8")
ACQ_XML = (FIX / "opds_acquisition.xml").read_text(encoding="utf-8")

ENV = {
    "KAVITA_OPDS_URL": "http://kavita:5000/api/opds/SECRETKEY",
    "BRIDGE_ID_SECRET": "test-secret",
}


def make_handler(routes=None, fail=False):
    routes = routes or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if fail:
            raise httpx.ConnectError("kavita down")
        path = request.url.path
        if path == "/api/opds/SECRETKEY":
            return httpx.Response(200, text=ROOT_XML)
        if path.endswith("/recently-added") or "recently-added" in path:
            return httpx.Response(200, text=ACQ_XML)
        if "/search" in path:
            return httpx.Response(200, text=ACQ_XML)
        if "/download/" in path:
            async def _book_stream():
                yield b"PK\x03\x04BOOKBYTES"
            return httpx.Response(200, content=_book_stream())
        if "/api/image" in path:
            async def _img_stream():
                yield b"\x89PNG"
            return httpx.Response(200, content=_img_stream(), headers={"Content-Type": "image/png"})
        return httpx.Response(404, text="nope")
    return handler


def make_client(handler) -> TestClient:
    cfg = load_config(ENV)
    app = create_app(cfg)
    # Inject a mocked KavitaClient/ids/cache via a custom lifespan replacement.
    transport = httpx.MockTransport(handler)

    def _override_lifespan():
        from contextlib import asynccontextmanager
        from app.main import FeedCache

        @asynccontextmanager
        async def ls(a):
            http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(connect=5, read=None, write=None, pool=5))
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


def test_health_is_plain_text_ok():
    client = make_client(make_handler())
    r = client.get("/health")
    assert r.status_code == 200
    assert r.text == "ok"
    assert r.headers["content-type"].startswith("text/plain")


def test_home_renders_and_links_root_feed():
    with make_client(make_handler()) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "RetroShelf" in r.text
        assert "/feed/" in r.text
        assert "Connected" in r.text
        # No apiKey leak anywhere.
        assert "SECRETKEY" not in r.text and "/api/opds/" not in r.text


def test_home_when_kavita_down_shows_status_not_500():
    with make_client(make_handler(fail=True)) as client:
        r = client.get("/")
        assert r.status_code == 200
        assert "Cannot reach the library server" in r.text


def test_feed_browse_and_book_links_are_bridge_ids():
    with make_client(make_handler()) as client:
        # Discover the root feed id from home.
        home = client.get("/").text
        import re
        fid = re.search(r'/feed/([A-Za-z0-9_\-.]+)', home).group(1)
        r = client.get(f"/feed/{fid}")
        assert r.status_code == 200
        assert "RetroShelf Library" in r.text
        assert "SECRETKEY" not in r.text and "/api/opds/" not in r.text
        assert "/feed/" in r.text  # nav entries


def test_sorted_page_helper_orders_books_keeps_nav_first():
    from app.main import _sorted_page
    entries = [
        {"is_nav": True, "title": "Browse"},
        {"is_nav": False, "title": "Zebra", "author": "Young, A", "badge": "PDF"},
        {"is_nav": False, "title": "alpha", "author": "", "badge": "EPUB"},
        {"is_nav": False, "title": "Mango", "author": "Brown, B", "badge": "EPUB"},
    ]
    # default / unknown → unchanged (upstream order)
    assert _sorted_page(entries, "") == entries
    assert _sorted_page(entries, "bogus") == entries
    # title: nav first, then case-insensitive title order
    titles = [e["title"] for e in _sorted_page(entries, "title")]
    assert titles == ["Browse", "alpha", "Mango", "Zebra"]
    # author: empty author sorts last, nav still first
    authors = [e.get("title") for e in _sorted_page(entries, "author")]
    assert authors == ["Browse", "Mango", "Zebra", "alpha"]
    # format: EPUB before PDF, nav first
    fmts = [e["title"] for e in _sorted_page(entries, "format")]
    assert fmts[0] == "Browse" and fmts[-1] == "Zebra"  # PDF last
    # never drops/dupes rows
    assert len(_sorted_page(entries, "title")) == len(entries)


def test_feed_current_page_sort_and_control():
    import re
    with make_client(make_handler()) as client:
        home = client.get("/").text
        fid = re.search(r'/feed/([A-Za-z0-9_\-.]+)', home).group(1)
        root_feed = client.get(f"/feed/{fid}").text
        # Find the acquisition page (the one with both books).
        books_fid = None
        for fid2 in re.findall(r'/feed/([A-Za-z0-9_\-.]+)"', root_feed):
            page = client.get(f"/feed/{fid2}").text
            if "The Time Machine" in page and "Annual Report 2025" in page:
                books_fid = fid2
                break
        assert books_fid, "expected an acquisition page with both books"

        def order(html):
            return (html.index("Annual Report 2025"), html.index("The Time Machine"))

        # Default: upstream order — Time Machine before Annual Report.
        default = client.get(f"/feed/{books_fid}").text
        ar, tm = order(default)
        assert tm < ar
        # The sort control renders with links preserving the feed path.
        assert 'class="sortbar"' in default
        assert f"/feed/{books_fid}?sort=title" in default
        assert f"/feed/{books_fid}?sort=author" in default
        # sort=author: Acme Corp before H. G. Wells → Annual Report first.
        by_author = client.get(f"/feed/{books_fid}?sort=author").text
        ar, tm = order(by_author)
        assert ar < tm
        assert "sorted: this page" in by_author
        assert 'class="sortopt on"' in by_author   # active option marked
        # bogus sort falls back to upstream order.
        bogus = client.get(f"/feed/{books_fid}?sort=bogus").text
        ar, tm = order(bogus)
        assert tm < ar


def test_search_inputs_have_ios_keyboard_hints():
    # SS-04: the q input must suppress iOS auto-correct/auto-capitalize so it
    # doesn't mangle titles/authors. Rendered on home, search, and feed pages.
    import re
    with make_client(make_handler()) as client:
        home = client.get("/").text
        assert 'autocorrect="off"' in home and 'autocapitalize="off"' in home
        srch = client.get("/search?q=").text
        assert 'autocorrect="off"' in srch and 'autocapitalize="off"' in srch
        fid = re.search(r'/feed/([A-Za-z0-9_\-.]+)', home).group(1)
        feed = client.get(f"/feed/{fid}").text
        assert 'autocorrect="off"' in feed and 'autocapitalize="off"' in feed


def test_static_has_long_cache_and_is_not_gzipped():
    # SS-04: /static gets a one-week cache; CSS (non-HTML) is never gzipped.
    with make_client(make_handler()) as client:
        r = client.get("/static/app.css", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert r.headers["cache-control"] == "public, max-age=604800"
        assert "Accept-Encoding" not in r.headers.get("vary", "")


def test_html_pages_gzipped_when_accepted():
    # SS-04: HTML responses are gzipped (Vary: Accept-Encoding is set only by the
    # gzip branch; httpx transparently decodes the body).
    with make_client(make_handler()) as client:
        r = client.get("/", headers={"Accept-Encoding": "gzip"})
        assert r.status_code == 200
        assert "Accept-Encoding" in r.headers.get("vary", "")
        assert "RetroShelf" in r.text


def test_download_stream_is_never_gzipped():
    # SS-04: streaming proxy responses must not be gzipped/buffered.
    import re
    with make_client(make_handler()) as client:
        home = client.get("/").text
        fid = re.search(r'/feed/([A-Za-z0-9_\-.]+)', home).group(1)
        root = client.get(f"/feed/{fid}").text
        did = fname = None
        for f2 in re.findall(r'/feed/([A-Za-z0-9_\-.]+)"', root):
            detail_link = re.search(r'/book/([A-Za-z0-9_\-.]+)"', client.get(f"/feed/{f2}").text)
            if detail_link:
                dm = re.search(r'/download/([A-Za-z0-9_\-.]+)/([^"]+)',
                               client.get(f"/book/{detail_link.group(1)}").text)
                if dm:
                    did, fname = dm.group(1), dm.group(2)
                    break
        assert did, "expected a download link"
        r = client.get(f"/download/{did}/{fname}", headers={"Accept-Encoding": "gzip"})
        assert r.headers.get("content-encoding") != "gzip"
        assert "Accept-Encoding" not in r.headers.get("vary", "")


def test_big_mode_enlarges_tap_targets():
    import pathlib
    css = (pathlib.Path(__file__).resolve().parent.parent / "app" / "static" / "app.css").read_text()
    assert "body.big .menubar a" in css        # tap-target enlargement in large-print mode


def test_epub_link_is_plain_pdf_opens_new_tab():
    # The EPUB "Open in iBooks" button must be a plain same-tab link so Safari
    # hands application/epub+zip straight to Books (no new tab, single tap).
    # PDFs render inline, so they keep target="_blank".
    import re
    with make_client(make_handler()) as client:
        home = client.get("/").text
        fid = re.search(r'/feed/([A-Za-z0-9_\-.]+)', home).group(1)
        root = client.get(f"/feed/{fid}").text
        epub_html = pdf_html = None
        for f2 in re.findall(r'/feed/([A-Za-z0-9_\-.]+)"', root):
            page = client.get(f"/feed/{f2}").text
            for bid in re.findall(r'/book/([A-Za-z0-9_\-.]+)"', page):
                d = client.get(f"/book/{bid}").text
                if "Open in iBooks" in d:
                    epub_html = d
                if "Open PDF" in d:
                    pdf_html = d
        assert epub_html, "expected an EPUB book detail page"
        m = re.search(r'<a class="button big" href="(/download/[^"]+\.epub)"([^>]*)>', epub_html)
        assert m, "EPUB download button not found"
        assert "target=" not in m.group(2), "EPUB link must not open a new tab"
        if pdf_html:
            mp = re.search(r'<a class="button big" href="(/download/[^"]+\.pdf)"([^>]*)>', pdf_html)
            assert mp and 'target="_blank"' in mp.group(2), "PDF link should open a new tab"


def test_full_chain_feed_to_download():
    with make_client(make_handler()) as client:
        home = client.get("/").text
        import re
        fid = re.search(r'/feed/([A-Za-z0-9_\-.]+)', home).group(1)
        # Navigate root -> 'Recently Added' acquisition feed.
        root_feed = client.get(f"/feed/{fid}").text
        # Follow every /feed id until we reach a page that has book detail links.
        ids = re.findall(r'/feed/([A-Za-z0-9_\-.]+)"', root_feed)
        book_detail_id = None
        for fid2 in ids:
            page = client.get(f"/feed/{fid2}").text
            m = re.search(r'/book/([A-Za-z0-9_\-.]+)"', page)
            if m:
                book_detail_id = m.group(1)
                break
        assert book_detail_id, "expected a book detail link"
        detail = client.get(f"/book/{book_detail_id}")
        assert detail.status_code == 200
        m = re.search(r'/download/([A-Za-z0-9_\-.]+)/([^"]+)', detail.text)
        assert m, "expected an extension-bearing download link"
        did, fname = m.group(1), m.group(2)
        assert fname.endswith(".epub") or fname.endswith(".pdf")
        # HEAD must work too (curl -I / Safari probes) — returns headers, no body.
        head = client.head(f"/download/{did}/{fname}")
        assert head.status_code == 200
        assert head.headers["content-type"] in ("application/epub+zip", "application/pdf")
        assert "filename=" in head.headers["content-disposition"]
        assert head.headers["x-content-type-options"] == "nosniff"
        dl = client.get(f"/download/{did}/{fname}")
        assert dl.status_code == 200
        assert dl.content == b"PK\x03\x04BOOKBYTES"
        ct = dl.headers["content-type"]
        assert ct in ("application/epub+zip", "application/pdf")
        assert "attachment" in dl.headers["content-disposition"] or "inline" in dl.headers["content-disposition"]
        assert dl.headers["x-content-type-options"] == "nosniff"


def test_unknown_id_is_404():
    with make_client(make_handler()) as client:
        r = client.get("/feed/not-a-valid-id")
        assert r.status_code == 404


def test_access_key_enforced_when_configured():
    cfg = load_config({**ENV, "BRIDGE_ACCESS_KEY": "letmein"})
    app = create_app(cfg)
    transport = httpx.MockTransport(make_handler())
    from contextlib import asynccontextmanager
    from app.main import FeedCache

    @asynccontextmanager
    async def ls(a):
        http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(connect=5, read=None, write=None, pool=5))
        a.state.http = http
        a.state.kavita = KavitaClient(cfg, http)
        a.state.ids = IdCodec(cfg.bridge_id_secret)
        a.state.cache = FeedCache(cfg.cache_feeds_seconds)
        a.state.store = _test_store()
        yield
        await http.aclose()
    app.router.lifespan_context = ls
    with TestClient(app) as client:
        assert client.get("/").status_code == 403
        assert client.get("/?key=letmein").status_code == 200
        # health bypasses the gate
        assert client.get("/health").status_code == 200


def test_search_uses_opensearch_template_q_param():
    # Root advertises ?q={searchTerms}; the search endpoint only answers ?q=.
    root_q = ROOT_XML.replace(
        'href="/api/opds/KEY/search?query={searchTerms}"',
        'href="/api/opds/KEY/search?q={searchTerms}"',
    )

    def handler(request):
        p = request.url.path
        q = str(request.url.query or "")
        if p == "/api/opds/SECRETKEY":
            return httpx.Response(200, text=root_q)
        if "/search" in p:
            # Only the correct ?q= form returns results.
            return httpx.Response(200, text=ACQ_XML) if "q=verne" in q else httpx.Response(404)
        return httpx.Response(200, text=ACQ_XML)

    with make_client(handler) as client:
        r = client.get("/search?q=verne")
        assert r.status_code == 200
        # Results rendered means the ?q= template was used (not Kavita's ?query=).
        assert "The Time Machine" in r.text
        assert "unavailable" not in r.text.lower()


def test_search_navigation_results_link_to_feeds_not_empty():
    # Some libraries (e.g. ManyBooks) return *navigation* entries for a search —
    # links to per-title detail feeds, not direct acquisition entries. The result
    # links must point at /feed/... and never render an empty href (which would
    # reload the same search page). [search nav-entry regression]
    def handler(request):
        if "/search" in request.url.path:
            return httpx.Response(200, text=ROOT_XML)  # navigation entries
        if request.url.path == "/api/opds/SECRETKEY":
            return httpx.Response(200, text=ROOT_XML)
        return httpx.Response(200, text=ACQ_XML)
    with make_client(handler) as client:
        r = client.get("/search?q=anything")
        assert r.status_code == 200
        # Every rendered result must have a real link, never href="".
        assert 'href=""' not in r.text
        # Navigation results link into the bridge feed browser.
        assert "/feed/" in r.text


def test_search_unavailable_shows_message_not_silent_empty():
    # Handler 404s the search endpoint (e.g. Kavita search disabled).
    def handler(request):
        if "/search" in request.url.path:
            return httpx.Response(404, text="no search")
        if request.url.path == "/api/opds/SECRETKEY":
            return httpx.Response(200, text=ROOT_XML)
        return httpx.Response(200, text=ACQ_XML)
    with make_client(handler) as client:
        r = client.get("/search?q=anything")
        assert r.status_code == 200
        assert "unavailable" in r.text.lower()


def _client_for_cfg(cfg, handler):
    from contextlib import asynccontextmanager
    from app.main import FeedCache
    app = create_app(cfg)
    transport = httpx.MockTransport(handler)

    @asynccontextmanager
    async def ls(a):
        http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(connect=5, read=None, write=None, pool=5))
        a.state.http = http
        a.state.kavita = KavitaClient(cfg, http)
        a.state.ids = IdCodec(cfg.bridge_id_secret)
        a.state.cache = FeedCache(0)
        a.state.store = _test_store()
        a.state.search_templates = {}
        yield
        await http.aclose()
    app.router.lifespan_context = ls
    return TestClient(app)


def test_portal_multi_feed_menu_and_browse():
    cfg = load_config({
        "KAVITA_OPDS_URL": "http://kavita:5000/api/opds/SECRETKEY",
        "OPDS_FEEDS": "Public Domain|http://pub.example:8080/opds",
        "BRIDGE_ID_SECRET": "s",
    })
    pub_root = ROOT_XML.replace("RetroShelf Library", "Public Domain Books")

    def handler(request):
        host = request.url.host
        p = request.url.path
        if host == "kavita" and p == "/api/opds/SECRETKEY":
            return httpx.Response(200, text=ROOT_XML)
        if host == "pub.example" and p == "/opds":
            return httpx.Response(200, text=pub_root)
        if "recently-added" in p or "libraries" in p:
            return httpx.Response(200, text=ACQ_XML)
        return httpx.Response(404)

    with _client_for_cfg(cfg, handler) as client:
        home = client.get("/")
        assert home.status_code == 200
        # Portal menu lists BOTH libraries by name.
        assert "Library" in home.text and "Public Domain" in home.text
        import re
        fids = re.findall(r'/feed/([\w\-.]+)"', home.text)
        assert len(fids) >= 2, "expected a feed link per library"
        # Browsing the SECONDARY feed (different origin) works and leaks nothing.
        sec = client.get(f"/feed/{fids[1]}")
        assert sec.status_code == 200
        assert "Public Domain Books" in sec.text
        assert "SECRETKEY" not in sec.text and "/api/opds/" not in sec.text


def test_fan_out_search_across_all_feeds():
    cfg = load_config({
        "KAVITA_OPDS_URL": "http://kavita:5000/api/opds/SECRETKEY",
        "OPDS_FEEDS": "Public Domain|http://pub.example:8080/opds",
        "BRIDGE_ID_SECRET": "s",
    })
    pub_root = ROOT_XML.replace("RetroShelf Library", "Public Domain Books")

    def handler(request):
        host = request.url.host
        p = request.url.path
        if host == "kavita" and p == "/api/opds/SECRETKEY":
            return httpx.Response(200, text=ROOT_XML)
        if host == "pub.example" and p == "/opds":
            return httpx.Response(200, text=pub_root)
        if "/search" in p:  # both libraries' search endpoints return results
            return httpx.Response(200, text=ACQ_XML)
        return httpx.Response(404)

    with _client_for_cfg(cfg, handler) as client:
        r = client.get("/search?q=time&feed=*")
        assert r.status_code == 200
        assert "across all libraries" in r.text
        # Grouped by library — both library headers appear.
        assert "Library" in r.text and "Public Domain" in r.text
        # A result from EACH library group (The Time Machine is in the fixture).
        assert r.text.count("The Time Machine") >= 2
        assert "SECRETKEY" not in r.text and "/api/opds/" not in r.text


def test_fan_out_one_feed_down_still_shows_others():
    cfg = load_config({
        "KAVITA_OPDS_URL": "http://kavita:5000/api/opds/SECRETKEY",
        "OPDS_FEEDS": "Broken|http://down.example/opds",
        "BRIDGE_ID_SECRET": "s",
    })

    def handler(request):
        host = request.url.host
        p = request.url.path
        if host == "kavita" and p == "/api/opds/SECRETKEY":
            return httpx.Response(200, text=ROOT_XML)
        if host == "down.example":
            return httpx.Response(503, text="down")
        if "/search" in p:
            return httpx.Response(200, text=ACQ_XML)
        return httpx.Response(404)

    with _client_for_cfg(cfg, handler) as client:
        r = client.get("/search?q=time&feed=*")
        assert r.status_code == 200
        assert "The Time Machine" in r.text          # working library still shown
        assert "library unavailable" in r.text       # broken library flagged, not fatal


def _first_book_id(client):
    import re
    home = client.get("/").text
    fid = re.search(r'/feed/([\w\-.]+)', home).group(1)
    for f2 in re.findall(r'/feed/([\w\-.]+)"', client.get(f"/feed/{fid}").text):
        m = re.search(r'/book/([\w\-.]+)"', client.get(f"/feed/{f2}").text)
        if m:
            return m.group(1)
    return None


def test_reading_list_star_and_unstar():
    import re
    with make_client(make_handler()) as client:
        bid = _first_book_id(client)
        assert bid
        assert "reading list is empty" in client.get("/list").text.lower()
        client.get(f"/star/{bid}?t={_token(client)}")   # add (303 → /list)
        lst = client.get("/list").text
        assert "The Time Machine" in lst and "/unstar/" in lst
        assert "Remove from Reading List" in client.get(f"/book/{bid}").text
        key = re.search(r"/unstar/(\w+)", lst).group(1)
        client.get(f"/unstar/{key}?t={_token(client)}")  # remove
        assert "reading list is empty" in client.get("/list").text.lower()


def test_download_recorded_in_history_and_marked():
    import re
    with make_client(make_handler()) as client:
        bid = _first_book_id(client)
        detail = client.get(f"/book/{bid}").text
        m = re.search(r'/download/([\w\-.]+)/([^"]+)', detail)
        client.get(f"/download/{m.group(1)}/{m.group(2)}")  # GET records history
        assert "Recently sent to iBooks" in client.get("/").text
        assert "already sent" in client.get(f"/book/{bid}").text


def test_prefs_large_print_cookie_sets_body_class():
    with make_client(make_handler()) as client:
        assert 'class="big"' not in client.get("/").text
        home = client.get(f"/prefs?big=toggle&next=/&t={_token(client)}").text   # follows 303 → /
        assert "big" in home and 'class="color-amber big"' in home


def test_opds_republish_reading_list_roundtrips():
    from app.opds import parse
    with make_client(make_handler()) as client:
        bid = _first_book_id(client)
        client.get(f"/star/{bid}?t={_token(client)}")
        # Navigation catalog.
        root = client.get("/opds")
        assert root.status_code == 200
        assert "kind=navigation" in root.headers["content-type"]
        assert b"My Reading List" in root.content
        # Acquisition feed — re-parse it with OUR OWN parser (consume == produce).
        r = client.get("/opds/reading-list")
        assert "kind=acquisition" in r.headers["content-type"]
        feed = parse(r.text)
        assert len(feed.entries) == 1
        e = feed.entries[0]
        assert e.title == "The Time Machine"
        assert e.primary_acquisition is not None
        assert "/download/" in e.primary_acquisition.href
        # The republished feed leaks no upstream apiKey.
        assert "SECRETKEY" not in r.text and "/api/opds/" not in r.text


def test_prefs_phosphor_color_theme():
    with make_client(make_handler()) as client:
        assert 'class="color-amber"' in client.get("/").text  # default
        green = client.get(f"/prefs?color=green&next=/&t={_token(client)}").text
        assert "color-green" in green
        white = client.get(f"/prefs?color=white&next=/&t={_token(client)}").text
        assert "color-white" in white


def test_prefs_reader_font_sets_body_class():
    with make_client(make_handler()) as client:
        # Default (serif) sets no font class.
        assert "read-font-" not in client.get("/").text
        sans = client.get(f"/prefs?font=sans&next=/&t={_token(client)}").text
        assert "read-font-sans" in sans
        mono = client.get(f"/prefs?font=mono&next=/&t={_token(client)}").text
        assert "read-font-mono" in mono
        # A crafted value is rejected — the cookie is not changed, so the last
        # good value (mono) still stands and no bogus class ever appears.
        bogus = client.get(f"/prefs?font=comic-sans&next=/&t={_token(client)}").text
        assert "read-font-mono" in bogus and "comic-sans" not in bogus


def test_prefs_reader_align_sets_body_class():
    with make_client(make_handler()) as client:
        assert "read-align-" not in client.get("/").text  # default: left
        just = client.get(f"/prefs?align=justify&next=/&t={_token(client)}").text
        assert "read-align-justify" in just
        bogus = client.get(f"/prefs?align=center&next=/&t={_token(client)}").text
        assert "read-align-justify" in bogus and "read-align-center" not in bogus


def test_prefs_reader_margin_sets_body_class():
    with make_client(make_handler()) as client:
        assert "read-marg-" not in client.get("/").text  # default: normal
        narrow = client.get(f"/prefs?margin=narrow&next=/&t={_token(client)}").text
        assert "read-marg-narrow" in narrow
        wide = client.get(f"/prefs?margin=wide&next=/&t={_token(client)}").text
        assert "read-marg-wide" in wide
        bogus = client.get(f"/prefs?margin=huge&next=/&t={_token(client)}").text
        assert "read-marg-wide" in bogus and "read-marg-huge" not in bogus


def test_more_by_author_link_on_book_detail():
    import re
    with make_client(make_handler()) as client:
        bid = _first_book_id(client)
        detail = client.get(f"/book/{bid}").text
        assert "more by this author" in detail
        m = re.search(r'href="(/search\?q=[^"]+)"', detail)
        assert m, "expected an author-search link"
        assert "feed=" in m.group(1)


def test_status_dashboard():
    with make_client(make_handler()) as client:
        r = client.get("/status")
        assert r.status_code == 200
        assert "Library Status" in r.text
        assert "ONLINE" in r.text          # the mocked feed responds
        assert "Library" in r.text          # default feed name


def test_status_shows_offline_feed():
    with make_client(make_handler(fail=True)) as client:
        r = client.get("/status")
        assert r.status_code == 200
        assert "OFFLINE" in r.text


def test_surprise_me_redirects_to_a_book():
    with make_client(make_handler()) as client:
        r = client.get("/random", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("/book/")


def test_help_page():
    with make_client(make_handler()) as client:
        r = client.get("/help")
        assert r.status_code == 200
        assert "iBooks" in r.text


def test_static_css_served():
    with make_client(make_handler()) as client:
        r = client.get("/static/app.css")
        assert r.status_code == 200
        assert "RetroShelf" in r.text
