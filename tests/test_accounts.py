"""Unit tests for app.accounts — the pure auth primitives.

These cover the security-critical building blocks in isolation (no FastAPI, no
Store): password hashing, session signing/verification/expiry, and CSRF tokens.
The route-level flows live in tests/test_auth_routes.py.
"""
import time

from app import accounts

SECRET = "unit-test-secret"


# -- password hashing --------------------------------------------------------
def test_hash_is_salted_and_verifies():
    salt1, iters1, hash1 = accounts.hash_password("hunter2", iterations=1000)
    salt2, iters2, hash2 = accounts.hash_password("hunter2", iterations=1000)
    # A fresh random salt each time → same password hashes differently.
    assert salt1 != salt2
    assert hash1 != hash2
    # The hash is not the plaintext, and the salt is real 16-byte hex.
    assert "hunter2" not in hash1
    assert len(bytes.fromhex(salt1)) == 16
    # Correct password verifies; wrong password does not.
    assert accounts.verify_password("hunter2", salt1, iters1, hash1) is True
    assert accounts.verify_password("Hunter2", salt1, iters1, hash1) is False
    assert accounts.verify_password("", salt1, iters1, hash1) is False


def test_verify_rejects_malformed_stored_params():
    # A corrupt/hand-edited record must fail closed, not raise.
    assert accounts.verify_password("x", "not-hex", 1000, "also-not-hex") is False
    assert accounts.verify_password("x", "aa", 0, "bb") is False
    assert accounts.verify_password("x", "aa", -5, "bb") is False


def test_dummy_verify_is_always_false():
    assert accounts.dummy_verify("anything", iterations=1000) is False


# -- session tokens ----------------------------------------------------------
def test_session_roundtrip_carries_all_fields():
    exp = int(time.time()) + 3600
    tok = accounts.encode_session(SECRET, "acct1", "prof1", 3, exp)
    data = accounts.decode_session(SECRET, tok)
    assert data == {"account_id": "acct1", "profile_id": "prof1",
                    "token_version": 3, "expiry": exp}


def test_session_empty_profile_is_valid():
    exp = int(time.time()) + 3600
    data = accounts.decode_session(SECRET, accounts.encode_session(SECRET, "a", "", 1, exp))
    assert data is not None and data["profile_id"] == ""


def test_tampered_session_is_rejected():
    exp = int(time.time()) + 3600
    tok = accounts.encode_session(SECRET, "acct1", "prof1", 1, exp)
    prefix, body, mac = tok.split(".")
    # Flip a byte in the body → MAC no longer matches → None.
    flipped = "A" if body[0] != "A" else "B"
    bad_body = f"{prefix}.{flipped}{body[1:]}.{mac}"
    assert accounts.decode_session(SECRET, bad_body) is None
    # Flip a byte in the MAC → None.
    flipped_mac = "A" if mac[0] != "A" else "B"
    assert accounts.decode_session(SECRET, f"{prefix}.{body}.{flipped_mac}{mac[1:]}") is None


def test_session_signed_by_a_different_secret_is_rejected():
    exp = int(time.time()) + 3600
    tok = accounts.encode_session("other-secret", "acct1", "prof1", 1, exp)
    assert accounts.decode_session(SECRET, tok) is None


def test_expired_session_is_rejected():
    past = int(time.time()) - 1
    tok = accounts.encode_session(SECRET, "acct1", "prof1", 1, past)
    # The signature is valid, but the absolute expiry has passed → None.
    assert accounts.decode_session(SECRET, tok) is None


def test_malformed_session_tokens_are_rejected():
    assert accounts.decode_session(SECRET, None) is None
    assert accounts.decode_session(SECRET, "") is None
    assert accounts.decode_session(SECRET, "garbage") is None
    assert accounts.decode_session(SECRET, "a.b") is None
    assert accounts.decode_session(SECRET, "x1.body.mac") is None  # wrong version marker
    assert accounts.decode_session(SECRET, "s1.!!!.!!!") is None   # bad base64
    assert accounts.decode_session(SECRET, "s1." + "A" * 5000 + ".mac") is None  # oversized


# -- CSRF --------------------------------------------------------------------
def test_csrf_token_matches_and_binds():
    good = accounts.csrf_token(SECRET, "session-cookie-value")
    assert accounts.csrf_ok(SECRET, "session-cookie-value", good) is True
    # Wrong token, empty token, and a token for a different binding all fail.
    assert accounts.csrf_ok(SECRET, "session-cookie-value", "deadbeef") is False
    assert accounts.csrf_ok(SECRET, "session-cookie-value", None) is False
    assert accounts.csrf_ok(SECRET, "session-cookie-value", "") is False
    assert accounts.csrf_ok(SECRET, "OTHER-session", good) is False
    # A different secret yields a different token.
    assert accounts.csrf_token("other", "session-cookie-value") != good


def test_csrf_ok_non_ascii_provided_does_not_raise():
    # Regression: the submitted _csrf field is attacker-controlled. A non-ASCII
    # value must simply fail the check, not raise TypeError out of
    # hmac.compare_digest (which would surface as an unhandled 500).
    assert accounts.csrf_ok(SECRET, "binding", "café") is False
    assert accounts.csrf_ok(SECRET, "binding", "🧨" * 8) is False


def test_password_length_is_capped_before_hashing():
    # Two passwords that differ only past the cap hash identically, proving the
    # KDF input is bounded. No real password reaches the cap; this just confirms
    # a giant login body cannot force unbounded hashing.
    from app.accounts import _MAX_PASSWORD_BYTES

    base = "A" * _MAX_PASSWORD_BYTES
    salt, iters, digest = accounts.hash_password(base + "extra-tail")
    # A verify with the base (no tail) still succeeds — the tail was truncated.
    assert accounts.verify_password(base, salt, iters, digest) is True
    # And an entirely different password still fails.
    assert accounts.verify_password("B" * _MAX_PASSWORD_BYTES, salt, iters, digest) is False
