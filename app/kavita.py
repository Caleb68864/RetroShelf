"""Kavita upstream client: SSRF-guarded URL resolution, feed fetch, and
memory-safe streaming proxy.

Design (verified — see vault/MOC - FastAPI Streaming and Range):
- ONE shared :class:`httpx.AsyncClient` is created in the app lifespan with
  ``Timeout(read=None)`` so large book transfers are not killed by httpx's
  default 5s per-phase timeout. It is injected, never created at import time.
- Downloads are streamed (``client.stream``) and never read fully into memory.
- ``resolve_url`` is the SSRF choke point: every upstream URL — including those
  decoded from a signed bridge id — must pass through it before any fetch.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx

from .config import Config, origin_tuple
from .errors import KavitaError, SsrfError


def build_client(timeout_connect: float = 10.0) -> httpx.AsyncClient:
    """Create the shared AsyncClient. ``read=None`` disables the per-chunk read
    timeout so slow/large streams from Kavita are not aborted; connect/pool keep
    a sane bound. ``follow_redirects`` stays False to avoid redirect-based SSRF.
    Connection limits cap concurrent upstream sockets so a burst of iPad requests
    cannot exhaust the host; waiters past the pool timeout fail with a clear error."""
    timeout = httpx.Timeout(connect=timeout_connect, read=None, write=None, pool=timeout_connect)
    limits = httpx.Limits(max_connections=50, max_keepalive_connections=20)
    return httpx.AsyncClient(timeout=timeout, follow_redirects=False, limits=limits)


class KavitaClient:
    """Thin wrapper around a shared AsyncClient bound to one :class:`Config`."""

    def __init__(self, config: Config, client: httpx.AsyncClient):
        self._cfg = config
        self._client = client

    # -- SSRF guard ----------------------------------------------------------
    def resolve_url(self, href: str) -> str:
        """Resolve *href* against the Kavita origin and return an absolute URL,
        or raise :class:`SsrfError` if it points anywhere else.

        Accepts only:
          * already-absolute URLs whose origin == the Kavita origin, or
          * root-relative absolute paths (a single leading ``/``).
        Rejects protocol-relative (``//host``), backslash, foreign-origin, and
        scheme-relative forms — the exact bypasses the red-team found. [C-2]
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

        parts = urlsplit(raw)
        if parts.scheme or parts.netloc:
            # Absolute URL: must match one of the allowed origins (Kavita plus any
            # configured extras), with default ports normalized.
            try:
                allowed = {origin_tuple(o) for o in self._cfg.allowed_origins}
                if origin_tuple(raw) not in allowed:
                    raise SsrfError(self._cfg.mask(f"Refusing foreign-origin href: {raw!r}"))
            except ValueError as exc:
                raise SsrfError(self._cfg.mask(f"Unparseable href: {raw!r}")) from exc
            return raw

        # Relative: only a root-relative absolute path is allowed.
        if not raw.startswith("/"):
            raise SsrfError(self._cfg.mask(f"Refusing non-absolute path href: {raw!r}"))
        return f"{self._cfg.kavita_origin}{raw}"

    # -- feed fetch ----------------------------------------------------------
    async def fetch_feed(self, url: str) -> str:
        """GET an OPDS feed (resolving + SSRF-checking first) and return the body
        text. Raises :class:`KavitaError` (secrets masked) on any failure."""
        safe = self.resolve_url(url)
        try:
            resp = await self._client.get(safe, headers={"Accept": "application/atom+xml, */*"})
        except httpx.HTTPError as exc:
            raise KavitaError(
                self._cfg.mask(f"Could not reach Kavita: {exc}"), url=self._cfg.mask(safe)
            ) from exc
        if resp.status_code >= 400:
            raise KavitaError(
                self._cfg.mask(f"Kavita returned HTTP {resp.status_code} for {safe}"),
                url=self._cfg.mask(safe),
                status=resp.status_code,
            )
        return resp.text

    # -- streaming proxy -----------------------------------------------------
    async def open_stream(self, url: str, *, range_header: str | None = None) -> httpx.Response:
        """Open an upstream streaming GET and return the live ``httpx.Response``
        WITHOUT reading the body. The caller MUST ``aclose()`` it (e.g. via a
        Starlette ``BackgroundTask``) after streaming. Status/headers are
        available immediately for header relay. Raises :class:`KavitaError`
        (closing the connection) on transport error or HTTP >= 400."""
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
        if resp.status_code >= 400:
            await resp.aread()
            await resp.aclose()
            raise KavitaError(
                self._cfg.mask(f"Kavita returned HTTP {resp.status_code} for {safe}"),
                url=self._cfg.mask(safe),
                status=resp.status_code,
            )
        return resp

    @asynccontextmanager
    async def stream(self, url: str, *, range_header: str | None = None):
        """Async-context-manager yielding the upstream ``httpx.Response`` opened
        in streaming mode. Forwards a client ``Range`` header so the bridge can
        relay 206. The caller iterates ``response.aiter_raw()`` and the upstream
        connection is released when the context exits.
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
