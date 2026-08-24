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

import asyncio
import gzip
import json
import logging
import os
import random
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from . import __version__, opds
from .config import Config, ConfigError, FeedSource, load_config, origin_tuple
from .download import (
    EPUB_MIME,
    PDF_MIME,
    format_of,
    stream_cover,
    stream_download,
)
from .download import (
    _safe_image_type,
)
from .download import (
    build_headers as build_download_headers,
)
from .errors import BadIdError, KavitaError, ReaderError, RetroShelfError, SsrfError
from .ids import IdCodec
from .kavita import KavitaClient, build_client
from .opds import OpdsParseError
from .publish import ACQ_TYPE, NAV_TYPE, build_feed
from .reader import (
    DEFAULT_SPLIT,
    SPLIT_TARGETS,
    load_chapter,
    load_manifest,
    part_containing,
    parts_for,
    percent_of,
    shelve_book,
)
from .render import STATIC_DIR, templates
from .security import access_key_ok, ip_allowed, sanitize_filename
from .store import Store, book_key

log = logging.getLogger("retroshelf")

# Routes that must never be gated by the access-key / IP-allowlist middleware
# (the container HEALTHCHECK + the stylesheet). [M-7]
_OPEN_PREFIXES = ("/health", "/static")

# Cookie that remembers a validated BRIDGE_ACCESS_KEY for the rest of the visit,
# so internal links never have to carry the secret. HttpOnly and SameSite=Lax;
# both attributes are simply ignored by iOS 5/6 Safari, which has no JavaScript
# running here to protect against anyway. [SS-16]
_KEY_COOKIE = "rs_key"

# Current-page sort keys for /feed (SS-03). Sorting only reorders the books on
# the page already fetched — pagination is upstream-driven, so we never hold the
# whole library. Navigation entries keep their order and stay grouped first.
_SORT_KEYS = ("title", "author", "format")
_FORMAT_ORDER = {"EPUB": 0, "PDF": 1}

# Defence-in-depth headers applied to HTML pages only.
#
# Every one of these is *ignored* by the browsers RetroShelf exists for — iOS
# 5/6 Safari does not implement CSP, Referrer-Policy, or frame-ancestors — and
# an unknown response header has never broken an old browser. They cost the
# vintage iPad nothing and protect the modern desktop browser that an
# administrator uses to configure the same bridge. The policy is written to
# match what this app actually is: server-rendered HTML, one same-origin
# stylesheet, same-origin images, and no JavaScript at all. Notably absent is
# ``upgrade-insecure-requests`` — RetroShelf is served over plain HTTP on a
# LAN, and upgrading would break every link on the iPad.
_HTML_SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    # ``same-origin`` and not ``no-referrer``: the Back link is derived from the
    # Referer header, and it is only ever read for same-origin navigation.
    "Referrer-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'none'; "
        "img-src 'self' data:; "
        "style-src 'self'; "
        "form-action 'self'; "
        "base-uri 'none'; "
        "frame-ancestors 'none'"
    ),
}


# The metadata keys a book record carries through a bridge id. Projection onto
# this set keeps tokens short: a stored record's bookkeeping fields (``key``,
# ``added``, ``when``, ``feed_name``) never ride along in a URL.
_RECORD_KEYS = ("u", "m", "t", "a", "s", "c")


def _record_id(codec: IdCodec, record: dict) -> str:
    """Encode a book record as the opaque id behind ``/book/{bid}``.

    Includes per-format download info (``fmts``) when the record has it, so a
    book opened from the Reading List or the "recently sent" shelf offers the
    same download buttons as one opened from a feed.

    :param codec: The application id codec.
    :param record: A book view-record (from a feed or the store).
    :returns: An opaque token safe for a URL path segment.
    :rtype: str
    """
    slim: dict = {k: record.get(k) for k in _RECORD_KEYS}
    if record.get("fmts"):
        slim["fmts"] = record["fmts"]
    return codec.encode(json.dumps(slim, separators=(",", ":")))


def _download_record(record: dict, fm: dict) -> dict:
    """Return the single-format record encoded into a ``/download/{did}`` id.

    Combines one format's URL/MIME with the book's display metadata, so the
    download history can show a title even for ids minted per-format.

    :param record: The full book record.
    :param fm: One entry of the record's ``fmts`` list.
    :rtype: dict
    """
    return {"u": fm["u"], "m": fm.get("m"), "t": record.get("t"),
            "a": record.get("a"), "s": record.get("s"), "c": record.get("c")}


def _sorted_page(entries: list[dict], sort: str) -> list[dict]:
    """Return *entries* with book entries stably reordered by *sort*.

    Navigation entries (``is_nav``) keep their original order and stay grouped
    ahead of book entries. An empty/unknown *sort* returns *entries* unchanged
    (upstream order). The sort is stable, so it never drops or duplicates rows.

    :param entries: View-model dicts from ``_to_view_model``.
    :param sort: One of :data:`_SORT_KEYS`, or ``""`` for upstream order.
    :returns: A new list; the input is not mutated.
    """
    if sort not in _SORT_KEYS:
        return entries
    navs = [e for e in entries if e["is_nav"]]
    books = [e for e in entries if not e["is_nav"]]
    if sort == "title":
        books = sorted(books, key=lambda e: (e.get("title") or "").casefold())
    elif sort == "author":
        # Empty authors sort last; otherwise case-insensitive by author.
        books = sorted(books, key=lambda e: ((e.get("author") or "") == "",
                                             (e.get("author") or "").casefold()))
    else:  # "format" — group EPUB before PDF, then by title
        books = sorted(books, key=lambda e: (_FORMAT_ORDER.get(e.get("badge") or "", 2),
                                             (e.get("title") or "").casefold()))
    return navs + books


class FeedCache:
    """Tiny bounded TTL cache keyed by the bridge feed id (NOT the apiKey URL).

    Entries older than *ttl_seconds* are considered stale and evicted on the
    next access. When the cache is at capacity the oldest entry is evicted
    before a new one is inserted.
    """

    def __init__(self, ttl_seconds: int, max_entries: int = 256) -> None:
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

    def __init__(self, cfg: Config) -> None:
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


