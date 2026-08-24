"""Authentication primitives for the opt-in accounts + profiles layer.

This module is deliberately **pure**: it imports nothing from FastAPI, the
:class:`~app.store.Store`, or the request layer, so every function here is a
small unit-testable piece of cryptography with no I/O. The application layer
(:mod:`app.main`) wires these into routes and the session cookie; the on-disk
account records live in :mod:`app.store`.

Three concerns live here, all built on the standard library only (no bcrypt /
argon2 / passlib / itsdangerous dependency — the container runs a plain
``python:3.12-slim``):

- **Passwords** — salted PBKDF2-HMAC-SHA256. :func:`hash_password` returns the
  salt, iteration count, and hash so the parameters can evolve without a schema
  change; :func:`verify_password` is constant-time via
  :func:`hmac.compare_digest`. A password is never stored, returned, or logged
  in plaintext, and :func:`dummy_verify` burns the same work for an unknown
  user so login timing cannot distinguish "no such user" from "wrong password".
- **Sessions** — a stateless, HMAC-signed cookie value carrying the
  ``account_id``, ``profile_id``, ``token_version``, and an absolute ``expiry``.
  :func:`encode_session` signs it (mirroring :class:`app.ids.IdCodec`'s
  encrypt-then-verify discipline — here just sign, the payload is not secret);
  :func:`decode_session` verifies the MAC in constant time and rejects any
  tampered, malformed, or expired token by returning ``None``. The
  ``token_version`` is carried in the payload and returned to the caller, which
  compares it against the account's current version so a bump (password change
  or "sign out everywhere") invalidates every outstanding cookie.
- **CSRF** — :func:`csrf_token` derives a per-session token by HMAC'ing a
  *binding* string (the raw session-cookie value, or ``""`` for the
  pre-login setup/login forms) under a domain-separated key; :func:`csrf_ok`
  checks a submitted token in constant time. Every mutating POST form embeds a
  hidden ``_csrf`` field validated server-side, layered on top of the cookie's
  ``SameSite=Lax``.

All key material is derived from one configured secret (the same
``BRIDGE_ID_SECRET`` that stabilises :class:`app.ids.IdCodec`), domain-separated
per purpose so the password, session, and CSRF keys are independent.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time

# -- password hashing parameters ---------------------------------------------
#
# PBKDF2-HMAC-SHA256 with a per-password 16-byte random salt. 600k iterations is
# in line with current OWASP guidance for PBKDF2-SHA256 and is comfortably fast
# on the small LAN this tool serves. The chosen count is *stored per hash* so a
# future contributor can raise it and re-hash on next login without a migration.
_KDF_NAME = "pbkdf2_sha256"
_DEFAULT_ITERATIONS = 600_000
_SALT_BYTES = 16

# A fixed salt used only by :func:`dummy_verify`. It never guards a real
# account — its sole job is to make the KDF run for the same wall-clock time on
# the unknown-user path as on the wrong-password path, so login timing does not
# leak which usernames exist.
_DUMMY_SALT = b"retroshelf/dummy"

# -- session token wire format -----------------------------------------------
#
# ``s1.<body>.<mac>`` — a version marker, the base64url JSON payload, and a
# truncated HMAC-SHA256 over ``"s1.<body>"``. Binding the version into the MAC
# means the format can evolve without a downgrade being forgeable, exactly as
# :class:`app.ids.IdCodec` binds its own version marker.
_SESSION_PREFIX = "s1"
_SESSION_MAC_LEN = 16

#: Name of the HttpOnly session cookie carrying the signed session token.
SESSION_COOKIE = "rs_session"
# A legitimate session cookie is a few hundred bytes; anything far larger is
# junk or abuse and is rejected before any crypto work is done.
_MAX_SESSION_LEN = 4096


def _b64e(raw: bytes) -> str:
    """Encode *raw* as unpadded URL-safe base64 text.

    :param raw: Bytes to encode.
    :returns: URL-safe base64 with the ``=`` padding stripped.
    :rtype: str
    """
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    """Decode unpadded URL-safe base64 *text*, restoring stripped padding.

    :param text: URL-safe base64 string (padding optional).
    :returns: The decoded bytes.
    :rtype: bytes
    """
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _subkey(secret: str, purpose: bytes) -> bytes:
    """Derive a 256-bit key for *purpose* from the configured *secret*.

    Domain separation means the password-pepper, session-signing, and CSRF keys
    are cryptographically independent even though one secret configures them
    all. The secret is stable across restarts when ``BRIDGE_ID_SECRET`` is set,
    so issued sessions survive a restart.

    :param secret: The configured application secret.
    :param purpose: A short, unique domain-separation label.
    :returns: A 32-byte key.
    :rtype: bytes
    """
    return hashlib.sha256(purpose + b":" + secret.encode("utf-8")).digest()


# -- passwords ---------------------------------------------------------------
def hash_password(
    password: str, iterations: int = _DEFAULT_ITERATIONS
) -> tuple[str, int, str]:
    """Hash *password* with a fresh random salt using PBKDF2-HMAC-SHA256.

    The plaintext is never retained. The returned salt and iteration count are
    stored alongside the hash so :func:`verify_password` can reproduce it and so
    the cost can be raised in future without breaking existing hashes.

    :param password: The plaintext password (never stored or logged).
    :param iterations: PBKDF2 iteration count; defaults to
        :data:`_DEFAULT_ITERATIONS`.
    :returns: ``(salt_hex, iterations, hash_hex)``.
    :rtype: tuple[str, int, str]
    """
    salt = os.urandom(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return salt.hex(), iterations, digest.hex()


def verify_password(
    password: str, salt_hex: str, iterations: int, hash_hex: str
) -> bool:
    """Constant-time check of *password* against a stored PBKDF2 hash.

    Any malformed stored parameter (non-hex salt/hash, non-positive iteration
    count) yields ``False`` rather than raising, so a corrupt or hand-edited
    account record simply fails to authenticate.

    :param password: The submitted plaintext password.
    :param salt_hex: The stored salt, hex-encoded.
    :param iterations: The stored PBKDF2 iteration count.
    :param hash_hex: The stored hash, hex-encoded.
    :returns: ``True`` only when the password reproduces the stored hash.
    :rtype: bool
    """
    if not isinstance(iterations, int) or iterations <= 0:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(digest, expected)


def dummy_verify(password: str, iterations: int = _DEFAULT_ITERATIONS) -> bool:
    """Burn a PBKDF2 computation and return ``False`` (no-enumeration timing).

    Called on the login path when the submitted username does not exist, so the
    unknown-user branch spends the same time as a real
    :func:`verify_password`. Always returns ``False``.

    :param password: The submitted plaintext password.
    :param iterations: Iteration count to match a real verify; defaults to
        :data:`_DEFAULT_ITERATIONS`.
    :returns: Always ``False``.
    :rtype: bool
    """
    hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), _DUMMY_SALT, iterations)
    return False


# -- sessions ----------------------------------------------------------------
def encode_session(
    secret: str, account_id: str, profile_id: str, token_version: int, expiry: int
) -> str:
    """Sign a session payload into an opaque, tamper-evident cookie value.

    The payload (account id, profile id, token version, absolute expiry) is not
    secret, so it is signed rather than encrypted: a base64url JSON body plus a
    truncated HMAC-SHA256 over ``"s1.<body>"``. A forged or edited value fails
    :func:`decode_session`.

    :param secret: The configured application secret.
    :param account_id: The owning account's id.
    :param profile_id: The selected profile's id (``""`` before a profile is
        chosen).
    :param token_version: The account's current token version, so a later bump
        invalidates this cookie.
    :param expiry: Absolute Unix expiry time (seconds).
    :returns: A ``s1.<body>.<mac>`` cookie value.
    :rtype: str
    """
    payload = json.dumps(
        {"a": account_id, "p": profile_id, "v": token_version, "e": int(expiry)},
        separators=(",", ":"),
    )
    body_b64 = _b64e(payload.encode("utf-8"))
    mac = _session_mac(secret, body_b64)
    return f"{_SESSION_PREFIX}.{body_b64}.{_b64e(mac)}"


def _session_mac(secret: str, body_b64: str) -> bytes:
    """Return the truncated MAC binding the version marker to *body_b64*.

    :param secret: The configured application secret.
    :param body_b64: The base64url payload exactly as it appears in the token.
    :returns: The truncated HMAC-SHA256 bytes.
    :rtype: bytes
    """
    message = f"{_SESSION_PREFIX}.{body_b64}".encode("ascii")
    key = _subkey(secret, b"retroshelf/session/v1")
    return hmac.new(key, message, hashlib.sha256).digest()[:_SESSION_MAC_LEN]


def decode_session(secret: str, token: str | None) -> dict | None:
    """Verify and decode a session cookie previously made by :func:`encode_session`.

    Performs constant-time MAC verification before trusting any field, then
    enforces the absolute expiry. Returns ``None`` — never raises — for an
    empty, oversized, malformed, tampered, or expired token, so a caller can
    treat "no valid session" and "bad cookie" identically.

    The ``token_version`` is returned for the caller to compare against the
    account's current version (this function cannot see the store); a mismatch
    there is what revokes a still-unexpired cookie after a password change.

    :param secret: The configured application secret.
    :param token: The raw cookie value, or ``None``.
    :returns: ``{"account_id", "profile_id", "token_version", "expiry"}`` on a
        valid, unexpired token, else ``None``.
    :rtype: dict | None
    """
    if not token or len(token) > _MAX_SESSION_LEN:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _SESSION_PREFIX:
        return None
    _prefix, body_b64, mac_b64 = parts
    try:
        mac = _b64d(mac_b64)
        body = _b64d(body_b64)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(_session_mac(secret, body_b64), mac):
        return None
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    account_id = payload.get("a")
    profile_id = payload.get("p")
    version = payload.get("v")
    expiry = payload.get("e")
    if not isinstance(account_id, str) or not isinstance(profile_id, str):
        return None
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    if not isinstance(expiry, int) or isinstance(expiry, bool):
        return None
    if expiry <= int(time.time()):
        return None
    return {
        "account_id": account_id,
        "profile_id": profile_id,
        "token_version": version,
        "expiry": expiry,
    }


# -- CSRF --------------------------------------------------------------------
def csrf_token(secret: str, binding: str) -> str:
    """Derive the CSRF token for a session *binding*.

    The binding is the raw session-cookie value (so the token rotates whenever
    the session is re-minted, e.g. on a profile switch), or ``""`` for the
    pre-login ``/setup`` and ``/login`` forms. Because the key is
    domain-separated from the session-signing key, the CSRF token cannot be used
    as, or derived from, a valid session.

    :param secret: The configured application secret.
    :param binding: The session-cookie value the form belongs to, or ``""``.
    :returns: A 32-hex-character token safe to embed in a hidden form field.
    :rtype: str
    """
    key = _subkey(secret, b"retroshelf/csrf/v1")
    return hmac.new(key, binding.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def csrf_ok(secret: str, binding: str, provided: str | None) -> bool:
    """Constant-time check of a submitted CSRF token for *binding*.

    :param secret: The configured application secret.
    :param binding: The session-cookie value the form belonged to, or ``""``.
    :param provided: The ``_csrf`` field from the submitted form, or ``None``.
    :returns: ``True`` only when *provided* matches the expected token.
    :rtype: bool
    """
    if not provided:
        return False
    return hmac.compare_digest(provided, csrf_token(secret, binding))
