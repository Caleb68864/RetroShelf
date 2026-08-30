"""In-process brute-force throttle for the opt-in login route.

RetroShelf runs as a **single uvicorn worker**, so a plain in-memory tracker —
one dict of recent failure timestamps, guarded by a lock — is a correct and
dependency-free rate limiter (an attacker cannot fork the state or restart the
process to clear it; a restart resets the counters, which is acceptable because
only the operator can restart the box). Nothing about failed logins is written
to the JSON state file: attempt data is ephemeral, high-churn, and must never
outlive the process or leak into a backup.

Design — the lockout-vs-DoS tradeoff
------------------------------------
A naive *per-username* hard lockout is a footgun: anyone who can reach ``/login``
could lock a victim **out** of their own account just by spamming the victim's
username with wrong passwords. The victim, not the attacker, is denied. So the
two dimensions are split by what each can safely gate:

- **Per-IP hard lockout** — the real stop. After :data:`hard_max_ip` failed
  verifications from one client address within :data:`window` seconds, that
  address is refused for the rest of the window. The lock follows the *attacker's
  own address*, never a username, so it can never be weaponised to deny a victim:
  the worst an attacker achieves is locking themselves out. On the LAN this tool
  serves, each device has its own address, so one hammering device is throttled
  while the rest of the household is untouched. (Behind a shared-IP reverse
  proxy the cap is generous enough that ordinary use never trips it; operators
  fronting the bridge that way are told to let the proxy do auth — see README.)
- **Per-username escalating tarpit** — a *delay*, not a denial. Each failed
  attempt for a given username adds a small, capped ``await`` before the response
  (non-blocking, so it never stalls the single worker for other visitors). It
  slows guessing without ever locking a real user out: someone who then types the
  right password still gets in, just after a brief pause. The delay is keyed on
  the *submitted* username string whether or not that account exists, so it adds
  identical time on the unknown-user and wrong-password paths and cannot be used
  to enumerate usernames.

Both counters clear for a username/IP on that identity's first success. The
hard-lock response the route returns is deliberately username-independent
("too many attempts from this device"), so it reveals nothing about which
usernames exist — the no-enumeration property the login path already has is
preserved.
"""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable


