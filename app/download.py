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

import hashlib
import io
import os

import httpx
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

try:
    from PIL import Image as _PILImage
    _PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PIL_AVAILABLE = False

_PASSTHROUGH_FORMATS = {"JPEG", "PNG"}


def _cover_cache_key(url: str) -> str:
    return hashlib.sha256(url.encode()).hexdigest()

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
    cache_dir: str = "/cache",
    cover_max_edge: int = 320,
    cover_jpeg_quality: int = 80,
) -> Response:
    """Proxy a cover image from Kavita with disk caching and optional transcoding.

    Algorithm:
    (a) Compute ``key = sha256(url)``; if ``{cache_dir}/covers/{key}`` exists,
        serve it directly (no upstream fetch, no Pillow).
    (b) Else fetch the cover fully into memory via ``kc.open_stream`` + ``aread()``.
    (c) If Pillow is available: pass through original bytes when format ∈
        {JPEG, PNG} and ``max(w, h) ≤ cover_max_edge``.
    (d) Otherwise downscale to ``cover_max_edge`` (preserving aspect) and
        re-encode as baseline JPEG at ``cover_jpeg_quality``.
    (e) Write served bytes + content-type to the disk cache, then return.

    Range requests are dropped — covers are always served as full 200 responses.
    The upstream ``apiKey`` never appears in cache filenames, response headers,
    or response bodies.

    :param kc: Kavita client; ``open_stream`` is called for the upstream fetch.
    :param url: Fully-qualified cover URL (already SSRF-validated by the caller).
    :param cache_dir: Root directory for the cover disk cache.
    :param cover_max_edge: Maximum pixel dimension; larger images are downscaled.
    :param cover_jpeg_quality: JPEG re-encode quality (1–95).
    :returns: A buffered :class:`Response` with appropriate ``Cache-Control``.
    :raises KavitaError: If the upstream fetch fails and the cache is cold.
    """
    key = _cover_cache_key(url)
    cache_covers = os.path.join(cache_dir, "covers")
    cache_path = os.path.join(cache_covers, key)
    cache_ct_path = cache_path + ".ct"

    _resp_headers = {"Cache-Control": "private, max-age=86400", "X-Content-Type-Options": "nosniff"}

    # (a) Cache hit
    if os.path.exists(cache_path):
        with open(cache_path, "rb") as f:
            data = f.read()
        ct = "image/jpeg"
        if os.path.exists(cache_ct_path):
            with open(cache_ct_path) as f:
                ct = f.read().strip() or ct
        return Response(content=data, status_code=200, media_type=ct, headers=_resp_headers)

    # (b) Fetch fully into memory
    upstream = await kc.open_stream(url)
    upstream_ct = upstream.headers.get("content-type", "image/jpeg")
    raw_bytes = await upstream.aread()
    await upstream.aclose()

    # (c) & (d) Pillow processing
    served_bytes = raw_bytes
    served_ct = upstream_ct

    if _PIL_AVAILABLE:
        try:
            img = _PILImage.open(io.BytesIO(raw_bytes))
            fmt = img.format or ""
            w, h = img.size
            if fmt in _PASSTHROUGH_FORMATS and max(w, h) <= cover_max_edge:
                pass  # passthrough: format and size are fine
            else:
                if max(w, h) > cover_max_edge:
                    ratio = cover_max_edge / max(w, h)
                    new_w = max(1, int(w * ratio))
                    new_h = max(1, int(h * ratio))
                    img = img.resize((new_w, new_h), _PILImage.LANCZOS)
                if img.mode not in ("RGB", "L"):
                    img = img.convert("RGB")
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=cover_jpeg_quality)
                served_bytes = buf.getvalue()
                served_ct = "image/jpeg"
        except Exception:
            pass  # Pillow failed; serve raw upstream bytes with upstream content-type

    # (e) Write to disk cache
    try:
        os.makedirs(cache_covers, exist_ok=True)
        with open(cache_path, "wb") as f:
            f.write(served_bytes)
        with open(cache_ct_path, "w") as f:
            f.write(served_ct)
    except OSError:
        pass

    return Response(content=served_bytes, status_code=200, media_type=served_ct, headers=_resp_headers)
