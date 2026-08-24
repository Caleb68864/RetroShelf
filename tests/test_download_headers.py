"""Tests for app.download — iOS header correctness + streaming cleanup."""
import httpx
import pytest

from app.download import (
    EPUB_MIME, PDF_MIME, format_of, content_disposition, build_headers,
    stream_download, stream_cover,
)


def test_format_of():
    assert format_of("application/epub+zip") == "epub"
    assert format_of("application/pdf") == "pdf"
    assert format_of("application/octet-stream") is None


def test_content_disposition_values():
    assert content_disposition("attachment", "Book.epub") == 'attachment; filename="Book.epub"'
    assert content_disposition("inline", "Doc.pdf") == 'inline; filename="Doc.pdf"'
    # unknown disposition falls back to attachment
    assert content_disposition("weird", "x.epub").startswith("attachment;")


def test_build_headers_basic():
    h = build_headers(filename="Book.epub", disposition="attachment")
    assert h["Content-Disposition"] == 'attachment; filename="Book.epub"'
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["Cache-Control"] == "no-store"


def test_build_headers_relays_range_and_length():
    upstream = httpx.Response(206, headers={
        "Content-Length": "7", "Content-Range": "bytes 0-6/100", "Accept-Ranges": "bytes",
    })
    h = build_headers(filename="x.pdf", disposition="inline", upstream=upstream)
    assert h["Content-Range"] == "bytes 0-6/100"
    assert h["Content-Length"] == "7"
    assert h["Accept-Ranges"] == "bytes"


# -- fake upstream/client for streaming-path tests ----------------------------

class FakeUpstream:
    def __init__(self, status=200, headers=None, chunks=(b"AB", b"CD")):
        self.status_code = status
        self.headers = httpx.Headers(headers or {})
        self._chunks = list(chunks)
        self.closed = False

    async def aiter_raw(self):
        for c in self._chunks:
            yield c

    async def aiter_bytes(self):
        # Mirrors httpx.Response.aiter_bytes(): content-decoded chunks. This is
        # what the size-capped cover read consumes.
        for c in self._chunks:
            yield c

    async def aread(self):
        # Mirrors httpx.Response.aread(): buffer the full body. stream_cover
        # buffers covers (small) rather than streaming them.
        return b"".join(self._chunks)

    async def aclose(self):
        self.closed = True


class FakeKC:
    def __init__(self, upstream):
        self._u = upstream
        self.opened_with = None

    async def open_stream(self, url, *, range_header=None):
        self.opened_with = (url, range_header)
        return self._u


@pytest.mark.asyncio
async def test_stream_download_epub_headers_and_body():
    up = FakeUpstream(200, {"Content-Length": "4"}, chunks=[b"PK\x03\x04", b"rest"])
    kc = FakeKC(up)
    resp = await stream_download(
        kc, "/api/opds/K/x/download/book.epub",
        media_type=EPUB_MIME, filename="The-Time-Machine.epub", disposition="attachment",
    )
    assert resp.media_type == EPUB_MIME
    assert resp.headers["content-disposition"] == 'attachment; filename="The-Time-Machine.epub"'
    assert resp.headers["x-content-type-options"] == "nosniff"
    body = b"".join([c async for c in resp.body_iterator])
    assert body == b"PK\x03\x04rest"
    # background task closes the upstream
    await resp.background()
    assert up.closed is True


@pytest.mark.asyncio
async def test_stream_download_pdf_inline_default():
    up = FakeUpstream(200)
    resp = await stream_download(
        FakeKC(up), "/api/opds/K/x/download/doc.pdf",
        media_type=PDF_MIME, filename="Report.pdf", disposition="inline",
    )
    assert resp.media_type == PDF_MIME
    assert resp.headers["content-disposition"] == 'inline; filename="Report.pdf"'


@pytest.mark.asyncio
async def test_stream_download_relays_206_and_range():
    up = FakeUpstream(206, {"Content-Range": "bytes 0-3/8", "Content-Length": "4"})
    kc = FakeKC(up)
    resp = await stream_download(
        kc, "/x.pdf", media_type=PDF_MIME, filename="x.pdf",
        disposition="inline", range_header="bytes=0-3",
    )
    assert resp.status_code == 206
    assert resp.headers["content-range"] == "bytes 0-3/8"
    assert kc.opened_with == ("/x.pdf", "bytes=0-3")


@pytest.mark.asyncio
async def test_stream_cover_passes_through_undecodable_upstream(tmp_path):
    # A body Pillow can't decode (here a 2-byte stub) falls back to serving the
    # upstream bytes with the upstream content-type — covers are now buffered
    # (not streamed) and the upstream is closed inline. [cover transcode SS-01]
    up = FakeUpstream(200, {"Content-Type": "image/png", "Content-Length": "2"}, chunks=[b"\x89P"])
    resp = await stream_cover(FakeKC(up), "/api/image/series-cover?seriesId=1&apiKey=K",
                              cache_dir=str(tmp_path))
    assert resp.media_type == "image/png"
    assert resp.body == b"\x89P"
    assert up.closed is True
    # The apiKey must never leak into a cache filename.
    assert not any("K" in p.name and "apiKey" in p.name for p in tmp_path.rglob("*"))
