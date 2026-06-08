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
        client.get(f"/star/{bid}")            # add (303 → /list)
        lst = client.get("/list").text
        assert "The Time Machine" in lst and "/unstar/" in lst
        assert "Remove from Reading List" in client.get(f"/book/{bid}").text
        key = re.search(r"/unstar/(\w+)", lst).group(1)
        client.get(f"/unstar/{key}")          # remove
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
        home = client.get("/prefs?big=toggle&next=/").text   # follows 303 → /
        assert 'class="big"' in home


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
