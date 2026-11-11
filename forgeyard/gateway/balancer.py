"""Which replica gets the next request: the one with the fewest requests in flight through this
server (least load, as SkyPilot's SkyServe does). Ties go round in turn, so with one request at a
time the replicas simply alternate.

A replica the gateway could not connect to is tried last for a few seconds (`PENALTY`): the agent
reports a dead engine within seconds, and until it does, requests should not keep knocking on it.
The penalty ends early when the replica has become ready again since (it was restarted).

Provider targets (1.2) are counted the same way, keyed "pm:<provider model id>", and one that
failed (429, 5xx, no connection) cools down for longer (PROVIDER_COOLDOWN_SECONDS, or the
provider's Retry-After when that is longer, up to five minutes).
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
    ready_since: float = 0.0         # when it last became ready (epoch seconds)


class Balancer:
    def __init__(self, clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self._clock = clock
        self._wall = wall
        self._lock = threading.Lock()
        self.inflight: dict[str, int] = {}
        self.served: dict[str, int] = {}           # requests sent per replica since the server started
        self._turn: dict[str, int] = {}
        self._failed: dict[str, tuple[float, float]] = {}   # uid -> (penalty until, wall time of the failure)
        self._cool: dict[str, float] = {}                    # provider target -> cooled down until

    def reset(self) -> None:
        with self._lock:
            self.inflight.clear()
            self.served.clear()
            self._turn.clear()
            self._failed.clear()
            self._cool.clear()

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
                until, at = self._failed.get(t.uid, (0.0, 0.0))
                failed = until > now and at >= t.ready_since  # not if it became ready again after failing
                return (failed, self.inflight.get(t.uid, 0), (i - turn) % n)

            return [t for _, t in sorted(enumerate(targets), key=key)]

    def acquire(self, uid: str) -> None:
        with self._lock:
            self.inflight[uid] = self.inflight.get(uid, 0) + 1
            self.served[uid] = self.served.get(uid, 0) + 1

    def try_acquire(self, uid: str, cap: Optional[int]) -> bool:
        """Take a slot only while the replica has fewer than `cap` requests in flight (its deployment's
        concurrency): the check and the count happen together, so two requests cannot both take the
        last slot. No cap: always."""
        with self._lock:
            if cap and self.inflight.get(uid, 0) >= cap:
                return False
            self.inflight[uid] = self.inflight.get(uid, 0) + 1
            self.served[uid] = self.served.get(uid, 0) + 1
            return True

    def release(self, uid: str) -> None:
        with self._lock:
            left = self.inflight.get(uid, 0) - 1
            if left > 0:
                self.inflight[uid] = left
            else:
                self.inflight.pop(uid, None)

    def failed(self, uid: str) -> None:
        with self._lock:
            self._failed[uid] = (self._clock() + PENALTY, self._wall())
            self.served[uid] = max(0, self.served.get(uid, 0) - 1)  # it never got the request

    def cool_down(self, key: str, seconds: float) -> None:
        with self._lock:
            self._cool[key] = max(self._cool.get(key, 0.0), self._clock() + max(0.0, seconds))

    def cooldown_left(self, key: str) -> float:
        with self._lock:
            return max(0.0, self._cool.get(key, 0.0) - self._clock())

    def snapshot(self, uid: str) -> dict:
        with self._lock:
            return {"in_flight": self.inflight.get(uid, 0), "served": self.served.get(uid, 0)}


balancer = Balancer()
