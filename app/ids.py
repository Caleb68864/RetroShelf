"""Opaque bridge-id codec.

Maps an upstream Kavita URL ↔ an opaque, tamper-evident token used in bridge
URLs (``/download/{id}/...``, ``/feed/{id}``, ``/cover/{id}``). The token is
**authenticated encryption** (encrypt-then-MAC with an HMAC-SHA256 keystream),
so a user who sees the id in their address bar cannot recover the apiKey it
encodes, and a forged/tampered id is rejected.

Stdlib only (no ``cryptography`` dependency). The secret is stable across
restarts when ``BRIDGE_ID_SECRET`` is set, so bookmarked links keep working.
[M-2]

:raises BadIdError: Raised by :meth:`IdCodec.decode` whenever a token is
    malformed, too large, fails MAC verification, is truncated, or contains
    a non-UTF-8 payload.
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
    """Encode and decode opaque, authenticated bridge ids.

    Each encoded id consists of a random nonce, an XOR-encrypted ciphertext,
    and a truncated HMAC-SHA256 MAC — all base64url-encoded and joined by a
    ``.`` separator. Verification is always performed in constant time via
    :func:`hmac.compare_digest`.
    """

    def __init__(self, secret: str | None) -> None:
        """Initialise the codec, deriving a 256-bit key from *secret*.

        If *secret* is falsy (``None`` or empty string), a cryptographically
        random secret is generated at process startup and a warning is logged.
        In that case bookmarked bridge URLs will break on restart.

        :param secret: Shared secret string used to derive the HMAC key.
            Pass ``None`` or an empty string to auto-generate an ephemeral key.
        :type secret: str or None
        """
        if not secret:
            secret = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii")
            log.warning(
                "BRIDGE_ID_SECRET not set — using a per-process random secret; "
                "bookmarked /download links will break on restart."
            )
        self._key = hashlib.sha256(secret.encode("utf-8")).digest()

    def _keystream(self, nonce: bytes, length: int) -> bytes:
        """Generate a deterministic keystream via counter-mode HMAC-SHA256.

        Produces at least *length* bytes by iterating
        ``HMAC-SHA256(key, nonce || counter)`` with a 4-byte big-endian
        counter, concatenating digest blocks until the requested length is
        satisfied, then truncating.

        :param nonce: Per-message random nonce mixed into each HMAC block.
        :type nonce: bytes
        :param length: Number of keystream bytes required.
        :type length: int
        :returns: Exactly *length* keystream bytes.
        :rtype: bytes
        """
        out = bytearray()
        counter = 0
        while len(out) < length:
            block = hmac.new(self._key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
            out.extend(block)
            counter += 1
        return bytes(out[:length])

    def encode(self, value: str) -> str:
        """Encode *value* into an opaque, tamper-evident token string.

        Encodes the UTF-8 representation of *value* with a fresh random nonce,
        appends a truncated HMAC-SHA256 MAC, and returns the result as a
        base64url dot-delimited token safe for use in URL path segments.

        :param value: The plaintext string to encode (e.g. a Kavita URL).
        :type value: str
        :returns: A base64url token of the form ``<body>.<mac>``.
        :rtype: str
        """
        data = value.encode("utf-8")
        nonce = os.urandom(_NONCE_LEN)
        ks = self._keystream(nonce, len(data))
        ct = bytes(a ^ b for a, b in zip(data, ks))
        body = nonce + ct
        mac = hmac.new(self._key, body, hashlib.sha256).digest()[:_MAC_LEN]
        return f"{_b64e(body)}.{_b64e(mac)}"

    # A legitimate token encodes a URL of a few hundred chars; anything far
    # larger is junk/abuse and is rejected before doing crypto work.
    MAX_TOKEN_LEN = 8192

    def decode(self, token: str) -> str:
        """Verify and decode a token previously produced by :meth:`encode`.

        Performs constant-time MAC verification before any payload is
        returned. Rejects tokens that are empty, lack the ``.`` separator,
        exceed :attr:`MAX_TOKEN_LEN`, fail base64 decoding, have a bad or
        tampered MAC, are truncated, or whose plaintext is not valid UTF-8.

        :param token: A base64url token of the form ``<body>.<mac>``.
        :type token: str
        :returns: The original plaintext string that was passed to
            :meth:`encode`.
        :rtype: str
        :raises BadIdError: If the token is malformed, too large, fails MAC
            verification, is truncated, or contains a non-UTF-8 payload.
        """
        if not token or "." not in token:
            raise BadIdError("Malformed id")
        if len(token) > self.MAX_TOKEN_LEN:
            raise BadIdError("Id too large")
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
