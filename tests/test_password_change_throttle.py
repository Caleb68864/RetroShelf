"""The "Change your password" form is throttled like /login.

Without this, anyone holding a valid session cookie (a borrowed device, a
stolen cookie) could guess the account's current password freely: the form
verifies it with no delay and no lockout. These tests check that a wrong
current password feeds the same :class:`app.throttle.LoginThrottle` that
``/login`` uses: the per-username tarpit (case-insensitive) and the per-IP
hard lockout. They also check that the two routes share that state, that a
correct current password still gets through and clears the counters the way a
successful login does, and that CSRF and the new-password checks still apply.

The tarpit's ``await asyncio.sleep`` is swapped for a recorder so the delay is
asserted without real sleeping.
"""
import asyncio
import types

import pytest

import app.main as main_mod
from app.throttle import LoginThrottle
from tests.test_auth_routes import _csrf, make_client, register_admin


class Clock:
    def __init__(self, start: float = 5000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def sleeps(monkeypatch):
    """Record every tarpit delay ``app.main`` awaits instead of sleeping."""
    recorded: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(seconds)

    monkeypatch.setattr(main_mod, "asyncio", types.SimpleNamespace(
        sleep=fake_sleep, gather=asyncio.gather))
    return recorded


def _throttle(**kw) -> LoginThrottle:
    opts = dict(now=Clock(), tarpit_free=2, tarpit_base=1.0, tarpit_cap=5.0,
                hard_max_ip=1000)
    opts.update(kw)
    return LoginThrottle(**opts)


def _change(client, current: str, new: str = "brand-new-pass",
            confirm: str | None = None, token: str | None = None):
    token = token if token is not None else _csrf(client.get("/account").text)
    return client.post("/account", data={
        "_csrf": token, "action": "password", "current": current,
        "new": new, "confirm": new if confirm is None else confirm,
    }, follow_redirects=False)


def _login(client, username: str, password: str):
    token = _csrf(client.get("/login").text)
    return client.post("/login", data={"_csrf": token, "username": username,
                                       "password": password}, follow_redirects=False)


def test_wrong_current_password_builds_the_tarpit(sleeps):
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        th = _throttle()
        client.app.state.throttle = th
        for _ in range(5):
            r = _change(client, "WRONG")
            assert r.status_code == 400 and "current password is wrong" in r.text
        # tarpit_free=2, then +1s per failure: 0, 0, 1, 2, 3.
        assert sleeps == [0.0, 0.0, 1.0, 2.0, 3.0]
        # Keyed on the account's username, matched case-insensitively.
        assert th.tarpit_delay("ADMIN") == 3.0
        # Nothing changed.
        assert client._store.verify_login("admin", "hunter2000") is not None


def test_correct_current_password_after_the_delay_succeeds_and_clears(sleeps):
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        th = _throttle()
        client.app.state.throttle = th
        for _ in range(4):
            _change(client, "WRONG")
        assert th.tarpit_delay("admin") > 0
        n = len(sleeps)
        r = _change(client, "hunter2000")
        assert r.status_code == 200 and "Password changed." in r.text
        # A success is not tarpitted, and it clears the counters just as a
        # successful login does (username and this address).
        assert len(sleeps) == n
        assert th.tarpit_delay("admin") == 0.0
        assert not th._by_ip
        assert client._store.verify_login("admin", "brand-new-pass") is not None
        # Still signed in on this device (session re-minted).
        assert client.get("/", follow_redirects=False).status_code == 200


def test_password_change_failures_slow_login_for_that_username(sleeps):
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        th = _throttle()
        client.app.state.throttle = th
        for _ in range(4):
            _change(client, "WRONG")
        sleeps.clear()
        client.cookies.clear()
        r = _login(client, "Admin", "nope")
        assert r.status_code == 401
        # Fifth failure overall for "admin": 3 over the free allowance.
        assert sleeps == [3.0]


def test_login_failures_slow_the_password_change_form(sleeps):
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        th = _throttle()
        client.app.state.throttle = th
        for _ in range(3):
            th.record_failure("ADMIN", "10.9.9.9")  # guesses from elsewhere
        sleeps.clear()
        r = _change(client, "WRONG")
        assert r.status_code == 400
        assert sleeps == [2.0]


def test_per_ip_hard_lock_refuses_even_the_correct_current_password(sleeps):
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        th = _throttle(hard_max_ip=3)
        client.app.state.throttle = th
        for _ in range(3):
            assert _change(client, "WRONG").status_code == 400
        r = _change(client, "hunter2000")
        assert r.status_code == 429
        assert "Too many" in r.text
        assert client._store.verify_login("admin", "hunter2000") is not None
        # The lock lifts after the window, and the correct password works.
        th._now.advance(901)
        assert _change(client, "hunter2000").status_code == 200


def test_csrf_still_applies_and_does_not_feed_the_throttle(sleeps):
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        th = _throttle(tarpit_free=0)
        client.app.state.throttle = th
        r = _change(client, "WRONG", token="0" * 64)
        assert r.status_code == 403
        r = _change(client, "hunter2000", token="0" * 64)
        assert r.status_code == 403
        assert sleeps == []
        assert th.tarpit_delay("admin") == 0.0
        assert client._store.verify_login("admin", "hunter2000") is not None


def test_new_password_checks_still_apply(sleeps):
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        th = _throttle()
        client.app.state.throttle = th
        r = _change(client, "hunter2000", new="x")
        assert r.status_code == 400 and "at least" in r.text
        r = _change(client, "hunter2000", new="brand-new-pass", confirm="other-pass")
        assert r.status_code == 400 and "do not match" in r.text
        # The current password was right, so neither attempt counts as a guess.
        assert th.tarpit_delay("admin") == 0.0
        assert sleeps == []
        assert client._store.verify_login("admin", "hunter2000") is not None


def test_password_change_still_works_without_a_throttle():
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        assert getattr(client.app.state, "throttle", None) is None
        assert _change(client, "WRONG").status_code == 400
        assert _change(client, "hunter2000").status_code == 200


def test_over_long_current_password_is_capped_like_login(sleeps):
    with make_client() as client:
        register_admin(client, "admin", "hunter2000")
        th = _throttle(tarpit_free=0)
        client.app.state.throttle = th
        calls: list[str] = []
        real_verify = client._store.verify_login

        def spy(username, password):
            calls.append(password)
            return real_verify(username, password)

        client._store.verify_login = spy
        r = _change(client, "A" * (main_mod._MAX_PASSWORD_LEN + 1))
        # Same answer as any wrong current password, as /login gives one
        # uniform error for an over-long password.
        assert r.status_code == 400 and "current password is wrong" in r.text
        # Refused before hashing, and without the tarpit sleep (as /login).
        assert calls == []
        assert sleeps == []
        # Still counted as a failure in the shared throttle.
        assert th.tarpit_delay("admin") > 0
        # A password exactly at the cap is still checked normally.
        r = _change(client, "A" * main_mod._MAX_PASSWORD_LEN)
        assert r.status_code == 400
        assert len(calls) == 1
        del client._store.verify_login
        assert client._store.verify_login("admin", "hunter2000") is not None
