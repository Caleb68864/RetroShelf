"""Kavita upstream client: SSRF-guarded URL resolution, feed fetch, and
memory-safe streaming proxy.

Design (verified — see vault/MOC - FastAPI Streaming and Range):

- ONE shared :class:`httpx.AsyncClient` is created in the app lifespan with
  ``Timeout(read=None)`` so large book transfers are not killed by httpx's
  default 5s per-phase timeout. It is injected, never created at import time.
- Downloads are streamed (``client.stream``) and never read fully into memory.
- :func:`~KavitaClient.resolve_url` is the SSRF choke point: every upstream
  URL — including those decoded from a signed bridge id — must pass through
  it before any fetch.

:var DEFAULT_USER_AGENT: A modern desktop-Safari User-Agent string used as
    the default ``User-Agent`` header for all upstream requests.
:vartype DEFAULT_USER_AGENT: str
"""
from __future__ import annotations

import ipaddress
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlsplit

import httpx

from .config import Config, ConfigError, origin_tuple, registrable_domain
from .errors import KavitaError, SsrfError

# A modern desktop-Safari User-Agent. Some public OPDS servers (e.g. ones behind
# Cloudflare) reject requests without a browser-like UA; Kavita doesn't care.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.0 Safari/605.1.15"
)

# An OPDS page is a few hundred kilobytes at the very worst. The bridge reads
# feeds fully into memory to parse them, so an upstream that streams without
# end — hostile, misconfigured, or simply the wrong URL pointing at a disk
# image — would otherwise exhaust the host's RAM. Downloads are unaffected:
# they are streamed chunk-by-chunk and never buffered. [SS-09]
MAX_FEED_BYTES = 8 * 1024 * 1024

# The shared client runs with ``read=None`` so multi-hundred-megabyte book
# transfers are never cut off mid-stream. That is wrong for a *feed*: a stalled
# upstream would pin a request (and a connection) forever. Feed fetches
# therefore override the read/write timeout per request. [SS-09]
FEED_READ_TIMEOUT = 30.0


# Only these schemes are ever handed to the HTTP client.
ALLOWED_SCHEMES = frozenset({"http", "https"})

# A URL longer than this is not a book link; it is an attempt to make the guard
# (or a log line, or a downstream parser) do unbounded work.
MAX_HREF_LEN = 4096


