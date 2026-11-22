"""HEAD on /download and /cover asks the library (decided by agent
2026-11-21): a missing book or cover is a failure, not a 200 that the GET
then breaks, and a book HEAD carries the real Content-Length."""
import json

import httpx

from tests.test_app import ROOT_XML, make_client


def _stream(data: bytes):
    async def gen():
        yield data
    return gen()


def _handler(seen):
    def handler(request):
        seen.append((request.method, request.url.path))
        p = request.url.path
        if p == "/api/opds/SECRETKEY":
            return httpx.Response(200, text=ROOT_XML)
        if p == "/ok.epub":
            return httpx.Response(200, content=_stream(b"PK\x03\x04ok"),
                                  headers={"Content-Length": "6"})
        if p == "/img/ok":
            return httpx.Response(200, content=_stream(b"\x89PNG"),
                                  headers={"Content-Type": "image/png"})
        return httpx.Response(404, text="gone")
    return handler


def _did(client, path):
    rec = {"u": f"http://kavita:5000{path}", "m": "application/epub+zip", "t": "T"}
    return client.app.state.ids.encode(json.dumps(rec))


def test_head_download_reports_the_library_status_and_size():
    seen = []
    with make_client(_handler(seen)) as client:
        ok = client.head(f"/download/{_did(client, '/ok.epub')}")
        assert ok.status_code == 200
        assert ok.headers["content-length"] == "6"
        assert ok.headers["content-type"] == "application/epub+zip"
        assert "filename=" in ok.headers["content-disposition"]
        assert client.head(f"/download/{_did(client, '/missing.epub')}").status_code == 502
        assert ("GET", "/missing.epub") in seen
        # A HEAD is a probe, not a download.
        assert client.app.state.store.recent_downloads() == []


def test_head_cover_is_404_when_the_library_has_none():
    seen = []
    with make_client(_handler(seen)) as client:
        ids = client.app.state.ids
        assert client.head(f"/cover/{ids.encode('http://kavita:5000/img/ok')}").status_code == 200
        assert client.head(f"/cover/{ids.encode('http://kavita:5000/img/missing')}").status_code == 404
