"""Admin account management: list other accounts, reset a forgotten password.

There is no email reset, so the admin resetting a password is the only way back
in for a user who forgot theirs. These tests check that only the admin can do it,
that the admin cannot use it on itself to skip the current-password check, that
it revokes the target's sessions, and that the password policy and CSRF apply.
"""
from app import accounts
from app.store import Store
from tests.test_account_authz import _sign_in
from tests.test_auth_routes import (
    _csrf,
    _logout_csrf,
    _make_second_account,
    make_client,
    register_admin,
)


def _reset(client, target_id: str, pw: str, confirm: str | None = None, token=None):
    token = token if token is not None else _csrf(client.get("/account").text)
    return client.post("/account", data={
        "_csrf": token, "action": "reset_password", "target_account_id": target_id,
        "reset_password": pw, "reset_confirm": pw if confirm is None else confirm,
    }, follow_redirects=False)


def _login_ok(client, username: str, password: str) -> bool:
    """Try a fresh login and report whether it produced a session."""
    client.cookies.clear()
    token = _csrf(client.get("/login").text)
    r = client.post("/login", data={"_csrf": token, "username": username,
                                    "password": password}, follow_redirects=False)
    return r.status_code == 303 and r.headers["location"] == "/profiles"


def test_store_list_accounts_is_sorted_and_has_no_secrets(tmp_path):
    st = Store(str(tmp_path / "state.json"))
    st.create_account("zed", "passwordz")
    st.create_account("Amy", "passworda")
    views = st.list_accounts()
    assert [v["username"] for v in views] == ["Amy", "zed"]
    for v in views:
        assert not {"pw_hash", "pw_salt", "iterations", "kdf"} & set(v)


def test_admin_sees_other_accounts_but_not_itself():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        page = client.get("/account").text
        assert "No other accounts yet." in page
        _make_second_account(client, "bob", "passwordb")
        page = client.get("/account").text
        bob = client._store.get_account_by_username("bob")
        admin = client._store.get_account_by_username("admin")
        assert f'name="target_account_id" value="{bob["account_id"]}"' in page
        assert f'name="target_account_id" value="{admin["account_id"]}"' not in page


def test_admin_reset_lets_user_back_in_and_revokes_old_sessions():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        bob_id = client._store.get_account_by_username("bob")["account_id"]
        # Bob signs in and his cookie is saved, standing in for a device he left signed in.
        client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        _sign_in(client, "bob", "passwordb")
        bob_cookie = client.cookies.get(accounts.SESSION_COOKIE)
        assert client.get("/account", follow_redirects=False).status_code == 200
        # Admin resets Bob's password.
        client.cookies.clear()
        _sign_in(client, "admin", "hunter2000")
        r = _reset(client, bob_id, "brandnew99")
        assert r.status_code == 200 and "Password reset for" in r.text
        assert "brandnew99" not in r.text           # never echoed back
        # The old Bob cookie no longer authenticates.
        client.cookies.clear()
        client.cookies.set(accounts.SESSION_COOKIE, bob_cookie)
        r = client.get("/account", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        # Old password fails, new one works.
        assert not _login_ok(client, "bob", "passwordb")
        assert _login_ok(client, "bob", "brandnew99")


def test_admin_cannot_reset_itself_through_the_admin_path():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        admin_id = client._store.get_account_by_username("admin")["account_id"]
        r = _reset(client, admin_id, "takeover99")
        assert r.status_code == 400 and "Change your password" in r.text
        assert _login_ok(client, "admin", "hunter2000")


def test_non_admin_cannot_reset_anyone():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        admin_id = client._store.get_account_by_username("admin")["account_id"]
        _make_second_account(client, "bob", "passwordb")
        client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        _sign_in(client, "bob", "passwordb")
        # No management section is rendered for a non-admin...
        assert "reset_password" not in client.get("/account").text
        # ...and a forged POST aimed at the admin is refused.
        r = _reset(client, admin_id, "takeover99")
        assert r.status_code == 403
        assert _login_ok(client, "admin", "hunter2000")


def test_reset_unknown_or_missing_target_is_refused():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        for target in ("", "nope-not-an-id"):
            r = _reset(client, target, "brandnew99")
            assert r.status_code == 400 and "not available" in r.text


def test_reset_enforces_password_policy():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        bob_id = client._store.get_account_by_username("bob")["account_id"]
        assert _reset(client, bob_id, "short").status_code == 400
        assert _reset(client, bob_id, "x" * 5000).status_code == 400
        assert _reset(client, bob_id, "brandnew99", confirm="different99").status_code == 400
        assert _login_ok(client, "bob", "passwordb")     # unchanged


def test_reset_requires_csrf():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        bob_id = client._store.get_account_by_username("bob")["account_id"]
        r = _reset(client, bob_id, "brandnew99", token="0" * 64)
        assert r.status_code in (400, 403)
        assert _login_ok(client, "bob", "passwordb")
