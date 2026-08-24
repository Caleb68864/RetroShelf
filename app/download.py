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
import re

import httpx
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from .errors import KavitaError
from .kavita import KavitaClient

try:
    from PIL import Image as _PILImage
    _PIL_AVAILABLE = True
    # A 320px thumbnail never needs tens of megapixels. Capping the decoded
    # pixel count turns a decompression bomb (a 100KB PNG that expands to
    # gigabytes) into a caught exception and a raw passthrough. [SS-09]
    _PILImage.MAX_IMAGE_PIXELS = 64_000_000
except ImportError:  # pragma: no cover
    _PIL_AVAILABLE = False

_PASSTHROUGH_FORMATS = {"JPEG", "PNG"}

# Covers — unlike books — are buffered in memory to be transcoded, so their
# size must be bounded. Real cover art is well under 1MB; 12MB is generous
# headroom that still cannot exhaust a small NAS. [SS-09]
MAX_COVER_BYTES = 12 * 1024 * 1024

# Ceiling for the on-disk cover cache. Without one, browsing a large public
# catalogue would fill the volume. Pruning is oldest-first by mtime. [SS-09]
MAX_COVER_CACHE_BYTES = 256 * 1024 * 1024


def _safe_image_type(content_type: str | None) -> str:
    """Return an ``image/*`` content type, or a neutral fallback.

    An upstream is free to label a "cover" ``text/html``. Relaying that verbatim
    would put attacker-influenced markup on this origin's content type; nosniff
    stops a browser acting on it, but the right answer is not to claim it is a
    document at all. Anything that is not an image is served as opaque bytes.

    :param content_type: The raw upstream ``Content-Type`` header.
    :returns: The upstream type when it is an image, else
        ``"application/octet-stream"``.
    :rtype: str
    """
    base = (content_type or "").split(";", 1)[0].strip().lower()
    return base if base.startswith("image/") else "application/octet-stream"


def _cover_cache_key(url: str) -> str:
    """Return the cover cache filename key for *url* (its SHA-256 hex digest).

    Hashing means the upstream URL — embedded ``apiKey`` included — never
    appears in a cache filename.

    :param url: The fully-qualified upstream cover URL.
    :rtype: str
    """
    return hashlib.sha256(url.encode()).hexdigest()


