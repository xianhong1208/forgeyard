"""Which replica gets the next request: the one with the fewest requests in flight through this
server (least load, as SkyPilot's SkyServe does). Ties go round in turn, so with one request at a
time the replicas simply alternate.

A replica the gateway could not connect to is tried last for a few seconds (`PENALTY`): the agent
reports a dead engine within seconds, and until it does, requests should not keep knocking on it.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

PENALTY = 15.0


@dataclass
class Target:
    """One ready replica of a route."""

    uid: str
    ordinal: int
    url: str                         # http://address:port
    machine: Optional[str]
    secret: Optional[str]            # its internal key (never logged or returned)
    protected: bool                  # its agent gave the engine that key


class Balancer:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self.inflight: dict[str, int] = {}
        self.served: dict[str, int] = {}           # requests sent per replica since the server started
        self._turn: dict[str, int] = {}
        self._failed: dict[str, float] = {}

    def reset(self) -> None:
        with self._lock:
            self.inflight.clear()
            self.served.clear()
            self._turn.clear()
            self._failed.clear()

    def order(self, route: str, targets: list[Target]) -> list[Target]:
        """Every target, best first: not recently failed, fewest in flight, then in turn."""
        if not targets:
            return []
        now = self._clock()
        with self._lock:
            turn = self._turn.get(route, 0)
            self._turn[route] = turn + 1
            n = len(targets)

            def key(item):
                i, t = item
                failed = self._failed.get(t.uid, 0.0) > now
                return (failed, self.inflight.get(t.uid, 0), (i - turn) % n)

            return [t for _, t in sorted(enumerate(targets), key=key)]

    def acquire(self, uid: str) -> None:
        with self._lock:
            self.inflight[uid] = self.inflight.get(uid, 0) + 1
            self.served[uid] = self.served.get(uid, 0) + 1

    def release(self, uid: str) -> None:
        with self._lock:
            left = self.inflight.get(uid, 0) - 1
            if left > 0:
                self.inflight[uid] = left
            else:
                self.inflight.pop(uid, None)

    def failed(self, uid: str) -> None:
        with self._lock:
            self._failed[uid] = self._clock() + PENALTY
            self.served[uid] = max(0, self.served.get(uid, 0) - 1)  # it never got the request

    def snapshot(self, uid: str) -> dict:
        with self._lock:
            return {"in_flight": self.inflight.get(uid, 0), "served": self.served.get(uid, 0)}


balancer = Balancer()
