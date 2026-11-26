"""In-memory rate limits (one process; M0 runs a single server).

- A global per-IP budget for the API (fixed one-minute windows).
- Sign-in failures, per account and per IP. Past the free failures each further try has to
  wait, doubling up to five minutes. Nothing is ever locked permanently, so an attacker
  cannot lock the owner out by guessing; a correct password after the wait still works.
- Tries with no account to count against (a single sign-on finish, an agent's join token, the
  setup token, a set-password link, a registration): the same back-off, counted per client
  address. Never under one name for the whole server: then anyone's failures would make
  everybody wait.

A limited request gets 429 with Retry-After. It never ends the session: the UI treats 429
as "wait", not as "signed out".
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field

MAX_DELAY = 300.0
IP_WINDOW = 15 * 60.0


@dataclass
class _Failures:
    count: int = 0
    last: float = 0.0
    times: list[float] = field(default_factory=list)


class RateLimiter:
    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[str, tuple[int, int]] = {}
        self._accounts: dict[str, _Failures] = {}
        self._ips: dict[str, _Failures] = {}
        self._attempts: dict[tuple[str, str], _Failures] = {}

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()
            self._accounts.clear()
            self._ips.clear()
            self._attempts.clear()

    # ------------------------------------------------------------- global budget
    def hit(self, key: str, per_minute: int) -> float:
        """Count one request. Returns 0 if allowed, else seconds until the next window."""
        if per_minute <= 0:
            return 0.0
        now = self._clock()
        window = int(now // 60)
        with self._lock:
            start, count = self._windows.get(key, (window, 0))
            if start != window:
                start, count = window, 0
            count += 1
            self._windows[key] = (start, count)
            if len(self._windows) > 50_000:  # forget old windows
                self._windows = {k: v for k, v in self._windows.items() if v[0] == window}
        if count > per_minute:
            return (window + 1) * 60 - now
        return 0.0

    # ------------------------------------------------------------- sign-in
    def login_wait(self, account: str, ip: str, free: int, ip_limit: int) -> float:
        """Seconds the caller must wait before another sign-in try (0 = go ahead)."""
        now = self._clock()
        with self._lock:
            return max(_backoff(self._accounts.get(account.lower()), free, now), self._ip_wait(ip, ip_limit, now))

    def _ip_wait(self, ip: str, ip_limit: int, now: float) -> float:
        rec = self._ips.get(ip)
        if not rec:
            return 0.0
        rec.times = [t for t in rec.times if now - t < IP_WINDOW]
        return max(0.0, rec.times[0] + IP_WINDOW - now) if len(rec.times) >= ip_limit else 0.0

    def login_failed(self, account: str, ip: str) -> None:
        now = self._clock()
        with self._lock:
            acc = self._accounts.setdefault(account.lower(), _Failures())
            acc.count += 1
            acc.last = now
            self._ips.setdefault(ip, _Failures()).times.append(now)

    def login_succeeded(self, account: str) -> None:
        with self._lock:
            self._accounts.pop(account.lower(), None)

    # ------------------------------------------------------------- tries without an account
    # `kind` names the door (sso, join, setup, link, register). The back-off is per (kind, client address), in its
    # own table: a name typed at the password sign-in can never reach it. The per-address cap of login_wait
    # (ip_limit failures in 15 minutes, every door and password sign-in together) still holds, and a success does
    # not clear that cap.
    def attempt_wait(self, kind: str, ip: str, free: int, ip_limit: int) -> float:
        now = self._clock()
        with self._lock:
            return max(_backoff(self._attempts.get((kind, ip)), free, now), self._ip_wait(ip, ip_limit, now))

    def attempt_failed(self, kind: str, ip: str) -> None:
        now = self._clock()
        with self._lock:
            rec = self._attempts.setdefault((kind, ip), _Failures())
            rec.count += 1
            rec.last = now
            self._ips.setdefault(ip, _Failures()).times.append(now)

    def attempt_succeeded(self, kind: str, ip: str) -> None:
        with self._lock:
            self._attempts.pop((kind, ip), None)


def _backoff(rec: _Failures | None, free: int, now: float) -> float:
    """Past `free` failures, each further try waits 2, 4, 8 … s after the last failure, up to MAX_DELAY."""
    if rec is None or rec.count < free:
        return 0.0
    return max(0.0, rec.last + min(MAX_DELAY, 2.0 ** (rec.count - free + 1)) - now)


def retry_after(seconds: float) -> str:
    return str(max(1, math.ceil(seconds)))


limiter = RateLimiter()
