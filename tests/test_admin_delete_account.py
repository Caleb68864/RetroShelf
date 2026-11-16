"""Admin deletes an account (roadmap A2).

The delete is irreversible, so these tests check that only the admin can do it,
that the admin's own account and any admin account are refused, that it needs
an explicit confirm, and that it removes the account's per-profile reading
state and makes its sessions stop working.
"""
import pytest

from app import accounts
from app.store import Store
from tests.test_account_authz import _sign_in
from tests.test_admin_reset import _login_ok
from tests.test_auth_routes import (
    _csrf,
    _logout_csrf,
    _make_second_account,
    make_client,
    register_admin,
)


def _delete(client, target_id: str, confirm: str | None = "yes"):
    token = _csrf(client.get("/account").text)
    data = {"_csrf": token, "action": "delete_account", "target_account_id": target_id}
    if confirm is not None:
        data["confirm_delete"] = confirm
    return client.post("/account", data=data, follow_redirects=False)


def test_store_delete_account_removes_profile_state(tmp_path):
    st = Store(str(tmp_path / "state.json"))
    st.create_account("admin", "hunter2000")
    bob = st.create_account("bob", "passwordb")
    pid = st.add_profile(bob, "Bob")
    st.add_favorite({"u": "http://x/b.epub", "t": "Bob's"}, profile_id=pid)
    assert st.favorite_keys(pid)
    assert st.delete_account(bob) is True
    assert st.get_account(bob) is None
    assert st.favorite_keys(pid) == set()
    assert st.delete_account(bob) is False          # already gone
    # Persisted: a fresh load does not resurrect it.
    assert Store(str(tmp_path / "state.json")).get_account(bob) is None


def test_store_refuses_to_delete_an_admin(tmp_path):
    st = Store(str(tmp_path / "state.json"))
    admin = st.create_account("admin", "hunter2000")
    with pytest.raises(ValueError):
        st.delete_account(admin)
    assert st.get_account(admin) is not None


def test_admin_deletes_account_and_its_sessions_die():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        bob = client._store.get_account_by_username("bob")
        bob_pid = next(iter(bob["profiles"]))
        client._store.add_favorite({"u": "http://x/b.epub", "t": "B"}, profile_id=bob_pid)
        client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        _sign_in(client, "bob", "passwordb")
        bob_cookie = client.cookies.get(accounts.SESSION_COOKIE)
        client.cookies.clear()
        _sign_in(client, "admin", "hunter2000")
        r = _delete(client, bob["account_id"])
        assert r.status_code == 200 and "deleted" in r.text
        assert client._store.get_account(bob["account_id"]) is None
        assert client._store.favorite_keys(bob_pid) == set()
        # Bob's old cookie no longer authenticates, and he cannot log in.
        client.cookies.clear()
        client.cookies.set(accounts.SESSION_COOKIE, bob_cookie)
        r = client.get("/account", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert not _login_ok(client, "bob", "passwordb")


def test_delete_needs_explicit_confirm():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        bob_id = client._store.get_account_by_username("bob")["account_id"]
        for confirm in (None, "", "no"):
            r = _delete(client, bob_id, confirm=confirm)
            assert r.status_code == 400 and "confirm" in r.text
        assert client._store.get_account(bob_id) is not None


def test_admin_cannot_delete_itself():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        admin_id = client._store.get_account_by_username("admin")["account_id"]
        r = _delete(client, admin_id)
        assert r.status_code == 400
        assert client._store.get_account(admin_id) is not None


def test_non_admin_cannot_delete_anyone():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        _make_second_account(client, "carol", "passwordc")
        carol_id = client._store.get_account_by_username("carol")["account_id"]
        admin_id = client._store.get_account_by_username("admin")["account_id"]
        client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        _sign_in(client, "bob", "passwordb")
        assert "delete_account" not in client.get("/account").text
        for target in (carol_id, admin_id):
            assert _delete(client, target).status_code == 403
            assert client._store.get_account(target) is not None


def test_delete_unknown_target_is_refused():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        for target in ("", "nope-not-an-id"):
            r = _delete(client, target)
            assert r.status_code == 400 and "not available" in r.text


def test_deleted_username_can_be_reused():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        old_id = client._store.get_account_by_username("bob")["account_id"]
        assert _delete(client, old_id).status_code == 200
        _make_second_account(client, "bob", "passwordnew")
        new = client._store.get_account_by_username("bob")
        assert new is not None and new["account_id"] != old_id


def test_route_refuses_to_delete_another_admin():
    # The UI hides the delete form for admins; a forged POST must still fail.
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        bob_id = client._store.get_account_by_username("bob")["account_id"]
        client._store.set_admin(bob_id, True)
        r = _delete(client, bob_id)
        assert r.status_code == 400 and "admin account cannot be deleted" in r.text
        assert client._store.get_account(bob_id) is not None


def test_delete_clears_the_deleted_usernames_login_tarpit():
    # A tarpit built up against the deleted username must not carry over to a
    # new account re-created with the same name. Same path as the reset: the
    # username match is case-insensitive and the per-IP counter is untouched.
    from app.throttle import LoginThrottle
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        bob_id = client._store.get_account_by_username("bob")["account_id"]
        th = LoginThrottle(tarpit_free=0, tarpit_base=1.0, hard_max_ip=1000)
        client.app.state.throttle = th
        for _ in range(4):
            th.record_failure("BOB", "10.0.0.9")
        assert th.tarpit_delay("bob") > 0
        # A refused delete (no confirm) leaves the tarpit in place.
        assert _delete(client, bob_id, confirm=None).status_code == 400
        assert th.tarpit_delay("bob") > 0
        assert _delete(client, bob_id).status_code == 200
        assert th.tarpit_delay("bob") == 0.0
        assert th.tarpit_delay("Bob") == 0.0
        assert th._by_ip.get("10.0.0.9")
