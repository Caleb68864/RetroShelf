"""Adversarial authorization tests for GET/POST /account.

Complements tests/test_auth_routes.py: proves a non-admin cannot reach across
into another account's profiles, an admin cannot delete an account into a
profile-less (unreachable) state, and the password policy bounds are enforced
on every write path. Authorization is taken from the signed session only —
never a form field — so these forge the fields an attacker would.
"""
from tests.test_auth_routes import (
    _csrf,
    _logout_csrf,
    _make_second_account,
    make_client,
    register_admin,
)


def _sign_in(client, username: str, password: str) -> None:
    """Sign *username* in and select their first profile."""
    token = _csrf(client.get("/login").text)
    client.post("/login", data={"_csrf": token, "username": username,
                                "password": password}, follow_redirects=False)
    acct = client._store.get_account_by_username(username)
    pid = next(iter(acct["profiles"]))
    token = _csrf(client.get("/profiles").text)
    client.post("/profiles", data={"_csrf": token, "action": "select",
                                   "profile_id": pid}, follow_redirects=False)


def test_non_admin_cannot_rename_another_accounts_profile():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        admin = client._store.get_account_by_username("admin")
        admin_pid = next(iter(admin["profiles"]))
        admin_name = admin["profiles"][admin_pid]["name"]
        _make_second_account(client, "bob", "passwordb")
        client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        _sign_in(client, "bob", "passwordb")
        # Bob forges admin's profile id into a rename → scoped out by account.
        token = _csrf(client.get("/account").text)
        r = client.post("/account", data={"_csrf": token, "action": "rename_profile",
                                          "profile_id": admin_pid, "name": "PWNED"},
                        follow_redirects=False)
        assert r.status_code == 400 and "not available" in r.text
        # Admin's profile name is untouched.
        again = client._store.get_account_by_username("admin")
        assert again["profiles"][admin_pid]["name"] == admin_name


def test_non_admin_cannot_delete_another_accounts_profile():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        admin = client._store.get_account_by_username("admin")
        admin_pid = next(iter(admin["profiles"]))
        _make_second_account(client, "bob", "passwordb")
        client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        _sign_in(client, "bob", "passwordb")
        token = _csrf(client.get("/account").text)
        r = client.post("/account", data={"_csrf": token, "action": "delete_profile",
                                          "profile_id": admin_pid}, follow_redirects=False)
        assert r.status_code == 400 and "not available" in r.text
        # Admin's profile still exists.
        again = client._store.get_account_by_username("admin")
        assert admin_pid in again["profiles"]


def test_cannot_delete_the_accounts_last_profile():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        admin = client._store.get_account_by_username("admin")
        only_pid = next(iter(admin["profiles"]))
        assert len(admin["profiles"]) == 1
        # The Delete button is hidden for the last profile...
        assert client.get("/account").text.count("action\" value=\"delete_profile") == 0
        # ...and a forged delete POST is refused, keeping the account reachable.
        token = _csrf(client.get("/account").text)
        r = client.post("/account", data={"_csrf": token, "action": "delete_profile",
                                          "profile_id": only_pid}, follow_redirects=False)
        assert r.status_code == 400 and "at least one profile" in r.text
        again = client._store.get_account_by_username("admin")
        assert only_pid in again["profiles"]


def test_setup_rejects_password_below_minimum():
    with make_client() as client:
        token = _csrf(client.get("/setup").text)
        r = client.post("/setup", data={"_csrf": token, "username": "admin",
                                        "password": "abc", "confirm": "abc"},
                        follow_redirects=False)
        assert r.status_code == 400 and "at least" in r.text
        assert client._store.account_count() == 0


def test_password_change_rejects_below_minimum_and_over_maximum():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        # Too short.
        token = _csrf(client.get("/account").text)
        r = client.post("/account", data={"_csrf": token, "action": "password",
                                          "current": "hunter2000", "new": "x",
                                          "confirm": "x"}, follow_redirects=False)
        assert r.status_code == 400 and "at least" in r.text
        # Too long (over the 256 cap) — rejected before any expensive re-hash.
        token = _csrf(client.get("/account").text)
        big = "z" * 300
        r = client.post("/account", data={"_csrf": token, "action": "password",
                                          "current": "hunter2000", "new": big,
                                          "confirm": big}, follow_redirects=False)
        assert r.status_code == 400 and "at most" in r.text
        # The original password still works (nothing was changed).
        assert client._store.verify_login("admin", "hunter2000") is not None


def test_admin_create_account_rejects_over_long_password():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        token = _csrf(client.get("/account").text)
        big = "q" * 300
        r = client.post("/account", data={"_csrf": token, "action": "create_account",
                                          "new_username": "eve", "new_password": big,
                                          "new_confirm": big}, follow_redirects=False)
        assert r.status_code == 400 and "at most" in r.text
        assert client._store.get_account_by_username("eve") is None
