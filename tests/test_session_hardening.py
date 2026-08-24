"""Tests for session-cookie hardening: the Secure flag and cross-device revocation.

Covers the ``Secure`` attribute being set only on an HTTPS-fronted deployment,
and the /account "sign out all devices" control that revokes every other
outstanding cookie for the account while keeping the acting device signed in.
"""
import os
import tempfile

import httpx
from fastapi.testclient import TestClient

from app import accounts

from tests.test_auth_routes import (
    ENV,
    _csrf,
    make_client,
    register_admin,
)
from tests.test_auth_routes import (
    _handler as _kav_handler,
)


def _set_cookie_header(resp: httpx.Response) -> str:
    return resp.headers.get("set-cookie", "")


def test_session_cookie_not_secure_on_plain_http():
    with make_client() as client:
        r = register_admin(client)
        header = _set_cookie_header(r)
        assert accounts.SESSION_COOKIE in header
        assert "Secure" not in header  # plain-HTTP LAN default → no Secure


def test_session_cookie_secure_when_public_url_is_https():
    # Build a client whose config has an https BRIDGE_PUBLIC_URL.
    from contextlib import asynccontextmanager

    from app.config import load_config
    from app.ids import IdCodec
    from app.kavita import KavitaClient
    from app.main import FeedCache, create_app
    from app.store import Store

    env = dict(ENV)
    env["BRIDGE_PUBLIC_URL"] = "https://books.example.org"
    cfg = load_config(env)
    app = create_app(cfg)
    st = Store(os.path.join(tempfile.mkdtemp(), "state.json"))
    transport = httpx.MockTransport(_kav_handler)

    @asynccontextmanager
    async def ls(a):
        http = httpx.AsyncClient(transport=transport,
                                 timeout=httpx.Timeout(connect=5, read=None, write=None, pool=5))
        a.state.http = http
        a.state.kavita = KavitaClient(cfg, http)
        a.state.ids = IdCodec(cfg.bridge_id_secret)
        a.state.cache = FeedCache(cfg.cache_feeds_seconds)
        a.state.store = st
        a.state.session_secret = cfg.bridge_id_secret
        a.state.search_templates = {}
        try:
            yield
        finally:
            await http.aclose()

    app.router.lifespan_context = ls
    with TestClient(app) as client:
        client._store = st  # type: ignore[attr-defined]
        r = register_admin(client)
        header = _set_cookie_header(r)
        assert accounts.SESSION_COOKIE in header and "Secure" in header


def test_signout_all_revokes_other_devices_keeps_this_one():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        # Capture a SECOND device's live cookie (a copy an attacker might hold).
        stolen = client.cookies.get(accounts.SESSION_COOKIE)
        # This device signs out everywhere.
        token = _csrf(client.get("/account").text)
        r = client.post("/account", data={"_csrf": token, "action": "signout_all"},
                        follow_redirects=False)
        assert r.status_code == 200 and "all other devices" in r.text
        # This device stays signed in (its cookie was re-minted on the response).
        assert client.get("/", follow_redirects=False).status_code == 200
        # The stolen/old cookie no longer authenticates → bounced to the gate.
        other = TestClient(client.app)
        other.cookies.set(accounts.SESSION_COOKIE, stolen)
        assert other.get("/", follow_redirects=False).headers["location"] == "/login"