def _install_mask_filter(cfg: Config) -> None:
    """Attach a :class:`_SecretMaskingFilter` to every live logging sink.

    Filters on the *root* logger only cover records that propagate to it.
    ``uvicorn``, ``uvicorn.access``, ``uvicorn.error`` and ``httpx`` each
    install their own handlers with ``propagate = False``, so a root-only
    filter never sees them — and ``uvicorn.access`` logs the full request
    line, which is exactly where ``?key=…`` would appear. Attaching to both
    the logger and each of its handlers covers records however they arrive.
    [H-7]

    :param cfg: Active configuration supplying :meth:`~app.config.Config.mask`.
    """
    mask_filter = _SecretMaskingFilter(cfg)
    manager_loggers = list(logging.Logger.manager.loggerDict.values())
    targets = [logging.getLogger()] + [
        lg for lg in manager_loggers if isinstance(lg, logging.Logger)
    ]
    for name in ("uvicorn", "uvicorn.access", "uvicorn.error", "httpx", "httpcore"):
        logger = logging.getLogger(name)
        if logger not in targets:
            targets.append(logger)
    for logger in targets:
        logger.addFilter(mask_filter)
        for handler in logger.handlers:
            handler.addFilter(mask_filter)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """AsyncContextManager that wires shared state onto *app* at startup and
    tears it down on shutdown.

    On entry:

    * Creates and attaches an :class:`~app.kavita.KavitaClient`,
      :class:`~app.ids.IdCodec`, :class:`FeedCache`, and
      :class:`~app.store.Store` to ``app.state``.
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
    app.state.search_templates = {}  # per-feed-origin OpenSearch templates, cached
    app.state.store = Store(cfg.state_path)  # Reading List + download history
    logging.basicConfig(level=getattr(logging, cfg.log_level.upper(), logging.INFO))
    _install_mask_filter(cfg)
    # One triage line an operator can read at a glance: what is being fronted
    # and which optional protections are actually active in this deployment.
    log.info(
        "RetroShelf %s started: %d feed(s) [%s]; access key %s; IP allowlist %s; "
        "id secret %s; state=%s cache=%s",
        __version__, len(cfg.feeds),
        ", ".join(f"{f.name} @ {f.origin}" for f in cfg.feeds),
        "on" if cfg.bridge_access_key else "off",
        f"on ({len(cfg.allowed_ips)} rule{'s' if len(cfg.allowed_ips) != 1 else ''})"
        if cfg.allowed_ips else "off",
        "stable" if (cfg.bridge_id_secret or cfg.bridge_access_key) else "ephemeral",
        cfg.state_dir, cfg.cache_dir,
    )
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
    async def gate(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Response:
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
        from_query = False
        if not any(path.startswith(p) for p in _OPEN_PREFIXES):
            client_ip = request.client.host if request.client else None
            if not ip_allowed(client_ip, cfg.allowed_ips):
                log.info("denied %s from %s: not in ALLOWED_IPS", path, client_ip)
                return _error_response(request, "Forbidden", "This bridge is restricted to the local network.", 403)
            query_key = request.query_params.get("key")
            # The key may arrive three ways: in the URL (how you first open the
            # bridge on the iPad), as a header (scripts, OPDS readers), or in
            # the cookie set on the first successful visit. Without the cookie,
            # every internal link would have to carry the secret — which
            # 403'd real navigation, and put the key in the address bar, the
            # Referer of every request, and every access-log line. [SS-16]
            provided = (query_key
                        or request.headers.get("x-access-key")
                        or request.cookies.get(_KEY_COOKIE))
            if not access_key_ok(provided, cfg.bridge_access_key):
                log.info("denied %s from %s: missing or wrong access key",
                         path, client_ip)
                return _error_response(
                    request, "Access key required",
                    "Append ?key=YOURKEY to the address once; "
                    "this browser will remember it.", 403)
            from_query = bool(query_key) and cfg.bridge_access_key is not None
        response = await call_next(request)
        if from_query:
            # Remember it so the rest of the visit needs no ?key= at all.
            response.set_cookie(_KEY_COOKIE, cfg.bridge_access_key, max_age=31536000,
                                httponly=True, samesite="lax", path="/")
        return response

    # -- middleware: static caching + HTML-only gzip (SS-04 polish) -----------
    @app.middleware("http")
    async def polish(request: Request, call_next: Callable[[Request], Awaitable[Any]]) -> Response:
        """Add a long cache header to ``/static`` and gzip HTML pages.

        Gzip is applied **only** to ``text/html`` responses when the client
        sent ``Accept-Encoding: gzip`` — the content-type is checked *before*
        the body is consumed, so streaming proxy responses (``/download``,
        ``/cover``) and any Range request are never buffered or compressed.
        This protects book import and ``206``/``Content-Range`` behaviour.
        """
        response = await call_next(request)
        if request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "public, max-age=604800"
        accepts_gzip = "gzip" in request.headers.get("accept-encoding", "")
        ctype = response.headers.get("content-type", "")
        if ctype.startswith("text/html"):
            # HTML pages only. A book stream must keep exactly the headers
            # :func:`~app.download.build_headers` chose for iOS. [SS-10]
            for name, value in _HTML_SECURITY_HEADERS.items():
                response.headers.setdefault(name, value)
        if (accepts_gzip and ctype.startswith("text/html")
                and response.status_code == 200
                and "range" not in request.headers
                and "content-encoding" not in response.headers):
            body = b"".join([chunk async for chunk in response.body_iterator])
            packed = gzip.compress(body)
            headers = dict(response.headers)
            headers.pop("content-length", None)
            headers["content-encoding"] = "gzip"
            headers["content-length"] = str(len(packed))
            vary = headers.get("vary")
            headers["vary"] = f"{vary}, Accept-Encoding" if vary else "Accept-Encoding"
            return Response(content=packed, status_code=response.status_code,
                            headers=headers, media_type=ctype)
        return response

    # -- error handlers ------------------------------------------------------
    @app.exception_handler(RetroShelfError)
    async def handle_domain_error(request: Request, exc: RetroShelfError) -> Response:
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
    async def handle_unexpected(request: Request, exc: Exception) -> Response:
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


# One row per failure mode: (exception type, HTTP status, page heading,
# user-facing message). First match wins, so subclasses must precede their
# bases. Every message is complete and secret-free on its own — this table is
# the only place the two lookups below can drift apart, so they can't.
_ERROR_TABLE: tuple[tuple[type[Exception], int, str, str], ...] = (
    (BadIdError, 404, "Not found", "That link is not valid or has expired."),
    (SsrfError, 400, "Bad request", "That request was refused for safety."),
    (ReaderError, 502, "Can't read this book",
     "This book can't be read in the browser — use Open in iBooks instead."),
    (KavitaError, 502, "Library unavailable",
     "Could not reach your Kavita library. Check that it is running."),
    (OpdsParseError, 502, "Library unavailable", "Could not read the library feed."),
)
_ERROR_DEFAULT = (500, "Something went wrong", "Please try again.")


def _status_for(exc: Exception) -> tuple[int, str]:
    """Return the HTTP status code and page heading for *exc*.

    :param exc: The exception to classify.
    :returns: ``(status_code, heading)``.
    :rtype: tuple[int, str]
    """
    for exc_type, status, heading, _message in _ERROR_TABLE:
        if isinstance(exc, exc_type):
            return status, heading
    return _ERROR_DEFAULT[0], _ERROR_DEFAULT[1]


def _friendly_message(exc: Exception) -> str:
    """Return a safe, user-facing error message for *exc*.

    The message contains no internal details, stack traces, or secrets.
    Used by :func:`create_app` exception handlers when debug mode is off.

    :param exc: The exception to describe.
    :returns: A short string suitable for the browser error page.
    :rtype: str
    """
    for exc_type, _status, _heading, message in _ERROR_TABLE:
        if isinstance(exc, exc_type):
            return message
    return _ERROR_DEFAULT[2]


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


# Matches a sanitizer-emitted placeholder (``{IMG:n}`` / ``{CH:i}``) whose
# index is restricted to this charset. sanitize_chapter only ever emits plain
# decimal indexes here, but the substitution result is spliced straight into
# an HTML attribute and rendered with ``| safe`` — so the regex itself must
# refuse to match anything wider than a bare id, never trust the input shape. [SS-04]
_PLACEHOLDER_RE = re.compile(r"\{(IMG|CH):([A-Za-z0-9/._-]+)\}")


def _substitute_placeholders(html: str, bid: str) -> str:
    """Replace sanitizer placeholders with concrete ``/read/{bid}/...`` URLs.

    ``{IMG:n}`` becomes ``/read/{bid}/img/{n}``; ``{CH:i}`` becomes
    ``/read/{bid}/{i}/1`` (the first part of chapter *i*). Only characters in
    :data:`_PLACEHOLDER_RE`'s charset can appear in the substituted index, so
    no attacker-controlled text can widen the emitted URL beyond a path
    segment. [SS-04]

    :param html: A joined chapter part's HTML, straight from
        :func:`app.reader.sanitize_chapter`.
    :param bid: The book's bridge id, already known-good (decoded upstream
        of this call).
    :returns: *html* with every placeholder replaced by a concrete URL.
    :rtype: str
    """

    def repl(m: re.Match[str]) -> str:
        kind, value = m.group(1), m.group(2)
        if kind == "IMG":
            return f"/read/{bid}/img/{value}"
        return f"/read/{bid}/{value}/1"

    return _PLACEHOLDER_RE.sub(repl, html)


def _register_routes(app: FastAPI, cfg: Config) -> None:
    """Register all URL routes on *app* using *cfg* for configuration.

    Defines and registers:

    * Private helper closures (``kc``, ``codec``, ``store``) for extracting
      shared state from the request.
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

    def store(request: Request) -> Store:
        """Return the :class:`~app.store.Store` (Reading List + history)."""
        return request.app.state.store

    # -- view-model seam: encode every upstream href as a bridge id [H-2] ----
    def _to_view_model(feed: opds.Feed, ids: IdCodec, kavita: KavitaClient,
                       base_url: str | None = None, downloaded: set | None = None) -> list[dict]:
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
        :param base_url: URL of the feed document the entries came from;
            relative hrefs are resolved against it. ``None`` restricts
            resolution to root-relative paths on the primary origin.
        :type base_url: str or None
        :param downloaded: Set of already-downloaded book keys used to flag
            entries, or ``None`` to skip flagging.
        :type downloaded: set or None
        :returns: A list of dicts ready for Jinja2 template rendering.  Each
            dict has an ``is_nav`` boolean key and further keys depending on
            whether the entry is a navigation link or an acquisition entry.
        :rtype: list[dict]
        """
        entries = []
        for e in feed.entries:
            if e.is_navigation and e.nav_href:
                try:
                    nav_url = kavita.resolve_url(e.nav_href, base=base_url)
                except SsrfError:
                    continue
                entries.append({"is_nav": True, "title": (e.title or "").strip() or "Untitled",
                                "href": f"/feed/{ids.encode(nav_url)}"})
                continue
            # Only surface EPUB/PDF — the formats old iPads import into iBooks.
            # Entries offering only mobi/Kindle/CBZ are skipped, not mislabeled.
            acq = e.supported_acquisition
            if acq is None:
                continue
            try:
                acq_url = kavita.resolve_url(acq.href, base=base_url)
            except SsrfError:
                continue
            badge = "EPUB" if acq.is_epub else "PDF"
            cover_abs = None
            if cfg.show_covers and e.cover_url:
                try:
                    cover_abs = kavita.resolve_url(e.cover_url, base=base_url)
                except SsrfError:
                    cover_abs = None
            cover_bridge = f"/cover/{ids.encode(cover_abs)}" if cover_abs else None
            # One download per format (first EPUB and/or first PDF) with size.
            fmts = []
            epub = next((a for a in e.acquisitions if a.is_epub), None)
            pdf = next((a for a in e.acquisitions if a.is_pdf), None)
            for a, fmt in ((epub, "epub"), (pdf, "pdf")):
                if a is None:
                    continue
                try:
                    furl = kavita.resolve_url(a.href, base=base_url)
                except SsrfError:
                    continue
                fmts.append({"u": furl, "m": a.media_type, "f": fmt, "len": a.length})
            bid = _record_id(ids, {
                "u": acq_url, "m": acq.media_type, "t": e.title, "a": e.author,
                "s": e.summary, "c": cover_abs, "fmts": fmts,
            })
            entries.append({
                "is_nav": False,
                "title": (e.title or "").strip() or "Untitled",
                "author": e.author,
                "badge": badge,
                "detail_url": f"/book/{bid}",
                "cover_url": cover_bridge,
                "downloaded": downloaded is not None and book_key(acq_url) in downloaded,
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

    def _feed_for_url(url: str) -> FeedSource:
        """Return the configured :class:`FeedSource` whose origin owns *url*.

        Used to scope search to the right library and to label feed pages.
        Falls back to the primary feed when no origin matches.

        :param url: An upstream OPDS URL.
        :rtype: app.config.FeedSource
        """
        try:
            want = origin_tuple(url)
        except Exception:  # noqa: BLE001
            return cfg.feeds[0]
        for f in cfg.feeds:
            try:
                if origin_tuple(f.origin) == want:
                    return f
            except Exception:  # noqa: BLE001
                continue
        return cfg.feeds[0]

    async def _resolve_search_url(request: Request, q: str, feed_url: str) -> str:
        """Build the upstream OPDS search URL for query *q* within one feed.

        Prefers that feed's advertised OpenSearch template (its root feed's
        ``rel="search"`` link with a ``{searchTerms}`` placeholder, e.g.
        ManyBooks' ``/opds/search?q={searchTerms}``). Falls back to the Kavita
        ``/search?query=`` convention when no template is advertised.

        Templates are cached per feed origin on ``app.state.search_templates`` so
        we don't re-fetch a root feed on every search (which can trip upstream
        rate limits and fall back to the wrong query parameter).

        :param request: The incoming request (for the shared Kavita client).
        :param q: The user's search text (will be percent-encoded).
        :param feed_url: The root URL of the feed being searched.
        :returns: An absolute or root-relative upstream search URL.
        :rtype: str
        """
        encoded = quote(q)
        cache = getattr(request.app.state, "search_templates", None)
        if cache is None:
            cache = {}
            request.app.state.search_templates = cache
        key: tuple[str, str, int] | str
        try:
            key = origin_tuple(feed_url)
        except Exception:  # noqa: BLE001
            key = feed_url
        template = cache.get(key)
        if template is None:
            try:
                root = opds.parse(await kc(request).fetch_feed(feed_url))
                template = root.search_url or ""
            except RetroShelfError:
                template = ""
            cache[key] = template
        if template and "{searchTerms}" in template:
            url = template.replace("{searchTerms}", encoded)
            # Drop any remaining OpenSearch optional tokens (e.g. {startIndex?}).
            while "{" in url and "}" in url:
                start = url.index("{")
                url = url[:start] + url[url.index("}", start) + 1:]
            url = url.rstrip("?&")
        else:
            url = f"{feed_url.rstrip('/')}/search?query={encoded}"
        # Resolve to an absolute, SSRF-checked URL against THIS feed's origin so a
        # root-relative template on a secondary feed targets the right server.
        return kc(request).resolve_url(url, base=feed_url)

    # -- diagnostics + insight pages -----------------------------------------
    @app.get("/status", response_class=HTMLResponse)
    async def status_page(request: Request) -> HTMLResponse:
        """GET ``/status`` — live health of every configured library."""
        async def check(source: FeedSource) -> dict:
            """Probe one library's root feed, timing it; never raises.

            :param source: The configured feed to probe.
            :returns: A status row dict for ``status.html``.
            :rtype: dict
            """
            t0 = time.monotonic()
            try:
                body = await kc(request).fetch_feed(source.url)
                ms = int((time.monotonic() - t0) * 1000)
                try:
                    shelves = len(opds.parse(body).entries)
                except OpdsParseError:
                    shelves = None
                return {"name": source.name, "origin": source.origin,
                        "online": True, "ms": ms, "shelves": shelves}
            except RetroShelfError as exc:
                ms = int((time.monotonic() - t0) * 1000)
                return {"name": source.name, "origin": source.origin, "online": False,
                        "ms": ms, "detail": _friendly_message(exc)}
        rows = list(await asyncio.gather(*[check(f) for f in cfg.feeds]))
        return templates.TemplateResponse(request, "status.html", {"rows": rows})

    @app.get("/random")
    async def surprise_me(request: Request) -> Response:
        """GET ``/random`` — "Surprise Me": a budgeted random walk into the
        libraries that jumps to a random book. Tolerant of dead-ends/404s — it
        skips a failed branch and tries others rather than giving up."""
        sources = list(cfg.feeds)
        random.shuffle(sources)
        visited: set[str] = set()
        for source in sources[:2]:               # try up to 2 libraries
            try:
                frontier = [kc(request).resolve_url(source.url)]
            except RetroShelfError:
                continue
            budget = 10                          # max upstream fetches per library
            while frontier and budget > 0:
                budget -= 1
                url = frontier.pop(random.randrange(len(frontier)))
                if url in visited:
                    continue
                visited.add(url)
                try:
                    parsed = opds.parse(await kc(request).fetch_feed(url))
                except RetroShelfError:
                    continue                     # bad branch — try another
                vm = _to_view_model(parsed, codec(request), kc(request), base_url=url)
                books = [e for e in vm if not e["is_nav"]]
                if books:
                    return RedirectResponse(random.choice(books)["detail_url"], status_code=303)
                # Grow the frontier from the parsed feed directly — encoding a
                # nav href into a bridge id only to decode it again is wasted
                # crypto on a hot loop.
                for entry in parsed.entries:
                    if entry.is_navigation and entry.nav_href:
                        try:
                            frontier.append(kc(request).resolve_url(entry.nav_href, base=url))
                        except RetroShelfError:
                            continue
        return _error_response(request, "No luck",
                               "Could not pick a random book just now. Try browsing a library.", 502)

    # -- pages ---------------------------------------------------------------
    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request) -> HTMLResponse:
        """GET ``/`` — render the home/status page.

        Probes the primary (first) configured feed to report connectivity and
        builds the portal menu of all configured libraries.

        :param request: The incoming HTTP request.
        :type request: Request
        :returns: Rendered ``home.html`` with connectivity status, the portal
            feed menu, the Reading List count, and the recent-downloads shelf.
        :rtype: HTMLResponse
        """
        # Connectivity status reflects the primary (first) feed.
        primary = cfg.feeds[0]
        kavita_ok, detail = True, ""
        try:
            await kc(request).fetch_feed(primary.url)
        except RetroShelfError as exc:
            kavita_ok, detail = False, _friendly_message(exc)
        # Build the portal menu: one entry per configured feed, each URL
        # resolved and encoded exactly once.
        feed_ids = [codec(request).encode(kc(request).resolve_url(f.url)) for f in cfg.feeds]
        menu = [{"name": f.name, "url": f"/feed/{fid}"}
                for f, fid in zip(cfg.feeds, feed_ids)]
        primary_id = feed_ids[0]
        multi = len(cfg.feeds) > 1
        # "Recently sent to iBooks" shelf + Reading List count.
        recent = []
        for rec in store(request).recent_downloads(8):
            bid = _record_id(codec(request), rec)
            fmt = format_of(rec.get("m", "")) or "epub"
            recent.append({"title": rec.get("t") or "Untitled", "author": rec.get("a") or "",
                           "badge": "EPUB" if fmt == "epub" else "PDF",
                           "detail_url": f"/book/{bid}", "downloaded": True})
        # "Currently Reading" shelf: up to 4 most-recently-read positions. [SS-05]
        reading = []
        for pos in store(request).reading_list(4):
            rbid = _record_id(codec(request), pos)
            pct = int(pos.get("percent", 0))
            reading.append({
                "title": pos.get("t") or "Untitled", "author": pos.get("a") or "",
                "detail_url": f"/read/{rbid}",
                "progress": "finished" if pct >= 100 else f"{pct}%",
            })
        return templates.TemplateResponse(request, "home.html", {
            "kavita_ok": kavita_ok, "status_detail": detail,
            "feeds": menu, "multi": multi,
            "root_feed_url": menu[0]["url"],   # back-compat for single-feed
            # From home, search every library at once; a single feed searches itself.
            "search_feed": "*" if multi else primary_id,
            "reading_count": len(store(request).favorite_keys()),
            "recent": recent,
            "reading": reading,
        })

    @app.get("/feed/{fid}", response_class=HTMLResponse)
    async def feed(request: Request, fid: str, sort: str = "") -> HTMLResponse:
        """GET ``/feed/{fid}`` — render a paginated OPDS feed page.

        Decodes the bridge feed id *fid*, re-validates the resolved URL through
        the SSRF guard, fetches (or returns a cached) feed, converts entries to
        view-model dicts, and renders ``feed.html``.

        The optional ``sort`` query parameter (``title``/``author``/``format``)
        reorders the **books on this page only** — pagination is upstream-driven,
        so the whole library is never in hand. Any other value falls back to the
        upstream order. [SS-03]

        :param request: The incoming HTTP request.
        :type request: Request
        :param fid: Opaque bridge id that encodes the upstream OPDS feed URL.
        :type fid: str
        :param sort: Current-page sort key (``title``/``author``/``format``).
        :type sort: str
        :returns: Rendered ``feed.html`` with feed title, entries, and
            pagination URLs.
        :rtype: HTMLResponse
        :raises BadIdError: If *fid* cannot be decoded or the resolved URL
            fails SSRF validation.
        :raises KavitaError: If the upstream OPDS request fails.
        """
        url = kc(request).resolve_url(codec(request).decode(fid))  # decode + re-validate SSRF
        parsed = await _load_feed(request, url, fid)
        entries = _to_view_model(parsed, codec(request), kc(request), base_url=url,
                                 downloaded=store(request).downloaded_keys())
        sort = sort if sort in _SORT_KEYS else ""   # normalize unknown → upstream order
        entries = _sorted_page(entries, sort)
        suffix = f"?sort={sort}" if sort else ""    # carry the sort across pages
        next_url = f"/feed/{codec(request).encode(kc(request).resolve_url(parsed.next_url, base=url))}{suffix}" if parsed.next_url else None
        prev_url = f"/feed/{codec(request).encode(kc(request).resolve_url(parsed.prev_url, base=url))}{suffix}" if parsed.prev_url else None
        # Scope the on-page search box to the library this feed belongs to.
        owner = _feed_for_url(url)
        search_feed = codec(request).encode(kc(request).resolve_url(owner.url))
        has_books = any(not e["is_nav"] for e in entries)
        return templates.TemplateResponse(request, "feed.html", {
            "feed_title": parsed.title or "Library",
            "entries": entries, "next_url": next_url, "prev_url": prev_url,
            "search_url": "/search", "search_feed": search_feed,
            "sort": sort, "feed_path": f"/feed/{fid}", "has_books": has_books,
        })

    @app.get("/book/{bid}", response_class=HTMLResponse)
    async def book(request: Request, bid: str) -> HTMLResponse:
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
        cover_url = None
        if rec.get("c"):
            cover_url = f"/cover/{codec(request).encode(rec['c'])}"
        key = book_key(rec.get("u", ""))
        is_fav = store(request).is_favorite(key)

        # In-browser reader entry point (EPUB only; PDFs keep their existing
        # inline-viewer behaviour and get no reader button). [SS-05]
        read_url = None
        read_label = None
        read_hint = False
        if fmt == "epub":
            read_url = f"/read/{bid}"
            pos = store(request).get_position(key)
            if pos is None:
                read_label = "Read here"
                read_hint = True
            elif int(pos.get("percent", 0)) >= 100:
                read_label = "Read again — finished"
            else:
                read_label = (
                    f"Continue reading (Ch. {int(pos.get('chapter', 0)) + 1} · "
                    f"{int(pos.get('percent', 0))}%)"
                )

        # One download button per available format (EPUB/PDF), with size.
        fmts = rec.get("fmts") or [{"u": rec.get("u"), "m": rec.get("m"), "f": fmt, "len": None}]
        dlkeys = store(request).downloaded_keys()
        downloads = []
        for fm in fmts:
            ext = "pdf" if fm.get("f") == "pdf" else "epub"
            fid = _record_id(codec(request), _download_record(rec, fm))
            fname = sanitize_filename(rec.get("t"), ext)
            downloads.append({
                "badge": ext.upper(),
                "url": f"/download/{fid}/{fname}",
                "size": _human_size(fm.get("len")),
                "label": "Open in iBooks" if ext == "epub" else "Open PDF",
            })
        downloaded = any(book_key(fm["u"]) in dlkeys for fm in fmts)
        author = rec.get("a") or ""
        # Cross-library author discovery: search every library (fan-out) for more.
        author_search = None
        if author:
            if len(cfg.feeds) > 1:
                scope = "*"
            else:
                owner = _feed_for_url(rec.get("u", ""))
                scope = codec(request).encode(kc(request).resolve_url(owner.url))
            author_search = f"/search?q={quote(author)}&feed={scope}"
        return templates.TemplateResponse(request, "book.html", {
            "title": rec.get("t") or "Untitled", "author": author,
            "summary": rec.get("s") or "", "badge": badge, "cover_url": cover_url,
            "author_search": author_search,
            "downloads": downloads,
            "downloaded": downloaded,
            "is_fav": is_fav,
            "star_url": f"/unstar/{key}" if is_fav else f"/star/{bid}",
            "star_label": "Remove from Reading List" if is_fav else "Add to Reading List",
            "read_url": read_url, "read_label": read_label, "read_hint": read_hint,
            "back_url": _back_to(request),
        })

    @app.get("/search", response_class=HTMLResponse)
    async def search(request: Request, q: str = "", feed: str = "") -> HTMLResponse:
        """GET ``/search?q=QUERY[&feed=FID]`` — render the search results page.

        Searches within one library (the *feed* bridge id, or the primary feed
        when omitted), using that feed's OpenSearch template. If the endpoint is
        unavailable the page renders ``search_error=True`` rather than silently
        showing zero results. [H6]

        :param request: The incoming HTTP request.
        :param q: The search query string (defaults to empty string).
        :param feed: Optional bridge id selecting which library to search.
        :returns: Rendered ``search.html``.
        :rtype: HTMLResponse
        """
        q = (q or "").strip()
        multi = len(cfg.feeds) > 1
        # ``feed == "*"`` means fan out across every configured library.
        fan_out = feed == "*" and multi
        # One history snapshot for the whole request; the fan-out groups run
        # concurrently and must not each re-read (and re-lock) the store.
        downloaded = store(request).downloaded_keys()

        async def _search_one(source: FeedSource) -> dict:
            """Search one library; never raises — failures become error groups."""
            try:
                su = await _resolve_search_url(request, q, source.url)
                body = await kc(request).fetch_feed(su)
                ents = _to_view_model(opds.parse(body), codec(request), kc(request),
                                      base_url=source.url, downloaded=downloaded)
                return {"name": source.name, "entries": ents, "error": False}
            except RetroShelfError as exc:
                log.info("search failed in %r for %r: %s", source.name, q, cfg.mask(str(exc)))
                return {"name": source.name, "entries": [], "error": True}

        groups: list[dict] = []
        feed_name = None
        if fan_out:
            search_feed = "*"
            if q:
                groups = list(await asyncio.gather(*[_search_one(f) for f in cfg.feeds]))
        else:
            # Single library: the given feed id, or the primary feed.
            target = cfg.feeds[0]
            if feed and feed != "*":
                try:
                    target = _feed_for_url(kc(request).resolve_url(codec(request).decode(feed)))
                except RetroShelfError:
                    target = cfg.feeds[0]
            feed_name = target.name
            search_feed = codec(request).encode(kc(request).resolve_url(target.url))
            if q:
                groups = [await _search_one(target)]

        total = sum(len(g["entries"]) for g in groups)
        return templates.TemplateResponse(request, "search.html", {
            "query": q, "groups": groups, "total": total, "fan_out": fan_out,
            "multi": multi, "search_feed": search_feed, "feed_name": feed_name,
        })

    @app.get("/help", response_class=HTMLResponse)
    async def help_page(request: Request) -> HTMLResponse:
        """GET ``/help`` — render the static help page.

        :param request: The incoming HTTP request.
        :type request: Request
        :returns: Rendered ``help.html`` with no additional template variables.
        :rtype: HTMLResponse
        """
        return templates.TemplateResponse(request, "help.html", {})

    # -- Reading List (cross-feed favourites) --------------------------------
    @app.get("/list", response_class=HTMLResponse)
    async def reading_list(request: Request) -> HTMLResponse:
        """GET ``/list`` — the cross-library Reading List of starred books."""
        items = []
        for rec in store(request).favorites():
            bid = _record_id(codec(request), rec)
            fmt = format_of(rec.get("m", "")) or "epub"
            items.append({
                "title": rec.get("t") or "Untitled", "author": rec.get("a") or "",
                "badge": "EPUB" if fmt == "epub" else "PDF",
                "detail_url": f"/book/{bid}", "feed_name": rec.get("feed_name"),
                "unstar_url": f"/unstar/{rec.get('key')}",
                "cover_url": f"/cover/{codec(request).encode(rec['c'])}" if rec.get("c") else None,
            })
        return templates.TemplateResponse(request, "list.html", {"items": items})

    def _require_site_token(request: Request) -> Response | None:
        """Refuse a state-changing request that did not come from our own pages.

        ``/star``, ``/unstar`` and ``/prefs`` change server-side state from a
        plain ``GET`` link — which is exactly what an old iPad needs, since
        RetroShelf ships no JavaScript and a ``<form>`` would spoil the plain
        hyperlink UI. The cost is that any other page on the network could
        trigger one with a single ``<img src="http://retroshelf/unstar/…">``.

        The fix that keeps the links plain: every such link carries ``t=``, an
        unguessable token derived from the bridge secret. Our own templates
        know it; a foreign page does not. No cookie, no JavaScript, no header —
        nothing an iOS 5 browser has to understand. [SS-15]

        :param request: The incoming request.
        :returns: A 403 response when the token is missing or wrong, else
            ``None`` to signal the caller may proceed.
        :rtype: Response or None
        """
        if codec(request).token_ok(request.query_params.get("t")):
            return None
        log.info("rejected untokened state change on %s", request.url.path)
        return _error_response(
            request, "Refused",
            "That action has to be started from a RetroShelf page. "
            "Open the book and try again.", 403,
        )

    @app.get("/star/{bid}")
    async def star(request: Request, bid: str) -> Response:
        """GET ``/star/{bid}`` — add a book to the Reading List, then go back."""
        refusal = _require_site_token(request)
        if refusal is not None:
            return refusal
        try:
            rec = json.loads(codec(request).decode(bid))
        except (ValueError, TypeError) as exc:
            raise BadIdError("Malformed book id") from exc
        rec["feed_name"] = _feed_for_url(kc(request).resolve_url(rec["u"])).name
        store(request).add_favorite(rec)
        return RedirectResponse(_back_to(request, default="/list"), status_code=303)

    @app.get("/unstar/{key}")
    async def unstar(request: Request, key: str) -> Response:
        """GET ``/unstar/{key}`` — remove a book from the Reading List."""
        refusal = _require_site_token(request)
        if refusal is not None:
            return refusal
        store(request).remove_favorite(key)
        return RedirectResponse(_back_to(request, default="/list"), status_code=303)

    # -- OPDS publisher: re-publish the Reading List as a real OPDS feed ------
    def _public_base(request: Request) -> str:
        """Return the public base URL used in published OPDS feed links.

        Prefers the configured ``BRIDGE_PUBLIC_URL``; falls back to the
        request's own base URL. Never ends in a ``/``.

        :param request: The incoming request.
        :rtype: str
        """
        return (cfg.bridge_public_url or str(request.base_url)).rstrip("/")

    def _key_suffix() -> str:
        """Return the ``?key=…`` suffix for published OPDS links, or ``""``.

        An external OPDS reader has no cookie primed by a browser visit, so
        the links it follows must carry the access key when one is configured.

        :rtype: str
        """
        return f"?key={cfg.bridge_access_key}" if cfg.bridge_access_key else ""

    @app.get("/opds")
    async def opds_root(request: Request) -> Response:
        """GET ``/opds`` — RetroShelf's own OPDS navigation catalog."""
        base, ks = _public_base(request), _key_suffix()
        xml = build_feed(
            feed_id="urn:retroshelf:opds", title="RetroShelf",
            self_href=f"{base}/opds{ks}", start_href=f"{base}/opds{ks}", kind="navigation",
            entries=[{
                "id": "urn:retroshelf:reading-list", "title": "My Reading List",
                "summary": "Books you starred across all your libraries.",
                "nav_href": f"{base}/opds/reading-list{ks}", "nav_type": ACQ_TYPE,
            }],
        )
        return Response(content=xml, media_type=NAV_TYPE)

    @app.get("/opds/reading-list")
    async def opds_reading_list(request: Request) -> Response:
        """GET ``/opds/reading-list`` — the Reading List as an OPDS acquisition
        feed, so any OPDS reader can subscribe to your curated shelf."""
        base, ks = _public_base(request), _key_suffix()
        entries = []
        for rec in store(request).favorites():
            fmts = rec.get("fmts") or [{"u": rec.get("u"), "m": rec.get("m"), "f": "epub"}]
            acqs = []
            for fm in fmts:
                if not fm.get("u"):
                    continue
                ext = "pdf" if fm.get("f") == "pdf" else "epub"
                did = _record_id(codec(request), _download_record(rec, fm))
                fname = sanitize_filename(rec.get("t"), ext)
                mime = fm.get("m") or ("application/epub+zip" if ext == "epub" else "application/pdf")
                acqs.append({"type": mime, "href": f"{base}/download/{did}/{fname}{ks}"})
            entries.append({
                "id": f"urn:retroshelf:book:{rec.get('key')}",
                "title": rec.get("t"), "author": rec.get("a"), "summary": rec.get("s"),
                "acquisitions": acqs,
                "cover_href": f"{base}/cover/{codec(request).encode(rec['c'])}{ks}" if rec.get("c") else None,
            })
        xml = build_feed(
            feed_id="urn:retroshelf:reading-list", title="RetroShelf — My Reading List",
            self_href=f"{base}/opds/reading-list{ks}", start_href=f"{base}/opds{ks}",
            kind="acquisition", entries=entries,
        )
        return Response(content=xml, media_type=ACQ_TYPE)

    # -- Accessibility preferences (optional cookies) ------------------------
    @app.get("/prefs")
    async def prefs(request: Request, big: str = "", covers: str = "",
                    color: str = "", split: str = "", reader: str = "",
                    next: str = "/") -> Response:
        """GET ``/prefs`` — toggle display prefs via cookies (no JS, optional).

        Everything works without the cookie; this only enhances. ``big=toggle``
        flips large-print; ``covers=off``/``on`` hides/shows covers;
        ``color=amber|green|white`` picks the CRT phosphor palette;
        ``split=small|medium|large|whole`` sets the reader's part size;
        ``reader=book|phosphor`` picks the reader's colour theme.
        """
        refusal = _require_site_token(request)
        if refusal is not None:
            return refusal
        resp = RedirectResponse(_safe_path(next, "/"), status_code=303)

        def remember(name: str, value: str) -> None:
            """Set a one-year display preference cookie.

            ``httponly`` and ``samesite`` are inert on iOS 5/6 Safari, which
            simply ignores attributes it does not know — they cost the iPad
            nothing and harden the same cookie in a modern browser.
            """
            resp.set_cookie(name, value, max_age=31536000,
                            httponly=True, samesite="lax", path="/")

        if big == "toggle":
            remember("rs_big", "0" if request.cookies.get("rs_big") == "1" else "1")
        if covers in ("on", "off"):
            remember("rs_covers", "1" if covers == "on" else "0")
        if color in ("amber", "green", "white"):
            remember("rs_color", color)
        if split in SPLIT_TARGETS:
            remember("rs_split", split)
        if reader in ("book", "phosphor"):
            remember("rs_reader_theme", reader)
        return resp

    @app.get("/health")
    async def health() -> PlainTextResponse:
        """GET ``/health`` — container health-check endpoint.

        Returns the plain-text string ``ok`` with a 200 status.  This route
        is excluded from access-key / IP-allowlist middleware so container
        orchestrators can probe it without credentials. [M-7]

        :returns: Plain-text ``"ok"`` with HTTP 200.
        :rtype: PlainTextResponse
        """
        return PlainTextResponse("ok")

    # -- downloads / covers --------------------------------------------------
    async def _do_download(request: Request, did: str, name_hint: str | None = None) -> Response:
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
        :param name_hint: Preferred filename (the URL ``{filename}`` segment, a
            clean title); falls back to the upstream URL basename. Some upstream
            URLs (e.g. Gutenberg's ``103.epub.noimages``) make ugly names.
        :type name_hint: str | None
        :returns: A headers-only :class:`~fastapi.responses.Response` for HEAD
            requests, or a streaming response for GET requests.
        :rtype: Response
        :raises BadIdError: If *did* cannot be decoded or the resolved URL
            fails SSRF validation.
        :raises KavitaError: If the upstream download request fails.
        """
        decoded = codec(request).decode(did)
        # The id may encode a full book record (JSON, so history gets a title) or
        # a bare URL (cover/legacy). Handle both.
        record = None
        try:
            parsed = json.loads(decoded)
            if isinstance(parsed, dict) and "u" in parsed:
                record, raw = parsed, parsed["u"]
            else:
                raw = decoded
        except (ValueError, TypeError):
            raw = decoded
        url = kc(request).resolve_url(raw)  # re-validate
        media_type, disposition, ext = _media_for_url(url, cfg)
        filename = sanitize_filename(name_hint or (record or {}).get("t") or _basename(url), ext)
        if request.method == "GET" and record is not None:
            store(request).record_download({**record, "feed_name": _feed_for_url(url).name})
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
    async def download_named(request: Request, did: str, filename: str) -> Response:
        """GET/HEAD ``/download/{did}/{filename}`` — download a book with an explicit filename.

        The *filename* path segment is the clean, title-based name old Safari
        uses for the saved file; it is also used for the Content-Disposition.

        :param request: The incoming HTTP request.
        :type request: Request
        :param did: Opaque bridge id that encodes the upstream download URL.
        :type did: str
        :param filename: The clean filename segment, used for the saved name.
        :type filename: str
        :returns: Streaming book download or headers-only response for HEAD.
        :rtype: Response
        """
        return await _do_download(request, did, name_hint=filename)

    @app.api_route("/download/{did}", methods=["GET", "HEAD"])
    async def download(request: Request, did: str) -> Response:
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
    async def open_alias(request: Request, did: str) -> Response:
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
    async def cover(request: Request, cid: str) -> Response:
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
            return await stream_cover(
                kc(request), url,
                cache_dir=cfg.cache_dir,
                cover_max_edge=cfg.cover_max_edge,
                cover_jpeg_quality=cfg.cover_jpeg_quality,
            )
        except RetroShelfError as exc:
            log.info("cover unavailable on %s: %s", request.url.path, cfg.mask(str(exc)))
            return Response(status_code=404, media_type="image/gif")

    # -- in-browser EPUB reader (SS-04) --------------------------------------
    def _decode_book_record(request: Request, bid: str) -> dict:
        """Decode *bid* into its book record dict, or raise :class:`BadIdError`.

        :param request: The incoming HTTP request.
        :param bid: Opaque bridge id, as minted by :func:`_record_id`.
        :returns: The decoded book record.
        :rtype: dict
        :raises BadIdError: If *bid* cannot be decoded, or does not decode to
            a JSON object.
        """
        try:
            rec = json.loads(codec(request).decode(bid))
        except (ValueError, TypeError) as exc:
            raise BadIdError("Malformed book id") from exc
        if not isinstance(rec, dict):
            raise BadIdError("Malformed book id")
        return rec

    def _split_target(request: Request) -> int | None:
        """Return the target part size (in characters) for *request*.

        Reads the ``rs_split`` cookie set by the extended ``/prefs`` route;
        an unset or unrecognised cookie value falls back to
        :data:`~app.reader.DEFAULT_SPLIT`.

        :param request: The incoming HTTP request.
        :returns: Target characters per part, or ``None`` for one part per
            chapter (the ``"whole"`` setting).
        :rtype: int or None
        """
        split = request.cookies.get("rs_split", DEFAULT_SPLIT)
        if split not in SPLIT_TARGETS:
            split = DEFAULT_SPLIT
        return SPLIT_TARGETS[split]

    @app.get("/read/{bid}")
    async def read_book(request: Request, bid: str) -> Response:
        """GET ``/read/{bid}`` — open a book in the in-browser reader.

        Shelves the book on first open (parses and sanitizes the EPUB into
        the reader cache); every later open is a local cache hit. Redirects
        (303) to the reader's resume position when one is stored, else to
        chapter 0 part 1.

        :param request: The incoming HTTP request.
        :param bid: Opaque bridge id that encodes a JSON book record.
        :returns: A 303 redirect into the reader, or a friendly 404 for a
            non-EPUB record.
        :rtype: Response
        :raises BadIdError: If *bid* cannot be decoded.
        :raises ReaderError: If the book cannot be shelved (DRM, malformed,
            oversized, etc.) — rendered via the ``_ERROR_TABLE`` row.
        """
        rec = _decode_book_record(request, bid)
        if format_of(rec.get("m", "")) != "epub":
            return _error_response(
                request, "Not found", "Only EPUB books can be read in the browser.", 404
            )
        key = book_key(rec.get("u", ""))
        manifest = load_manifest(cfg.cache_dir, key)
        if manifest is None:
            manifest = await shelve_book(kc(request), rec, cfg.cache_dir)
        pos = store(request).get_position(key)
        if pos is not None and 0 <= int(pos.get("chapter", 0)) < len(manifest.chapters):
            chapter = int(pos["chapter"])
            blocks = load_chapter(cfg.cache_dir, key, chapter)
            parts = parts_for([len(b) for b in blocks], _split_target(request))
            part = part_containing(int(pos.get("block", 0)), parts)
            return RedirectResponse(f"/read/{bid}/{chapter}/{part}", status_code=303)
        return RedirectResponse(f"/read/{bid}/0/1", status_code=303)

    @app.get("/read/{bid}/toc", response_class=HTMLResponse)
    async def read_toc(request: Request, bid: str) -> Response:
        """GET ``/read/{bid}/toc`` — render the book's table of contents.

        :param request: The incoming HTTP request.
        :param bid: Opaque bridge id that encodes a JSON book record.
        :returns: Rendered ``toc.html`` with the chapter list, or a friendly
            404 for a non-EPUB record.
        :rtype: Response
        :raises BadIdError: If *bid* cannot be decoded.
        :raises ReaderError: If the book cannot be shelved.
        """
        rec = _decode_book_record(request, bid)
        if format_of(rec.get("m", "")) != "epub":
            return _error_response(
                request, "Not found", "Only EPUB books can be read in the browser.", 404
            )
        key = book_key(rec.get("u", ""))
        manifest = load_manifest(cfg.cache_dir, key)
        if manifest is None:
            manifest = await shelve_book(kc(request), rec, cfg.cache_dir)
        pos = store(request).get_position(key)
        current_chapter = int(pos["chapter"]) if pos is not None else None
        chapters = [
            {"index": i, "title": c.title, "current": i == current_chapter}
            for i, c in enumerate(manifest.chapters)
        ]
        return templates.TemplateResponse(request, "toc.html", {
            "book_title": manifest.title, "bid": bid, "chapters": chapters,
        })

    # Registered before /read/{bid}/{chapter}/{part}: routing is match-order
    # sensitive, and "img" would otherwise be swallowed as a non-numeric
    # {chapter} segment (a 422, not the 404 the image route promises).
    @app.get("/read/{bid}/img/{n}")
    async def read_image(request: Request, bid: str, n: int) -> Response:
        """GET ``/read/{bid}/img/{n}`` — serve a shelved chapter image.

        Missing images (including images stripped for a text-only shelve
        with no Pillow) return the tiny empty 404 GIF, same as ``/cover``,
        so a broken image never corrupts the surrounding page. [H5]

        :param request: The incoming HTTP request.
        :param bid: Opaque bridge id that encodes a JSON book record.
        :param n: The image's index within the book, as embedded by
            :func:`app.reader.sanitize_chapter`.
        :returns: The image bytes, or a 404 empty GIF when unavailable.
        :rtype: Response
        :raises BadIdError: If *bid* cannot be decoded.
        """
        rec = _decode_book_record(request, bid)
        key = book_key(rec.get("u", ""))
        image_dir = os.path.join(cfg.cache_dir, "reader", key, "images")
        try:
            with open(os.path.join(image_dir, str(n)), "rb") as f:
                data = f.read()
            with open(os.path.join(image_dir, f"{n}.ct"), encoding="ascii") as f:
                upstream_ct = f.read().strip()
        except OSError:
            return Response(status_code=404, media_type="image/gif")
        return Response(
            content=data, media_type=_safe_image_type(upstream_ct),
            headers={"Cache-Control": "private, max-age=86400"},
        )

    @app.get("/read/{bid}/{chapter}/{part}", response_class=HTMLResponse)
    async def read_part(request: Request, bid: str, chapter: int, part: int) -> Response:
        """GET ``/read/{bid}/{chapter}/{part}`` — render one reading-length
        part of a chapter.

        Groups the chapter's sanitized blocks into parts sized by the
        ``rs_split`` cookie, slices to *part*, substitutes sanitizer
        placeholders into concrete ``/read/{bid}/...`` URLs, and records the
        new reading position.

        :param request: The incoming HTTP request.
        :param bid: Opaque bridge id that encodes a JSON book record.
        :param chapter: 0-based spine chapter index.
        :param part: 1-based part number within *chapter*.
        :returns: Rendered ``read.html``, or a 404 for an out-of-range
            chapter or part.
        :rtype: Response
        :raises BadIdError: If *bid* cannot be decoded.
        :raises ReaderError: If the book cannot be shelved.
        """
        rec = _decode_book_record(request, bid)
        if format_of(rec.get("m", "")) != "epub":
            return _error_response(
                request, "Not found", "Only EPUB books can be read in the browser.", 404
            )
        key = book_key(rec.get("u", ""))
        manifest = load_manifest(cfg.cache_dir, key)
        if manifest is None:
            manifest = await shelve_book(kc(request), rec, cfg.cache_dir)
        if chapter < 0 or chapter >= len(manifest.chapters):
            return _error_response(request, "Not found", "That chapter does not exist.", 404)
        target = _split_target(request)
        blocks = load_chapter(cfg.cache_dir, key, chapter)
        parts = parts_for([len(b) for b in blocks], target)
        if part < 1 or part > len(parts):
            return _error_response(request, "Not found", "That part does not exist.", 404)
        start, end = parts[part - 1]
        content_html = _substitute_placeholders("".join(blocks[start:end]), bid)

        prev_url = None
        if part > 1:
            prev_url = f"/read/{bid}/{chapter}/{part - 1}"
        elif chapter > 0:
            prev_blocks = load_chapter(cfg.cache_dir, key, chapter - 1)
            prev_parts = parts_for([len(b) for b in prev_blocks], target)
            prev_url = f"/read/{bid}/{chapter - 1}/{max(1, len(prev_parts))}"

        next_url = None
        if part < len(parts):
            next_url = f"/read/{bid}/{chapter}/{part + 1}"
        elif chapter < len(manifest.chapters) - 1:
            next_url = f"/read/{bid}/{chapter + 1}/1"

        store(request).set_position(rec, chapter, start, percent_of(manifest, chapter, start))

        return templates.TemplateResponse(request, "read.html", {
            "book_title": manifest.title, "bid": bid,
            "chapter": chapter, "part": part, "parts_count": len(parts),
            "chapter_title": manifest.chapters[chapter].title,
            "content_html": content_html,
            "prev_url": prev_url, "next_url": next_url,
        })


def _safe_path(candidate: str | None, default: str = "/") -> str:
    """Return *candidate* only when it is an unambiguous same-origin **path**.

    A ``Location:`` (or ``next=``) value that a visitor controls must never be
    able to leave this origin. A bare leading ``/`` is not sufficient: browsers
    read ``//evil.example`` as protocol-relative and ``/\\evil.example`` as the
    same thing, so both are open redirects. Accepted values therefore are a
    single leading ``/`` followed by something that is neither ``/`` nor ``\\``,
    with no control characters and no scheme.

    :param candidate: The untrusted redirect target.
    :param default: Path returned when *candidate* is not safe.
    :returns: A safe, relative, same-origin path.
    :rtype: str
    """
    value = (candidate or "").strip()
    if not value.startswith("/"):
        return default
    if value.startswith(("//", "/\\")):
        return default            # protocol-relative → another origin
    if any(ch in value for ch in "\r\n\t") or "\x00" in value:
        return default            # header/URL smuggling
    if ":" in value.split("/", 2)[1][:16]:
        return default            # a scheme hiding in the first segment
    return value


# Pages a "back" link may return to. Anchored so a crafted Referer such as
# ``/searchevil`` or ``/feed/../..`` cannot widen the set.
_BACK_PREFIXES = ("/feed/", "/search", "/book/", "/list")


def _back_to(request: Request, default: str = "/") -> str:
    """Return a same-site path to go "back" to, derived from the Referer.

    If the user arrived from a ``/feed/``, ``/search``, ``/book/`` or ``/list``
    page we return there; otherwise we fall back to *default*. Only the
    path+query is used (never the full referrer URL) and the result is run
    through :func:`_safe_path`, so it is always same-origin and safe.

    :param request: The incoming request.
    :param default: Path to use when there is no usable Referer.
    :returns: A relative path such as ``/feed/<id>`` or *default*.
    :rtype: str
    """
    ref = request.headers.get("referer", "")
    if ref:
        rp = urlsplit(ref)
        if rp.path.startswith(_BACK_PREFIXES):
            return _safe_path(rp.path + (f"?{rp.query}" if rp.query else ""), default)
    return default


def _human_size(n: int | None) -> str:
    """Return a compact human-readable size (e.g. ``"1.2 MB"``) or ``""``.

    :param n: A size in bytes, or ``None``.
    :rtype: str
    """
    if not n or n <= 0:
        return ""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" or size >= 100 else f"{size:.1f} {unit}"
        size /= 1024
    return ""


def _basename(url: str) -> str:
    """Return the final path segment of *url* (i.e. the filename part).

    :param url: An absolute URL whose path basename is required.
    :type url: str
    :returns: The last ``/``-delimited component of the URL's path, or the
        full path if it contains no ``/``.
    :rtype: str
    """
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
    async def _health_unconfigured() -> PlainTextResponse:
        """GET ``/health`` — health check for the unconfigured fallback app.

        :returns: Plain-text ``"ok"`` with HTTP 200.
        :rtype: PlainTextResponse
        """
        return PlainTextResponse("ok")

    @app.get("/{_path:path}")
    async def _unconfigured(_path: str) -> PlainTextResponse:
        """Catch-all: explain that RetroShelf is missing its configuration.

        :param _path: The requested path (unused).
        :type _path: str
        :returns: A plain-text 500 response naming the configuration error.
        :rtype: PlainTextResponse
        """
        return PlainTextResponse(
            f"RetroShelf is not configured: {_startup_error}", status_code=500
        )
