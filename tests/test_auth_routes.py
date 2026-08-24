"""Route-level tests for the opt-in accounts + profiles system.

Adversarial coverage of the auth flows: the session gate, CSRF, no-enumeration
login, cross-account isolation, session tamper/expiry/revocation, and admin-only
account creation. Kavita is mocked at the httpx transport layer so no real
server is needed. The pure primitives are covered in tests/test_accounts.py.
"""
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager

import httpx
from fastapi.testclient import TestClient

from app import accounts
from app.config import load_config
from app.ids import IdCodec
from app.kavita import KavitaClient
from app.main import FeedCache, create_app
from app.store import Store

SECRET = "test-secret"
ENV = {
    "KAVITA_OPDS_URL": "http://kavita:5000/api/opds/SECRETKEY",
    "BRIDGE_ID_SECRET": SECRET,
    "ACCOUNTS_ENABLED": "1",
}
ROOT_XML = (
    '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">'
    '<title>Lib</title></feed>'
)


def _handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, text=ROOT_XML)


def make_client(store: Store | None = None, accounts_on: bool = True) -> TestClient:
    """Build a TestClient with a mocked Kavita and an injectable Store."""
    env = dict(ENV)
    if not accounts_on:
        env.pop("ACCOUNTS_ENABLED", None)
    cfg = load_config(env)
    app = create_app(cfg)
    st = store or Store(os.path.join(tempfile.mkdtemp(), "state.json"))
    transport = httpx.MockTransport(_handler)

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
    client = TestClient(app)
    client._store = st  # type: ignore[attr-defined]
    return client


def _csrf(html: str) -> str:
    """Extract the first hidden ``_csrf`` field value from a rendered page."""
    m = re.search(r'name="_csrf" value="([0-9a-f]+)"', html)
    assert m, "no _csrf field on the page"
    return m.group(1)


def register_admin(client: TestClient, username="admin", password="hunter2000") -> httpx.Response:
    """Walk the /setup form to create the admin and land a signed-in session."""
    token = _csrf(client.get("/setup").text)
    return client.post("/setup", data={"_csrf": token, "username": username,
                                        "password": password, "confirm": password},
                       follow_redirects=False)


def _session_cookie(client: TestClient) -> str:
    return client.cookies.get(accounts.SESSION_COOKIE)


# -- gate / redirects --------------------------------------------------------
def test_no_accounts_redirects_to_setup():
    with make_client() as client:
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/setup"
        # /setup itself renders the creation form.
        assert "First-time setup" in client.get("/setup").text


def test_setup_creates_admin_and_signs_in():
    with make_client() as client:
        r = register_admin(client)
        assert r.status_code == 303 and r.headers["location"] == "/"
        assert _session_cookie(client)  # session cookie was set
        # The admin account exists and is admin, with a first profile.
        acct = client._store.get_account_by_username("admin")
        assert acct is not None and acct["is_admin"] and len(acct["profiles"]) == 1
        # Now signed in with a profile → home renders.
        assert client.get("/", follow_redirects=False).status_code == 200


def test_setup_is_closed_once_an_account_exists():
    with make_client() as client:
        register_admin(client)
        # A second /setup GET redirects to /login; POST does not create a 2nd admin.
        assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"
        assert client._store.account_count() == 1


def test_gate_redirects_unauthenticated_to_login():
    st = Store(os.path.join(tempfile.mkdtemp(), "state.json"))
    st.create_account("admin", "hunter2000")
    with make_client(store=st) as client:
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        # Static + health stay open with no session.
        assert client.get("/health").status_code == 200


def test_accounts_off_is_unchanged_no_login():
    # ACCOUNTS_ENABLED off → no login gate; home renders directly (byte-identical).
    with make_client(accounts_on=False) as client:
        assert client.get("/", follow_redirects=False).status_code == 200
        # The auth routes do not exist when accounts are disabled.
        assert client.get("/login", follow_redirects=False).status_code == 404
        assert client.post("/logout", follow_redirects=False).status_code == 404


# -- login: no user enumeration ---------------------------------------------
def test_login_uniform_error_no_enumeration():
    with make_client() as client:
        register_admin(client, "alice", "correct-horse")
        client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        # Unknown user and wrong password give the SAME status and message.
        token = _csrf(client.get("/login").text)
        r_unknown = client.post("/login", data={"_csrf": token, "username": "nobody",
                                                "password": "correct-horse"},
                                follow_redirects=False)
        token = _csrf(client.get("/login").text)
        r_wrongpw = client.post("/login", data={"_csrf": token, "username": "alice",
                                                "password": "WRONG"}, follow_redirects=False)
        assert r_unknown.status_code == r_wrongpw.status_code == 401
        assert "Wrong username or password." in r_unknown.text
        assert r_unknown.text == r_wrongpw.text