def _is_internal_literal(host: str) -> bool:
    """Return whether *host* is an IP literal pointing somewhere non-public.

    Covers loopback, RFC 1918 / ULA private space, link-local (which includes
    the ``169.254.169.254`` cloud metadata endpoint), and the unspecified and
    reserved ranges. A hostname that is not an IP literal returns ``False``:
    this is a syntactic check, deliberately not a DNS resolution.

    :param host: A bare hostname or IP literal (no port, no brackets).
    :rtype: bool
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return (addr.is_loopback or addr.is_private or addr.is_link_local
            or addr.is_unspecified or addr.is_reserved or addr.is_multicast)


def _decode_feed(body: bytes, resp: httpx.Response) -> str:
    """Decode an OPDS feed body to ``str`` without ever raising.

    Prefers the charset the server declared, falls back to UTF-8, and replaces
    undecodable bytes rather than failing the whole page — a single bad byte in
    one book blurb must not take a shelf offline.

    :param body: Raw response bytes (already size-capped).
    :param resp: The response the bytes came from, for its declared charset.
    :returns: The decoded document text.
    :rtype: str
    """
    encoding = resp.charset_encoding or "utf-8"
    try:
        return body.decode(encoding, errors="replace")
    except LookupError:  # server named a charset Python doesn't know
        return body.decode("utf-8", errors="replace")


def build_client(timeout_connect: float = 10.0, user_agent: str | None = None) -> httpx.AsyncClient:
    """Create the shared :class:`httpx.AsyncClient` for the bridge.

    ``read=None`` disables the per-chunk read timeout so slow or large streams
    from Kavita are not aborted; connect and pool timeouts keep a sane bound.
    ``follow_redirects`` is left ``False`` to prevent redirect-based SSRF.
    Connection limits cap concurrent upstream sockets so a burst of iPad
    requests cannot exhaust the host; waiters past the pool timeout fail with a
    clear error. A browser-like ``User-Agent`` is set so public or
    Cloudflare-fronted OPDS servers do not reject the bridge.

    :param timeout_connect: Seconds before a connect or pool-wait times out.
    :type timeout_connect: float
    :param user_agent: Override the ``User-Agent`` header; falls back to
        :data:`DEFAULT_USER_AGENT` when ``None``.
    :type user_agent: str or None
    :returns: A configured :class:`httpx.AsyncClient` ready for use as the
        shared upstream transport.
    :rtype: httpx.AsyncClient
    """
    timeout = httpx.Timeout(connect=timeout_connect, read=None, write=None, pool=timeout_connect)
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
    headers = {"User-Agent": user_agent or DEFAULT_USER_AGENT}
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, limits=limits, headers=headers)


class KavitaClient:
    """Thin wrapper around a shared :class:`httpx.AsyncClient` bound to one
    :class:`~app.config.Config`.

    All upstream HTTP calls are SSRF-guarded via :meth:`resolve_url` before
    any network I/O takes place.  The client is injected rather than created
    internally so the same connection pool is shared across the application
    lifetime.
    """

    def __init__(self, config: Config, client: httpx.AsyncClient) -> None:
        """Initialise the client wrapper.

        :param config: Application configuration including the Kavita origin
            and allowed-origins list used by the SSRF guard.
        :type config: ~app.config.Config
        :param client: Shared async HTTP client; should have ``read=None``
            timeout and ``follow_redirects=False`` (see :func:`build_client`).
        :type client: httpx.AsyncClient
        """
        self._cfg = config
        self._client = client

    # -- SSRF guard ----------------------------------------------------------
    def resolve_url(self, href: str, base: str | None = None) -> str:
        """Resolve *href* (optionally against *base*) and return an absolute URL.

        This is the SSRF choke point.  Every upstream URL must pass through
        this method before any network fetch is issued.

        Accepted forms:

        * Already-absolute URLs whose origin matches the configured Kavita
          origin (or an entry in ``Config.allowed_origins``).
        * Root-relative absolute paths (a single leading ``/``), which are
          prefixed with the Kavita origin.

        Rejected forms (the exact bypasses identified in red-team finding
        C-2): protocol-relative (``//host/…``), backslash-containing,
        foreign-origin, and scheme-relative strings.

        :param href: The raw href to validate and resolve.  May be an
            absolute URL or a root-relative path.
        :type href: str
        :returns: An absolute URL whose origin is the configured Kavita
            origin (or an explicitly allowed origin).
        :rtype: str
        :raises SsrfError: If *href* is ``None``, empty, contains a
            backslash, is protocol-relative, points to a foreign origin, or
            cannot be parsed.
        """
        if href is None:
            raise SsrfError("Refusing to resolve empty href")
        raw = href.strip()
        if not raw:
            raise SsrfError("Refusing to resolve empty href")
        if len(raw) > MAX_HREF_LEN:
            raise SsrfError(f"Refusing oversized href ({len(raw)} chars)")
        # Embedded control characters (CR, LF, tab, NUL) are how a crafted feed
        # would try to smuggle a second request line into the upstream
        # connection. There is no legitimate URL that contains one. [SS-13]
        if any(ch in raw for ch in "\r\n\t\x00") or any(ord(ch) < 0x20 for ch in raw):
            raise SsrfError("Refusing href containing control characters")
        # Reject backslashes outright (Windows-style / smuggling).
        if "\\" in raw:
            raise SsrfError(self._cfg.mask(f"Refusing backslash href: {raw!r}"))
        # Protocol-relative //host/...  → urlsplit gives a netloc with empty scheme.
        if raw.startswith("//"):
            raise SsrfError(self._cfg.mask(f"Refusing protocol-relative href: {raw!r}"))

        parts = urlsplit(raw)
        if parts.scheme and parts.scheme.lower() not in ALLOWED_SCHEMES:
            # An origin comparison would reject these anyway, but naming the
            # reason keeps ``file:``/``gopher:``/``data:`` out of the logs as
            # "foreign origin" and out of httpx entirely. [SS-13]
            raise SsrfError(f"Refusing non-HTTP scheme: {parts.scheme.lower()!r}")
        if parts.username or parts.password:
            # Credentials in the URL would be forwarded upstream and are a
            # classic way to make a hostile host *look* like a trusted one in
            # a log line ("https://manybooks.net@evil.example/").
            raise SsrfError("Refusing href with embedded credentials")
        if parts.scheme or parts.netloc:
            # Absolute URL: must match an allowed origin (every feed plus any
            # configured extras), or be a same-site sibling of one (see
            # _origin_allowed), with default ports normalized.
            if not self._origin_allowed(raw):
                raise SsrfError(self._cfg.mask(f"Refusing foreign-origin href: {raw!r}"))
            return raw

        # Relative href. With *base* (the parent feed URL) we resolve it against
        # that document so hrefs from a non-primary feed point at the right
        # origin; without a base, only a root-relative path against the primary
        # origin is allowed. Either way the result must be an allowed origin.
        if base:
            absolute = urljoin(base, raw)
        elif raw.startswith("/"):
            absolute = f"{self._cfg.kavita_origin}{raw}"
        else:
            raise SsrfError(self._cfg.mask(f"Refusing non-absolute path href: {raw!r}"))
        if not self._origin_allowed(absolute):
            raise SsrfError(self._cfg.mask(f"Refusing foreign-origin href: {raw!r}"))
        return absolute

    def _origin_allowed(self, url: str) -> bool:
        """Return whether *url*'s origin may be fetched under the SSRF policy.

        An origin is permitted when it either (a) exactly matches a configured
        allowed origin — every feed plus ``EXTRA_UPSTREAM_ORIGINS`` — or (b) is
        a *same-site sibling* of one: identical scheme and port, and a shared
        registrable domain (eTLD+1). Rule (b) lets a feed implicitly trust its
        own download CDN (``manybooks.net`` → ``library.manybooks.net``) without
        per-host configuration, while still rejecting scheme downgrades, foreign
        ports, look-alike domains, and suffix-confusion tricks.

        :param url: An absolute URL whose origin is to be authorised.
        :type url: str
        :returns: ``True`` if the origin is permitted, ``False`` otherwise.
        :rtype: bool
        """
        try:
            scheme, host, port = origin_tuple(url)
        except (ValueError, ConfigError):
            return False
        try:
            allowed = [origin_tuple(o) for o in self._cfg.allowed_origins]
        except (ValueError, ConfigError):
            return False
        if (scheme, host, port) in allowed:
            return True
        # Past this point we are about to widen trust from a configured feed to
        # a *sibling host under the same registrable domain*. That is what lets
        # manybooks.net reach library.manybooks.net without configuration — but
        # it must never be the route by which an attacker-chosen subdomain
        # points the bridge at the host's own loopback, the LAN, or a cloud
        # metadata service. An exactly-configured origin is still honoured
        # above; only the *implicit* widening is refused here. [SS-13]
        if _is_internal_literal(host):
            return False
        site = registrable_domain(host)
        if not site:
            return False
        for a_scheme, a_host, a_port in allowed:
            if (scheme == a_scheme and port == a_port
                    and registrable_domain(a_host) == site):
                return True
        return False

    # -- feed fetch ----------------------------------------------------------
    async def fetch_feed(self, url: str) -> str:
        """Fetch an OPDS feed URL and return its full body as text.

        The URL is passed through :meth:`resolve_url` before the request is
        made, so SSRF protection is always applied.  Any secret values in
        error messages are masked via :meth:`~app.config.Config.mask` before
        they are included in the raised exception.

        :param url: The OPDS feed URL to retrieve.  May be absolute or
            root-relative; must resolve to the configured Kavita origin.
        :type url: str
        :returns: The response body decoded as text (UTF-8 by default).
        :rtype: str
        :raises SsrfError: If *url* fails the SSRF guard in
            :meth:`resolve_url`.
        :raises KavitaError: If the HTTP transport raises
            :class:`httpx.HTTPError`, or if Kavita returns an HTTP status
            code >= 400.
        """
        safe = self.resolve_url(url)
        timeout = httpx.Timeout(
            connect=FEED_READ_TIMEOUT, read=FEED_READ_TIMEOUT,
            write=FEED_READ_TIMEOUT, pool=FEED_READ_TIMEOUT,
        )
        for _ in range(6):
            req = self._client.build_request(
                "GET", safe, headers={"Accept": "application/atom+xml, */*"}, timeout=timeout,
            )
            try:
                resp = await self._client.send(req, stream=True)
            except httpx.HTTPError as exc:
                raise KavitaError(
                    self._cfg.mask(f"Could not reach upstream: {exc}"), url=self._cfg.mask(safe)
                ) from exc
            try:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        break
                    safe = self.resolve_url(location, base=safe)  # SSRF re-check each hop
                    continue
                if resp.status_code >= 400:
                    raise KavitaError(
                        self._cfg.mask(f"Upstream returned HTTP {resp.status_code} for {safe}"),
                        url=self._cfg.mask(safe),
                        status=resp.status_code,
                    )
                body = await self._read_capped(resp, MAX_FEED_BYTES, safe)
            finally:
                await resp.aclose()
            return _decode_feed(body, resp)
        raise KavitaError(self._cfg.mask(f"Too many redirects fetching {url!r}"))

    async def _read_capped(self, resp: httpx.Response, limit: int, safe: str) -> bytes:
        """Read *resp*'s body into memory, refusing to exceed *limit* bytes.

        A declared ``Content-Length`` over the cap is rejected before a single
        byte is transferred; an upstream that lies about (or omits) the length
        is cut off as soon as the accumulated body crosses the cap. [SS-09]

        :param resp: A response opened with ``stream=True``.
        :param limit: Maximum number of body bytes to accept.
        :param safe: The already-masked URL, for the error message.
        :returns: The complete body, guaranteed ``<= limit`` bytes.
        :rtype: bytes
        :raises KavitaError: If the body exceeds *limit*, or the transport fails.
        """
        declared = resp.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > limit:
            raise KavitaError(
                self._cfg.mask(f"Upstream feed is too large ({declared} bytes) for {safe}"),
                url=self._cfg.mask(safe),
            )
        chunks: list[bytes] = []
        total = 0
        try:
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > limit:
                    raise KavitaError(
                        self._cfg.mask(f"Upstream feed exceeded {limit} bytes for {safe}"),
                        url=self._cfg.mask(safe),
                    )
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise KavitaError(
                self._cfg.mask(f"Could not read upstream feed: {exc}"), url=self._cfg.mask(safe)
            ) from exc
        return b"".join(chunks)

    # -- streaming proxy -----------------------------------------------------
    async def open_stream(self, url: str, *, range_header: str | None = None) -> httpx.Response:
        """Open an upstream streaming GET and return the live response object.

        The response body is **not** read into memory; the caller must iterate
        it (e.g. via ``aiter_raw()``) and **must** call ``aclose()`` when
        finished — typically via a Starlette ``BackgroundTask``.  Status code
        and headers are available immediately for relay to the downstream
        client.  On error the upstream connection is closed before raising.

        :param url: The upstream resource URL.  May be absolute or
            root-relative; passed through :meth:`resolve_url` before use.
        :type url: str
        :param range_header: Value for an HTTP ``Range`` header, forwarded
            verbatim to Kavita to support partial-content (206) responses.
            Pass ``None`` to omit the header.
        :type range_header: str or None
        :returns: An :class:`httpx.Response` opened in streaming mode with
            its body not yet consumed.
        :rtype: httpx.Response
        :raises SsrfError: If *url* fails the SSRF guard in
            :meth:`resolve_url`.
        :raises KavitaError: If the HTTP transport raises
            :class:`httpx.HTTPError`, or if Kavita returns an HTTP status
            code >= 400 (the upstream connection is closed before raising).
        """
        safe = self.resolve_url(url)
        headers: dict[str, str] = {}
        if range_header:
            headers["Range"] = range_header
        # Follow redirects manually, re-validating EACH hop through the SSRF
        # guard. Many public feeds (e.g. Project Gutenberg) 302 a download to a
        # cache/CDN URL; a same-origin hop just works, while a cross-origin hop
        # is refused unless its origin is in EXTRA_UPSTREAM_ORIGINS.
        for _ in range(6):
            req = self._client.build_request("GET", safe, headers=headers)
            try:
                resp = await self._client.send(req, stream=True)
            except httpx.HTTPError as exc:
                raise KavitaError(
                    self._cfg.mask(f"Could not stream from upstream: {exc}"),
                    url=self._cfg.mask(safe),
                ) from exc
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                # Close WITHOUT reading: the redirect body is discarded, and a
                # hostile upstream could pair a 3xx with a multi-gigabyte body
                # that an uncapped aread() would buffer into memory. Status and
                # headers are already available before the body is touched.
                await resp.aclose()
                if not location:
                    raise KavitaError(
                        self._cfg.mask(f"Redirect without Location from {safe}"),
                        url=self._cfg.mask(safe), status=resp.status_code,
                    )
                safe = self.resolve_url(location, base=safe)  # SSRF re-check
                continue
            if resp.status_code >= 400:
                await resp.aclose()  # discard body unread (see redirect note above)
                raise KavitaError(
                    self._cfg.mask(f"Upstream returned HTTP {resp.status_code} for {safe}"),
                    url=self._cfg.mask(safe),
                    status=resp.status_code,
                )
            return resp
        raise KavitaError(self._cfg.mask(f"Too many redirects from {url!r}"))

    @asynccontextmanager
    async def stream(self, url: str, *, range_header: str | None = None) -> AsyncIterator[httpx.Response]:
        """Async context manager that yields a streaming upstream response.

        Opens the upstream GET request in streaming mode and yields the live
        :class:`httpx.Response` to the caller.  An optional ``Range`` header
        is forwarded so the bridge can relay HTTP 206 partial-content
        responses.  The caller should iterate the body via
        ``response.aiter_raw()``.  The upstream connection is always closed
        when the ``async with`` block exits, even if an exception is raised
        inside it.

        Usage::

            async with kavita_client.stream(url, range_header=range_val) as resp:
                async for chunk in resp.aiter_raw():
                    yield chunk

        :param url: The upstream resource URL.  May be absolute or
            root-relative; passed through :meth:`resolve_url` before use.
        :type url: str
        :param range_header: Value for an HTTP ``Range`` header, forwarded
            verbatim to Kavita to support partial-content (206) responses.
            Pass ``None`` to omit the header.
        :type range_header: str or None
        :returns: An async context manager yielding an
            :class:`httpx.Response` opened in streaming mode.
        :raises SsrfError: If *url* fails the SSRF guard in
            :meth:`resolve_url`.
        :raises KavitaError: If the HTTP transport raises
            :class:`httpx.HTTPError`, or if Kavita returns an HTTP status
            code >= 400 (the upstream connection is closed before raising).
        """
        safe = self.resolve_url(url)
        headers: dict[str, str] = {}
        if range_header:
            headers["Range"] = range_header
        req = self._client.build_request("GET", safe, headers=headers)
        try:
            resp = await self._client.send(req, stream=True)
        except httpx.HTTPError as exc:
            raise KavitaError(
                self._cfg.mask(f"Could not stream from Kavita: {exc}"), url=self._cfg.mask(safe)
            ) from exc
        try:
            if resp.status_code >= 400:
                # Discard the error body unread — an uncapped aread() would let a
                # hostile upstream OOM the host with a giant 4xx/5xx body.
                await resp.aclose()
                raise KavitaError(
                    self._cfg.mask(f"Kavita returned HTTP {resp.status_code} for {safe}"),
                    url=self._cfg.mask(safe),
                    status=resp.status_code,
                )
            yield resp
        finally:
            await resp.aclose()
