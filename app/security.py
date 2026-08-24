"""Security helpers: filename sanitization, access-key check, IP allowlist.

All functions are pure (accept primitives, not a ``Request``) so they are
trivially unit-testable; the application layer extracts the relevant fields
from each request and delegates here.

:no-index:
"""
from __future__ import annotations

import hmac
import ipaddress
import re
import unicodedata

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_filename(name: str | None, ext: str) -> str:
    """Return a safe ASCII filename ending in ``.{ext}``.

    Strips directory components (both ``/`` and ``\\`` separators), control
    characters, quotes, ``..``, and non-ASCII code points; folds Unicode to
    ASCII via NFKD normalisation; collapses any remaining unsafe characters to
    ``_``; and guarantees a single correct extension. The result is truncated
    to 120 characters (before the extension) and is never empty or a
    path-traversal sequence. [SS-04]

    :param name: Raw filename supplied by the caller (e.g. from a
        ``Content-Disposition`` header). ``None`` or an empty string falls
        back to ``"download"``.
    :type name: str or None
    :param ext: Desired file extension **without** a leading dot (e.g.
        ``"epub"``). An empty string omits the extension entirely.
    :type ext: str
    :returns: A sanitised filename string such as ``"My_Book.epub"``.
    :rtype: str
    """
    ext = (ext or "").lower().lstrip(".")
    base = name or "download"
    # Strip any directory components (both separators).
    base = base.replace("\\", "/").split("/")[-1]
    # Fold unicode to ASCII (e.g. accented chars -> plain or dropped).
    base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    # Drop a pre-existing matching extension to avoid "name.epub.epub".
    if ext and base.lower().endswith("." + ext):
        base = base[: -(len(ext) + 1)]
    base = _UNSAFE.sub("_", base).strip("._")
    if not base:
        base = "download"
    base = base[:120]
    return f"{base}.{ext}" if ext else base


def access_key_ok(provided: str | None, configured: str | None) -> bool:
    """Return ``True`` when the bridge endpoint should be accessible.

    When no key is configured the bridge is considered open and every request
    is accepted. When a key *is* configured the comparison is performed with
    :func:`hmac.compare_digest` to prevent timing-based key enumeration.

    :param provided: The access key submitted by the client, or ``None`` if
        absent from the request.
    :type provided: str or None
    :param configured: The expected access key read from application config,
        or ``None`` / empty string when authentication is disabled.
    :type configured: str or None
    :returns: ``True`` if access should be granted, ``False`` otherwise.
    :rtype: bool
    """
    if not configured:
        return True
    if not provided:
        return False
    return hmac.compare_digest(provided, configured)


def ip_allowed(client_ip: str | None, allowed: tuple[str, ...]) -> bool:
    """Return ``True`` when the client IP is permitted by the allowlist.

    When *allowed* is empty (no allowlist configured) every IP is accepted.
    Otherwise *client_ip* must be a valid IP address that matches at least one
    entry in *allowed*, which may be either a bare IP address or a CIDR network
    string (e.g. ``"192.168.1.0/24"``). Invalid entries in *allowed* are
    skipped silently.

    .. warning::
        This function uses the **socket peer address** (direct-LAN only).
        It does **not** inspect ``X-Forwarded-For`` or similar headers and
        must not be relied upon for access control behind a reverse proxy.
        [H-6]

    :param client_ip: The peer IP address string of the connecting client,
        or ``None`` if unavailable.
    :type client_ip: str or None
    :param allowed: Tuple of allowed IP address or CIDR network strings.
        An empty tuple disables the allowlist entirely.
    :type allowed: tuple[str, ...]
    :returns: ``True`` if the IP is allowed or no allowlist is configured,
        ``False`` otherwise.
    :rtype: bool
    """
    if not allowed:
        return True
    if not client_ip:
        return False
    try:
        addr = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    for entry in allowed:
        try:
            if "/" in entry:
                if addr in ipaddress.ip_network(entry, strict=False):
                    return True
            elif addr == ipaddress.ip_address(entry):
                return True
        except ValueError:
            continue
    return False
