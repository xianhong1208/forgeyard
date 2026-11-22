"""Which replica gets the next request: the one with the fewest requests in flight through this
server (least load, as SkyPilot's SkyServe does). Ties go round in turn, so with one request at a
time the replicas simply alternate.

A replica the gateway could not connect to is tried last for a few seconds (`PENALTY`): the agent
reports a dead engine within seconds, and until it does, requests should not keep knocking on it.
The penalty ends early when the replica has become ready again since (it was restarted).

A replica the autoscaler removes is **drained** first (1.5.0, #37; forgeyard/workloads/autoscale.py): it takes no new
request (try_acquire refuses it, order puts it last), the ones it has finish, and `drained` says when none is left.

Provider targets (1.2) are counted the same way, keyed "pm:<provider model id>", and one that
failed (429, 5xx, no connection) cools down for longer (`PROVIDER_COOLDOWN`, or the provider's
Retry-After when that is longer, up to `PROVIDER_COOLDOWN_MAX`).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

PENALTY = 15.0
PROVIDER_COOLDOWN = 30.0        # seconds a failed provider target is tried last
PROVIDER_COOLDOWN_MAX = 300.0   # the longest a provider's Retry-After can make it


@dataclass
class Target:
    """One ready replica of a route."""

    uid: str
    ordinal: int
    url: str                         # http://address:port, or (1.4.5) https://address:port
    machine: Optional[str]
    secret: Optional[str]            # its internal key (never logged or returned)
    protected: bool                  # its agent gave the engine that key
    ready_since: float = 0.0         # when it last became ready (epoch seconds)
    # 1.4.5 (#163): the machine's binding name its guard's certificate must carry (https:// only; None: plain HTTP).
    tls_name: Optional[str] = None

    @property
    def ext(self) -> dict:
        """The request extensions that make the gateway check this replica's machine (security/replica_tls.py)."""
        return {"sni_hostname": self.tls_name} if self.tls_name else {}


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
        self._listeners: list[Callable[[str], None]] = []    # told when a slot is released (1.4.5)
        self._draining: set[str] = set()                     # replicas being drained: no new requests (1.5.0)

    def reset(self) -> None:
        with self._lock:
            self.inflight.clear()
            self.served.clear()
            self._turn.clear()
            self._failed.clear()
            self._cool.clear()
            self._draining.clear()

    def order(self, route: str, targets: list[Target]) -> list[Target]:
        """Every target, best first: not being drained, not recently failed, fewest in flight, then in turn."""
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
                return (t.uid in self._draining, failed, self.inflight.get(t.uid, 0), (i - turn) % n)

            return [t for _, t in sorted(enumerate(targets), key=key)]

    def acquire(self, uid: str) -> None:
        with self._lock:
            self.inflight[uid] = self.inflight.get(uid, 0) + 1
            self.served[uid] = self.served.get(uid, 0) + 1

    def try_acquire(self, uid: str, cap: Optional[int]) -> bool:
        """Take a slot only while the replica has fewer than `cap` requests in flight (its deployment's
        concurrency): the check and the count happen together, so two requests cannot both take the
        last slot. No cap: always. Never on a replica being drained (1.5.0)."""
        with self._lock:
            if uid in self._draining:
                return False
            if cap and self.inflight.get(uid, 0) >= cap:
                return False
            self.inflight[uid] = self.inflight.get(uid, 0) + 1
            self.served[uid] = self.served.get(uid, 0) + 1
            return True

    def drain(self, uid: str) -> None:
        """Send this replica nothing new (the autoscaler removes it once `drained`)."""
        with self._lock:
            self._draining.add(uid)

    def undrain(self, uid: str) -> None:
        with self._lock:
            self._draining.discard(uid)

    def draining(self, uid: str) -> bool:
        with self._lock:
            return uid in self._draining

    def drained(self, uid: str) -> bool:
        """Being drained, and nothing in flight on it any more: checked under the same lock as try_acquire, so no
        request can take a slot on it in between."""
        with self._lock:
            return uid in self._draining and not self.inflight.get(uid, 0)

    def listen(self, fn: Callable[[str], None]) -> None:
        """Call fn(uid) whenever a slot is released (the gateway's waiting line, forgeyard/gateway/waiting.py)."""
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def release(self, uid: str) -> None:
        with self._lock:
            left = self.inflight.get(uid, 0) - 1
            if left > 0:
                self.inflight[uid] = left
            else:
                self.inflight.pop(uid, None)
            listeners = list(self._listeners)
        for fn in listeners:
            fn(uid)

    def failed(self, uid: str, reached: bool = False) -> None:
        """Try the replica last for a while. `reached`: it got the request (and then broke off or went
        silent), so it still counts as served."""
        with self._lock:
            self._failed[uid] = (self._clock() + PENALTY, self._wall())
            if not reached:
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