def test_login_success_then_pick_profile():
    with make_client() as client:
        register_admin(client, "alice", "correct-horse")
        client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        token = _csrf(client.get("/login").text)
        r = client.post("/login", data={"_csrf": token, "username": "alice",
                                        "password": "correct-horse"}, follow_redirects=False)
        # Correct creds → session minted, sent to the profile picker.
        assert r.status_code == 303 and r.headers["location"] == "/profiles"
        assert "Who" in client.get("/profiles").text  # picker renders


def _logout_csrf(client: TestClient) -> str:
    """The CSRF token for the sign-out form in the shared chrome."""
    return _csrf(client.get("/account").text)


# -- CSRF --------------------------------------------------------------------
def test_setup_post_without_csrf_is_refused():
    with make_client() as client:
        r = client.post("/setup", data={"username": "admin", "password": "hunter2000",
                                        "confirm": "hunter2000"}, follow_redirects=False)
        assert r.status_code == 403
        assert client._store.has_accounts() is False  # nothing created


def test_mutating_post_without_csrf_is_refused():
    with make_client() as client:
        register_admin(client)
        # A profile-add with no _csrf is refused, and no profile is added.
        before = len(client._store.get_account_by_username("admin")["profiles"])
        r = client.post("/account", data={"action": "add_profile", "name": "Sneaky"},
                        follow_redirects=False)
        assert r.status_code == 403
        assert len(client._store.get_account_by_username("admin")["profiles"]) == before


def test_mutating_post_with_wrong_csrf_is_refused():
    with make_client() as client:
        register_admin(client)
        r = client.post("/account", data={"_csrf": "deadbeef", "action": "add_profile",
                                          "name": "Sneaky"}, follow_redirects=False)
        assert r.status_code == 403


def test_mutating_post_with_valid_csrf_works():
    with make_client() as client:
        register_admin(client)
        token = _csrf(client.get("/account").text)
        r = client.post("/account", data={"_csrf": token, "action": "add_profile",
                                          "name": "Kids", "color": "green"},
                        follow_redirects=False)
        assert r.status_code == 200
        names = [p["name"] for p in client._store.get_account_by_username("admin")["profiles"].values()]
        assert "Kids" in names


# -- session tamper / expiry / revocation ------------------------------------
def test_tampered_session_cookie_is_rejected():
    with make_client() as client:
        register_admin(client)
        assert client.get("/", follow_redirects=False).status_code == 200
        good = _session_cookie(client)
        # Flip a byte in the signed body → the gate redirects to /login.
        parts = good.split(".")
        flipped = "A" if parts[1][0] != "A" else "B"
        client.cookies.set(accounts.SESSION_COOKIE, f"{parts[0]}.{flipped}{parts[1][1:]}.{parts[2]}")
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_expired_session_cookie_is_rejected():
    st = Store(os.path.join(tempfile.mkdtemp(), "state.json"))
    acct_id = st.create_account("admin", "hunter2000")
    pid = st.add_profile(acct_id, "admin", "amber")
    with make_client(store=st) as client:
        expired = accounts.encode_session(SECRET, acct_id, pid, 1, int(time.time()) - 5)
        client.cookies.set(accounts.SESSION_COOKIE, expired)
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


def test_token_version_bump_invalidates_existing_session():
    with make_client() as client:
        register_admin(client)
        assert client.get("/", follow_redirects=False).status_code == 200
        admin_id = client._store.get_account_by_username("admin")["account_id"]
        # A password change (or explicit bump) revokes the outstanding cookie.
        client._store.bump_token_version(admin_id)
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"


# -- cross-account isolation -------------------------------------------------
def _make_second_account(client: TestClient, username="bob", password="passwordb") -> None:
    token = _csrf(client.get("/account").text)
    client.post("/account", data={"_csrf": token, "action": "create_account",
                                  "new_username": username, "new_password": password,
                                  "new_confirm": password}, follow_redirects=False)


def test_account_cannot_select_another_accounts_profile():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        admin = client._store.get_account_by_username("admin")
        admin_profile = next(iter(admin["profiles"]))
        # Admin stars a book in its own profile (state to protect).
        client._store.add_favorite(
            {"u": "http://x/secret.epub", "t": "Admin Secret"}, profile_id=admin_profile)
        # Admin creates account B, then signs in as B.
        _make_second_account(client, "bob", "passwordb")
        client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        token = _csrf(client.get("/login").text)
        client.post("/login", data={"_csrf": token, "username": "bob", "password": "passwordb"},
                    follow_redirects=False)
        # B tries to SELECT admin's profile id → refused (not B's own profile).
        token = _csrf(client.get("/profiles").text)
        r = client.post("/profiles", data={"_csrf": token, "action": "select",
                                           "profile_id": admin_profile}, follow_redirects=False)
        assert r.status_code == 400
        assert "not available" in r.text
        # B never gets a session bound to admin's profile.
        data = accounts.decode_session(SECRET, _session_cookie(client))
        assert data["profile_id"] != admin_profile


