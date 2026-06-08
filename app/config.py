"""Configuration: typed env parsing, validation, Kavita-origin derivation, and
secret masking.

All app configuration comes from environment variables (12-factor). `load_config`
is pure and accepts an explicit mapping so it is trivially testable. Required
variables that are missing raise a typed :class:`ConfigError` with a clear
message — never a raw traceback leaked to the operator.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

# Redaction token used wherever a secret would otherwise appear.
REDACTED = "***"

_DEFAULT_PORTS = {"http": 80, "https": 443}


class ConfigError(Exception):
    """Raised when configuration is missing or invalid. Carries a clear,
    operator-facing message and never leaks a traceback to end users."""


def _normalize_origin(url: str) -> str:
    """Return the canonical ``scheme://host[:port]`` origin for *url*.

    Implicit default ports (80/http, 443/https) are dropped so that
    ``http://kavita`` and ``http://kavita:80`` compare equal. Raises
    :class:`ConfigError` if *url* has no scheme or host.
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
    """Return ``(scheme, host, port)`` with default ports normalized. Used by
    the SSRF guard so default-port and protocol variations compare correctly."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        raise ConfigError("Invalid URL (missing scheme or host)")
    scheme = parts.scheme.lower()
    host = parts.hostname.lower()
    port = parts.port if parts.port is not None else _DEFAULT_PORTS.get(scheme, 0)
    return (scheme, host, port)


def _extract_api_key(opds_url: str) -> str:
    """Pull the Kavita apiKey out of an OPDS URL of the form
    ``.../api/opds/{apiKey}[/...]``. Returns "" if not found."""
    path = urlsplit(opds_url).path
    segments = [s for s in path.split("/") if s]
    for i, seg in enumerate(segments):
        if seg == "opds" and i + 1 < len(segments):
            return segments[i + 1]
    # Fallback: last non-empty path segment.
    return segments[-1] if segments else ""


def _mask_url_static(url: str) -> str:
    """Best-effort mask of an apiKey-looking trailing segment, used before a
    Config exists (e.g. while validating)."""
    try:
        key = _extract_api_key(url)
    except Exception:
        key = ""
    return url.replace(key, REDACTED) if key else url


def _as_bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str | None, default: int, name: str) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


@dataclass(frozen=True)
class Config:
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

    @property
    def allowed_origins(self) -> tuple[str, ...]:
        """All origins the bridge may fetch from (Kavita + any extras)."""
        return (self.kavita_origin, *self.extra_origins)

    # -- secret masking ------------------------------------------------------
    def mask(self, text: str) -> str:
        """Redact the Kavita apiKey (and bridge access key) anywhere they
        appear in *text*. Applied to BOTH logs and any user-visible surface —
        masking on response bodies is unconditional, debug only loosens logs.
        [C-4] Short (<8 char) values are NOT masked, so a generic OPDS path
        segment like ``opds`` doesn't mangle every URL."""
        if not text:
            return text
        out = text
        for secret in (self.api_key, self.bridge_access_key):
            if secret and len(secret) >= 8:
                out = out.replace(secret, REDACTED)
        return out

    @property
    def debug(self) -> bool:
        return self.log_level.strip().lower() == "debug"


def load_config(env: dict[str, str] | None = None) -> Config:
    """Build a :class:`Config` from *env* (defaults to ``os.environ``).

    Raises :class:`ConfigError` (clear message, no traceback) when a required
    variable is missing or malformed.
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
