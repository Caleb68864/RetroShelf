"""Gate tests: the accounts-open set is matched exactly, not by prefix.

A path that merely *starts with* ``/login`` or ``/setup`` (e.g. a future
``/login-history`` route, or a probe) must NOT be treated as an open,
unauthenticated route — only the exact ``/login`` and ``/setup`` leaves are.
"""
import os
import tempfile

from app.store import Store

from tests.test_auth_routes import make_client


def _store_with_admin() -> Store:
    st = Store(os.path.join(tempfile.mkdtemp(), "state.json"))
    st.create_account("admin", "hunter2000")
    return st


def test_login_prefixed_path_is_not_open():
    with make_client(store=_store_with_admin()) as client:
        # Unauthenticated: the exact gate leaves render/redirect openly...
        assert client.get("/login", follow_redirects=False).status_code == 200
        # ...but a merely login-PREFIXED path is gated like anything else and is
        # bounced to /login rather than served without a session.
        r = client.get("/login-history", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        r = client.get("/setupx", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