async def _read_capped(resp: httpx.Response, limit: int) -> bytes:
    """Read *resp* fully into memory, aborting past *limit* bytes.

    :param resp: A streaming ``httpx.Response``.
    :param limit: Maximum acceptable body size in bytes.
    :returns: The body bytes.
    :raises ValueError: If the body exceeds *limit*.
    """
    declared = resp.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise ValueError(f"cover too large: {declared} bytes")
    chunks: list[bytes] = []
    total = 0
    async for chunk in resp.aiter_bytes():
        total += len(chunk)
        if total > limit:
            raise ValueError(f"cover exceeded {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


def _prune_cover_cache(cache_covers: str, limit: int) -> None:
    """Delete the oldest cached covers until the directory fits under *limit*.

    Best-effort and never fatal: a cache that cannot be pruned is a disk-space
    problem, not a reason to fail the request that is currently in flight.

    :param cache_covers: The ``{cache_dir}/covers`` directory.
    :param limit: Target maximum total size in bytes.
    """
    try:
        entries = []
        total = 0
        with os.scandir(cache_covers) as it:
            for item in it:
                if not item.is_file():
                    continue
                stat = item.stat()
                entries.append((stat.st_mtime, stat.st_size, item.path))
                total += stat.st_size
        if total <= limit:
            return
        for _mtime, size, path in sorted(entries):
            try:
                os.remove(path)
                # The sidecar content-type file shares the fate of its payload.
                if not path.endswith(".ct"):
                    os.remove(path + ".ct")
            except OSError:
                pass
            total -= size
            if total <= limit:
                return
    except OSError:
        pass


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

    The filename is re-scrubbed here even though callers pass it through
    :func:`~app.security.sanitize_filename` first. This function is reachable
    on its own, and the one character that must never survive into a quoted
    header parameter is the quote that ends it — followed by CR and LF, which
    would end the header entirely. Defence at the point of construction means
    the guarantee does not depend on every caller remembering.

    :returns: A fully-formed ``Content-Disposition`` header value such as
        ``'attachment; filename="my-book.epub"'``.
    :rtype: str
    """
    disposition = disposition if disposition in ("inline", "attachment") else "attachment"
    safe_name = "".join(
        ch for ch in (filename or "") if ch.isprintable() and ch not in '"\\;'
    )[:200] or "download"
    return f'{disposition}; filename="{safe_name}"'


# Shapes an upstream header must match before the bridge repeats it. Relaying a
# malformed ``Content-Length`` is worse than omitting it: old Safari trusts the
# number and truncates or stalls the book import when it disagrees with the
# body. [SS-12]
_HEADER_VALIDATORS = {
    "content-length": re.compile(r"^\d{1,19}$"),
    "accept-ranges": re.compile(r"^(?i:bytes|none)$"),
    "content-range": re.compile(r"^(?i:bytes) (?:\d+-\d+|\*)/(?:\d+|\*)$"),
    # An HTTP-date is not worth re-parsing; require a plausible, control-free,
    # bounded token string.
    "last-modified": re.compile(r"^[\x20-\x7e]{1,64}$"),
}


def _valid_relay(name: str, value: str) -> bool:
    """Return whether *value* is a well-formed value for header *name*.

    :param name: Lower-case header name from :data:`_RELAY_HEADERS`.
    :param value: The raw upstream header value.
    :rtype: bool
    """
    validator = _HEADER_VALIDATORS.get(name)
    return bool(validator and validator.match(value.strip()))


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
            value = upstream.headers.get(key)
            if value is not None and _valid_relay(key, value):
                # Title-case for cleanliness; HTTP header names are case-insensitive.
                headers[key.title()] = value.strip()
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
                ct = _safe_image_type(f.read().strip()) or ct
        return Response(content=data, status_code=200, media_type=ct, headers=_resp_headers)

    # (b) Fetch fully into memory, bounded — a cover is transcoded, so unlike a
    # book it cannot be streamed straight through. [SS-09]
    upstream = await kc.open_stream(url)
    upstream_ct = upstream.headers.get("content-type", "image/jpeg")
    try:
        raw_bytes = await _read_capped(upstream, MAX_COVER_BYTES)
    except ValueError as exc:
        raise KavitaError(f"Refusing oversized cover: {exc}") from exc
    finally:
        await upstream.aclose()

    # (c) & (d) Pillow processing
    served_bytes = raw_bytes
    served_ct = _safe_image_type(upstream_ct)

    if _PIL_AVAILABLE:
        try:
            img: _PILImage.Image = _PILImage.open(io.BytesIO(raw_bytes))
            fmt = img.format or ""
            w, h = img.size
            if fmt in _PASSTHROUGH_FORMATS and max(w, h) <= cover_max_edge:
                pass  # passthrough: format and size are fine
            else:
                if max(w, h) > cover_max_edge:
                    ratio = cover_max_edge / max(w, h)
                    new_w = max(1, int(w * ratio))
                    new_h = max(1, int(h * ratio))
                    img = img.resize((new_w, new_h), _PILImage.Resampling.LANCZOS)
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
        # Write via a temp file + rename so a concurrent reader never sees a
        # half-written cover (and a crash mid-write leaves no corrupt entry).
        tmp = f"{cache_path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(served_bytes)
        os.replace(tmp, cache_path)
        with open(cache_ct_path, "w") as f:
            f.write(served_ct)
        _prune_cover_cache(cache_covers, MAX_COVER_CACHE_BYTES)
    except OSError:
        pass

    return Response(content=served_bytes, status_code=200, media_type=served_ct, headers=_resp_headers)
