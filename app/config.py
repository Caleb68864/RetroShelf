"""Configuration: typed env parsing, validation, Kavita-origin derivation, and
secret masking.

All app configuration comes from environment variables (12-factor).
:func:`load_config` is pure and accepts an explicit mapping so it is
trivially testable. Required variables that are missing raise a typed
:class:`ConfigError` with a clear message — never a raw traceback leaked
to the operator.

Module-level constants:

.. data:: REDACTED

    Sentinel string (``"***"``) substituted wherever a secret would
    otherwise appear in logs or user-visible output.
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# Redaction token used wherever a secret would otherwise appear.
REDACTED = "***"

# Query parameters whose *value* is a secret regardless of what it is. Masking
# by known value alone is not enough: the bridge access key can legitimately be
# shorter than the 8-character masking floor, and uvicorn's access log prints
# the raw request line (``GET /feed/x?key=hunter2``). Redacting by parameter
# name closes that hole for any value. [H-7]
_SECRET_QS_PARAMS = ("key", "apikey", "api_key", "access_key", "token",
                     "access_token", "auth", "password", "secret")
_SECRET_QS_RE = re.compile(
    r"(?i)\b(" + "|".join(_SECRET_QS_PARAMS) + r")=[^&\s\"'>\]]*"
)

# ``/api/opds/<apiKey>`` — Kavita puts the key in the *path*, so a URL logged by
# a third-party library is redacted even when that key is not this bridge's own.
_OPDS_PATH_KEY_RE = re.compile(r"(?i)(/api/opds/)[^/\s?\"'>\]]+")

_DEFAULT_PORTS = {"http": 80, "https": 443}


class ConfigError(Exception):
    """Raised when configuration is missing or invalid.

    Carries a clear, operator-facing message and never leaks a raw
    traceback to end users. Callers should catch this exception and
    surface its ``str()`` representation directly.
    """


def _normalize_origin(url: str) -> str:
    """Return the canonical ``scheme://host[:port]`` origin for *url*.

    Implicit default ports (80 for http, 443 for https) are dropped so
    that ``http://kavita`` and ``http://kavita:80`` compare equal.

    :param url: Any absolute URL whose origin is to be extracted.
    :type url: str
    :returns: Normalised origin string of the form
        ``scheme://host`` or ``scheme://host:port``.
    :rtype: str
    :raises ConfigError: If *url* has no scheme or no hostname.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        raise ConfigError(
            f"Invalid URL (missing scheme or host): {_mask_url_static(url)!r}"
        )
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    port = parts.port
    if port is None or _DEFAULT_PORTS.get(scheme) == port:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def origin_tuple(url: str) -> tuple[str, str, int]:
    """Return ``(scheme, host, port)`` with default ports normalised.

    Used by the SSRF guard so that default-port and protocol variations
    of the same origin compare as equal.

    :param url: Any absolute URL to decompose.
    :type url: str
    :returns: A three-tuple ``(scheme, host, port)`` where *port* is
        filled in from :data:`_DEFAULT_PORTS` when not explicit.
    :rtype: tuple[str, str, int]
    :raises ConfigError: If *url* has no scheme or no hostname.
    """
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        raise ConfigError("Invalid URL (missing scheme or host)")
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    port = parts.port if parts.port is not None else _DEFAULT_PORTS.get(scheme, 0)
    return (scheme, host, port)


