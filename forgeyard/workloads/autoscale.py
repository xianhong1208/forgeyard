"""Automatic replicas (1.5.0, #37): the number of a deployment's replicas follows the load the gateway sees.

- **Settings** (in the deployment's parameters, read by the server only, so changing them restarts nothing):
  `autoscale` (off by default, for every deployment old and new), `min_replicas` and `max_replicas` (1 and 2 by
  default, 1 to 16; `min_replicas` may be 0 when *Sleep when idle* is on), and, advanced, `autoscale_target` (80 %
  of a replica's *Concurrent requests*), `scale_up_seconds` (60) and `scale_down_minutes` (10). Video deployments
  are never scaled automatically: a video job belongs to the replica that makes it.
- **The load** of a deployment, sampled every SAMPLE_SECONDS in this server: the requests in flight on its
  replicas (the gateway's balancer; not those on a replica being drained, which finish there) plus those waiting in the gateway's line for a slot of a tier it is in (and in
  its image queue), a line shared by several deployments counted in equal parts. One replica takes its
  *Concurrent requests* (`concurrency`, 1 when the recipe has none) at the target: 80 % of 4 is 3.2.
- **Up**: one replica more when the average load over the last `scale_up_seconds` is above what the ready replicas
  take at the target, or over the last PANIC_SECONDS at least twice that (a burst is not left waiting a minute),
  never above `max_replicas`, and **only when it fits somewhere now**: the scheduler's check of a deploy, on a
  machine that is online (on the pinned one for a pinned deployment). One that does not fit is not added to wait
  for room: the page says it wants one more and why there is none.
- **Down**: one replica fewer once the average load over the last `scale_down_minutes`, all of it after the last
  change, would be served at the target by one replica fewer; never below `min_replicas` (at least 1: to 0 only by
  *Sleep when idle*, which puts the whole deployment to sleep). The replica removed is the newest one, and it is
  **drained** first: the gateway sends it nothing new (forgeyard/gateway/balancer.py `drain`), the requests it runs
  finish, and only once none is left (or after DRAIN_MAX_SECONDS) its row goes and its agent stops it. Load rising
  again meanwhile, or the others no longer ready, keeps it.
- **One at a time**: nothing is added while a replica is starting and nothing removed while one drains; nothing
  changes while an edit restarts the replicas, while a replica's machine is offline (it may be moved, #164), or
  within PAUSE_AFTER_CHANGE of a person starting or changing the deployment.
- **Asleep**: a deployment that went to sleep keeps `min_replicas` rows (at least 1), so it wakes with as many.
- Every decision is an event of the deployment ("Scaled up to 2: …", "Draining replica 1: …", "Removed replica 1
  …"), not audited (no person did it); `/metrics` has the replicas desired, ready and draining, the load and the
  decisions made.

Load samples, drains and decisions live in this server's memory: a restart starts the windows over and keeps every
replica (a drain under way is simply not finished; the next decision starts again).
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard.db import Deployment, GatewayRoute, Replica, RouteTarget, Workload, as_utc, new_session, utcnow

log = logging.getLogger("forgeyard.workloads")

PARAMS = ("autoscale", "min_replicas", "max_replicas", "autoscale_target", "scale_up_seconds", "scale_down_minutes")
MIN_DEFAULT, MAX_DEFAULT, MOST = 1, 2, 16              # replicas (Tony, #37)
TARGET_DEFAULT, TARGET_MIN, TARGET_MAX = 80, 20, 100     # % of a replica's concurrent requests
UP_DEFAULT, UP_MIN, UP_MAX = 60, 10, 600                 # seconds of load above the target before one is added
DOWN_DEFAULT, DOWN_MIN, DOWN_MAX = 10, 1, 120            # minutes of load below it before one is removed
PANIC_SECONDS = 10.0          # twice the target for this long adds one at once
SAMPLE_SECONDS = 2.0
PAUSE_AFTER_CHANGE = timedelta(minutes=2)
DRAIN_MAX_SECONDS = 1800.0    # a drained replica goes at the latest after this, requests left or not
KEEP_SAMPLES = DOWN_MAX * 60 + 60   # seconds of samples kept per deployment


# ------------------------------------------------------------------ settings
@dataclass
class Setting:
    on: bool
    min: int
    max: int
    target: int
    up_seconds: int
    down_minutes: int

    @property
    def floor(self) -> int:
        """The fewest replicas while it is awake (a min of 0 means: down to 0 only by sleeping)."""
        return max(1, self.min)


def _whole(v, low: int, high: int) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) and low <= v <= high else None


def setting(params: Optional[dict]) -> Setting:
    p = params or {}
    low = _whole(p.get("min_replicas"), 0, MOST)
    high = _whole(p.get("max_replicas"), 1, MOST)
    low = MIN_DEFAULT if low is None else low
    high = max(MAX_DEFAULT if high is None else high, max(1, low))
    return Setting(on=p.get("autoscale") is True, min=low, max=high,
                   target=_whole(p.get("autoscale_target"), TARGET_MIN, TARGET_MAX) or TARGET_DEFAULT,
                   up_seconds=_whole(p.get("scale_up_seconds"), UP_MIN, UP_MAX) or UP_DEFAULT,
                   down_minutes=_whole(p.get("scale_down_minutes"), DOWN_MIN, DOWN_MAX) or DOWN_DEFAULT)


def _int_of(raw: dict, name: str, unit: str) -> tuple[Optional[int], Optional[str]]:
    v = raw.get(name)
    if v in (None, ""):
        return None, None
    try:
        return int(str(v).strip()), None   # true, 1.5 and "two" are not whole numbers
    except ValueError:
        return None, f"{name} must be a whole number{unit}"


def normalise(raw: dict, out: dict, *, video: bool) -> Optional[str]:
    """Check the parameters into `out` (after sleep's: `min_replicas` 0 needs `sleep_when_idle` on). Returns why
    they are refused, or None. `autoscale` is kept only when on; the others are kept as given (also while off)."""
    on = raw.get("autoscale")
    if on not in (None, ""):
        if not isinstance(on, bool):
            return "autoscale: true or false"
        if on and video:
            return "autoscale: video deployments are not scaled automatically (a video job belongs to its replica)"
        if on:
            out["autoscale"] = True
    bounds = {"min_replicas": (0, MOST, ""), "max_replicas": (1, MOST, ""),
              "autoscale_target": (TARGET_MIN, TARGET_MAX, " (%)"),
              "scale_up_seconds": (UP_MIN, UP_MAX, " of seconds"),
              "scale_down_minutes": (DOWN_MIN, DOWN_MAX, " of minutes")}
    for name, (low, high, unit) in bounds.items():
        v, why = _int_of(raw, name, unit)
        if why:
            return why
        if v is None:
            continue
        if not low <= v <= high:
            return f"{name}: {low} to {high}"
        out[name] = v
    low, high = out.get("min_replicas", MIN_DEFAULT), out.get("max_replicas", MAX_DEFAULT)
    if low > high:
        return f"min_replicas ({low}) is more than max_replicas ({high})"
    if low == 0 and out.get("sleep_when_idle") is not True:
        return "min_replicas: 0 only with sleep_when_idle on (a deployment goes to 0 replicas by sleeping)"
    return None


def clamp(count: int, s: Setting) -> int:
    """The number of replicas a person's change leaves, with automatic replicas on: the current one, within the
    bounds."""
    return min(max(count, s.floor), s.max)


# ------------------------------------------------------------------ what it reads from the gateway
class Signals(Protocol):
    """The gateway's view the autoscaler reads and the one thing it asks of it (draining a replica)."""

    def in_flight(self, uid: str) -> int: ...
    def waiting(self) -> dict[str, int]: ...           # "<served name>#<tier>" -> requests waiting for a slot
    def drain(self, uid: str) -> None: ...             # send it nothing new
    def undrain(self, uid: str) -> None: ...
    def drained(self, uid: str) -> bool: ...           # draining, and nothing in flight on it any more


class GatewaySignals:
    """The real gateway: the balancer's counts and drains, the waiting lines and the image queues."""

    def in_flight(self, uid: str) -> int:
        from forgeyard.gateway.balancer import balancer

        return balancer.snapshot(uid)["in_flight"]

    def waiting(self) -> dict[str, int]:
        from forgeyard.gateway import imagegen, waiting

        out = dict(waiting.lengths())
        for key, n in imagegen.lengths().items():
            out[key] = out.get(key, 0) + n
        return out

    def drain(self, uid: str) -> None:
        from forgeyard.gateway.balancer import balancer

        balancer.drain(uid)

    def undrain(self, uid: str) -> None:
        from forgeyard.gateway.balancer import balancer

        balancer.undrain(uid)

    def drained(self, uid: str) -> bool:
        from forgeyard.gateway.balancer import balancer

        return balancer.drained(uid)


# ------------------------------------------------------------------ the autoscaler
@dataclass
class Drain:
    uid: str
    ordinal: int
    since: float               # monotonic
    at: datetime               # wall time, for the page


@dataclass
class Watch:
    """One deployment's samples and where its scaling stands (this server's memory)."""

    since: float                                   # first sample of this watch (monotonic)
    changed: float                                 # last scaling change (or the watch's start)
    samples: deque = field(default_factory=deque)  # (monotonic time, load)
    drain: Optional[Drain] = None
    room: Optional[str] = None                     # why the one more it wants does not fit, while it does not
    paused: Optional[str] = None
    wanted: Optional[int] = None
    last: Optional[dict] = None                    # {"at", "text"} of the last decision


def capacity(params: Optional[dict]) -> int:
    """Requests one replica takes at once: its Concurrent requests, 1 when the recipe has none."""
    v = (params or {}).get("concurrency")
    return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 1


def is_video(recipe) -> bool:
    return recipe is not None and "video" in recipe.tasks


def _ready(r: Replica, w: Workload) -> bool:
    from forgeyard.models.routes import _ready as ready

    return ready(r, w)


def _starting(r: Replica, w: Workload) -> bool:
    """On its way to ready: pending (placed or waiting for room), starting, or running before its first health check
    passed. A replica in backoff, failed or unhealthy is not coming (it does not block one more)."""
    return not _ready(r, w) and r.state in ("pending", "starting", "running")


def _fmt(x: float) -> str:
    return f"{x:.2f}".rstrip("0").rstrip(".") if abs(x) < 100 else f"{x:.0f}"


def _window(seconds: float) -> str:
    return f"{int(seconds // 60)} min" if seconds >= 120 and seconds % 60 == 0 else f"{int(seconds)} s"


class Autoscaler:
    def __init__(self, signals: Optional[Signals] = None, clock: Callable[[], float] = time.monotonic):
        self.signals: Signals = signals or GatewaySignals()
        self._clock = clock
        self._lock = threading.Lock()
        self._watch: dict[int, Watch] = {}

    def reset(self) -> None:
        """Tests: forget every watch (the balancer's own reset ends its drains)."""
        with self._lock:
            self._watch.clear()

    # -------------------------------------------------------------- reading
    def status(self, d: Deployment) -> dict:
        """The setting and where it stands, for the API (the deployment page shows it)."""
        s = setting(d.params)
        w = d.workload
        live = [r for r in w.replicas if not r.final]
        with self._lock:
            watch = self._watch.get(d.id)
            out = {"on": s.on, "min": s.min, "max": s.max, "target": s.target, "scale_up_seconds": s.up_seconds,
                   "scale_down_minutes": s.down_minutes, "capacity": capacity(d.params), "replicas": len(live),
                   "ready": sum(1 for r in live if _ready(r, w)), "load": None, "per_replica": None, "wanted": None,
                   "draining": None, "room": None, "paused": None, "last": None}
            if not s.on or watch is None:
                return out
            now = self._clock()
            load = _average(watch.samples, now, s.up_seconds)
            draining = watch.drain.uid if watch.drain is not None else None
            ready = sum(1 for r in live if _ready(r, w) and r.uid != draining)
            out.update(load=None if load is None else round(load, 2),
                       per_replica=None if load is None or not ready else round(load / ready, 2),
                       wanted=watch.wanted, room=watch.room, paused=watch.paused, last=watch.last)
            if watch.drain is not None:
                out["draining"] = {"replica": watch.drain.ordinal, "since": watch.drain.at,
                                   "in_flight": self.signals.in_flight(watch.drain.uid)}
            return out

    def draining(self) -> dict[int, int]:
        """Deployment id -> the ordinal of the replica it drains (for /metrics)."""
        with self._lock:
            return {dep: w.drain.ordinal for dep, w in self._watch.items() if w.drain is not None}

    def loads(self) -> dict[int, float]:
        """Deployment id -> its latest load sample (for /metrics)."""
        with self._lock:
            return {dep: w.samples[-1][1] for dep, w in self._watch.items() if w.samples}

    # -------------------------------------------------------------- one pass
    def tick(self, wall: Optional[datetime] = None) -> list[str]:
        """Sample every automatic deployment's load and make at most one change to each. Returns what was done
        (the events' texts)."""
        from forgeyard.machines.hub import hub
        from forgeyard.workloads import placement

        wall = wall or utcnow()
        now = self._clock()
        done: list[str] = []
        nodes: set[int] = set()
        place = False
        db = new_session()
        try:
            rows = db.execute(select(Deployment.id, Deployment.params).join(Workload, Deployment.workload_id == Workload.id)
                              .where(Workload.desired_state.in_(("running", "asleep")))).all()
            # Only those with automatic replicas on (a watch of one turned off, stopped or deleted ends below).
            ids = [dep_id for dep_id, params in rows if setting(params).on]
            shares = self._shares(db, self.signals.waiting()) if ids else {}
            seen: set[int] = set()
            for dep_id in ids:
                with placement._lock:   # not while a placement pass places replicas
                    db.expire_all()
                    d = db.scalar(select(Deployment).where(Deployment.id == dep_id).options(
                        selectinload(Deployment.workload).selectinload(Workload.replicas).selectinload(Replica.node)))
                    if d is None or d.workload is None:
                        continue
                    seen.add(dep_id)
                    text, poke, added = self._decide(db, d, shares.get(dep_id, 0.0), now, wall)
                    if text is None:
                        db.rollback()
                        continue
                    db.commit()
                    done.append(text)
                    nodes |= poke
                    place = place or added
            with self._lock:
                for dep_id in list(self._watch):
                    if dep_id not in seen:   # turned off, stopped by a person or deleted: its drain ends
                        self._forget(dep_id)
        finally:
            for node_id in nodes:
                hub.poke(node_id)
            if place:
                try:
                    placement.place_and_poke(db)
                finally:
                    db.close()
            else:
                db.close()
        return done

    def _shares(self, db: DbSession, lines: dict[str, int]) -> dict[int, float]:
        """Requests waiting, per deployment: each line's length shared by the deployments of its tier."""
        if not any(lines.values()):
            return {}
        tiers: dict[str, list[int]] = {}
        rows = db.execute(select(GatewayRoute.served_name, RouteTarget.tier, RouteTarget.deployment_id)
                          .join(RouteTarget, RouteTarget.route_id == GatewayRoute.id)
                          .where(RouteTarget.kind == "local", RouteTarget.enabled.is_(True),
                                 RouteTarget.deployment_id.is_not(None))).all()
        for served, tier, dep in rows:
            tiers.setdefault(f"{served}#{tier}", []).append(dep)
        out: dict[int, float] = {}
        for key, n in lines.items():
            deps = tiers.get(key)
            if not n or not deps:
                continue
            for dep in deps:
                out[dep] = out.get(dep, 0.0) + n / len(deps)
        return out

    def _drain_of(self, dep_id: int) -> Optional[Drain]:
        with self._lock:
            watch = self._watch.get(dep_id)
            return watch.drain if watch is not None else None

    def _forget(self, dep_id: int) -> None:
        watch = self._watch.pop(dep_id, None)
        if watch is not None and watch.drain is not None:
            self.signals.undrain(watch.drain.uid)

    def _decide(self, db: DbSession, d: Deployment, waiting: float, now: float,
                wall: datetime) -> tuple[Optional[str], set[int], bool]:
        """At most one change to this deployment: (its event's text or None, machines to poke, a row added)."""
        from forgeyard.models.catalog import get_recipe

        w = d.workload
        s = setting(d.params)
        if not s.on or is_video(get_recipe(db, d.recipe)):
            with self._lock:
                self._forget(d.id)
            return None, set(), False
        if w.desired_state != "running":
            with self._lock:
                self._forget(d.id)
            return self._trim_asleep(db, d, s)
        live = sorted((r for r in w.replicas if not r.final), key=lambda r: r.ordinal)
        # What the replicas that stay must take: not what a replica being drained still runs (it finishes there; counted,
        # its stream would ask for the replica back the moment it is removed).
        drain = self._drain_of(d.id)
        going = drain.uid if drain is not None else None
        load = waiting + sum(self.signals.in_flight(r.uid) for r in live if r.uid != going)
        with self._lock:
            watch = self._watch.get(d.id)
            if watch is None:
                watch = self._watch[d.id] = Watch(since=now, changed=now)
            watch.samples.append((now, load))
            while watch.samples and watch.samples[0][0] < now - KEEP_SAMPLES:
                watch.samples.popleft()
        cap = capacity(d.params) * s.target / 100
        up_avg = _average(watch.samples, now, s.up_seconds)
        watch.wanted = None if up_avg is None else min(max(math.ceil(up_avg / cap - 1e-9), s.floor), s.max)

        paused = self._paused(w, live, wall)
        if paused is not None:
            watch.paused = paused
            if watch.drain is not None:
                return self._keep(db, d, watch, f"Kept replica {watch.drain.ordinal}: {paused}.")
            return None, set(), False
        watch.paused = None

        if watch.drain is not None:
            return self._draining(db, d, s, watch, live, cap, now)
        if any(_starting(r, w) for r in live):
            return None, set(), False   # one at a time: the next once this one is ready (or failed)
        ready = [r for r in live if _ready(r, w)]
        if len(live) < s.floor:
            return self._add(db, d, s, watch, live, now, f"{len(live)} replica{'s' if len(live) != 1 else ''}, "
                                                          f"fewer than the minimum {s.floor}")
        if not ready:
            return None, set(), False
        why = _up_reason(watch.samples, watch.since, s, cap, len(ready), now)
        if why is not None and len(live) < s.max:
            return self._add(db, d, s, watch, live, now, why)
        watch.room = None
        down = s.down_minutes * 60
        if len(live) > s.floor and now - watch.changed >= down and now - watch.since >= down:
            victim = live[-1]
            serving = len(ready) - (1 if victim in ready else 0)
            avg = _average(watch.samples, now, down)
            # Not in a burst either: one fewer must not be busy enough now to want the replica back at once.
            if serving >= 1 and avg is not None and avg <= serving * cap + 1e-9 \
                    and _up_reason(watch.samples, watch.since, s, cap, serving, now) is None:
                return self._start_drain(db, d, s, watch, victim, avg, len(ready), now, wall)
        return None, set(), False

    def _paused(self, w: Workload, live: list[Replica], wall: datetime) -> Optional[str]:
        from forgeyard.workloads import offline

        if any(r.generation < w.generation for r in live):
            return "an edit is restarting its replicas"
        if any(r.node is not None and offline.skipped(r.node, wall) for r in live):
            return "a replica's machine is offline"
        if w.updated_at is not None and wall - as_utc(w.updated_at) < PAUSE_AFTER_CHANGE:
            return "it was started or changed less than 2 minutes ago"
        return None

    # -------------------------------------------------------------- changes
    def _note(self, watch: Watch, text: str, wall: Optional[datetime] = None) -> None:
        watch.last = {"at": wall or utcnow(), "text": text}

    def _add(self, db: DbSession, d: Deployment, s: Setting, watch: Watch, live: list[Replica], now: float,
             why: str) -> tuple[Optional[str], set[int], bool]:
        from forgeyard.workloads import state as wstate

        w = d.workload
        room = self._room(db, d, live)
        if room is not None:
            text = f"Wants {len(live) + 1} replicas, room for {len(live)}: {room}"[:400]
            first = watch.room is None
            watch.room = text   # the page shows the reason as it is now; the events say it once per episode
            if first:
                wstate.event(db, w, "scaled", text)
                return text, set(), False
            return None, set(), False
        watch.room = None
        n = len(live) + 1
        ordinal = max((r.ordinal for r in w.replicas), default=-1) + 1
        w.replicas.append(Replica(ordinal=ordinal, generation=w.generation, state="pending", gpus=[], ports=[]))
        spec = dict(w.spec or {})
        spec["replicas"] = len(w.replicas)
        w.spec = spec
        text = f"Scaled up to {n}: {why}."
        wstate.event(db, w, "scaled", text)
        watch.changed = now
        self._note(watch, text)
        _count(d.name, "up")
        log.info("Deployment %s: %s", d.name, text)
        return text, set(), True

    def _room(self, db: DbSession, d: Deployment, live: list[Replica]) -> Optional[str]:
        """None when one more replica fits somewhere now (online; on the pinned machine when pinned), else why
        not."""
        from forgeyard.models import scheduler
        from forgeyard.models.catalog import context_for

        ctx = context_for(db, d)
        if ctx is None:
            return "its recipe is gone"
        verdicts = scheduler.evaluate(db, ctx)
        siblings = {r.node_id for r in live if r.node_id is not None}
        opt = scheduler.choose(verdicts, ctx.machine_id, siblings=siblings)
        if opt is not None and opt.node.status == "online":
            return None
        if opt is not None:
            return f"{opt.node.name} (pinned) is {opt.node.status}"
        return scheduler.summary(verdicts, ctx)

    def _start_drain(self, db: DbSession, d: Deployment, s: Setting, watch: Watch, victim: Replica, avg: float,
                     ready: int, now: float, wall: datetime) -> tuple[Optional[str], set[int], bool]:
        from forgeyard.workloads import state as wstate

        self.signals.drain(victim.uid)
        watch.drain = Drain(victim.uid, victim.ordinal, now, wall)
        busy = self.signals.in_flight(victim.uid)
        cap = capacity(d.params) * s.target / 100
        text = (f"Draining replica {victim.ordinal}: load {_fmt(avg / max(1, ready))} per replica over "
                f"{_window(s.down_minutes * 60)} (target {_fmt(cap)}); it takes no new requests and is removed once "
                f"{'the ' + str(busy) + ' in flight finish' if busy != 1 else 'the 1 in flight finishes'}."
                if busy else
                f"Draining replica {victim.ordinal}: load {_fmt(avg / max(1, ready))} per replica over "
                f"{_window(s.down_minutes * 60)} (target {_fmt(cap)}); it takes no new requests and nothing is in "
                "flight on it.")
        wstate.event(db, d.workload, "scaled", text, victim.ordinal)
        self._note(watch, text, wall)
        log.info("Deployment %s: %s", d.name, text)
        return text, set(), False

    def _keep(self, db: DbSession, d: Deployment, watch: Watch, text: str) -> tuple[Optional[str], set[int], bool]:
        from forgeyard.workloads import state as wstate

        drain = watch.drain
        watch.drain = None
        if drain is None:
            return None, set(), False
        self.signals.undrain(drain.uid)
        watch.changed = self._clock()   # a full down window again before the next drain
        if not any(r.uid == drain.uid for r in d.workload.replicas):
            return None, set(), False   # a person removed it meanwhile
        wstate.event(db, d.workload, "scaled", text, drain.ordinal)
        self._note(watch, text)
        return text, set(), False

    def _draining(self, db: DbSession, d: Deployment, s: Setting, watch: Watch, live: list[Replica], cap: float,
                  now: float) -> tuple[Optional[str], set[int], bool]:
        from forgeyard.workloads import placement, state as wstate

        w = d.workload
        drain = watch.drain
        r = next((x for x in live if x.uid == drain.uid), None)
        if r is None:   # gone (a person's edit, or it ended): nothing to remove
            self.signals.undrain(drain.uid)
            watch.drain = None
            return None, set(), False
        others = [x for x in live if x.uid != drain.uid and _ready(x, w)]
        if not others:
            return self._keep(db, d, watch, f"Kept replica {drain.ordinal}: no other replica is ready.")
        why = _up_reason(watch.samples, drain.since, s, cap, len(others), now)   # samples since the drain began
        if why is not None:
            return self._keep(db, d, watch, f"Kept replica {drain.ordinal}: {why}.")
        took = now - drain.since
        left = self.signals.in_flight(drain.uid)
        if not self.signals.drained(drain.uid) and took < DRAIN_MAX_SECONDS:
            return None, set(), False
        nodes = {r.node_id} if r.node_id is not None and not r.final else set()
        placement.forget(r.id)
        w.replicas.remove(r)   # delete-orphan: the row goes; its agent stops it
        spec = dict(w.spec or {})
        spec["replicas"] = len(w.replicas)
        w.spec = spec
        n = len(live) - 1
        text = (f"Removed replica {drain.ordinal} after draining for {_fmt(took)} s; {n} replica"
                f"{'s' if n != 1 else ''} now." if not left else
                f"Removed replica {drain.ordinal} after draining for {int(took // 60)} min with {left} request"
                f"{'s' if left != 1 else ''} still in flight; {n} replica{'s' if n != 1 else ''} now.")
        wstate.event(db, w, "scaled", text, drain.ordinal)
        self.signals.undrain(drain.uid)
        watch.drain = None
        watch.changed = now
        self._note(watch, text)
        _count(d.name, "down")
        log.info("Deployment %s: %s", d.name, text)
        return text, nodes, False

    def _trim_asleep(self, db: DbSession, d: Deployment, s: Setting) -> tuple[Optional[str], set[int], bool]:
        """An asleep deployment keeps its minimum of rows (at least 1), so it wakes with as many; the others go
        (they are stopped: nothing to drain)."""
        from forgeyard.workloads import placement, state as wstate

        w = d.workload
        rows = sorted(w.replicas, key=lambda r: r.ordinal)
        extra = [r for r in rows[s.floor:] if r.state == "stopped" or r.node_id is None or r.final]
        if len(rows) <= s.floor or len(extra) != len(rows) - s.floor:
            return None, set(), False   # some are still stopping: next time
        for r in extra:
            placement.forget(r.id)
            w.replicas.remove(r)
        spec = dict(w.spec or {})
        spec["replicas"] = len(w.replicas)
        w.spec = spec
        text = (f"Asleep with {len(w.replicas)} replica{'s' if len(w.replicas) != 1 else ''} (its minimum): it wakes "
                "with as many.")
        wstate.event(db, w, "scaled", text)
        return text, set(), False


def _up_reason(samples, since: float, s: Setting, cap: float, ready: int, now: float) -> Optional[str]:
    """Why one more replica is wanted (or a drained one kept), or None: the load over the up window above what
    `ready` replicas take at the target, or over the last PANIC_SECONDS at least twice that. A window counts only
    once the samples cover it (`since`: the first of them)."""
    target = f"target {_fmt(cap)} of {_fmt(cap * 100 / s.target)} concurrent"
    if now - since >= s.up_seconds:
        avg = _average(samples, now, s.up_seconds)
        if avg is not None and avg > ready * cap + 1e-9:
            return f"load {_fmt(avg / ready)} per replica for {_window(s.up_seconds)} ({target})"
    if now - since >= PANIC_SECONDS:
        avg = _average(samples, now, PANIC_SECONDS)
        if avg is not None and avg >= 2 * ready * cap - 1e-9:
            return f"load {_fmt(avg / ready)} per replica for {_window(PANIC_SECONDS)}, twice the {target}"
    return None


def _average(samples, now: float, seconds: float) -> Optional[float]:
    xs = [load for t, load in samples if t > now - seconds - 1e-9]
    return sum(xs) / len(xs) if xs else None


def _count(name: str, direction: str) -> None:
    from forgeyard import metrics

    metrics.scaled(name, direction)


autoscaler = Autoscaler()


async def run_forever(interval: float = SAMPLE_SECONDS) -> None:
    while True:
        try:
            await asyncio.to_thread(autoscaler.tick)
        except Exception:  # keep scaling whatever one round hit (a locked database, say)
            log.exception("Autoscaling pass failed")
        await asyncio.sleep(interval)
