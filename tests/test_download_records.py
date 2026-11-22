"""A download is recorded in the history only once the library starts
sending the book (decided by agent 2026-11-21): a failed upstream, a 416,
or a HEAD probe leaves no record."""
import json

import httpx

from tests.test_app import ROOT_XML, make_client


def _stream(data: bytes):
    async def gen():
        yield data
    return gen()


def _handler(request):
    p = request.url.path
    if p == "/api/opds/SECRETKEY":
        return httpx.Response(200, text=ROOT_XML)
    if p == "/ok.epub":
        return httpx.Response(200, content=_stream(b"PK\x03\x04ok"))
    if p == "/broken.epub":
        return httpx.Response(500, text="boom")
    if p == "/range.epub":
        return httpx.Response(416, headers={"Content-Range": "bytes */5"})
    return httpx.Response(404)


def _did(client, path, title):
    rec = {"u": f"http://kavita:5000{path}", "m": "application/epub+zip", "t": title}
    return client.app.state.ids.encode(json.dumps(rec))


def _titles(client):
    return [r.get("t") for r in client.app.state.store.recent_downloads()]


def test_failed_download_is_not_recorded():
    with make_client(_handler) as client:
        assert client.get(f"/download/{_did(client, '/broken.epub', 'Broken')}").status_code == 502
        assert "Broken" not in _titles(client)


def test_unsatisfiable_range_is_not_recorded():
    with make_client(_handler) as client:
        r = client.get(f"/download/{_did(client, '/range.epub', 'Ranged')}",
                       headers={"Range": "bytes=99-"})
        assert r.status_code == 416
        assert "Ranged" not in _titles(client)


def test_successful_download_is_recorded():
    with make_client(_handler) as client:
        assert client.get(f"/download/{_did(client, '/ok.epub', 'Good')}").status_code == 200
        assert "Good" in _titles(client)
