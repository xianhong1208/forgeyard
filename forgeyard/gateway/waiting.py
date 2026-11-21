"""Fair waiting for a busy local tier (1.4.5, #169): text requests that find every replica of a route's
tier at its deployment's *Concurrent requests* wait in line, first come first served, for a free slot.

- **One line per route tier** (`<served name>#<tier>`): a request can be served by any deployment of the
  tier, so that is what it waits for. A deployment's own route has one deployment: one line each.
- **Only the first in line takes a slot.** A request that arrives while others wait joins the end of
  the line instead of taking a slot that has just come free, so no request starves behind newcomers.
- **No polling.** Whoever releases a slot (forgeyard/gateway/balancer.py) wakes the first in line,
  from any thread. As a safety net against a missed wake-up (a replica became ready, a deployment's
  concurrency changed) a waiter also looks again every `RECHECK` seconds.
- **Bounded:** a request waits at most the route's *Wait for a free local slot* (`wait_ms`, at most 10 s),
  and at most `MAX_WAITERS` requests wait per line. One that finds the line full does not wait: it is
  busy at once (it spills, or goes to a replica anyway, as before).
- **A client that goes away leaves the line** (the waiter is removed in a `finally`), and the next one
  is woken if it became first.

Images keep their own queue (forgeyard/gateway/imagegen.py), with its own length and timeout.
"""

from __future__ import annotations

import asyncio
import threading
from collections import deque
from typing import Optional

MAX_WAITERS = 256       # requests waiting per route tier; more do not wait
RECHECK = 0.5           # seconds: a waiter looks again even without a wake-up


class Waiter:
    __slots__ = ("event", "loop")

    def __init__(self):
        self.loop = asyncio.get_running_loop()
        self.event = asyncio.Event()

    def wake(self) -> None:
        try:
            self.loop.call_soon_threadsafe(self.event.set)
        except RuntimeError:   # its loop is closed: nobody waits on it any more
            pass


class Line:
    """Requests waiting for a slot on one route's tier, in the order they came."""

    def __init__(self):
        self._lock = threading.Lock()
        self.waiting: deque[Waiter] = deque()

    def __len__(self) -> int:
        return len(self.waiting)

    def join(self, limit: Optional[int] = None) -> Optional[Waiter]:
        """A place at the end of the line, or None when it is full (MAX_WAITERS)."""
        with self._lock:
            if len(self.waiting) >= (MAX_WAITERS if limit is None else limit):
                return None
            w = Waiter()
            self.waiting.append(w)
            return w

    def may_try(self, w: Optional[Waiter]) -> bool:
        """May this request take a free slot now: one not in line only when nobody waits, one in line
        only when it is first."""
        with self._lock:
            if w is None:
                return not self.waiting
            return bool(self.waiting) and self.waiting[0] is w

    def leave(self, w: Optional[Waiter]) -> None:
        """Out of the line (served, given up, or gone); the next one is woken if it is now first."""
        if w is None:
            return
        with self._lock:
            was_first = bool(self.waiting) and self.waiting[0] is w
            try:
                self.waiting.remove(w)
            except ValueError:
                return
            nxt = self.waiting[0] if was_first and self.waiting else None
        if nxt is not None:
            nxt.wake()

    def wake_first(self) -> None:
        with self._lock:
            first = self.waiting[0] if self.waiting else None
        if first is not None:
            first.wake()

    async def wait(self, w: Waiter, seconds: float) -> None:
        """Until woken, or `seconds` (at most RECHECK) have passed."""
        try:
            await asyncio.wait_for(w.event.wait(), max(0.0, min(seconds, RECHECK)))
        except asyncio.TimeoutError:
            pass
        w.event.clear()


_lines: dict[str, Line] = {}
_lines_lock = threading.Lock()


def line(key: str) -> Line:
    with _lines_lock:
        found = _lines.get(key)
        if found is None:
            found = _lines[key] = Line()
        return found


def slot_freed(uid: str) -> None:
    """A replica released a slot: the first in every line looks again (lines are few, and a replica
    may be in several routes)."""
    with _lines_lock:
        lines = list(_lines.values())
    for ln in lines:
        ln.wake_first()


def forget() -> None:
    with _lines_lock:
        _lines.clear()
