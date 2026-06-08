"""Tests for app.kavita — SSRF guard, feed fetch, streaming proxy (mocked)."""
import httpx
import pytest

from app.config import load_config
from app.errors import KavitaError, SsrfError
from app.kavita import KavitaClient

ENV = {"KAVITA_OPDS_URL": "http://kavita:5000/api/opds/SECRETKEY"}


def make_client(handler) -> KavitaClient:
    cfg = load_config(ENV)
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(connect=5, read=None, write=None, pool=5))
    return KavitaClient(cfg, http)


# -- resolve_url / SSRF --------------------------------------------------------

def test_resolve_root_relative_path():
    kc = make_client(lambda r: httpx.Response(200))
    assert kc.resolve_url("/api/opds/SECRETKEY/libraries") == "http://kavita:5000/api/opds/SECRETKEY/libraries"


def test_resolve_same_origin_absolute_ok():
    kc = make_client(lambda r: httpx.Response(200))
    assert kc.resolve_url("http://kavita:5000/api/image/x") == "http://kavita:5000/api/image/x"


@pytest.mark.parametrize("bad", [
    "//evil.com/x",                 # protocol-relative
    "https://evil.com/x",           # foreign absolute (also scheme mismatch)
    "http://evil.com/x",            # foreign host
    "\\\\evil.com",                 # backslash
    "http://kavita:6000/x",         # wrong port
    "relative/path",                # not root-relative
    "",                              # empty
])
def test_resolve_rejects_ssrf(bad):
    kc = make_client(lambda r: httpx.Response(200))
    with pytest.raises(SsrfError):
        kc.resolve_url(bad)


def test_ssrf_error_message_masks_key():
    kc = make_client(lambda r: httpx.Response(200))
    try:
        kc.resolve_url("http://evil.com/SECRETKEY")
    except SsrfError as exc:
        assert "SECRETKEY" not in str(exc)


# -- fetch_feed ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_feed_success():
    def handler(req):
        assert req.url.host == "kavita"
        return httpx.Response(200, text="<feed/>")
    kc = make_client(handler)
    body = await kc.fetch_feed("/api/opds/SECRETKEY")
    assert body == "<feed/>"


@pytest.mark.asyncio
async def test_fetch_feed_http_error_masked():
    kc = make_client(lambda r: httpx.Response(503, text="down"))
    with pytest.raises(KavitaError) as exc:
        await kc.fetch_feed("/api/opds/SECRETKEY")
    assert exc.value.status == 503
    assert "SECRETKEY" not in str(exc.value)


@pytest.mark.asyncio
async def test_fetch_feed_connection_error():
    def handler(req):
        raise httpx.ConnectError("boom")
    kc = make_client(handler)
    with pytest.raises(KavitaError):
        await kc.fetch_feed("/api/opds/SECRETKEY")


# -- stream --------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stream_yields_bytes_and_forwards_range():
    seen = {}
    def handler(req):
        seen["range"] = req.headers.get("range")
        return httpx.Response(206, content=b"PARTIAL", headers={"Content-Range": "bytes 0-6/100"})
    kc = make_client(handler)
    async with kc.stream("/api/opds/SECRETKEY/series/1/.../download/x.epub", range_header="bytes=0-6") as resp:
        assert resp.status_code == 206
        assert resp.headers["Content-Range"] == "bytes 0-6/100"
        chunks = await resp.aread()
    assert chunks == b"PARTIAL"
    assert seen["range"] == "bytes=0-6"


@pytest.mark.asyncio
async def test_open_stream_follows_same_origin_redirect():
    # Gutenberg-style: download URL 302s to a same-origin cache URL.
    calls = []

    def handler(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/dl"):
            return httpx.Response(302, headers={"Location": "http://kavita:5000/cache/x.epub"})
        if req.url.path == "/cache/x.epub":
            return httpx.Response(200, content=b"PK\x03\x04EPUB")
        return httpx.Response(404)

    kc = make_client(handler)
    resp = await kc.open_stream("/api/opds/SECRETKEY/dl")
    assert resp.status_code == 200
    body = await resp.aread()
    await resp.aclose()
    assert body == b"PK\x03\x04EPUB"
    assert "/cache/x.epub" in calls


@pytest.mark.asyncio
async def test_open_stream_rejects_cross_origin_redirect():
    def handler(req):
        return httpx.Response(302, headers={"Location": "http://evil.com/x.epub"})
    kc = make_client(handler)
    with pytest.raises(SsrfError):
        await kc.open_stream("/api/opds/SECRETKEY/dl")


@pytest.mark.asyncio
async def test_stream_http_error_raises():
    kc = make_client(lambda r: httpx.Response(404, content=b"nope"))
    with pytest.raises(KavitaError):
        async with kc.stream("/api/opds/SECRETKEY/x"):
            pass
