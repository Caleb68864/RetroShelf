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

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

# Redaction token used wherever a secret would otherwise appear.
REDACTED = "***"

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

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        """All origins the bridge may fetch from (Kavita + any extras).

        Combines :attr:`kavita_origin` with every entry in
        :attr:`extra_origins` into a single ordered tuple consumed by
        the SSRF guard.

        :returns: Tuple of normalised ``scheme://host[:port]`` origin
            strings that upstream fetches are permitted to target.
        :rtype: tuple[str, ...]
        """
        return (self.kavita_origin, *self.extra_origins)

    # -- secret masking ------------------------------------------------------
    def mask(self, text: str) -> str:
        """Redact known secrets anywhere they appear in *text*.

        Replaces :attr:`api_key` and :attr:`bridge_access_key` with
        :data:`REDACTED` wherever they occur. Applied unconditionally to
        both log output and any user-visible surface — debug mode only
        loosens *log verbosity*, not masking.

        **[C-4]** Secret values shorter than 8 characters are NOT
        masked to avoid mangling generic OPDS path segments such as
        ``opds`` that would otherwise match trivially short keys.

        :param text: Arbitrary string that may contain secret values.
        :type text: str
        :returns: *text* with all qualifying secret occurrences replaced
            by :data:`REDACTED`. Returns *text* unchanged when empty.
        :rtype: str
        """
        if not text:
            return text
        out = text
        for secret in (self.api_key, self.bridge_access_key):
            if secret and len(secret) >= 8:
                out = out.replace(secret, REDACTED)
        return out

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

    Recognised environment variables
    (all optional unless noted):

    - ``KAVITA_OPDS_URL`` (**required**) — full user-specific OPDS URL.
    - ``KAVITA_BASE_URL`` — Kavita web base URL; derived if absent.
    - ``APP_PORT`` — bridge listen port (default ``8099``).
    - ``BRIDGE_PUBLIC_URL`` — public base URL of this bridge.
    - ``BRIDGE_ACCESS_KEY`` — shared secret for incoming requests.
    - ``BRIDGE_ID_SECRET`` — secret for opaque identifier signing.
    - ``ALLOWED_IPS`` — comma-separated IP allow-list.
    - ``SHOW_COVERS`` — bool; default ``true``.
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

    opds_url = (e.get("KAVITA_OPDS_URL") or "").strip()
    base_url = (e.get("KAVITA_BASE_URL") or "").strip()

    if not opds_url:
        raise ConfigError(
            "KAVITA_OPDS_URL is required (the full user-specific Kavita OPDS URL, "
            "e.g. http://kavita:5000/api/opds/YOUR_AUTH_KEY)."
        )
    # If base URL is omitted, derive it from the OPDS origin.
    if not base_url:
        base_url = _normalize_origin(opds_url)

    kavita_origin = _normalize_origin(opds_url)
    # Sanity: base and opds should share an origin.
    if _normalize_origin(base_url) != kavita_origin:
        raise ConfigError(
            "KAVITA_BASE_URL and KAVITA_OPDS_URL must share the same origin "
            f"(got {_normalize_origin(base_url)} vs {kavita_origin})."
        )

    api_key = _extract_api_key(opds_url)

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

    return Config(
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
        cache_feeds_seconds=_as_int(e.get("CACHE_FEEDS_SECONDS"), 300, "CACHE_FEEDS_SECONDS"),
        cache_books=_as_bool(e.get("CACHE_BOOKS"), False),
        log_level=(e.get("LOG_LEVEL") or "info").strip(),
        pdf_disposition=pdf_disp,
        epub_disposition=epub_disp,
        tz=(e.get("TZ") or "America/Chicago").strip(),
    )
