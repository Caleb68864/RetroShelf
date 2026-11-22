"""Opt-in per-profile PIN (deferred item, built 2026-11-21): hashed, never
stored or shown in the clear, rate-limited, plain HTML forms."""

import pytest

from app.store import Store
from tests.test_auth_routes import _csrf, make_client, register_admin
from tests.test_old_ipad_guard import _html_violations


def test_store_hashes_the_pin_and_never_exposes_it(tmp_path):
    path = tmp_path / "s.json"
    st = Store(str(path))
    acct = st.create_account("ann", "password1")
    pid = st.add_profile(acct, "Ann")
    for bad in ("12", "123456789", "12a4", ""):
        with pytest.raises(ValueError):
            st.set_pin(acct, pid, bad)
    assert st.set_pin(acct, pid, "4821")
    raw = path.read_text()
    assert "4821" not in raw and '"pin"' in raw
    reloaded = Store(str(path))
    assert reloaded.verify_pin(acct, pid, "4821") and not reloaded.verify_pin(acct, pid, "4822")
    view = reloaded.get_account(acct)["profiles"][pid]
    assert view["has_pin"] is True and "pin" not in view
    assert reloaded.clear_pin(acct, pid) and not reloaded.verify_pin(acct, pid, "4821")


def _profile_ids(client):
    acct = client._store.get_account_by_username("admin")
    return list(acct["profiles"])


def _set_pin(client, pid, pin, current=""):
    token = _csrf(client.get("/account").text)
    return client.post("/account", data={"_csrf": token, "action": "set_pin", "profile_id": pid,
                                         "pin": pin, "confirm": pin, "current": current})


def _select(client, pid, pin=None):
    token = _csrf(client.get("/profiles").text)
    data = {"_csrf": token, "action": "select", "profile_id": pid}
    if pin is not None:
        data["pin"] = pin
    return client.post("/profiles", data=data, follow_redirects=False)


def test_a_pinned_profile_needs_its_pin_to_select():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        pid = _profile_ids(client)[0]
        assert "PIN set." in _set_pin(client, pid, "4821").text
        page = client.get("/profiles").text
        assert 'name="pin"' in page and _html_violations(page) == []
        assert _select(client, pid).status_code == 400
        r = _select(client, pid, "0000")
        assert r.status_code == 400 and "That PIN is wrong." in r.text
        assert _select(client, pid, "4821").status_code == 303


def test_pin_attempts_are_rate_limited():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        pid = _profile_ids(client)[0]
        _set_pin(client, pid, "4821")
        for _ in range(5):
            _select(client, pid, "1111")
        r = _select(client, pid, "4821")  # right PIN, but locked out now
        assert r.status_code == 400 and "Too many wrong tries" in r.text


def test_changing_removing_or_deleting_needs_the_pin_or_password():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        token = _csrf(client.get("/account").text)
        client.post("/account", data={"_csrf": token, "action": "add_profile", "name": "Kid"})
        pid = _profile_ids(client)[0]
        _set_pin(client, pid, "4821")
        assert "That PIN is wrong." in _set_pin(client, pid, "1234", current="").text
        assert "PIN set." in _set_pin(client, pid, "1234", current="4821").text
        token = _csrf(client.get("/account").text)
        r = client.post("/account", data={"_csrf": token, "action": "delete_profile",
                                          "profile_id": pid})
        assert r.status_code == 400 and pid in _profile_ids(client)
        token = _csrf(client.get("/account").text)
        r = client.post("/account", data={"_csrf": token, "action": "clear_pin",
                                          "profile_id": pid, "current": "hunter2000"})
        assert "PIN removed." in r.text  # forgotten PIN: the account password works
        assert _select(client, pid).status_code == 303
