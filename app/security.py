"""Security helpers: filename sanitization, access-key check, IP allowlist.

All functions are pure (take primitives, not a Request) so they are trivially
testable; the app layer extracts request fields and calls them.
"""
from __future__ import annotations

import hmac
import ipaddress
import re
import unicodedata

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_filename(name: str | None, ext: str) -> str:
    """Return a safe ASCII filename ending in ``.{ext}``.

    Strips directory components, control characters, quotes, ``..``, and
    non-ASCII; collapses anything unsafe to ``_``; guarantees the correct
    single extension. Never returns a path-traversal or empty name. [C-? / SS-04]
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
    """True when no key is configured (open) or *provided* matches *configured*
    in constant time."""
    if not configured:
        return True
    if not provided:
        return False
    return hmac.compare_digest(provided, configured)


def ip_allowed(client_ip: str | None, allowed: tuple[str, ...]) -> bool:
    """True when no allowlist is configured, or *client_ip* is in one of the
    allowed addresses/CIDR networks. Direct-LAN only — uses the socket peer
    address, NOT X-Forwarded-For; do not rely on this behind a reverse proxy. [H-6]"""
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
