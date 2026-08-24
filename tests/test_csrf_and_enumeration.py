"""CSRF coverage sweep and cross-account enumeration-equivalence tests.

Pass 8: every mutating POST — login, logout, profile switch/add, and account
ops — refuses a request with no CSRF token, not just the two already covered in
test_auth_routes.py.

Pass 7: selecting a profile that belongs to *another* account is indistinguishable
from selecting one that does not exist at all, so the isolation boundary leaks no
information about other accounts' profile ids.
"""
from tests.test_auth_routes import (
    _csrf,
    _logout_csrf,
    _make_second_account,
    make_client,
    register_admin,
)


def test_every_mutating_post_refuses_missing_csrf():
    with make_client() as client:
        # /setup before any account exists.
        assert client.post("/setup", data={"username": "admin", "password": "hunter2000",
                                            "confirm": "hunter2000"},
                           follow_redirects=False).status_code == 403
        register_admin(client, "admin", "hunter2000")
        # /account (signed in).
        assert client.post("/account", data={"action": "add_profile", "name": "x"},
                           follow_redirects=False).status_code == 403
        # /profiles (signed in).
        assert client.post("/profiles", data={"action": "add", "name": "x"},
                           follow_redirects=False).status_code == 403
        # /logout (signed in).
        assert client.post("/logout", data={}, follow_redirects=False).status_code == 403
        # /login (sign out first so the form is the gate again).
        client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        assert client.post("/login", data={"username": "admin", "password": "hunter2000"},
                           follow_redirects=False).status_code == 403


def test_foreign_profile_and_nonexistent_profile_are_indistinguishable():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        admin = client._store.get_account_by_username("admin")
        admin_pid = next(iter(admin["profiles"]))
        _make_second_account(client, "bob", "passwordb")
        client.post("/logout", data={"_csrf": _logout_csrf(client)}, follow_redirects=False)
        token = _csrf(client.get("/login").text)
        client.post("/login", data={"_csrf": token, "username": "bob", "password": "passwordb"},
                    follow_redirects=False)
        # Selecting ADMIN's real profile id and a wholly made-up id both fail the
        # same way — an attacker cannot use the response to confirm a real id.
        token = _csrf(client.get("/profiles").text)
        r_foreign = client.post("/profiles", data={"_csrf": token, "action": "select",
                                                   "profile_id": admin_pid},
                                follow_redirects=False)
        token = _csrf(client.get("/profiles").text)
        r_bogus = client.post("/profiles", data={"_csrf": token, "action": "select",
                                                 "profile_id": "ffffffffffffffff"},
                              follow_redirects=False)
        assert r_foreign.status_code == r_bogus.status_code == 400
        assert r_foreign.text == r_bogus.text
