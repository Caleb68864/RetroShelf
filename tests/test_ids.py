"""Tests for app.ids — opaque authenticated bridge-id codec."""
import pytest

from app.errors import BadIdError
from app.ids import IdCodec

URL = "http://kavita:5000/api/opds/SECRETKEY/series/1/volume/1/chapter/1/download/book.epub"


def test_roundtrip():
    c = IdCodec("stable-secret")
    token = c.encode(URL)
    assert c.decode(token) == URL


def test_token_does_not_leak_api_key():
    c = IdCodec("stable-secret")
    token = c.encode(URL)
    assert "SECRETKEY" not in token
    # base64-decoding the token must not reveal the key either (it's encrypted).
    import base64
    # v2 tokens are "<version>.<body>.<mac>"; the ciphertext is the middle part.
    body = token.split(".")[-2]
    raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
    assert b"SECRETKEY" not in raw
    assert b"api/opds" not in raw


def test_tampered_token_rejected():
    c = IdCodec("stable-secret")
    token = c.encode(URL)
    body, mac = token.split(".", 1)
    tampered = body[:-2] + ("AA" if body[-2:] != "AA" else "BB") + "." + mac
    with pytest.raises(BadIdError):
        c.decode(tampered)


def test_wrong_secret_rejected():
    a = IdCodec("secret-a")
    b = IdCodec("secret-b")
    token = a.encode(URL)
    with pytest.raises(BadIdError):
        b.decode(token)


def test_garbage_rejected():
    c = IdCodec("s")
    for bad in ["", "nodot", "a.b", "!!!.???", "."]:
        with pytest.raises(BadIdError):
            c.decode(bad)


def test_stable_secret_survives_new_instance():
    token = IdCodec("persist").encode(URL)
    # A fresh codec with the same secret (e.g. after restart) still decodes.
    assert IdCodec("persist").decode(token) == URL


def test_random_secret_when_none():
    c = IdCodec(None)
    assert c.decode(c.encode(URL)) == URL


def test_token_ok_non_ascii_does_not_raise():
    # Regression: the site token comparison must tolerate a non-ASCII ``t=``
    # query parameter. hmac.compare_digest raises TypeError on non-ASCII str, so
    # "?t=café" would otherwise 500 instead of cleanly failing the check.
    codec = IdCodec("stable-secret")
    assert codec.token_ok("café") is False
    assert codec.token_ok("🔓" * 8) is False
    # The genuine token still validates.
    assert codec.token_ok(codec.site_token) is True
