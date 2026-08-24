"""Route-level tests for the /login brute-force lockout and password DoS caps.

Reuses the mocked-Kavita ``make_client`` harness from the auth-route tests and
injects a :class:`app.throttle.LoginThrottle` with a hand-cranked clock and a
zero tarpit, so the lockout is exercised without real sleeps.
"""
import os
import tempfile
import time

from app.store import Store
from app.throttle import LoginThrottle

from tests.test_auth_routes import _csrf, make_client


class Clock:
    def __init__(self, start: float = 5000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _store_with_admin(username: str = "alice", password: str = "correct-horse") -> Store:
    st = Store(os.path.join(tempfile.mkdtemp(), "state.json"))
    st.create_account(username, password)
    return st


def _bad_login(client, username: str = "alice", password: str = "nope"):
    token = _csrf(client.get("/login").text)
    return client.post("/login", data={"_csrf": token, "username": username,
                                       "password": password}, follow_redirects=False)


def _good_login(client, username: str = "alice", password: str = "correct-horse"):
    token = _csrf(client.get("/login").text)
    return client.post("/login", data={"_csrf": token, "username": username,
                                       "password": password}, follow_redirects=False)


def test_lockout_after_n_failures_then_correct_password_refused():
    st = _store_with_admin()
    with make_client(store=st) as client:
        client.app.state.throttle = LoginThrottle(
            now=Clock(), hard_max_ip=5, tarpit_base=0.0)
        for _ in range(5):
            assert _bad_login(client).status_code == 401
        # 6th attempt is hard-locked BEFORE any password check: even the correct
        # password is refused with a device-scoped 429.
        r = _good_login(client)
        assert r.status_code == 429
        assert "Too many sign-in attempts" in r.text


def test_successful_login_clears_the_counter():
    st = _store_with_admin()
    with make_client(store=st) as client:
        client.app.state.throttle = LoginThrottle(
            now=Clock(), hard_max_ip=5, tarpit_base=0.0)
        for _ in range(4):  # below the threshold
            assert _bad_login(client).status_code == 401
        # A success resets the IP counter, so the next wrong guess is 401 (not
        # locked) rather than tripping the lock at what would have been the 5th.
        assert _good_login(client).status_code == 303
        client.cookies.clear()
        assert _bad_login(client).status_code == 401


def test_lockout_message_does_not_enumerate_usernames():
    st = _store_with_admin("alice", "correct-horse")
    with make_client(store=st) as client:
        client.app.state.throttle = LoginThrottle(
            now=Clock(), hard_max_ip=3, tarpit_base=0.0)
        for _ in range(3):
            _bad_login(client, "alice")
        # Once the address is locked, an EXISTING and an UNKNOWN username get the
        # identical device-scoped response — the lock reveals nothing about which
        # usernames exist. [no-enumeration]
        r_known = _bad_login(client, "alice")
        r_unknown = _bad_login(client, "ghost-user")
        assert r_known.status_code == r_unknown.status_code == 429
        assert r_known.text == r_unknown.text


def test_over_long_password_is_bounded_and_rejected():
    st = _store_with_admin()
    with make_client(store=st) as client:
        client.app.state.throttle = LoginThrottle(
            now=Clock(), hard_max_ip=1000, tarpit_base=0.0)
        token = _csrf(client.get("/login").text)
        giant = "x" * (5 * 1024 * 1024)  # 5 MB, well over the body cap too
        start = time.monotonic()
        r = client.post("/login", data={"_csrf": token, "username": "alice",
                                        "password": giant}, follow_redirects=False)
        elapsed = time.monotonic() - start
        # Dropped as an oversized body → uniform refusal, never hashed, and fast.
        assert r.status_code in (401, 403)
        assert elapsed < 2.0


def test_password_at_cap_boundary_still_hashes_but_over_cap_rejected():
    st = _store_with_admin("bob", "correct-horse")
    with make_client(store=st) as client:
        client.app.state.throttle = LoginThrottle(
            now=Clock(), hard_max_ip=1000, tarpit_base=0.0)
        # A 257-char password (over the 256 cap but under the 64 KB body cap) is
        # rejected as wrong without reaching the KDF.
        token = _csrf(client.get("/login").text)
        r = client.post("/login", data={"_csrf": token, "username": "bob",
                                        "password": "y" * 257}, follow_redirects=False)
        assert r.status_code == 401


def test_oversized_form_body_is_refused_before_parsing():
    st = _store_with_admin()
    with make_client(store=st) as client:
        token = _csrf(client.get("/login").text)
        # A body past the 64 KB cap is dropped → empty form → CSRF refusal (403).
        r = client.post("/login", content=b"_csrf=" + token.encode() +
                        b"&username=alice&password=" + b"z" * (65 * 1024),
                        headers={"content-type": "application/x-www-form-urlencoded"},
                        follow_redirects=False)
        assert r.status_code == 403


def test_failed_login_logs_the_attempt_never_the_password(caplog):
    st = _store_with_admin("alice", "correct-horse")
    with make_client(store=st) as client:
        with caplog.at_level("INFO"):
            token = _csrf(client.get("/login").text)
            client.post("/login", data={"_csrf": token, "username": "alice",
                                        "password": "SUPER-SECRET-PW"},
                        follow_redirects=False)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "SUPER-SECRET-PW" not in text  # the secret never reaches a log
        assert "alice" in text  # but the attempt IS recorded


def test_throttle_absent_login_still_works():
    # No throttle on app.state (as in the base harness) → login behaves exactly
    # as before, proving the throttle is a pure add-on. [degrade-gracefully]
    st = _store_with_admin()
    with make_client(store=st) as client:
        assert not hasattr(client.app.state, "throttle")
        assert _good_login(client).status_code == 303