# A small, curated set of multi-label public suffixes so that ``co.uk`` &c. are
# not mistaken for a registrable domain. This is deliberately *not* the full
# Public Suffix List (which would need a bundled data file / dependency); it
# covers the common ccTLDs book sources use. Hosts under an unlisted multi-label
# suffix fall back to last-two-labels — still safe against the suffix-confusion
# trick (``trusted.net.evil.com`` resolves to ``evil.com``), only slightly more
# permissive within e.g. a shared cloud domain. Configure ``EXTRA_UPSTREAM_ORIGINS``
# for any host this heuristic doesn't cover.
_MULTI_LABEL_SUFFIXES = frozenset({
    "co.uk", "org.uk", "gov.uk", "ac.uk", "me.uk", "net.uk", "sch.uk", "ltd.uk",
    "com.au", "net.au", "org.au", "edu.au", "gov.au", "id.au",
    "co.nz", "net.nz", "org.nz", "govt.nz",
    "co.jp", "or.jp", "ne.jp", "go.jp", "ac.jp",
    "com.br", "net.br", "org.br", "gov.br",
    "co.za", "org.za", "net.za",
    "com.cn", "net.cn", "org.cn", "gov.cn",
    "co.in", "net.in", "org.in", "gen.in",
    "com.mx", "com.ar", "com.tr", "com.sg", "com.hk", "com.tw", "com.ua",
})


def registrable_domain(host: str) -> str:
    """Return the registrable domain (eTLD+1) of *host* for same-site matching.

    Used by the SSRF guard so that a configured feed implicitly trusts its own
    sibling hosts (e.g. a feed on ``manybooks.net`` trusts the book-download
    host ``library.manybooks.net``; a feed on ``www.gutenberg.org`` trusts
    ``aleph.gutenberg.org`` and the bare ``gutenberg.org``).

    IP-literal hosts are returned unchanged — they are never collapsed to a
    "domain", so two distinct addresses can never be treated as same-site.

    :param host: A lower/mixed-case hostname (no port, no brackets).
    :type host: str
    :returns: The registrable domain, or the host itself when it is an IP
        literal, a single label, or empty.
    :rtype: str
    """
    host = (host or "").strip(".").lower()
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host  # raw IP — never widen to a registrable domain
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) <= 2:
        return host
    last_two = ".".join(labels[-2:])
    if last_two in _MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return last_two


def _extract_api_key(opds_url: str) -> str:
    """Pull the Kavita ``apiKey`` out of an OPDS URL.

    Expects a URL of the form ``.../api/opds/{apiKey}[/...]`` and
    returns the segment immediately following ``opds``. Falls back to
    the last non-empty path segment when the ``opds`` sentinel is
    absent, and returns ``""`` if the path is empty.

    :param opds_url: The full Kavita OPDS URL containing the API key.
    :type opds_url: str
    :returns: The extracted API key string, or ``""`` if not found.
    :rtype: str
    """
    path = urlsplit(opds_url).path
    segments = [s for s in path.split("/") if s]
    for i, seg in enumerate(segments):
        if seg == "opds" and i + 1 < len(segments):
            return segments[i + 1]
    # Fallback: last non-empty path segment.
    return segments[-1] if segments else ""


def _mask_url_static(url: str) -> str:
    """Best-effort mask of an ``apiKey``-looking segment in *url*.

    Used before a :class:`Config` instance exists (e.g. during early
    validation), so it cannot rely on :meth:`Config.mask`. Any key
    extracted by :func:`_extract_api_key` is replaced with
    :data:`REDACTED`. If no key can be found, *url* is returned
    unchanged.

    :param url: The URL string to sanitise for safe display.
    :type url: str
    :returns: The URL with the API-key segment replaced by
        :data:`REDACTED`, or the original string when no key is found.
    :rtype: str
    """
    try:
        key = _extract_api_key(url)
    except Exception:
        key = ""
    return url.replace(key, REDACTED) if key else url


