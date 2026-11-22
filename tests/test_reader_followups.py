"""Reader follow-ups (2026-11-21): bounded HTML image fetches and placeholder
neutralization across unwrapped elements."""
import asyncio
import time

import httpx

from app import reader


class _StallingKC:
    async def open_stream(self, url, *, range_header=None):
        async def body():
            yield b"\x89PNG"
            await asyncio.sleep(8)
            yield b"never"
        return httpx.Response(200, content=body())


def test_stalled_html_image_fetch_times_out(monkeypatch):
    monkeypatch.setattr(reader, "IMAGE_FETCH_TIMEOUT", 0.2, raising=False)
    start = time.monotonic()
    got = asyncio.run(reader._fetch_capped_image(_StallingKC(), "http://kavita:5000/i.png"))
    assert got is None
    assert time.monotonic() - start < 5


def test_image_fetch_timeout_reuses_the_feed_read_timeout():
    from app.kavita import FEED_READ_TIMEOUT
    assert reader.IMAGE_FETCH_TIMEOUT == FEED_READ_TIMEOUT
