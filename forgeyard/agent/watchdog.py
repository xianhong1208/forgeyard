"""Stop a blocking read from another thread: when it is cancelled, or when nothing moved for a while.

A download or an image pull runs in a worker thread and spends its time in a blocking read. A cancel
flag checked between reads is not enough: a stalled connection keeps the read waiting for its whole
socket timeout, and a slow one between two reads. The watchdog sits beside the read and calls `abort`
(close the socket, end the process) the moment the cancel event is set, or when `moved()` was not
called for `limit` seconds. The reader then sees its read fail and asks `reason` why.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

CANCELLED = "cancelled"
STALLED = "stalled"


class Watchdog:
    def __init__(self, abort: Callable[[], None], cancel: Optional[threading.Event] = None, *,
                 limit: Optional[float] = None, poll: float = 0.2, clock: Callable[[], float] = time.monotonic,
                 tick: Optional[Callable[[], None]] = None):
        self._abort = abort
        self._tick = tick  # called on every poll: e.g. to send a progress report held back by a throttle
        self.cancel = cancel or threading.Event()
        self.limit = limit
        self.poll = poll
        self.clock = clock
        self.last = clock()
        self.reason: Optional[str] = None
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def moved(self) -> None:
        self.last = self.clock()

    def idle(self) -> float:
        return self.clock() - self.last

    def _run(self) -> None:
        while not self._stop.is_set():
            if self.cancel.wait(self.poll):
                why = CANCELLED
            elif self.limit is not None and self.idle() >= self.limit:
                why = STALLED
            else:
                if self._tick is not None:
                    try:
                        self._tick()
                    except Exception:
                        pass
                continue
            if self._stop.is_set():
                return
            self.reason = why
            try:
                self._abort()
            except Exception:  # the read it was meant to end may have ended by itself
                pass
            return

    def __enter__(self) -> "Watchdog":
        self.last = self.clock()
        self._thread = threading.Thread(target=self._run, name="forgeyard-watchdog", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
