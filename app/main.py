"""RetroShelf FastAPI application: routes, lifespan, middleware, feed cache,
and the opds→ids→render→download wiring.

Everything the iPad sees is a bridge id; the Kavita apiKey is held server-side
and never appears in a response body. Every decoded id is re-validated through
the SSRF guard before any upstream fetch. [C-3][C-6][H-2]
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

from . import opds
from .config import Config, ConfigError, load_config
from .download import (
    EPUB_MIME, PDF_MIME, build_headers as build_download_headers,
    format_of, stream_cover, stream_download,
)
from .errors import BadIdError, KavitaError, RetroShelfError, SsrfError
from .ids import IdCodec
from .kavita import KavitaClient, build_client
from .opds import OpdsParseError
from .render import STATIC_DIR, templates
from .security import access_key_ok, ip_allowed, sanitize_filename

log = logging.getLogger("retroshelf")

# Routes that must never be gated by the access-key / IP-allowlist middleware
# (the container HEALTHCHECK + the stylesheet). [M-7]
_OPEN_PREFIXES = ("/health", "/static")


class FeedCache:
    """Tiny bounded TTL cache keyed by the bridge feed id (NOT the apiKey URL)."""

    def __init__(self, ttl_seconds: int, max_entries: int = 256):
        self._ttl = ttl_seconds
        self._max = max_entries
        self._data: dict[str, tuple[float, opds.Feed]] = {}

    def get(self, key: str) -> opds.Feed | None:
        if self._ttl <= 0:
            return None
        hit = self._data.get(key)
        if not hit:
            return None
        ts, feed = hit
        if time.monotonic() - ts > self._ttl:
            self._data.pop(key, None)
            return None
        return feed

    def put(self, key: str, feed: opds.Feed) -> None:
        if self._ttl <= 0:
            return
        if len(self._data) >= self._max:
            # Evict the oldest entry.
            oldest = min(self._data.items(), key=lambda kv: kv[1][0])[0]
            self._data.pop(oldest, None)
        self._data[key] = (time.monotonic(), feed)


class _SecretMaskingFilter(logging.Filter):
    """Safety net: mask the apiKey/access key in EVERY log record, including
    those emitted by third-party libraries (e.g. httpx logging the full URL). [H7]"""

    def __init__(self, cfg: Config):
        super().__init__()
        self._cfg = cfg

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self._cfg.mask(record.getMessage())
            record.args = ()
        except Exception:  # never let logging crash the request
            pass
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg: Config = app.state.config
    client = build_client(user_agent=cfg.upstream_user_agent)
    app.state.http = client
    app.state.kavita = KavitaClient(cfg, client)
    app.state.ids = IdCodec(cfg.bridge_id_secret or cfg.bridge_access_key)
    app.state.cache = FeedCache(cfg.cache_feeds_seconds)
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO))
    mask_filter = _SecretMaskingFilter(cfg)
    for handler in logging.getLogger().handlers:
        handler.addFilter(mask_filter)
    log.info("RetroShelf started; proxying %s", cfg.mask(cfg.kavita_origin))
    try:
        yield
    finally:
        await client.aclose()


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    app = FastAPI(title="RetroShelf", lifespan=lifespan)
    app.state.config = cfg
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # -- middleware: optional access key + IP allowlist ----------------------
    @app.middleware("http")
    async def gate(request: Request, call_next):
        path = request.url.path
        if not any(path.startswith(p) for p in _OPEN_PREFIXES):
            client_ip = request.client.host if request.client else None
            if not ip_allowed(client_ip, cfg.allowed_ips):
                return _error_response(request, "Forbidden", "This bridge is restricted to the local network.", 403)
            provided = request.query_params.get("key") or request.headers.get("x-access-key")
            if not access_key_ok(provided, cfg.bridge_access_key):
                return _error_response(request, "Access key required", "Append ?key=YOURKEY to the address.", 403)
        return await call_next(request)

    # -- error handlers ------------------------------------------------------
    @app.exception_handler(RetroShelfError)
    async def handle_domain_error(request: Request, exc: RetroShelfError):
        status, heading = _status_for(exc)
        # The exception message is already masked by the raiser; mask again defensively.
        msg = cfg.mask(str(exc)) if cfg.debug else _friendly_message(exc)
        log.warning("%s on %s: %s", type(exc).__name__, request.url.path, cfg.mask(str(exc)))
        return _error_response(request, heading, msg, status)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        # Fail LOUD in the logs (full type + masked detail + traceback) but never
        # leak internals to the user. [H9]
        log.exception("Unexpected %s on %s: %s", type(exc).__name__,
                      request.url.path, cfg.mask(str(exc)))
        return _error_response(request, "Something went wrong",
                               "An unexpected error occurred. Check the server logs.", 500)

    _register_routes(app, cfg)
    return app


def _status_for(exc: Exception) -> tuple[int, str]:
    if isinstance(exc, BadIdError):
        return 404, "Not found"
    if isinstance(exc, SsrfError):
        return 400, "Bad request"
    if isinstance(exc, (KavitaError, OpdsParseError)):
        return 502, "Library unavailable"
    return 500, "Something went wrong"


def _friendly_message(exc: Exception) -> str:
    if isinstance(exc, BadIdError):
        return "That link is not valid or has expired."
    if isinstance(exc, SsrfError):
        return "That request was refused for safety."
    if isinstance(exc, KavitaError):
        return "Could not reach your Kavita library. Check that it is running."
    if isinstance(exc, OpdsParseError):
        return "Could not read the library feed."
    return "Please try again."


def _error_response(request: Request, heading: str, message: str, status: int) -> Response:
    return templates.TemplateResponse(
        request, "error.html", {"heading": heading, "message": message}, status_code=status
    )


def _register_routes(app: FastAPI, cfg: Config) -> None:

    def kc(request: Request) -> KavitaClient:
        return request.app.state.kavita

    def codec(request: Request) -> IdCodec:
        return request.app.state.ids

    # -- view-model seam: encode every upstream href as a bridge id [H-2] ----
    def _to_view_model(feed: opds.Feed, ids: IdCodec, kavita: KavitaClient) -> list[dict]:
        entries = []
        for e in feed.entries:
            if e.is_navigation and e.nav_href:
                try:
                    url = kavita.resolve_url(e.nav_href)
                except SsrfError:
                    continue
                entries.append({"is_nav": True, "title": (e.title or "").strip() or "Untitled",
                                "href": f"/feed/{ids.encode(url)}"})
                continue
            acq = e.primary_acquisition
            if acq is None:
                continue
            try:
                acq_url = kavita.resolve_url(acq.href)
            except SsrfError:
                continue
            fmt = format_of(acq.media_type) or "epub"
            badge = "EPUB" if fmt == "epub" else "PDF"
            cover_bridge = None
            if cfg.show_covers and e.cover_url:
                try:
                    cover_bridge = f"/cover/{ids.encode(kavita.resolve_url(e.cover_url))}"
                except SsrfError:
                    cover_bridge = None
            record = json.dumps({
                "u": acq_url, "m": acq.media_type, "t": e.title, "a": e.author,
                "s": e.summary, "c": (kavita.resolve_url(e.cover_url) if (cfg.show_covers and e.cover_url) else None),
            }, separators=(",", ":"))
            entries.append({
                "is_nav": False,
                "title": (e.title or "").strip() or "Untitled",
                "author": e.author,
                "badge": badge,
                "detail_url": f"/book/{ids.encode(record)}",
                "cover_url": cover_bridge,
            })
        return entries

    async def _load_feed(request: Request, url: str, cache_key: str) -> opds.Feed:
        cache: FeedCache = request.app.state.cache
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        body = await kc(request).fetch_feed(url)
        feed = opds.parse(body)
        cache.put(cache_key, feed)
        return feed

    # -- pages ---------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        root_url = cfg.kavita_opds_url
        kavita_ok, detail = True, ""
        try:
            await kc(request).fetch_feed(root_url)
        except RetroShelfError as exc:
            kavita_ok, detail = False, _friendly_message(exc)
        root_id = codec(request).encode(kc(request).resolve_url(root_url))
        return templates.TemplateResponse(request, "home.html", {
            "kavita_ok": kavita_ok, "status_detail": detail,
            "root_feed_url": f"/feed/{root_id}",
        })

    @app.get("/feed/{fid}", response_class=HTMLResponse)
    async def feed(request: Request, fid: str):
        url = kc(request).resolve_url(codec(request).decode(fid))  # decode + re-validate SSRF
        parsed = await _load_feed(request, url, fid)
        entries = _to_view_model(parsed, codec(request), kc(request))
        next_url = f"/feed/{codec(request).encode(kc(request).resolve_url(parsed.next_url))}" if parsed.next_url else None
        prev_url = f"/feed/{codec(request).encode(kc(request).resolve_url(parsed.prev_url))}" if parsed.prev_url else None
        return templates.TemplateResponse(request, "feed.html", {
            "feed_title": parsed.title or "Library",
            "entries": entries, "next_url": next_url, "prev_url": prev_url,
            "search_url": "/search",
        })

    @app.get("/book/{bid}", response_class=HTMLResponse)
    async def book(request: Request, bid: str):
        try:
            rec = json.loads(codec(request).decode(bid))
        except (ValueError, TypeError) as exc:
            raise BadIdError("Malformed book id") from exc
        fmt = format_of(rec.get("m", "")) or "epub"
        badge = "EPUB" if fmt == "epub" else "PDF"
        download_id = codec(request).encode(rec["u"])
        filename = sanitize_filename(rec.get("t"), "epub" if fmt == "epub" else "pdf")
        cover_url = None
        if rec.get("c"):
            cover_url = f"/cover/{codec(request).encode(rec['c'])}"
        return templates.TemplateResponse(request, "book.html", {
            "title": rec.get("t") or "Untitled", "author": rec.get("a") or "",
            "summary": rec.get("s") or "", "badge": badge, "cover_url": cover_url,
            "download_url": f"/download/{download_id}/{filename}", "back_url": "/",
        })

    @app.get("/search", response_class=HTMLResponse)
    async def search(request: Request, q: str = ""):
        q = (q or "").strip()
        entries = []
        search_error = False
        if q:
            search_url = f"{cfg.kavita_opds_url}/search?query={quote(q)}"
            try:
                body = await kc(request).fetch_feed(search_url)
                parsed = opds.parse(body)
                entries = _to_view_model(parsed, codec(request), kc(request))
            except RetroShelfError as exc:
                # Search endpoint missing/unreachable — tell the user it's
                # unavailable rather than silently showing "no results". [H6]
                search_error = True
                log.info("search failed for %r: %s", q, cfg.mask(str(exc)))
        return templates.TemplateResponse(
            request, "search.html", {"query": q, "entries": entries, "search_error": search_error}
        )

    @app.get("/help", response_class=HTMLResponse)
    async def help_page(request: Request):
        return templates.TemplateResponse(request, "help.html", {})

    @app.get("/health")
    async def health():
        return PlainTextResponse("ok")

    # -- downloads / covers --------------------------------------------------
    async def _do_download(request: Request, did: str):
        url = kc(request).resolve_url(codec(request).decode(did))  # decode + re-validate
        # Media type is derived from the acquisition URL suffix (Kavita download
        # URLs end in the real filename, .epub/.pdf), defaulting to EPUB.
        media_type, disposition, ext = _media_for_url(url, cfg)
        filename = sanitize_filename(_basename(url), ext)
        if request.method == "HEAD":
            # Answer header probes (e.g. `curl -I`, Safari) without fetching the
            # body. Same headers a GET would produce.
            headers = build_download_headers(filename=filename, disposition=disposition)
            return Response(status_code=200, media_type=media_type, headers=headers)
        range_header = request.headers.get("range")
        return await stream_download(
            kc(request), url, media_type=media_type, filename=filename,
            disposition=disposition, range_header=range_header,
        )

    @app.api_route("/download/{did}/{filename}", methods=["GET", "HEAD"])
    async def download_named(request: Request, did: str, filename: str):
        return await _do_download(request, did)

    @app.api_route("/download/{did}", methods=["GET", "HEAD"])
    async def download(request: Request, did: str):
        return await _do_download(request, did)

    @app.api_route("/open/{did}", methods=["GET", "HEAD"])
    async def open_alias(request: Request, did: str):
        return await _do_download(request, did)

    @app.api_route("/cover/{cid}", methods=["GET", "HEAD"])
    async def cover(request: Request, cid: str):
        # A cover failure must NOT render a full HTML error page into an <img>;
        # return a tiny empty 404 so the browser just shows a broken image. [H5]
        try:
            url = kc(request).resolve_url(codec(request).decode(cid))
            if request.method == "HEAD":
                return Response(status_code=200, media_type="image/jpeg",
                                headers={"Cache-Control": "private, max-age=86400"})
            range_header = request.headers.get("range")
            return await stream_cover(kc(request), url, range_header=range_header)
        except RetroShelfError as exc:
            log.info("cover unavailable on %s: %s", request.url.path, cfg.mask(str(exc)))
            return Response(status_code=404, media_type="image/gif")


def _basename(url: str) -> str:
    from urllib.parse import urlsplit
    path = urlsplit(url).path
    return path.rsplit("/", 1)[-1] if "/" in path else path


def _media_for_url(url: str, cfg: Config) -> tuple[str, str, str]:
    """Return (media_type, disposition, ext) inferred from the download URL's
    extension. Kavita download URLs end in the real filename (.epub/.pdf)."""
    low = url.lower()
    if ".pdf" in low:
        return PDF_MIME, cfg.pdf_disposition, "pdf"
    # Default to EPUB (the common case) for .epub or anything else.
    return EPUB_MIME, cfg.epub_disposition, "epub"


# Module-level app for `uvicorn app.main:app`. Built lazily-safe: if config is
# missing at import, defer to a clear error at startup rather than crashing import
# in tools that only introspect the module.
try:
    app = create_app()
except ConfigError as _cfg_exc:  # pragma: no cover - only when env unset
    _startup_error = _cfg_exc
    app = FastAPI(title="RetroShelf (unconfigured)")

    @app.get("/health")
    async def _health_unconfigured():
        return PlainTextResponse("ok")

    @app.get("/{_path:path}")
    async def _unconfigured(_path: str):
        return PlainTextResponse(
            f"RetroShelf is not configured: {_startup_error}", status_code=500
        )