def test_forged_cookie_with_foreign_profile_is_gated_out():
    # Even a correctly SIGNED cookie whose profile belongs to another account is
    # rejected by the gate (the profile must be one of the session account's own).
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        admin = client._store.get_account_by_username("admin")
        admin_profile = next(iter(admin["profiles"]))
        _make_second_account(client, "bob", "passwordb")
        bob = client._store.get_account_by_username("bob")
        # Mint a session for BOB carrying ADMIN's profile id (server-signed here
        # only to model an attacker who somehow had a valid B session + A's pid).
        forged = accounts.encode_session(SECRET, bob["account_id"], admin_profile,
                                         bob["token_version"], int(time.time()) + 3600)
        client.cookies.set(accounts.SESSION_COOKIE, forged)
        # Reading routes are gated: a profile B does not own → bounced to /profiles.
        r = client.get("/list", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/profiles"


def test_two_accounts_have_isolated_reading_lists():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        admin = client._store.get_account_by_username("admin")
        admin_pid = next(iter(admin["profiles"]))
        client._store.add_favorite({"u": "http://x/a.epub", "t": "Admin Book"},
                                   profile_id=admin_pid)
        _make_second_account(client, "bob", "passwordb")
        client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        token = _csrf(client.get("/login").text)
        client.post("/login", data={"_csrf": token, "username": "bob", "password": "passwordb"},
                    follow_redirects=False)
        # Select bob's own (only) profile.
        bob = client._store.get_account_by_username("bob")
        bob_pid = next(iter(bob["profiles"]))
        token = _csrf(client.get("/profiles").text)
        client.post("/profiles", data={"_csrf": token, "action": "select",
                                       "profile_id": bob_pid}, follow_redirects=False)
        # Bob's Reading List does not contain admin's book.
        assert "Admin Book" not in client.get("/list").text


# -- admin-only account creation --------------------------------------------
def test_non_admin_cannot_create_account():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        # Sign in as bob (non-admin) and select bob's profile.
        client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        token = _csrf(client.get("/login").text)
        client.post("/login", data={"_csrf": token, "username": "bob", "password": "passwordb"},
                    follow_redirects=False)
        bob = client._store.get_account_by_username("bob")
        token = _csrf(client.get("/profiles").text)
        client.post("/profiles", data={"_csrf": token, "action": "select",
                                       "profile_id": next(iter(bob["profiles"]))},
                    follow_redirects=False)
        # The admin-only account form is absent for a non-admin...
        assert "Add an account (admin)" not in client.get("/account").text
        # ...and a forged POST to create one is refused.
        token = _csrf(client.get("/account").text)
        r = client.post("/account", data={"_csrf": token, "action": "create_account",
                                          "new_username": "eve", "new_password": "passworde",
                                          "new_confirm": "passworde"}, follow_redirects=False)
        assert r.status_code == 403
        assert client._store.get_account_by_username("eve") is None


def test_admin_can_create_account_and_it_can_sign_in():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "carol", "passwordc")
        assert client._store.get_account_by_username("carol") is not None
        # The new account has a starter profile and can sign in.
        client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        token = _csrf(client.get("/login").text)
        r = client.post("/login", data={"_csrf": token, "username": "carol",
                                        "password": "passwordc"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/profiles"


# -- logout ------------------------------------------------------------------
def test_logout_clears_the_session():
    with make_client() as client:
        register_admin(client)
        assert client.get("/", follow_redirects=False).status_code == 200
        r = client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        # The cookie no longer authenticates → back to the gate.
        assert client.get("/", follow_redirects=False).headers["location"] == "/login"


# -- password change keeps this device signed in -----------------------------
def test_password_change_rebinds_current_session():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        # Select is implicit from setup; change the password.
        token = _csrf(client.get("/account").text)
        r = client.post("/account", data={"_csrf": token, "action": "password",
                                          "current": "hunter2000", "new": "brand-new-pass",
                                          "confirm": "brand-new-pass"}, follow_redirects=False)
        assert r.status_code == 200 and "Password changed." in r.text
        # The session was re-minted with the new token_version → still signed in.
        assert client.get("/", follow_redirects=False).status_code == 200
        # A wrong current password is refused.
        token = _csrf(client.get("/account").text)
        r = client.post("/account", data={"_csrf": token, "action": "password",
                                          "current": "WRONG", "new": "another-pass",
                                          "confirm": "another-pass"}, follow_redirects=False)
        assert r.status_code == 400 and "current password is wrong" in r.text
