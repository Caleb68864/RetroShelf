"""Admin dashboard (deferred item, built 2026-11-21): plain HTML tables and
forms. Admin-only; clearing a cache never touches reading state."""
import os

from tests.test_auth_routes import _csrf, _make_second_account, make_client, register_admin
from tests.test_old_ipad_guard import _html_violations


def test_dashboard_shows_accounts_storage_and_settings_to_an_admin():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        assert "/admin" in client.get("/account").text
        page = client.get("/admin")
        assert page.status_code == 200
        for text in ("admin", "bob", "Shelved books", "Cached covers", "Reader image size",
                     "Per-device positions", "Version"):
            assert text in page.text
        assert _html_violations(page.text) == []
        assert "SECRET" not in page.text and "hunter2000" not in page.text


def test_dashboard_is_refused_to_non_admins_and_off_without_accounts():
    from tests.test_account_authz import _sign_in
    from tests.test_auth_routes import _logout_csrf
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        _make_second_account(client, "bob", "passwordb")
        client.post("/logout", data={"_csrf": _logout_csrf(client)})
        assert client.get("/admin", follow_redirects=False).status_code == 303  # login
        _sign_in(client, "bob", "passwordb")
        assert client.get("/admin").status_code == 403
        assert client.post("/admin", data={"action": "clear_reader_cache"}).status_code in (403, 400)
    from tests.test_app import make_client as plain_client
    from tests.test_app import make_handler
    with plain_client(make_handler()) as client:
        assert client.get("/admin").status_code == 404


def test_clearing_the_reader_cache_keeps_in_flight_builds_and_needs_confirmation(tmp_path):
    with make_client(extra_env={"CACHE_DIR": str(tmp_path)}) as client:
        register_admin(client, "admin", "hunter2000")
        reader_dir = os.path.join(client.app.state.config.cache_dir, "reader")
        for name in ("bookA", "bookB.tmp-1", ".spool-1-x"):
            os.makedirs(os.path.join(reader_dir, name), exist_ok=True)
        token = _csrf(client.get("/admin").text)
        r = client.post("/admin", data={"_csrf": token, "action": "clear_reader_cache"})
        assert r.status_code == 400 and os.path.isdir(os.path.join(reader_dir, "bookA"))
        r = client.post("/admin", data={"_csrf": _csrf(r.text), "action": "clear_reader_cache",
                                        "confirm": "yes"})
        assert r.status_code == 200 and "Cleared the reader cache (1 entries)" in r.text
        assert sorted(os.listdir(reader_dir)) == [".spool-1-x", "bookB.tmp-1"]
        bad = client.post("/admin", data={"action": "clear_reader_cache", "confirm": "yes"})
        assert bad.status_code == 403  # no CSRF token
