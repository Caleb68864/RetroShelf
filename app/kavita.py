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

from contextlib import asynccontextmanager
from urllib.parse import urljoin, urlsplit

import httpx

from .config import Config, origin_tuple
from .errors import KavitaError, SsrfError


# A modern desktop-Safari User-Agent. Some public OPDS servers (e.g. ones behind
# Cloudflare) reject requests without a browser-like UA; Kavita doesn't care.
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/16.0 Safari/605.1.15"
)


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
        # Reject backslashes outright (Windows-style / smuggling).
        if "\\" in raw:
            raise SsrfError(self._cfg.mask(f"Refusing backslash href: {raw!r}"))
        # Protocol-relative //host/...  → urlsplit gives a netloc with empty scheme.
        if raw.startswith("//"):
            raise SsrfError(self._cfg.mask(f"Refusing protocol-relative href: {raw!r}"))

        allowed = {origin_tuple(o) for o in self._cfg.allowed_origins}
        parts = urlsplit(raw)
        if parts.scheme or parts.netloc:
            # Absolute URL: must match one of the allowed origins (every feed
            # plus any configured extras), with default ports normalized.
            try:
                if origin_tuple(raw) not in allowed:
                    raise SsrfError(self._cfg.mask(f"Refusing foreign-origin href: {raw!r}"))
            except ValueError as exc:
                raise SsrfError(self._cfg.mask(f"Unparseable href: {raw!r}")) from exc
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
        try:
            if origin_tuple(absolute) not in allowed:
                raise SsrfError(self._cfg.mask(f"Refusing foreign-origin href: {raw!r}"))
        except ValueError as exc:
            raise SsrfError(self._cfg.mask(f"Unparseable href: {raw!r}")) from exc
        return absolute

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
        for _ in range(6):
            try:
                resp = await self._client.get(safe, headers={"Accept": "application/atom+xml, */*"})
            except httpx.HTTPError as exc:
                raise KavitaError(
                    self._cfg.mask(f"Could not reach upstream: {exc}"), url=self._cfg.mask(safe)
                ) from exc
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
            return resp.text
        raise KavitaError(self._cfg.mask(f"Too many redirects fetching {url!r}"))

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
                await resp.aread()
                await resp.aclose()
                if not location:
                    raise KavitaError(
                        self._cfg.mask(f"Redirect without Location from {safe}"),
                        url=self._cfg.mask(safe), status=resp.status_code,
                    )
                safe = self.resolve_url(location, base=safe)  # SSRF re-check
                continue
            if resp.status_code >= 400:
                await resp.aread()
                await resp.aclose()
                raise KavitaError(
                    self._cfg.mask(f"Upstream returned HTTP {resp.status_code} for {safe}"),
                    url=self._cfg.mask(safe),
                    status=resp.status_code,
                )
            return resp
        raise KavitaError(self._cfg.mask(f"Too many redirects from {url!r}"))

    @asynccontextmanager
    async def stream(self, url: str, *, range_header: str | None = None):
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
                await resp.aread()
                await resp.aclose()
                raise KavitaError(
                    self._cfg.mask(f"Kavita returned HTTP {resp.status_code} for {safe}"),
                    url=self._cfg.mask(safe),
                    status=resp.status_code,
                )
            yield resp
        finally:
            await resp.aclose()
