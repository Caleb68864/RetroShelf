"""Unit tests for the in-process login brute-force throttle.

Drives :class:`app.throttle.LoginThrottle` with a fake monotonic clock so the
window, escalating tarpit, per-IP hard lock, and success-clears-counter
behaviour are all exercised deterministically without sleeping.
"""
from app.throttle import LoginThrottle


class Clock:
    """A hand-cranked monotonic clock for deterministic timing tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _throttle(clock: Clock, **kw) -> LoginThrottle:
    opts = dict(now=clock, window=900.0, hard_max_ip=5,
                tarpit_free=2, tarpit_base=1.0, tarpit_cap=4.0)
    opts.update(kw)
    return LoginThrottle(**opts)


def test_ip_hard_locks_after_threshold():
    clock = Clock()
    t = _throttle(clock)
    for _ in range(4):
        t.record_failure("alice", "10.0.0.9")
        assert not t.locked("10.0.0.9")
    t.record_failure("alice", "10.0.0.9")  # 5th failure
    assert t.locked("10.0.0.9")
    # A different address is unaffected — the lock follows the attacker only.
    assert not t.locked("10.0.0.10")


def test_lock_expires_after_window():
    clock = Clock()
    t = _throttle(clock)
    for _ in range(5):
        t.record_failure("alice", "10.0.0.9")
    assert t.locked("10.0.0.9")
    clock.advance(901.0)  # every failure has aged out of the window
    assert not t.locked("10.0.0.9")


def test_success_clears_both_counters():
    clock = Clock()
    t = _throttle(clock)
    for _ in range(4):
        t.record_failure("alice", "10.0.0.9")
    t.record_success("alice", "10.0.0.9")
    assert not t.locked("10.0.0.9")
    assert t.tarpit_delay("alice") == 0.0


def test_tarpit_is_zero_until_free_then_ramps_and_caps():
    clock = Clock()
    t = _throttle(clock)  # free=2, base=1.0, cap=4.0
    assert t.tarpit_delay("alice") == 0.0
    t.record_failure("alice", "10.0.0.9")
    t.record_failure("alice", "10.0.0.9")
    assert t.tarpit_delay("alice") == 0.0  # still within the free allowance
    t.record_failure("alice", "10.0.0.9")  # 3rd → 1 over free
    assert t.tarpit_delay("alice") == 1.0
    t.record_failure("alice", "10.0.0.9")  # 4th → 2 over free
    assert t.tarpit_delay("alice") == 2.0
    for _ in range(10):
        t.record_failure("alice", "10.0.0.9")
    assert t.tarpit_delay("alice") == 4.0  # capped


def test_tarpit_keyed_on_submitted_string_case_insensitively():
    clock = Clock()
    t = _throttle(clock)
    t.record_failure("Alice", "10.0.0.9")
    t.record_failure("alice", "10.0.0.9")
    t.record_failure("ALICE", "10.0.0.9")  # 3 over the same casefolded key
    assert t.tarpit_delay("alice") == 1.0
    # An unknown username accrues its own independent tarpit — the throttle
    # never distinguishes existing from non-existing accounts. [no-enumeration]
    assert t.tarpit_delay("ghost") == 0.0


def test_unknown_ip_is_never_locked():
    clock = Clock()
    t = _throttle(clock)
    for _ in range(20):
        t.record_failure("alice", None)
    assert not t.locked(None)


def test_key_tables_do_not_grow_unbounded():
    clock = Clock()
    t = _throttle(clock, max_keys=8)
    # Many distinct usernames, all expired, must be swept rather than retained.
    for i in range(50):
        t.record_failure(f"user{i}", None)
    clock.advance(901.0)
    t.record_failure("fresh", None)  # triggers the sweep via _recent
    assert len(t._by_user) <= 8
