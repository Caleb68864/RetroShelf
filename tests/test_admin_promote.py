"""Admin promotes/demotes accounts (roadmap A3); at least one admin always remains."""
import pytest

from app.store import Store
from tests.test_account_authz import _sign_in
from tests.test_auth_routes import (
    _csrf,
    _logout_csrf,
    _make_second_account,
    make_client,
    register_admin,
)


def _set_admin(client, target_id: str, make: str, token=None):
    token = token if token is not None else _csrf(client.get("/account").text)
    return client.post("/account", data={"_csrf": token, "action": "set_admin",
                                         "target_account_id": target_id,
                                         "make_admin": make}, follow_redirects=False)


def _is_admin(client, username: str) -> bool:
    return client._store.get_account_by_username(username)["is_admin"]


def test_store_refuses_to_demote_the_last_admin(tmp_path):
    st = Store(str(tmp_path / "state.json"))
    admin = st.create_account("admin", "hunter2000")
    bob = st.create_account("bob", "passwordb")
    with pytest.raises(ValueError):
        st.set_admin(admin, False)
    assert st.get_account(admin)["is_admin"] is True
    assert st.set_admin(bob, True) is True
    assert st.set_admin(admin, False) is True       # bob still admin
    with pytest.raises(ValueError):
        st.set_admin(bob, False)                    # now bob is the last one
    assert st.set_admin("missing", True) is False
    assert Store(str(tmp_path / "state.json")).get_account(bob)["is_admin"] is True


def test_admin_promotes_and_demotes_another_account():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        bob_id = client._store.get_account_by_username("bob")["account_id"]
        r = _set_admin(client, bob_id, "yes")
        assert r.status_code == 200 and "is now an admin" in r.text
        assert _is_admin(client, "bob")
        # A promoted admin can't be deleted until it is demoted.
        assert "Remove admin" in client.get("/account").text
        r = _set_admin(client, bob_id, "no")
        assert r.status_code == 200 and not _is_admin(client, "bob")


def test_last_admin_cannot_step_down_via_the_route():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        admin_id = client._store.get_account_by_username("admin")["account_id"]
        assert "Step down as admin" not in client.get("/account").text
        r = _set_admin(client, admin_id, "no")
        assert r.status_code == 400 and "At least one admin" in r.text
        assert _is_admin(client, "admin")


def test_admin_can_step_down_when_another_admin_exists():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        bob_id = client._store.get_account_by_username("bob")["account_id"]
        admin_id = client._store.get_account_by_username("admin")["account_id"]
        _set_admin(client, bob_id, "yes")
        assert "Step down as admin" in client.get("/account").text
        r = _set_admin(client, admin_id, "no")
        assert r.status_code == 303 and r.headers["location"] == "/account"
        assert not _is_admin(client, "admin")
        # The admin-only sections are gone for the demoted account.
        page = client.get("/account").text
        assert "create_account" not in page and "set_admin" not in page


def test_non_admin_cannot_promote_itself_or_demote_the_admin():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        bob_id = client._store.get_account_by_username("bob")["account_id"]
        admin_id = client._store.get_account_by_username("admin")["account_id"]
        client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        _sign_in(client, "bob", "passwordb")
        assert _set_admin(client, bob_id, "yes").status_code == 403
        assert _set_admin(client, admin_id, "no").status_code == 403
        assert not _is_admin(client, "bob") and _is_admin(client, "admin")


def test_set_admin_rejects_bad_input_and_missing_csrf():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        bob_id = client._store.get_account_by_username("bob")["account_id"]
        assert _set_admin(client, bob_id, "maybe").status_code == 400
        assert _set_admin(client, "nope-not-an-id", "yes").status_code == 400
        r = _set_admin(client, bob_id, "yes", token="0" * 64)
        assert r.status_code in (400, 403)
        assert not _is_admin(client, "bob")
