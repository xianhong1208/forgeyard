"""In-memory rate limits (one process; M0 runs a single server).

- A global per-IP budget for the API (fixed one-minute windows).
- Sign-in failures, per account and per IP. Past the free failures each further try has to
  wait, doubling up to five minutes. Nothing is ever locked permanently, so an attacker
  cannot lock the owner out by guessing; a correct password after the wait still works.

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

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()
            self._accounts.clear()
            self._ips.clear()

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
            wait = 0.0
            acc = self._accounts.get(account.lower())
            if acc and acc.count >= free:
                delay = min(MAX_DELAY, 2.0 ** (acc.count - free + 1))
                wait = max(wait, acc.last + delay - now)
            rec = self._ips.get(ip)
            if rec:
                rec.times = [t for t in rec.times if now - t < IP_WINDOW]
                if len(rec.times) >= ip_limit:
                    wait = max(wait, rec.times[0] + IP_WINDOW - now)
        return max(0.0, wait)

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


def retry_after(seconds: float) -> str:
    return str(max(1, math.ceil(seconds)))


limiter = RateLimiter()