class LoginThrottle:
    """Per-IP hard lockout + per-username tarpit for failed logins.

    Thread-safe (one :class:`threading.Lock`); construct once per process and
    share it. All timing goes through the injected *now* callable so tests can
    drive a deterministic clock instead of sleeping.

    :ivar window: Sliding window, in seconds, over which failures are counted.
    :ivar hard_max_ip: Failed verifications from one address within *window*
        that trip the hard lockout.
    :ivar tarpit_free: Failures for a username that incur no delay yet.
    :ivar tarpit_base: Seconds added per failure once past *tarpit_free*.
    :ivar tarpit_cap: Maximum tarpit delay in seconds.
    :ivar max_keys: Soft ceiling on tracked keys per table before a sweep.
    """

    def __init__(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        window: float = 900.0,
        hard_max_ip: int = 15,
        tarpit_free: int = 3,
        tarpit_base: float = 0.75,
        tarpit_cap: float = 5.0,
        max_keys: int = 4096,
    ) -> None:
        """:param now: Monotonic clock source (injectable for tests)."""
        self._now = now
        self._window = window
        self._hard_max_ip = hard_max_ip
        self._tarpit_free = tarpit_free
        self._tarpit_base = tarpit_base
        self._tarpit_cap = tarpit_cap
        self._max_keys = max_keys
        # Cap the timestamps stored per key. Both decisions are threshold tests
        # (``>= hard_max_ip`` for the lock; the tarpit ramp saturates at
        # ``tarpit_cap``), so once a key holds enough samples to max out both,
        # storing more buys nothing — it only lets an attacker who hammers a
        # single username (from many addresses, so the per-IP lock never fires
        # for them) grow that one list without bound, turning every O(n) prune
        # into O(n^2) work and unbounded memory. Keep only the newest few beyond
        # what the largest threshold needs; the sliding window still clears the
        # key after a quiet ``window`` and the ``>=`` lock test still trips.
        tarpit_span = (
            math.ceil(tarpit_cap / tarpit_base) if tarpit_base > 0 else 0
        )
        self._max_samples = max(hard_max_ip, tarpit_free + tarpit_span) + 1
        self._lock = threading.Lock()
        # key -> list of recent failure timestamps (monotonic seconds).
        self._by_ip: dict[str, list[float]] = {}
        self._by_user: dict[str, list[float]] = {}

    # -- internals -----------------------------------------------------------
    def _recent(self, table: dict[str, list[float]], key: str, cutoff: float) -> list[float]:
        """Return *key*'s timestamps newer than *cutoff*, pruning the rest in place.

        Empty entries are dropped so the tables cannot grow without bound; an
        oversized table (an abuser cycling many distinct keys) is swept whole.

        :param table: One of the per-IP / per-username tables.
        :param key: The IP or (casefolded) username to look up.
        :param cutoff: Timestamps at or before this are expired.
        :returns: The live (post-prune) timestamp list, possibly empty.
        :rtype: list[float]
        """
        if len(table) > self._max_keys:
            for k in list(table):
                fresh = [t for t in table[k] if t > cutoff]
                if fresh:
                    table[k] = fresh
                else:
                    table.pop(k, None)
        live = [t for t in table.get(key, ()) if t > cutoff]
        if live:
            table[key] = live
        else:
            table.pop(key, None)
        return live

    # -- queries -------------------------------------------------------------
    def locked(self, ip: str | None) -> bool:
        """Whether *ip* is currently hard-locked (too many recent failures).

        :param ip: The client address, or ``None`` when it is unknown (an
            unknown address is never locked — it cannot be attributed).
        :rtype: bool
        """
        if not ip:
            return False
        with self._lock:
            cutoff = self._now() - self._window
            return len(self._recent(self._by_ip, ip, cutoff)) >= self._hard_max_ip

    def tarpit_delay(self, username: str) -> float:
        """Delay, in seconds, to apply for *username*'s recent failure history.

        Zero for the first :data:`tarpit_free` failures, then a linear,
        capped ramp. Keyed on the submitted string (existing or not) so the
        delay never distinguishes a real account from an unknown one.

        :param username: The submitted username (casefolded internally).
        :rtype: float
        """
        key = (username or "").strip().casefold()
        if not key:
            return 0.0
        with self._lock:
            cutoff = self._now() - self._window
            n = len(self._recent(self._by_user, key, cutoff))
        over = n - self._tarpit_free
        if over <= 0:
            return 0.0
        return min(self._tarpit_cap, self._tarpit_base * over)

    # -- mutations -----------------------------------------------------------
    def record_failure(self, username: str, ip: str | None) -> None:
        """Record one failed login for *username* and *ip*.

        :param username: The submitted username (casefolded internally).
        :param ip: The client address, or ``None`` when unknown.
        """
        key = (username or "").strip().casefold()
        with self._lock:
            now = self._now()
            cutoff = now - self._window
            if key:
                live = self._recent(self._by_user, key, cutoff)
                live.append(now)
                if len(live) > self._max_samples:
                    del live[: -self._max_samples]  # keep only the newest
                self._by_user[key] = live
            if ip:
                live = self._recent(self._by_ip, ip, cutoff)
                live.append(now)
                if len(live) > self._max_samples:
                    del live[: -self._max_samples]  # keep only the newest
                self._by_ip[ip] = live

    def record_success(self, username: str, ip: str | None) -> None:
        """Clear both counters for a successful login by *username* from *ip*.

        :param username: The username that just authenticated.
        :param ip: The client address it came from, or ``None``.
        """
        key = (username or "").strip().casefold()
        with self._lock:
            if key:
                self._by_user.pop(key, None)
            if ip:
                self._by_ip.pop(ip, None)
