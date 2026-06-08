"""Opaque bridge-id codec.

Maps an upstream Kavita URL ↔ an opaque, tamper-evident token used in bridge
URLs (``/download/{id}/...``, ``/feed/{id}``, ``/cover/{id}``). The token is
**authenticated encryption** (encrypt-then-MAC with an HMAC-SHA256 keystream),
so a user who sees the id in their address bar cannot recover the apiKey it
encodes, and a forged/tampered id is rejected.

Stdlib only (no `cryptography` dependency). The secret is stable across restarts
when ``BRIDGE_ID_SECRET`` is set, so bookmarked links keep working. [M-2]
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os

from .errors import BadIdError

log = logging.getLogger("retroshelf.ids")

_NONCE_LEN = 6
_MAC_LEN = 12


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


class IdCodec:
    """Encode/decode opaque, authenticated bridge ids."""

    def __init__(self, secret: str | None):
        if not secret:
            secret = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
            log.warning(
                "BRIDGE_ID_SECRET not set — using a per-process random secret; "
                "bookmarked /download links will break on restart."
            )
        self._key = hashlib.sha256(secret.encode("utf-8")).digest()

    def _keystream(self, nonce: bytes, length: int) -> bytes:
        out = bytearray()
        counter = 0
        while len(out) < length:
            block = hmac.new(self._key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
            out.extend(block)
            counter += 1
        return bytes(out[:length])

    def encode(self, value: str) -> str:
        data = value.encode("utf-8")
        nonce = os.urandom(_NONCE_LEN)
        ks = self._keystream(nonce, len(data))
        ct = bytes(a ^ b for a, b in zip(data, ks))
        body = nonce + ct
        mac = hmac.new(self._key, body, hashlib.sha256).digest()[:_MAC_LEN]
        return f"{_b64e(body)}.{_b64e(mac)}"

    def decode(self, token: str) -> str:
        if not token or "." not in token:
            raise BadIdError("Malformed id")
        try:
            body_s, mac_s = token.split(".", 1)
            body = _b64d(body_s)
            mac = _b64d(mac_s)
        except Exception as exc:  # noqa: BLE001 - any decode failure is a bad id
            raise BadIdError("Malformed id") from exc
        expected = hmac.new(self._key, body, hashlib.sha256).digest()[:_MAC_LEN]
        if not hmac.compare_digest(expected, mac):
            raise BadIdError("Bad id signature (tampered or wrong secret)")
        if len(body) < _NONCE_LEN:
            raise BadIdError("Truncated id")
        nonce, ct = body[:_NONCE_LEN], body[_NONCE_LEN:]
        ks = self._keystream(nonce, len(ct))
        data = bytes(a ^ b for a, b in zip(ct, ks))
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BadIdError("Corrupt id payload") from exc
