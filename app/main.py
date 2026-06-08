"""RetroShelf FastAPI application: routes, lifespan, middleware, feed cache,
and the opds→ids→render→download wiring.

Everything the iPad sees is a bridge id; the Kavita apiKey is held server-side
and never appears in a response body. Every decoded id is re-validated through
the SSRF guard before any upstream fetch. [C-3][C-6][H-2]

:var log: Module-level logger named ``retroshelf``.
:var _OPEN_PREFIXES: Path prefixes that bypass access-key / IP-allowlist
    middleware (health check and static assets). [M-7]
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
    """Tiny bounded TTL cache keyed by the bridge feed id (NOT the apiKey URL).

    Entries older than *ttl_seconds* are considered stale and evicted on the
    next access. When the cache is at capacity the oldest entry is evicted
    before a new one is inserted.
    """

    def __init__(self, ttl_seconds: int, max_entries: int = 256):
        """Initialise the cache.

        :param ttl_seconds: Lifetime of each cached entry in seconds.
            A value of ``0`` or negative disables caching entirely.
        :type ttl_seconds: int
        :param max_entries: Maximum number of entries to hold before evicting
            the oldest one.
        :type max_entries: int
        """
        self._ttl = ttl_seconds
        self._max = max_entries
        self._data: dict[str, tuple[float, opds.Feed]] = {}

    def get(self, key: str) -> opds.Feed | None:
        """Return a cached feed if it exists and has not expired.

        :param key: The bridge feed id used as the cache key.
        :type key: str
        :returns: The cached :class:`~app.opds.Feed`, or ``None`` when the
            entry is missing, stale, or caching is disabled.
        :rtype: opds.Feed or None
        """
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
        """Store a feed in the cache, evicting the oldest entry if necessary.

        Does nothing when caching is disabled (``ttl_seconds <= 0``).

        :param key: The bridge feed id used as the cache key.
        :type key: str
        :param feed: The parsed OPDS feed to cache.
        :type feed: opds.Feed
        """
        if self._ttl <= 0:
            return
        if len(self._data) >= self._max:
            # Evict the oldest entry.
            oldest = min(self._data.items(), key=lambda kv: kv[1][0])[0]
            self._data.pop(oldest, None)
        self._data[key] = (time.monotonic(), feed)


class _SecretMaskingFilter(logging.Filter):
    """Safety net: mask the apiKey/access key in EVERY log record, including
    those emitted by third-party libraries (e.g. httpx logging the full URL). [H7]

    Attached to all root-logger handlers during :func:`lifespan` startup so
    that secrets cannot appear in any sink regardless of their origin.
    """

    def __init__(self, cfg: Config):
        """Initialise the filter with the active configuration.

        :param cfg: Application configuration object whose
            :meth:`~app.config.Config.mask` method is called on every
            formatted log message.
        :type cfg: Config
        """
        super().__init__()
        self._cfg = cfg

    def filter(self, record: logging.LogRecord) -> bool:
        """Mask secrets in *record* before it is emitted.

        Replaces ``record.msg`` with the masked, fully-formatted message and
        clears ``record.args`` so the logging machinery does not re-format it.
        Exceptions inside masking are silently swallowed to prevent the filter
        from crashing a request. [H7]

        :param record: The log record to sanitise in-place.
        :type record: logging.LogRecord
        :returns: Always ``True`` so the record is never suppressed.
        :rtype: bool
        """
        try:
            record.msg = self._cfg.mask(record.getMessage())
            record.args = ()
        except Exception:  # never let logging crash the request
            pass
        return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """AsyncContextManager that wires shared state onto *app* at startup and
    tears it down on shutdown.

    On entry:

    * Creates and attaches an :class:`~app.kavita.KavitaClient`,
      :class:`~app.ids.IdCodec`, and :class:`FeedCache` to ``app.state``.
    * Configures the root logger at the level specified by
      :attr:`~app.config.Config.log_level`.
    * Attaches a :class:`_SecretMaskingFilter` to every root-logger handler.

    On exit:

    * Closes the underlying ``httpx`` async client gracefully.

    :param app: The FastAPI application instance whose ``state`` will be
        populated.
    :type app: FastAPI
    :raises ConfigError: Propagates if the configuration is invalid (raised
        earlier by :func:`create_app`, not here directly).
    """
    cfg: Config = app.state.config
    client = build_client(user_agent=cfg.upstream_user_agent)
    app.state.http = client
    app.state.kavita = KavitaClient(cfg, client)
    app.state.ids = IdCodec(cfg.bridge_id_secret or cfg.bridge_access_key)
    app.state.cache = FeedCache(cfg.cache_feeds_seconds)
    app.state.search_template = None  # discovered + cached on first search
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
    """Build and return the configured :class:`~fastapi.FastAPI` application.

    Mounts static files, registers the HTTP middleware (IP allowlist and
    access-key gate), attaches domain-error and catch-all exception handlers,
    and delegates route registration to :func:`_register_routes`.

    :param config: Pre-built configuration object to use.  When ``None``,
        :func:`~app.config.load_config` is called to read the environment.
    :type config: Config or None
    :returns: A fully configured FastAPI application ready to be served.
    :rtype: FastAPI
    :raises ConfigError: If *config* is ``None`` and the environment is
        missing required variables (raised by :func:`~app.config.load_config`).
    """
    cfg = config or load_config()
    app = FastAPI(title="RetroShelf", lifespan=lifespan)
    app.state.config = cfg
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    # -- middleware: optional access key + IP allowlist ----------------------
    @app.middleware("http")
    async def gate(request: Request, call_next):
        """HTTP middleware: enforce IP allowlist and access-key checks.

        Requests whose path begins with any prefix in :data:`_OPEN_PREFIXES`
        (``/health``, ``/static``) are passed through unconditionally.  All
        other requests must originate from an allowed IP and supply a valid
        access key (via ``?key=`` query parameter or ``X-Access-Key`` header).

        :param request: The incoming HTTP request.
        :type request: Request
        :param call_next: ASGI callable for the next middleware or route handler.
        :returns: A 403 :class:`~fastapi.responses.Response` on access denial,
            otherwise the response produced by the downstream handler.
        :rtype: Response
        """
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
        """Exception handler for all :class:`~app.errors.RetroShelfError` subclasses.

        Maps the exception type to an HTTP status code and heading via
        :func:`_status_for`, then renders ``error.html``.  In debug mode the
        masked raw exception message is shown; otherwise a generic friendly
        message is produced by :func:`_friendly_message`.

        :param request: The request that triggered the exception.
        :type request: Request
        :param exc: The domain exception to handle.
        :type exc: RetroShelfError
        :returns: An HTML error response with the appropriate status code.
        :rtype: Response
        """
        status, heading = _status_for(exc)
        # The exception message is already masked by the raiser; mask again defensively.
        msg = cfg.mask(str(exc)) if cfg.debug else _friendly_message(exc)
        log.warning("%s on %s: %s", type(exc).__name__, request.url.path, cfg.mask(str(exc)))
        return _error_response(request, heading, msg, status)

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception):
        """Catch-all exception handler for any unhandled :class:`Exception`.

        Logs the full masked traceback at ``ERROR`` level but returns only a
        generic message to the client so that internal details are never leaked.
        [H9]

        :param request: The request that triggered the exception.
        :type request: Request
        :param exc: The unexpected exception.
        :type exc: Exception
        :returns: A 500 HTML error response with a generic message.
        :rtype: Response
        """
        # Fail LOUD in the logs (full type + masked detail + traceback) but never
        # leak internals to the user. [H9]
        log.exception("Unexpected %s on %s: %s", type(exc).__name__,
                      request.url.path, cfg.mask(str(exc)))
        return _error_response(request, "Something went wrong",
                               "An unexpected error occurred. Check the server logs.", 500)

    _register_routes(app, cfg)
    return app


def _status_for(exc: Exception) -> tuple[int, str]:
    """Return the HTTP status code and a short human-readable heading for *exc*.

    :param exc: The exception to classify.
    :type exc: Exception
    :returns: A 2-tuple of ``(status_code, heading)`` where *status_code* is
        the integer HTTP status and *heading* is a brief title string.
    :rtype: tuple[int, str]
    """
    if isinstance(exc, BadIdError):
        return 404, "Not found"
    if isinstance(exc, SsrfError):
        return 400, "Bad request"
    if isinstance(exc, (KavitaError, OpdsParseError)):
        return 502, "Library unavailable"
    return 500, "Something went wrong"


def _friendly_message(exc: Exception) -> str:
    """Return a safe, user-facing error message for *exc*.

    The message contains no internal details, stack traces, or secrets.
    Used by :func:`create_app` exception handlers when debug mode is off.

    :param exc: The exception to describe.
    :type exc: Exception
    :returns: A short string suitable for display in the browser error page.
    :rtype: str
    """
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
    """Render ``error.html`` as an :class:`~fastapi.responses.HTMLResponse`.

    :param request: The current HTTP request (required by the template engine).
    :type request: Request
    :param heading: Short error title shown as the page heading.
    :type heading: str
    :param message: Longer descriptive message shown in the error body.
    :type message: str
    :param status: HTTP status code for the response.
    :type status: int
    :returns: A rendered HTML response with *status* as the status code.
    :rtype: Response
    """
    return templates.TemplateResponse(
        request, "error.html", {"heading": heading, "message": message}, status_code=status
    )


def _register_routes(app: FastAPI, cfg: Config) -> None:
    """Register all URL routes on *app* using *cfg* for configuration.

    Defines and registers:

    * Two private helper closures (``kc``, ``codec``) for extracting shared
      state from the request.
    * ``_to_view_model`` — translates a parsed OPDS feed into template dicts.
    * ``_load_feed`` — cache-aware feed fetcher.
    * ``_do_download`` — shared download/HEAD logic reused by all download routes.
    * All page, download, cover, health, and search route handlers.

    :param app: The FastAPI application to register routes on.
    :type app: FastAPI
    :param cfg: Active application configuration used inside route closures.
    :type cfg: Config
    """

    def kc(request: Request) -> KavitaClient:
        """Return the :class:`~app.kavita.KavitaClient` stored on *request.app.state*.

        :param request: The current HTTP request.
        :type request: Request
        :returns: The shared Kavita client for this application instance.
        :rtype: KavitaClient
        """
        return request.app.state.kavita

    def codec(request: Request) -> IdCodec:
        """Return the :class:`~app.ids.IdCodec` stored on *request.app.state*.

        :param request: The current HTTP request.
        :type request: Request
        :returns: The shared id codec for this application instance.
        :rtype: IdCodec
        """
        return request.app.state.ids

    # -- view-model seam: encode every upstream href as a bridge id [H-2] ----
    def _to_view_model(feed: opds.Feed, ids: IdCodec, kavita: KavitaClient) -> list[dict]:
        """Convert a parsed OPDS *feed* into a list of template-ready dicts.

        Every upstream URL (navigation href, acquisition href, cover URL) is
        SSRF-validated via :meth:`~app.kavita.KavitaClient.resolve_url` and
        then encoded as a bridge id.  Entries whose primary URL fails
        validation are silently dropped. [H-2]

        :param feed: The parsed OPDS feed whose entries are to be converted.
        :type feed: opds.Feed
        :param ids: Id codec used to encode upstream URLs as opaque bridge ids.
        :type ids: IdCodec
        :param kavita: Kavita client used to validate and resolve upstream URLs.
        :type kavita: KavitaClient
        :returns: A list of dicts ready for Jinja2 template rendering.  Each
            dict has an ``is_nav`` boolean key and further keys depending on
            whether the entry is a navigation link or an acquisition entry.
        :rtype: list[dict]
        """
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
        """Fetch and parse an OPDS feed, returning a cached copy when available.

        :param request: The current HTTP request (used to access shared state).
        :type request: Request
        :param url: The fully-resolved upstream OPDS URL to fetch.
        :type url: str
        :param cache_key: The bridge feed id used as the :class:`FeedCache` key.
        :type cache_key: str
        :returns: The parsed :class:`~app.opds.Feed`.
        :rtype: opds.Feed
        :raises KavitaError: If the upstream request fails.
        :raises OpdsParseError: If the response body cannot be parsed as OPDS.
        """
        cache: FeedCache = request.app.state.cache
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
        body = await kc(request).fetch_feed(url)
        feed = opds.parse(body)
        cache.put(cache_key, feed)
        return feed

    async def _resolve_search_url(request: Request, q: str) -> str:
        """Build the upstream OPDS search URL for query *q*.

        Prefers the catalogue's advertised OpenSearch template (the root feed's
        ``rel="search"`` link containing a ``{searchTerms}`` placeholder, e.g.
        ManyBooks' ``/opds/search?q={searchTerms}``). Falls back to Kavita's
        ``/search?query=`` endpoint when no usable template is advertised.

        The discovered template is cached on ``app.state`` so we don't re-fetch
        the root feed on every search (which can trip upstream rate limits and
        fall back to the wrong query parameter).

        :param request: The incoming request (for the shared Kavita client).
        :param q: The user's search text (will be percent-encoded).
        :returns: An absolute or root-relative upstream search URL.
        :rtype: str
        """
        encoded = quote(q)
        # Use the cached template if we've already discovered it (``""`` means
        # "discovered, none advertised" so we don't keep re-fetching the root).
        template = getattr(request.app.state, "search_template", None)
        if template is None:
            try:
                root = opds.parse(await kc(request).fetch_feed(cfg.kavita_opds_url))
                template = root.search_url or ""
            except RetroShelfError:
                template = ""
            request.app.state.search_template = template
        if template and "{searchTerms}" in template:
            url = template.replace("{searchTerms}", encoded)
            # Drop any remaining OpenSearch optional tokens (e.g. {startIndex?}).
            while "{" in url and "}" in url:
                start = url.index("{")
                url = url[:start] + url[url.index("}", start) + 1:]
            return url.rstrip("?&")
        return f"{cfg.kavita_opds_url}/search?query={encoded}"

    # -- pages ---------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request):
        """GET ``/`` — render the home/status page.

        Probes the Kavita OPDS root feed to report connectivity status and
        provides a link to the root feed page.

        :param request: The incoming HTTP request.
        :type request: Request
        :returns: Rendered ``home.html`` with ``kavita_ok``, ``status_detail``,
            and ``root_feed_url`` template variables.
        :rtype: HTMLResponse
        """
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
        """GET ``/feed/{fid}`` — render a paginated OPDS feed page.

        Decodes the bridge feed id *fid*, re-validates the resolved URL through
        the SSRF guard, fetches (or returns a cached) feed, converts entries to
        view-model dicts, and renders ``feed.html``.

        :param request: The incoming HTTP request.
        :type request: Request
        :param fid: Opaque bridge id that encodes the upstream OPDS feed URL.
        :type fid: str
        :returns: Rendered ``feed.html`` with feed title, entries, and
            pagination URLs.
        :rtype: HTMLResponse
        :raises BadIdError: If *fid* cannot be decoded or the resolved URL
            fails SSRF validation.
        :raises KavitaError: If the upstream OPDS request fails.
        """
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
        """GET ``/book/{bid}`` — render the book detail / download page.

        Decodes the bridge book id *bid* (a JSON record encoded by
        :func:`_to_view_model`) and renders ``book.html`` with title, author,
        summary, format badge, cover URL, and a safe download URL.

        :param request: The incoming HTTP request.
        :type request: Request
        :param bid: Opaque bridge id that encodes a JSON book record.
        :type bid: str
        :returns: Rendered ``book.html`` with book metadata and download link.
        :rtype: HTMLResponse
        :raises BadIdError: If *bid* cannot be decoded or the JSON record is
            malformed.
        """
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
        """GET ``/search?q=QUERY`` — render the search results page.

        When *q* is non-empty, queries the Kavita OPDS search endpoint and
        converts results to view-model dicts.  If the search endpoint is
        unavailable the page renders with ``search_error=True`` rather than
        silently showing zero results. [H6]

        :param request: The incoming HTTP request.
        :type request: Request
        :param q: The search query string (defaults to empty string).
        :type q: str
        :returns: Rendered ``search.html`` with ``query``, ``entries``, and
            ``search_error`` template variables.
        :rtype: HTMLResponse
        """
        q = (q or "").strip()
        entries = []
        search_error = False
        if q:
            try:
                search_url = await _resolve_search_url(request, q)
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
        """GET ``/help`` — render the static help page.

        :param request: The incoming HTTP request.
        :type request: Request
        :returns: Rendered ``help.html`` with no additional template variables.
        :rtype: HTMLResponse
        """
        return templates.TemplateResponse(request, "help.html", {})

    @app.get("/health")
    async def health():
        """GET ``/health`` — container health-check endpoint.

        Returns the plain-text string ``ok`` with a 200 status.  This route
        is excluded from access-key / IP-allowlist middleware so container
        orchestrators can probe it without credentials. [M-7]

        :returns: Plain-text ``"ok"`` with HTTP 200.
        :rtype: PlainTextResponse
        """
        return PlainTextResponse("ok")

    # -- downloads / covers --------------------------------------------------
    async def _do_download(request: Request, did: str):
        """Shared GET/HEAD download logic reused by all download route handlers.

        Decodes the bridge download id *did*, re-validates the resolved URL
        through the SSRF guard, determines the media type and disposition from
        the URL extension, and either returns headers only (HEAD) or streams
        the file body (GET) with optional range support.

        :param request: The incoming HTTP request.  ``request.method`` is
            inspected to decide between HEAD and GET behaviour.
        :type request: Request
        :param did: Opaque bridge id that encodes the upstream download URL.
        :type did: str
        :returns: A headers-only :class:`~fastapi.responses.Response` for HEAD
            requests, or a streaming response for GET requests.
        :rtype: Response
        :raises BadIdError: If *did* cannot be decoded or the resolved URL
            fails SSRF validation.
        :raises KavitaError: If the upstream download request fails.
        """
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
        """GET/HEAD ``/download/{did}/{filename}`` — download a book with an explicit filename.

        The *filename* path segment is accepted for URL aesthetics (e.g. Safari
        uses it as the saved filename) but is otherwise ignored; the actual
        filename is derived from the upstream URL.

        :param request: The incoming HTTP request.
        :type request: Request
        :param did: Opaque bridge id that encodes the upstream download URL.
        :type did: str
        :param filename: Decorative filename segment; not used for routing logic.
        :type filename: str
        :returns: Streaming book download or headers-only response for HEAD.
        :rtype: Response
        """
        return await _do_download(request, did)

    @app.api_route("/download/{did}", methods=["GET", "HEAD"])
    async def download(request: Request, did: str):
        """GET/HEAD ``/download/{did}`` — download a book without an explicit filename.

        :param request: The incoming HTTP request.
        :type request: Request
        :param did: Opaque bridge id that encodes the upstream download URL.
        :type did: str
        :returns: Streaming book download or headers-only response for HEAD.
        :rtype: Response
        """
        return await _do_download(request, did)

    @app.api_route("/open/{did}", methods=["GET", "HEAD"])
    async def open_alias(request: Request, did: str):
        """GET/HEAD ``/open/{did}`` — alias of the download route for ADE / reader apps.

        Some e-reader applications (e.g. Adobe Digital Editions) POST or GET
        to ``/open/`` paths.  This alias delegates entirely to
        :func:`_do_download`.

        :param request: The incoming HTTP request.
        :type request: Request
        :param did: Opaque bridge id that encodes the upstream download URL.
        :type did: str
        :returns: Streaming book download or headers-only response for HEAD.
        :rtype: Response
        """
        return await _do_download(request, did)

    @app.api_route("/cover/{cid}", methods=["GET", "HEAD"])
    async def cover(request: Request, cid: str):
        """GET/HEAD ``/cover/{cid}`` — proxy a book cover image from Kavita.

        Cover failures return a tiny empty 404 GIF rather than a full HTML
        error page so that a broken cover does not corrupt an ``<img>`` tag in
        the browser. [H5]

        :param request: The incoming HTTP request.
        :type request: Request
        :param cid: Opaque bridge id that encodes the upstream cover image URL.
        :type cid: str
        :returns: Streaming JPEG cover image, a 200 headers-only response for
            HEAD requests, or a 404 empty GIF on any retrieval error.
        :rtype: Response
        """
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
    """Return the final path segment of *url* (i.e. the filename part).

    :param url: An absolute URL whose path basename is required.
    :type url: str
    :returns: The last ``/``-delimited component of the URL's path, or the
        full path if it contains no ``/``.
    :rtype: str
    """
    from urllib.parse import urlsplit
    path = urlsplit(url).path
    return path.rsplit("/", 1)[-1] if "/" in path else path


def _media_for_url(url: str, cfg: Config) -> tuple[str, str, str]:
    """Return ``(media_type, disposition, ext)`` inferred from the download URL's
    file extension.

    Kavita download URLs end in the real filename (e.g. ``book.epub`` or
    ``book.pdf``), so the extension is sufficient to determine the MIME type
    and Content-Disposition mode.  Anything that does not contain ``.pdf`` is
    treated as EPUB.

    :param url: The upstream download URL to inspect.
    :type url: str
    :param cfg: Application configuration supplying
        :attr:`~app.config.Config.epub_disposition` and
        :attr:`~app.config.Config.pdf_disposition`.
    :type cfg: Config
    :returns: A 3-tuple of ``(media_type, disposition, extension)`` where
        *media_type* is the MIME type string, *disposition* is either
        ``"inline"`` or ``"attachment"``, and *extension* is ``"pdf"`` or
        ``"epub"``.
    :rtype: tuple[str, str, str]
    """
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