def _as_bool(value: str | None, default: bool) -> bool:
    """Coerce an optional environment-variable string to :class:`bool`.

    Truthy string values are ``"1"``, ``"true"``, ``"yes"``, and
    ``"on"`` (case-insensitive). Any other non-empty string is
    falsy. ``None`` or ``""`` returns *default*.

    :param value: Raw string from the environment, or ``None``.
    :type value: str | None
    :param default: Value to return when *value* is absent or empty.
    :type default: bool
    :returns: Parsed boolean value.
    :rtype: bool
    """
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int, name: str) -> int:
    """Coerce an optional environment-variable string to :class:`int`.

    ``None`` or ``""`` returns *default*. Any non-integer string raises
    :class:`ConfigError` with a message that includes *name* so the
    operator immediately knows which variable is malformed.

    :param value: Raw string from the environment, or ``None``.
    :type value: str | None
    :param default: Value to return when *value* is absent or empty.
    :type default: int
    :param name: Environment variable name used in the error message.
    :type name: str
    :returns: Parsed integer value.
    :rtype: int
    :raises ConfigError: If *value* cannot be converted to ``int``.
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


@dataclass(frozen=True)
class FeedSource:
    """One configured OPDS feed (a "library" on the portal home menu).

    :ivar name: Human-friendly display name shown in the feed menu.
    :ivar url: The OPDS root URL (may embed an apiKey path segment).
    :ivar origin: Normalised ``scheme://host[:port]`` origin of *url*.
    :ivar api_key: API key extracted from *url* (used for masking).
    """

    name: str
    url: str
    origin: str
    api_key: str


def _feed_name_from_url(url: str) -> str:
    """Derive a friendly feed name from a URL's host (e.g. ``Manybooks``).

    :param url: The feed URL whose hostname supplies the name.
    :rtype: str
    """
    host = (urlsplit(url).hostname or "feed").lower()
    if host.startswith("www."):
        host = host[4:]
    label = host.split(".")[0] or "feed"
    return label.replace("-", " ").title()


def _parse_feeds(e: dict[str, str]) -> list[FeedSource]:
    """Build the ordered list of feeds from ``KAVITA_OPDS_URL`` and ``OPDS_FEEDS``.

    ``OPDS_FEEDS`` is a comma/newline-separated list whose entries are either
    ``Name|URL`` or just ``URL`` (the name is then derived from the host).
    ``KAVITA_OPDS_URL`` (if set) is prepended as the primary feed, named by
    ``KAVITA_FEED_NAME`` (default "Library"). Duplicate URLs are dropped.

    :param e: Environment mapping to read the feed variables from.
    :type e: dict[str, str]
    :returns: Ordered, de-duplicated feed list (primary feed first).
    :rtype: list[FeedSource]
    :raises ConfigError: if an entry's URL is missing a scheme or host.
    """
    entries: list[tuple[str | None, str]] = []
    kavita = (e.get("KAVITA_OPDS_URL") or "").strip()
    if kavita:
        entries.append(((e.get("KAVITA_FEED_NAME") or "Library").strip(), kavita))
    raw = (e.get("OPDS_FEEDS") or "").replace("\n", ",")
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "|" in chunk:
            label, _, chunk_url = chunk.partition("|")
            entries.append((label.strip() or None, chunk_url.strip()))
        else:
            entries.append((None, chunk))

    feeds: list[FeedSource] = []
    seen: set[str] = set()
    for name, url in entries:
        if not url or url in seen:
            continue
        seen.add(url)
        origin = _normalize_origin(url)  # raises ConfigError on a bad URL
        feeds.append(FeedSource(
            name=name or _feed_name_from_url(url),
            url=url,
            origin=origin,
            api_key=_extract_api_key(url),
        ))
    return feeds


@dataclass(frozen=True)
class Config:
    """Immutable, validated snapshot of all bridge configuration.

    Constructed exclusively by :func:`load_config`; never instantiated
    directly in application code. The dataclass is frozen to prevent
    accidental mutation after startup.

    :ivar kavita_base_url: Base URL of the Kavita server
        (e.g. ``http://kavita:5000``). Used to build Kavita web links.
    :vartype kavita_base_url: str
    :ivar kavita_opds_url: Full user-specific OPDS URL including the
        embedded ``apiKey`` path segment.
    :vartype kavita_opds_url: str
    :ivar kavita_origin: Normalised ``scheme://host[:port]`` origin
        derived from *kavita_opds_url*; used by the SSRF guard.
    :vartype kavita_origin: str
    :ivar api_key: Kavita API key extracted from *kavita_opds_url*.
    :vartype api_key: str
    :ivar app_port: TCP port on which the bridge listens.
        Defaults to ``8099``.
    :vartype app_port: int
    :ivar bridge_public_url: Optional public-facing base URL of this
        bridge (used when constructing absolute self-referential links).
    :vartype bridge_public_url: str | None
    :ivar bridge_access_key: Optional shared secret required in the
        ``X-Bridge-Key`` request header for all incoming requests.
    :vartype bridge_access_key: str | None
    :ivar bridge_id_secret: Optional secret used to sign/verify opaque
        item identifiers passed through the bridge.
    :vartype bridge_id_secret: str | None
    :ivar allowed_ips: Tuple of IP addresses (or CIDR strings) that are
        permitted to reach the bridge. Empty tuple means unrestricted.
    :vartype allowed_ips: tuple[str, ...]
    :ivar show_covers: Whether to proxy cover images through the bridge.
        Defaults to ``True``.
    :vartype show_covers: bool
    :ivar cache_feeds_seconds: TTL in seconds for cached OPDS feed
        responses. Defaults to ``300``.
    :vartype cache_feeds_seconds: int
    :ivar cache_books: Whether to cache proxied book file responses.
        Defaults to ``False``.
    :vartype cache_books: bool
    :ivar log_level: Logging verbosity level string (e.g. ``"info"``,
        ``"debug"``). Defaults to ``"info"``.
    :vartype log_level: str
    :ivar pdf_disposition: ``Content-Disposition`` value for PDF
        downloads; either ``"inline"`` or ``"attachment"``.
        Defaults to ``"inline"``.
    :vartype pdf_disposition: str
    :ivar epub_disposition: ``Content-Disposition`` value for EPUB
        downloads; either ``"attachment"`` or ``"inline"``.
        Defaults to ``"attachment"``.
    :vartype epub_disposition: str
    :ivar tz: IANA timezone name used for log timestamps and display.
        Defaults to ``"America/Chicago"``.
    :vartype tz: str
    :ivar extra_origins: Additional upstream origins (normalised) that
        the SSRF guard will allow, for non-Kavita OPDS servers that host
        downloads or covers on separate hosts or CDNs.
    :vartype extra_origins: tuple[str, ...]
    :ivar upstream_user_agent: Optional override for the ``User-Agent``
        header sent to upstream servers. ``None`` causes the bridge to
        use a browser-like default string.
    :vartype upstream_user_agent: str | None
    :ivar state_dir: Directory holding the JSON state file (Reading List +
        download history). Defaults to ``"/config"``.
    :vartype state_dir: str
    :ivar cache_dir: Root directory for the cover image disk cache.
        Defaults to ``"/cache"``.
    :vartype cache_dir: str
    :ivar cover_max_edge: Maximum pixel dimension for proxied covers;
        larger images are downscaled. Defaults to ``320``.
    :vartype cover_max_edge: int
    :ivar cover_jpeg_quality: JPEG re-encode quality for transcoded covers.
        Defaults to ``80``.
    :vartype cover_jpeg_quality: int
    :ivar feeds: All configured OPDS feeds (the portal menu); the first
        entry is the primary feed.
    :vartype feeds: tuple[FeedSource, ...]
    """

    kavita_base_url: str
    kavita_opds_url: str
    kavita_origin: str
    api_key: str
    app_port: int = 8099
    bridge_public_url: str | None = None
    bridge_access_key: str | None = None
    bridge_id_secret: str | None = None
    allowed_ips: tuple[str, ...] = ()
    show_covers: bool = True
    cache_feeds_seconds: int = 300
    cache_books: bool = False
    log_level: str = "info"
    pdf_disposition: str = "inline"
    epub_disposition: str = "attachment"
    tz: str = "America/Chicago"
    # Additional upstream origins the SSRF guard will allow, for generic
    # (non-Kavita) OPDS servers that host downloads/covers on other hosts/CDNs.
    extra_origins: tuple[str, ...] = ()
    # Override the upstream User-Agent (None → a browser-like default).
    upstream_user_agent: str | None = None
    # Directory for the JSON state file (Reading List + download history).
    state_dir: str = "/config"
    # Cover image disk cache
    cache_dir: str = "/cache"
    cover_max_edge: int = 320
    cover_jpeg_quality: int = 80
    # Opt-in multi-account login + profiles. Off (default) → no login, reading
    # state is global, and the optional access-key gate is unchanged. On → the
    # login page becomes the gate and reading state is per-profile.
    accounts_enabled: bool = False
    # All configured OPDS feeds (the portal menu). The first is the primary.
    feeds: tuple[FeedSource, ...] = ()

    @property
    def state_path(self) -> str:
        """Path to the JSON state file (Reading List + history)."""
        return os.path.join(self.state_dir, "retroshelf-state.json")

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        """All origins the bridge may fetch from (every feed + any extras).

        Combines each configured feed's origin (falling back to
        :attr:`kavita_origin` when no feeds are set) with
        :attr:`extra_origins`, de-duplicated, for the SSRF guard.

        :returns: Tuple of normalised ``scheme://host[:port]`` origins.
        :rtype: tuple[str, ...]
        """
        base = tuple(f.origin for f in self.feeds) or (self.kavita_origin,)
        return tuple(dict.fromkeys(base + tuple(self.extra_origins)))

    # -- secret masking ------------------------------------------------------
    def mask(self, text: str) -> str:
        """Redact known secrets anywhere they appear in *text*.

        Replaces :attr:`api_key` and :attr:`bridge_access_key` with
        :data:`REDACTED` wherever they occur. Applied unconditionally to
        both log output and any user-visible surface — debug mode only
        loosens *log verbosity*, not masking.

        **[C-4]** Secret values shorter than 8 characters are NOT
        masked *by value* — that would mangle generic OPDS path segments
        such as ``opds``. They are still caught structurally: any
        ``key=``/``apiKey=``/``token=``… query parameter and any
        ``/api/opds/<key>`` path segment is redacted by shape, whatever
        the value. [H-7]

        :param text: Arbitrary string that may contain secret values.
        :type text: str
        :returns: *text* with all qualifying secret occurrences replaced
            by :data:`REDACTED`. Returns *text* unchanged when empty.
        :rtype: str
        """
        if not text:
            return text
        out = text
        secrets: list[str | None] = [f.api_key for f in self.feeds]
        secrets += [self.api_key, self.bridge_access_key]
        for secret in secrets:
            if secret and len(secret) >= 8:
                out = out.replace(secret, REDACTED)
        out = _SECRET_QS_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", out)
        return _OPDS_PATH_KEY_RE.sub(lambda m: f"{m.group(1)}{REDACTED}", out)

    @property
    def debug(self) -> bool:
        """Whether the bridge is running at debug verbosity.

        Convenience property that normalises :attr:`log_level` to lower
        case before comparing, so ``"DEBUG"``, ``"Debug"``, and
        ``"debug"`` all return ``True``.

        :returns: ``True`` when :attr:`log_level` equals ``"debug"``
            (case-insensitive), ``False`` otherwise.
        :rtype: bool
        """
        return self.log_level.strip().lower() == "debug"


def load_config(env: dict[str, str] | None = None) -> Config:
    """Build a :class:`Config` from *env* (defaults to ``os.environ``).

    Reads and validates all recognised environment variables, derives
    ``kavita_origin`` from ``KAVITA_OPDS_URL``, and falls back to that
    origin for ``KAVITA_BASE_URL`` when the latter is omitted. The
    function is pure — passing an explicit *env* mapping makes it fully
    testable without touching the process environment.

    Recognised environment variables (all optional unless noted; at least
    one feed — ``KAVITA_OPDS_URL`` and/or ``OPDS_FEEDS`` — is **required**):

    - ``KAVITA_OPDS_URL`` — full user-specific OPDS URL; becomes the
      primary feed.
    - ``KAVITA_FEED_NAME`` — portal name for that feed; default ``Library``.
    - ``OPDS_FEEDS`` — comma/newline-separated ``Name|URL`` (or bare URL)
      entries for additional libraries in the portal menu.
    - ``KAVITA_BASE_URL`` — Kavita web base URL; derived if absent, and must
      share the primary feed's origin when given.
    - ``APP_PORT`` — bridge listen port (default ``8099``).
    - ``BRIDGE_PUBLIC_URL`` — public base URL of this bridge.
    - ``BRIDGE_ACCESS_KEY`` — shared secret for incoming requests.
    - ``BRIDGE_ID_SECRET`` — secret for opaque identifier signing.
    - ``ALLOWED_IPS`` — comma-separated IP/CIDR allow-list.
    - ``SHOW_COVERS`` — bool; default ``true``.
    - ``ACCOUNTS_ENABLED`` — bool; default ``false``. When true, the login
      page becomes the gate and reading state is per-profile.
    - ``CACHE_FEEDS_SECONDS`` — int TTL; default ``300``.
    - ``CACHE_BOOKS`` — bool; default ``false``.
    - ``LOG_LEVEL`` — verbosity; default ``"info"``.
    - ``PDF_DISPOSITION`` — ``"inline"`` or ``"attachment"``; default
      ``"inline"``.
    - ``EPUB_DISPOSITION`` — ``"attachment"`` or ``"inline"``; default
      ``"attachment"``.
    - ``TZ`` — IANA timezone; default ``"America/Chicago"``.
    - ``EXTRA_UPSTREAM_ORIGINS`` — comma-separated extra SSRF-allowed
      origins.
    - ``UPSTREAM_USER_AGENT`` — override User-Agent for upstream fetches.
    - ``STATE_DIR`` — directory for the Reading List / history JSON;
      default ``/config``.
    - ``CACHE_DIR`` — cover disk-cache root; default ``/cache``.
    - ``COVER_MAX_EDGE`` / ``COVER_JPEG_QUALITY`` — cover transcode bounds;
      defaults ``320`` / ``80``.

    :param env: Explicit environment mapping to use instead of
        ``os.environ``. Pass a plain ``dict`` in tests to keep them
        hermetic. ``None`` (default) reads from the live process
        environment.
    :type env: dict[str, str] | None
    :returns: Fully validated, frozen :class:`Config` instance.
    :rtype: Config
    :raises ConfigError: If ``KAVITA_OPDS_URL`` is absent, if
        ``KAVITA_BASE_URL`` and ``KAVITA_OPDS_URL`` resolve to different
        origins, or if any numeric/enum variable has an invalid value.
    """
    e = dict(os.environ if env is None else env)

    # One or more feeds (the portal). KAVITA_OPDS_URL is the primary; OPDS_FEEDS
    # adds more. At least one feed is required.
    feeds = _parse_feeds(e)
    if not feeds:
        raise ConfigError(
            "Configure at least one OPDS feed: set KAVITA_OPDS_URL (e.g. "
            "http://kavita:5000/api/opds/YOUR_AUTH_KEY) and/or OPDS_FEEDS "
            "(comma-separated 'Name|URL' entries)."
        )
    primary = feeds[0]
    opds_url = primary.url
    kavita_origin = primary.origin
    api_key = primary.api_key

    # KAVITA_BASE_URL (if given) must share the primary feed's origin.
    base_url = (e.get("KAVITA_BASE_URL") or "").strip() or kavita_origin
    if _normalize_origin(base_url) != kavita_origin:
        raise ConfigError(
            "KAVITA_BASE_URL must share the primary feed's origin "
            f"(got {_normalize_origin(base_url)} vs {kavita_origin})."
        )

    pdf_disp = (e.get("PDF_DISPOSITION") or "inline").strip().lower()
    if pdf_disp not in {"inline", "attachment"}:
        raise ConfigError(f"PDF_DISPOSITION must be 'inline' or 'attachment', got {pdf_disp!r}")
    epub_disp = (e.get("EPUB_DISPOSITION") or "attachment").strip().lower()
    if epub_disp not in {"attachment", "inline"}:
        raise ConfigError(f"EPUB_DISPOSITION must be 'attachment' or 'inline', got {epub_disp!r}")

    allowed = tuple(
        s.strip() for s in (e.get("ALLOWED_IPS") or "").split(",") if s.strip()
    )

    extra_origins = tuple(
        _normalize_origin(s.strip())
        for s in (e.get("EXTRA_UPSTREAM_ORIGINS") or "").split(",")
        if s.strip()
    )

    # Accounts sign session cookies with a stable secret so logins survive a
    # restart. Without one, every session would silently reset on each restart
    # — a confusing footgun for a household setup — so require a stable secret
    # rather than fall back to a per-process random one. (BRIDGE_ID_SECRET is
    # already recommended for durable bookmarked links, so this asks nothing new.)
    if _as_bool(e.get("ACCOUNTS_ENABLED"), False) and not (
        (e.get("BRIDGE_ID_SECRET") or "").strip()
        or (e.get("BRIDGE_ACCESS_KEY") or "").strip()
    ):
        raise ConfigError(
            "ACCOUNTS_ENABLED requires a stable BRIDGE_ID_SECRET (or "
            "BRIDGE_ACCESS_KEY) so sessions survive restarts. Set "
            "BRIDGE_ID_SECRET to a fixed random string."
        )

    return Config(
        feeds=tuple(feeds),
        state_dir=(e.get("STATE_DIR") or "/config").strip(),
        cache_dir=(e.get("CACHE_DIR") or "/cache").strip(),
        cover_max_edge=_as_int(e.get("COVER_MAX_EDGE"), 320, "COVER_MAX_EDGE"),
        cover_jpeg_quality=_as_int(e.get("COVER_JPEG_QUALITY"), 80, "COVER_JPEG_QUALITY"),
        extra_origins=extra_origins,
        upstream_user_agent=(e.get("UPSTREAM_USER_AGENT") or "").strip() or None,
        kavita_base_url=base_url,
        kavita_opds_url=opds_url,
        kavita_origin=kavita_origin,
        api_key=api_key,
        app_port=_as_int(e.get("APP_PORT"), 8099, "APP_PORT"),
        bridge_public_url=(e.get("BRIDGE_PUBLIC_URL") or "").strip() or None,
        bridge_access_key=(e.get("BRIDGE_ACCESS_KEY") or "").strip() or None,
        bridge_id_secret=(e.get("BRIDGE_ID_SECRET") or "").strip() or None,
        allowed_ips=allowed,
        show_covers=_as_bool(e.get("SHOW_COVERS"), True),
        accounts_enabled=_as_bool(e.get("ACCOUNTS_ENABLED"), False),
        cache_feeds_seconds=_as_int(e.get("CACHE_FEEDS_SECONDS"), 300, "CACHE_FEEDS_SECONDS"),
        cache_books=_as_bool(e.get("CACHE_BOOKS"), False),
        log_level=(e.get("LOG_LEVEL") or "info").strip(),
        pdf_disposition=pdf_disp,
        epub_disposition=epub_disp,
        tz=(e.get("TZ") or "America/Chicago").strip(),
    )
