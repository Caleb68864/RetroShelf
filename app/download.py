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
- ``X-Content-Type-Options: nosniff``; relay ``Content-Length`` /
  ``Accept-Ranges`` / ``Content-Range`` + 206 when upstream supplies;
  ``Cache-Control: no-store``.
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
    """Return the canonical format token for a media type string.

    Inspects *media_type* for the substrings ``"epub"`` or ``"pdf"``
    (case-insensitive) and returns the corresponding token, or ``None``
    when neither is found.

    :param media_type: Raw MIME type string (e.g. ``"application/epub+zip"``).
    :type media_type: str
    :returns: ``"epub"``, ``"pdf"``, or ``None``.
    :rtype: str or None
    """
    mt = (media_type or "").lower()
    if "epub" in mt:
        return "epub"
    if "pdf" in mt:
        return "pdf"
    return None


def content_disposition(disposition: str, filename: str) -> str:
    """Build a ``Content-Disposition`` header value with an ASCII filename.

    Old Safari ignores the RFC 5987 ``filename*`` parameter; this function
    always emits the plain ``filename`` token that Safari understands.
    Any *disposition* value other than ``"inline"`` or ``"attachment"``
    is silently coerced to ``"attachment"``.

    :param disposition: Desired disposition token — ``"inline"`` or
        ``"attachment"``.
    :type disposition: str
    :param filename: ASCII-safe filename including extension
        (e.g. ``"my-book.epub"``).
    :type filename: str
    :returns: A fully-formed ``Content-Disposition`` header value such as
        ``'attachment; filename="my-book.epub"'``.
    :rtype: str
    """
    disposition = disposition if disposition in ("inline", "attachment") else "attachment"
    return f'{disposition}; filename="{filename}"'


def build_headers(
    *,
    filename: str,
    disposition: str,
    upstream: httpx.Response | None = None,
) -> dict[str, str]:
    """Assemble the response headers for a book download.

    Always sets ``Content-Disposition``, ``X-Content-Type-Options``, and
    ``Cache-Control``.  When *upstream* is provided, the headers listed in
    ``_RELAY_HEADERS`` (``Content-Length``, ``Accept-Ranges``,
    ``Content-Range``, ``Last-Modified``) are forwarded verbatim, and
    ``Accept-Ranges: bytes`` is added as a fallback if upstream omitted it.

    :param filename: ASCII filename with extension for the
        ``Content-Disposition`` header (e.g. ``"book.pdf"``).
    :type filename: str
    :param disposition: ``"inline"`` or ``"attachment"`` — passed through to
        :func:`content_disposition`.
    :type disposition: str
    :param upstream: Live upstream ``httpx.Response`` whose headers should be
        relayed.  Pass ``None`` to skip relaying.
    :type upstream: httpx.Response or None
    :returns: Mapping of header name → value suitable for use in a
        ``StreamingResponse``.
    :rtype: dict[str, str]
    """
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
    """Proxy a book file from Kavita as a ``StreamingResponse`` with
    iOS-correct headers.

    Opens a streaming connection via *kc*, delegates header assembly to
    :func:`build_headers`, and schedules ``resp.aclose`` as a
    ``BackgroundTask`` so the pooled HTTPX client connection is released
    after the body finishes streaming (or the client disconnects) — the
    response object itself never leaks.

    :param kc: Authenticated Kavita client used to open the upstream stream.
    :type kc: KavitaClient
    :param url: Fully-qualified Kavita API URL for the book file.
    :type url: str
    :param media_type: MIME type to advertise in the response
        (e.g. ``"application/epub+zip"``).
    :type media_type: str
    :param filename: ASCII filename with extension for the
        ``Content-Disposition`` header.
    :type filename: str
    :param disposition: ``"inline"`` or ``"attachment"``.
    :type disposition: str
    :param range_header: Value of the client's ``Range`` header, forwarded
        to Kavita to support partial-content responses (206).  ``None``
        requests the full file.
    :type range_header: str or None
    :returns: A streaming proxy response ready to be returned from a
        FastAPI route handler.
    :rtype: starlette.responses.StreamingResponse
    :raises KavitaError: If Kavita returns a non-2xx status or the
        connection fails.
    """
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
    """Proxy a cover image from Kavita as a ``StreamingResponse``.

    The ``Content-Type`` is taken directly from the upstream response so
    JPEG, WebP, and PNG covers are all handled transparently.  The Kavita
    ``apiKey`` query parameter present on *url* is never forwarded to the
    browser client — it exists only in the server-to-Kavita leg.

    :param kc: Authenticated Kavita client used to open the upstream stream.
    :type kc: KavitaClient
    :param url: Fully-qualified Kavita API URL for the cover image, including
        the ``apiKey`` query parameter.
    :type url: str
    :param range_header: Value of the client's ``Range`` header, forwarded
        upstream to support partial-content responses.  ``None`` requests the
        full image.
    :type range_header: str or None
    :returns: A streaming proxy response ready to be returned from a
        FastAPI route handler.  Carries a ``Cache-Control: private,
        max-age=86400`` header so cover art is cached for one day.
    :rtype: starlette.responses.StreamingResponse
    :raises KavitaError: If Kavita returns a non-2xx status or the
        connection fails.
    """
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
