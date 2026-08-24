"""Opaque bridge-id codec.

Maps an upstream Kavita URL ↔ an opaque, tamper-evident token used in bridge
URLs (``/download/{id}/...``, ``/feed/{id}``, ``/cover/{id}``). The token is
**authenticated encryption** (encrypt-then-MAC with an HMAC-SHA256 keystream),
so a user who sees the id in their address bar cannot recover the apiKey it
encodes, and a forged/tampered id is rejected.

Two wire formats coexist:

- **v2** (``2.<body>.<mac>``) — what :meth:`IdCodec.encode` issues: 12-byte
  nonce, 128-bit truncated MAC, separate derived keys for encryption and
  authentication, version marker bound into the MAC. [SS-11]
- **v1** (``<body>.<mac>``) — legacy; still decoded so links bookmarked on an
  iPad home screen before the upgrade keep working, never issued any more.

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

# -- v1 (legacy) parameters. Still *decoded* so that links bookmarked on an
# iPad's home screen before an upgrade keep working; never issued any more.
_NONCE_LEN = 6
_MAC_LEN = 12

# -- v2 parameters.
#
# The keystream is XOR against HMAC-in-counter-mode, so a repeated nonce under
# the same key leaks the XOR of two plaintexts — and those plaintexts contain
# the apiKey. A 6-byte nonce collides with ~50% probability after roughly 2**24
# ids, and a busy shelf issues an id per book *per page render*, which puts
# that milestone within reach of an ordinary year of browsing. 12 bytes moves
# it to 2**48 and out of reach. The MAC widens from 96 to 128 bits at the same
# time, and encryption and authentication now use separately derived keys so
# neither primitive is analysing output of the other. [SS-11]
_V2_PREFIX = "2"
_V2_NONCE_LEN = 12
_V2_MAC_LEN = 16


def _b64e(b: bytes) -> str:
    """Encode *b* as unpadded URL-safe base64 text.

    :param b: Raw bytes to encode.
    :returns: URL-safe base64 string with the ``=`` padding stripped.
    :rtype: str
    """
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    """Decode unpadded URL-safe base64 *s*, restoring any stripped padding.

    :param s: URL-safe base64 string (padding optional).
    :returns: The decoded bytes.
    :rtype: bytes
    """
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
        # Domain-separated subkeys for v2. Deriving both from the same root
        # means one configured ``BRIDGE_ID_SECRET`` still drives everything,
        # while the MAC key and the keystream key are independent.
        self._enc_key = hmac.new(self._key, b"retroshelf/v2/enc", hashlib.sha256).digest()
        self._mac_key = hmac.new(self._key, b"retroshelf/v2/mac", hashlib.sha256).digest()
        # A third, unrelated subkey: the site token that guards state-changing
        # links. Derived here so it shares the configured secret's lifetime —
        # stable across restarts when BRIDGE_ID_SECRET is set, and rotated
        # automatically when it is not.
        self.site_token = hmac.new(
            self._key, b"retroshelf/v2/csrf", hashlib.sha256
        ).hexdigest()[:24]

    def token_ok(self, provided: str | None) -> bool:
        """Constant-time check of a supplied site token against :attr:`site_token`.

        :param provided: The ``t=`` query parameter from the request.
        :returns: ``True`` when it matches.
        :rtype: bool
        """
        if not provided:
            return False
        return hmac.compare_digest(provided, self.site_token)

    def _keystream(self, nonce: bytes, length: int, key: bytes | None = None) -> bytes:
        """Generate a deterministic keystream via counter-mode HMAC-SHA256.

        Produces at least *length* bytes by iterating
        ``HMAC-SHA256(key, nonce || counter)`` with a 4-byte big-endian
        counter, concatenating digest blocks until the requested length is
        satisfied, then truncating.

        :param nonce: Per-message random nonce mixed into each HMAC block.
        :type nonce: bytes
        :param length: Number of keystream bytes required.
        :type length: int
        :param key: HMAC key to use; defaults to the legacy root key.
        :type key: bytes or None
        :returns: Exactly *length* keystream bytes.
        :rtype: bytes
        """
        hmac_key = self._key if key is None else key
        out = bytearray()
        counter = 0
        while len(out) < length:
            block = hmac.new(hmac_key, nonce + counter.to_bytes(4, "big"), hashlib.sha256).digest()
            out.extend(block)
            counter += 1
        return bytes(out[:length])

    def encode(self, value: str) -> str:
        """Encode *value* into an opaque, tamper-evident token string.

        Encodes the UTF-8 representation of *value* with a fresh random nonce,
        appends a truncated HMAC-SHA256 MAC, and returns the result as a
        base64url dot-delimited token safe for use in URL path segments.

        Tokens are issued in the v2 format ``2.<body>.<mac>``; the version
        marker is authenticated along with the body, so a v2 token cannot be
        stripped back to a v1 one.

        :param value: The plaintext string to encode (e.g. a Kavita URL).
        :type value: str
        :returns: A base64url token of the form ``2.<body>.<mac>``.
        :rtype: str
        """
        data = value.encode("utf-8")
        nonce = os.urandom(_V2_NONCE_LEN)
        ks = self._keystream(nonce, len(data), self._enc_key)
        ct = bytes(a ^ b for a, b in zip(data, ks))
        body = nonce + ct
        body_b64 = _b64e(body)
        mac = self._v2_mac(body_b64)
        return f"{_V2_PREFIX}.{body_b64}.{_b64e(mac)}"

    def _v2_mac(self, body_b64: str) -> bytes:
        """Authenticate the version marker together with the encoded body.

        Binding the version into the MAC input is what makes the two formats
        safe to serve side by side: a v2 token cannot be re-presented as v1 to
        get the weaker parameters applied to it.

        :param body_b64: The base64url body exactly as it appears in the token.
        :returns: The truncated MAC bytes.
        :rtype: bytes
        """
        message = f"{_V2_PREFIX}.{body_b64}".encode("ascii")
        return hmac.new(self._mac_key, message, hashlib.sha256).digest()[:_V2_MAC_LEN]

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
        if token.startswith(f"{_V2_PREFIX}."):
            return self._decode_v2(token)
        return self._decode_v1(token)

    def _decode_v2(self, token: str) -> str:
        """Verify and decode a ``2.<body>.<mac>`` token.

        :param token: The full token including its version marker.
        :returns: The original plaintext.
        :rtype: str
        :raises BadIdError: On any malformed, tampered, or truncated token.
        """
        parts = token.split(".")
        if len(parts) != 3:
            raise BadIdError("Malformed id")
        _version, body_b64, mac_b64 = parts
        try:
            body = _b64d(body_b64)
            mac = _b64d(mac_b64)
        except Exception as exc:  # noqa: BLE001 - any decode failure is a bad id
            raise BadIdError("Malformed id") from exc
        if not hmac.compare_digest(self._v2_mac(body_b64), mac):
            raise BadIdError("Bad id signature (tampered or wrong secret)")
        if len(body) < _V2_NONCE_LEN:
            raise BadIdError("Truncated id")
        nonce, ct = body[:_V2_NONCE_LEN], body[_V2_NONCE_LEN:]
        ks = self._keystream(nonce, len(ct), self._enc_key)
        data = bytes(a ^ b for a, b in zip(ct, ks))
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BadIdError("Corrupt id payload") from exc

    def _decode_v1(self, token: str) -> str:
        """Verify and decode a legacy ``<body>.<mac>`` token.

        Retained so links already saved to an iPad home screen survive the
        upgrade. :meth:`encode` never produces this format.

        :param token: A legacy token.
        :returns: The original plaintext.
        :rtype: str
        :raises BadIdError: On any malformed, tampered, or truncated token.
        """
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
