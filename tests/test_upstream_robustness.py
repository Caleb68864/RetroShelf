"""Upstream input that used to break pages (bug hunt, 2026-11-21).

Each test drives a real route (or ``stream_cover``) with a mocked upstream:
a non-OPDS feed body, a foreign pagination link, gzip'd download bytes, a
download whose type is only in the record's MIME, a cover stream that dies
mid-body, an over-cap image, and a starred HTML-only book in the published
Reading List feed.
"""
import asyncio
import gzip
import io
import json
import tempfile
import warnings

import httpx
from PIL import Image

from tests.test_app import ACQ_XML, ROOT_XML, _token, make_client
from tests.test_covers import _MockKC

HTML_PAGE = "<html><body>Down for maintenance</body></html>"
FOREIGN_NEXT = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>Paged</title><link rel="next" href="https://evil.example/p2"/>
<link rel="previous" href="javascript:alert(1)"/>
<entry><title>Paged Book</title>
<link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
 href="/api/opds/SECRETKEY/dl/b.epub"/></entry></feed>"""


def _stream(data: bytes):
    async def gen():
        yield data
    return gen()


def _handler(seen=None):
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        p = request.url.path
        if p == "/api/opds/SECRETKEY":
            return httpx.Response(200, text=ROOT_XML)
        if p == "/html-feed" or "/search" in p:
            return httpx.Response(200, text=HTML_PAGE, headers={"Content-Type": "text/html"})
        if p == "/paged":
            return httpx.Response(200, text=FOREIGN_NEXT)
        if p == "/gz.epub":
            body = b"PK\x03\x04REALEPUB"
            if "gzip" in request.headers.get("accept-encoding", ""):
                return httpx.Response(200, content=_stream(gzip.compress(body)),
                                      headers={"Content-Encoding": "gzip"})
            return httpx.Response(200, content=_stream(body))
        if p.startswith("/series/1/download"):
            return httpx.Response(200, content=_stream(b"%PDF-1.4 fake"))
        return httpx.Response(404, text="nope")
    return handler


def _enc(client, s: str) -> str:
    return client.app.state.ids.encode(s)


# -- 1. a non-OPDS body is "Library unavailable" (502), not a 500 ------------
def test_feed_that_is_not_opds_is_a_502_not_a_500():
    with make_client(_handler()) as client:
        r = client.get(f"/feed/{_enc(client, 'http://kavita:5000/html-feed')}")
        assert r.status_code == 502
        assert "Library unavailable" in r.text


def test_search_against_a_non_opds_library_does_not_500():
    with make_client(_handler()) as client:
        r = client.get("/search?q=time")
        assert r.status_code != 500


# -- 2. a bad next/previous link drops the pager link, not the page ----------
def test_foreign_pagination_links_are_dropped_not_fatal():
    with make_client(_handler()) as client:
        r = client.get(f"/feed/{_enc(client, 'http://kavita:5000/paged')}")
        assert r.status_code == 200
        assert "Paged Book" in r.text
        assert "evil.example" not in r.text and "javascript:" not in r.text


# -- 3. downloads ask upstream for identity encoding -------------------------
def test_download_relays_real_bytes_not_gzip():
    seen = []
    with make_client(_handler(seen)) as client:
        r = client.get(f"/download/{_enc(client, 'http://kavita:5000/gz.epub')}")
        assert r.status_code == 200
        assert r.content == b"PK\x03\x04REALEPUB"
        dl = [q for q in seen if q.url.path == "/gz.epub"]
        assert dl and dl[0].headers.get("accept-encoding") == "identity"


# -- 4. download type comes from the record's MIME when it has one -----------
def test_pdf_record_without_pdf_in_url_downloads_as_pdf():
    url = "http://kavita:5000/series/1/download?id=7"
    with make_client(_handler()) as client:
        did = _enc(client, json.dumps({"u": url, "m": "application/pdf", "t": "Manual"}))
        r = client.get(f"/download/{did}")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/pdf")
        assert ".pdf" in r.headers["content-disposition"]


def test_epub_on_a_host_named_pdf_is_not_served_as_pdf():
    from app.config import load_config
    from app.main import _media_for_url
    from tests.test_app import ENV
    cfg = load_config(ENV)
    assert _media_for_url("https://files.pdfhost.example/book.epub", cfg)[2] == "epub"
    assert _media_for_url("http://k/a/book.PDF", cfg)[2] == "pdf"
    assert _media_for_url("http://k/download?file=x.pdf", cfg)[2] == "pdf"
    assert _media_for_url("http://k/x", cfg, "application/pdf")[2] == "pdf"
    assert _media_for_url("http://k/x.pdf", cfg, "application/epub+zip")[2] == "epub"


# -- 5. a cover stream that dies mid-body is the tiny 404, not a 500 page ----
def test_cover_stream_reset_mid_body_is_a_clean_kavita_error():
    from app.download import stream_cover
    from app.errors import KavitaError

    async def body():
        yield b"\x89PNG"
        raise httpx.ReadError("connection reset")

    kc = _MockKC(httpx.Response(200, content=body(), headers={"Content-Type": "image/png"}))
    try:
        asyncio.run(stream_cover(kc, "http://kavita:5000/api/image/reset",
                                 cache_dir=tempfile.mkdtemp(), cover_max_edge=320,
                                 cover_jpeg_quality=80))
    except KavitaError:
        return
    raise AssertionError("expected KavitaError")


# -- 6. the decoded-pixel cap applies at the cap, not at twice it ------------
def test_image_over_the_pixel_cap_is_not_decoded():
    from app import download
    big = Image.new("1", (9000, 8000))  # 72M px: over the 64M cap, under 2x
    buf = io.BytesIO()
    big.save(buf, format="PNG")
    raw = buf.getvalue()
    kc = _MockKC(httpx.Response(200, content=raw, headers={"Content-Type": "image/png"}))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        resp = asyncio.run(download.stream_cover(
            kc, "http://kavita:5000/api/image/huge", cache_dir=tempfile.mkdtemp(),
            cover_max_edge=320, cover_jpeg_quality=80))
    # Not transcoded: the raw upstream bytes are served as-is.
    assert resp.body == raw


# -- 7. the published Reading List offers no fake .epub for HTML books -------
def test_reading_list_feed_has_no_epub_link_for_an_html_only_book():
    with make_client(_handler()) as client:
        rec = {"u": "http://kavita:5000/books/1.html", "m": "text/html", "t": "Html Only"}
        client.get(f"/star/{_enc(client, json.dumps(rec))}?t={_token(client)}",
                   follow_redirects=False)
        xml = client.get("/opds/reading-list").text
        assert "Html Only" in xml
        assert ".epub" not in xml
        assert "/download/" not in xml


def test_reader_image_over_the_pixel_cap_is_dropped():
    from app.reader import _transcode_reader_image
    big = Image.new("1", (9000, 8000))  # 72M px: over the 64M cap, under 2x
    buf = io.BytesIO()
    big.save(buf, format="PNG")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        assert _transcode_reader_image(buf.getvalue()) is None


REL_SEARCH = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<title>Results</title><entry><title>Relative Book</title>
<link rel="http://opds-spec.org/acquisition" type="application/epub+zip"
 href="dl/1.epub"/></entry></feed>"""


def test_search_results_resolve_relative_links_against_the_search_url():
    import re
    from urllib.parse import urljoin
    seen = []

    def handler(request):
        seen.append(request)
        if request.url.path == "/api/opds/SECRETKEY":
            return httpx.Response(200, text=ROOT_XML)
        if "/search" in request.url.path:
            return httpx.Response(200, text=REL_SEARCH)
        return httpx.Response(404)

    with make_client(handler) as client:
        page = client.get("/search?q=time").text
        bid = re.search(r'/book/([\w\-.]+)"', page).group(1)
        rec = json.loads(client.app.state.ids.decode(bid))
        search_req = next(q for q in seen if "/search" in q.url.path)
        assert rec["u"] == urljoin(str(search_req.url), "dl/1.epub")
