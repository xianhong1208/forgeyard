"""Requests and tokens per minute, per API key, counted in this process (one server; a shared
store for several servers comes after the MVP).

Both are **sliding windows** over the last 60 seconds, not fixed minutes, so a burst at the end of
one minute and the start of the next cannot double the rate.

- **rpm**: every admitted request counts when it is admitted. A refused one does not count, so a
  client that keeps retrying is not locked out for longer.
- **tpm**: the tokens of a request count when its answer reports them (`usage`; for a streamed
  chat the gateway asks the engine for that last chunk). While it runs, a request that sets
  `max_tokens` holds that many (at most the whole tpm) so parallel requests cannot all slip in;
  the hold is replaced by the real count at the end. A request is refused while the window holds
  the whole tpm, or when its `max_tokens` would not fit in what is left.

A refusal says how long until there is room (Retry-After).
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

WINDOW = 60.0


@dataclass
class _Entry:
    at: float
    tokens: int


@dataclass
class _Key:
    requests: list[float] = field(default_factory=list)
    tokens: list[_Entry] = field(default_factory=list)


@dataclass
class Admission:
    key_id: int
    entry: Optional[_Entry]


@dataclass
class Refusal:
    which: str            # rpm | tpm
    retry_after: int      # seconds
    limit: int


class KeyLimiter:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: dict[int, _Key] = {}

    def reset(self) -> None:
        with self._lock:
            self._keys.clear()

    def _prune(self, k: _Key, now: float) -> None:
        start = now - WINDOW
        k.requests = [t for t in k.requests if t > start]
        k.tokens = [e for e in k.tokens if e.at > start]

    def admit(self, key_id: int, rpm: Optional[int], tpm: Optional[int], max_tokens: Optional[int] = None):
        """An Admission (settle it when the answer is in) or a Refusal."""
        if not rpm and not tpm:
            return Admission(key_id, None)
        now = self._clock()
        with self._lock:
            k = self._keys.setdefault(key_id, _Key())
            self._prune(k, now)
            if rpm and len(k.requests) >= rpm:
                # Room again when the oldest of the last `rpm` requests leaves the window.
                oldest = k.requests[len(k.requests) - rpm]
                return Refusal("rpm", _ceil(oldest + WINDOW - now), rpm)
            entry = None
            if tpm:
                want = min(max(0, int(max_tokens or 0)), tpm)
                used = sum(e.tokens for e in k.tokens)
                if used >= tpm or used + want > tpm:
                    return Refusal("tpm", _ceil(self._room_at(k, used, want, tpm) - now), tpm)
                entry = _Entry(now, want)
                k.tokens.append(entry)
            if rpm:
                k.requests.append(now)
            if len(self._keys) > 50_000:  # forget idle keys
                self._keys = {i: v for i, v in self._keys.items() if v.requests or v.tokens}
            return Admission(key_id, entry)

    @staticmethod
    def _room_at(k: _Key, used: int, want: int, tpm: int) -> float:
        """When enough tokens have left the window for this request."""
        for e in sorted(k.tokens, key=lambda e: e.at):
            used -= e.tokens
            if used < tpm and used + want <= tpm:
                return e.at + WINDOW
        return k.tokens[-1].at + WINDOW if k.tokens else 0.0

    def settle(self, admission: Admission, tokens: int) -> None:
        """The request's real token count, counted from now."""
        if admission.entry is None:
            return
        with self._lock:
            admission.entry.tokens = max(0, int(tokens))
            admission.entry.at = self._clock()
            k = self._keys.get(admission.key_id)
            if k is not None and admission.entry not in k.tokens:
                k.tokens.append(admission.entry)  # pruned while it ran: it still counts now

    def used(self, key_id: int) -> tuple[int, int]:
        """(requests, tokens) in the current window (the API keys page)."""
        now = self._clock()
        with self._lock:
            k = self._keys.get(key_id)
            if k is None:
                return 0, 0
            self._prune(k, now)
            return len(k.requests), sum(e.tokens for e in k.tokens)


def _ceil(seconds: float) -> int:
    return max(1, math.ceil(seconds))


key_limiter = KeyLimiter()
