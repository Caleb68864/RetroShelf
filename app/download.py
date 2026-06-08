"""Download/cover proxy: build iOS-correct headers and a memory-safe
StreamingResponse that proxies bytes from Kavita.

The header rules here are the heart of RetroShelf (verified — see
vault/Build Constraints.md / Corrected Assumptions.md):
- EPUB → ``Content-Type: application/epub+zip`` + ``Content-Disposition:
  attachment; filename="X.epub"`` (Safari can't render it → "Open in iBooks").
- PDF  → ``Content-Type: application/pdf`` + ``inline`` (default) so Safari
  renders it, then the user does Share → "Copy to Books".
- Always a sanitized ASCII ``filename`` with the correct extension; the saved
  extension is also reinforced by the URL path (``/download/{id}/{name}.ext``).
- ``X-Content-Type-Options: nosniff``; relay ``Content-Length`` / ``Accept-Ranges``
  / ``Content-Range`` + 206 when upstream supplies; ``Cache-Control: no-store``.
"""
from __future__ import annotations

import httpx
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from .kavita import KavitaClient

EPUB_MIME = "application/epub+zip"
PDF_MIME = "application/pdf"

# Headers worth relaying from the upstream response to the client.
_RELAY_HEADERS = ("content-length", "accept-ranges", "content-range", "last-modified")


def format_of(media_type: str) -> str | None:
    """Return ``"epub"`` / ``"pdf"`` / ``None`` for a media type string."""
    mt = (media_type or "").lower()
    if "epub" in mt:
        return "epub"
    if "pdf" in mt:
        return "pdf"
    return None


def content_disposition(disposition: str, filename: str) -> str:
    """Build a ``Content-Disposition`` value with an ASCII filename. Old Safari
    ignores ``filename*``; we provide a plain ``filename`` it understands."""
    disposition = disposition if disposition in ("inline", "attachment") else "attachment"
    return f'{disposition}; filename="{filename}"'


def build_headers(
    *,
    filename: str,
    disposition: str,
    upstream: httpx.Response | None = None,
) -> dict[str, str]:
    """Assemble the response headers for a book download."""
    headers = {
        "Content-Disposition": content_disposition(disposition, filename),
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "no-store",
    }
    if upstream is not None:
        for key in _RELAY_HEADERS:
            if key in upstream.headers:
                # Title-case for cleanliness; HTTP header names are case-insensitive.
                headers[key.title()] = upstream.headers[key]
        # Ensure clients know ranges are supported even if upstream omitted it on 200.
        headers.setdefault("Accept-Ranges", "bytes")
    return headers


async def stream_download(
    kc: KavitaClient,
    url: str,
    *,
    media_type: str,
    filename: str,
    disposition: str,
    range_header: str | None = None,
) -> StreamingResponse:
    """Proxy a book file from Kavita as a StreamingResponse with iOS-correct
    headers. The upstream connection is closed by a BackgroundTask after the
    body finishes (or the client disconnects), so the pooled client never leaks."""
    resp = await kc.open_stream(url, range_header=range_header)
    headers = build_headers(filename=filename, disposition=disposition, upstream=resp)
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        media_type=media_type,
        headers=headers,
        background=BackgroundTask(resp.aclose),
    )


async def stream_cover(
    kc: KavitaClient,
    url: str,
    *,
    range_header: str | None = None,
) -> StreamingResponse:
    """Proxy a cover image from Kavita. Content-Type comes from upstream; the
    apiKey (a query param on the upstream URL) never reaches the client."""
    resp = await kc.open_stream(url, range_header=range_header)
    media_type = resp.headers.get("content-type", "image/jpeg")
    headers = {"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"}
    if "content-length" in resp.headers:
        headers["Content-Length"] = resp.headers["content-length"]
    return StreamingResponse(
        resp.aiter_raw(),
        status_code=resp.status_code,
        media_type=media_type,
        headers=headers,
        background=BackgroundTask(resp.aclose),
    )
