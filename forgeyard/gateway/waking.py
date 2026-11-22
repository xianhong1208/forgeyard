"""Waking an asleep model on a request (1.5.0, #37; Tony's Q3 A). forgeyard/workloads/sleep.py puts a deployment
nobody uses to sleep; this is how a request brings it back.

- **One wake per deployment** however many requests arrive (single flight): the first request that finds it
  asleep wakes it, in a thread, the way *Start* does (a new generation, back to the machine it ran on when that
  still fits, the event "Woken by a request (key …)"); every other one sees the wake under way. While the
  replicas the sweeper stopped are still stopping, the wake is asked again until they have stopped.
- **Spill meanwhile**: a request that may move on (a later tier, or a cloud target of its tier, that its key
  may use: `fallback` or `on`) does so at once, with `X-Forgeyard-Spilled: waking`, as for a tier without a
  ready replica; the next requests go local again once a replica is ready. Structured output that a provider
  would lose is never spilled (forgeyard/gateway/proxy.py, step 8), and a request whose cloud targets its
  budgets block, or that every later target failed, waits after all, as a busy tier's does.
- **Otherwise it waits** in the wake line of its route tier (`<served name>#<tier>`), first come first served, at
  most MAX_WAITERS per line and the deployment's `wake_wait` seconds (300 by default, 10 to 1,800). Only the first
  in line looks how the wake goes (the route loaded again, at most once per LOOK_TTL for everyone); when a replica
  is ready it leaves the line, which lets the next one look, and goes on into the normal path (the fair line of
  #169 included), so the first to arrive is the first served. An agent's status wakes the first in every line to
  look at once (a replica became ready, an engine failed); without one they look again every waiting.RECHECK.
- **Nothing is sent to the client while it waits**: the answer's status comes from the model, as for every
  request (a stream starts with a normal 200, or a real error). A replica's `first_answer_timeout` counts only
  from the moment the request is written to it.
- **Give up at once, honestly**: when the wake cannot succeed, 503 `cannot_wake` with the reason: no machine has
  room ("Cannot be placed: …", the scheduler's own words), the machine it is pinned to (or waits for) is offline,
  its engine is failing (backoff, failed, exited). A person's Stop meanwhile: 503 `no_ready_replica` "stopped".
- **When the wait runs out**: 503 `model_waking` with `Retry-After`: the deployment's last start time
  (forgeyard/workloads/starts.py, when the server measured one) minus what has passed since the wake began, at
  least RETRY_AFTER_MIN; START_DEFAULT seconds when no start was measured yet. So is a line that is full.
- **A client that goes away leaves the line**; the wake carries on (the model was asked for).
- **Not woken**: a deployment a person stopped, /v1/models, health checks, the console's polling.
- Rate limits admit a request once, before it waits (forward()); the time it waited is in `/metrics`
  (`forgeyard_gateway_wake_wait_seconds`, `forgeyard_gateway_waking`, `forgeyard_deployment_wakes_total`).

Everything here is this server's memory: after a restart a deployment woken before it is simply starting
(requests get 503 no_ready_replica until it is ready), and one still asleep is woken by the next request.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from starlette.concurrency import run_in_threadpool

from forgeyard import metrics
from forgeyard.db import Deployment, Replica, Workload, new_session, utcnow
from forgeyard.gateway import waiting
from forgeyard.gateway.errors import GatewayError

log = logging.getLogger("forgeyard.gateway")

MAX_WAITERS = 256        # requests waiting per wake line; more get 503 model_waking at once
LOOK_TTL = 1.0           # seconds a look at the wake's progress is shared by everyone in its lines
RETRY_AFTER_MIN = 5      # seconds, the least Retry-After of a 503 model_waking
START_DEFAULT = 30       # seconds a start is expected to take when none was measured yet
FAILING = ("backoff", "failed", "exited")   # an engine that does not come up: the wake cannot succeed


@dataclass
class Wake:
    """A deployment being woken (by a request, or by a person's Start: Wake now)."""

    dep_id: int
    name: str
    by: str
    since: datetime = field(default_factory=utcnow)
    t0: float = field(default_factory=time.monotonic)
    woken: bool = False          # its desired state is `running` again
    workload_id: Optional[int] = None
    asking: bool = False         # a thread is waking it right now (single flight)
    expected: float = START_DEFAULT   # seconds its start is expected to take (its last start)
    waiting: int = 0             # requests waiting for it


@dataclass
class Look:
    """How the wake of a route tier's deployments goes, as of `at` (monotonic)."""

    state: str                   # ready | waiting | cannot | stopped | gone
    fresh: dict = field(default_factory=dict)   # deployment id -> its LocalGroup, loaded again
    reason: str = ""
    at: float = field(default_factory=time.monotonic)


_lock = threading.Lock()
_wakes: dict[int, Wake] = {}
_dep_locks: dict[int, threading.Lock] = {}
_lines: dict[str, "WakeLine"] = {}
_looks: dict[tuple, Look] = {}


class WakeLine(waiting.Line):
    """The requests waiting for a route tier's deployments to wake, in the order they came."""

    def waited(self, seconds: float) -> None:
        metrics.wake_waited(self.key, seconds)


def line(key: str) -> WakeLine:
    with _lock:
        found = _lines.get(key)
        if found is None:
            found = _lines[key] = WakeLine(key)
        return found


def lengths() -> dict[str, int]:
    """How many requests wait in each wake line now (/metrics)."""
    with _lock:
        lines = list(_lines.items())
    return {key: len(ln) for key, ln in lines}


def reset() -> None:
    """Tests: forget every wake, line and look."""
    with _lock:
        _wakes.clear()
        _dep_locks.clear()
        _lines.clear()
        _looks.clear()


def forget(dep_id: int) -> None:
    """Its wake is over without a request seeing it ready: it went to sleep again, a person stopped it, or it was
    removed."""
    with _lock:
        _wakes.pop(dep_id, None)
        _looks.clear()


def forget_workload(workload_id: int) -> None:
    db = new_session()
    try:
        dep_id = db.scalar(select(Deployment.id).where(Deployment.workload_id == workload_id))
    finally:
        db.close()
    if dep_id is not None:
        forget(dep_id)


def changed(ready_workloads=()) -> None:
    """An agent's status was stored (a replica may be ready, an engine may have failed): the first in every wake
    line looks at once. `ready_workloads`: workloads a replica of which became ready: their wakes are over."""
    ready = set(ready_workloads)
    with _lock:
        if ready:
            for dep_id in [i for i, wk in _wakes.items() if wk.workload_id in ready]:
                del _wakes[dep_id]
        if not _lines:
            return
        _looks.clear()
        lines = list(_lines.values())
    for ln in lines:
        ln.wake_first()


def wake_of(dep_id: int) -> Optional[Wake]:
    with _lock:
        return _wakes.get(dep_id)


def sleepy(groups: list) -> list:
    """The local groups a request should wait for: asleep, or being woken (running, no ready replica yet)."""
    with _lock:
        return [g for g in groups if not g.replicas and (g.asleep or (g.running and g.deployment_id in _wakes))]


def by_of(caller) -> str:
    """Who woke it, for its events: the key's name, or the Playground's user (never a key)."""
    if getattr(caller, "source", "") == "console":
        return f"a request from the Playground ({caller.username})"
    name = getattr(caller, "key_name", None)
    return f"a request (key {name})" if name else "a request"


# ------------------------------------------------------------------ the wake itself
def _last_start(db, workload_id: int) -> Optional[float]:
    """Seconds its last start took, when the server measured one (forgeyard/workloads/starts.py, 1.5.0)."""
    try:
        from forgeyard.workloads import starts
    except ImportError:       # pragma: no cover (an older tree without start times)
        return None
    try:
        got = starts.last_start(db, workload_id)
    except Exception:  # a start time is a hint for Retry-After: never fail a wake over it
        log.exception("Reading the last start time failed")
        return None
    seconds = (got or {}).get("seconds") if isinstance(got, dict) else None
    return float(seconds) if isinstance(seconds, (int, float)) and seconds > 0 else None


def _wake(dep_id: int, by: str) -> str:
    """Wake the deployment if it is asleep (in a thread). Returns woken | awake | stopping | stopped | gone."""
    from forgeyard.workloads import placement, state as wstate
    from forgeyard.workloads.sleep import ASLEEP

    with _lock:
        dep_lock = _dep_locks.setdefault(dep_id, threading.Lock())
    db = new_session()
    try:
        with dep_lock, placement._lock:   # not while the idle sweeper or a placement pass works on it
            d = db.scalar(select(Deployment).where(Deployment.id == dep_id).options(
                selectinload(Deployment.workload).selectinload(Workload.replicas)))
            if d is None or d.workload is None:
                return "gone"
            w = d.workload
            if w.desired_state == "running":
                return "awake"         # woken already (by another server thread, or a person's Start)
            if w.desired_state != ASLEEP:
                return "stopped"       # a person's Stop stays stopped
            if any(r.state == "stopping" for r in w.replicas):
                return "stopping"      # the sweeper's stop is not confirmed yet: asked again by the next look
            w.desired_state = "running"
            w.generation += 1
            w.updated_at = utcnow()
            for r in w.replicas:
                placement.remember(r)  # back to the machine it ran on, when that machine still takes it
                wstate.reset_replica(r, w.generation)
            wstate.event(db, w, "woken", f"Woken by {by} (generation {w.generation}).")
            expected = _last_start(db, w.id)
            db.commit()
            name, workload_id = d.name, w.id
        placement.place_and_poke(db)
    finally:
        db.close()
    with _lock:
        wk = _wakes.get(dep_id)
        if wk is not None:
            wk.woken, wk.workload_id = True, workload_id
            wk.expected = expected or START_DEFAULT
    metrics.woken(name, "request")
    log.info("Deployment %s was asleep; woken by %s.", name, by)
    return "woken"


def woken_by_person(db, w: Workload, by: str) -> None:
    """A person's Start (Wake now) woke an asleep deployment: requests that arrive while it starts wait for it."""
    d = db.scalar(select(Deployment).where(Deployment.workload_id == w.id))
    if d is None:
        return
    expected = _last_start(db, w.id)
    with _lock:
        _wakes[d.id] = Wake(d.id, d.name, by, woken=True, workload_id=w.id, expected=expected or START_DEFAULT)
        _looks.clear()
    metrics.woken(d.name, "person")


async def ask(groups: list, by: str) -> None:
    """Wake every asleep one of `groups` (once each, however many requests ask at the same time)."""
    for g in groups:
        with _lock:
            wk = _wakes.get(g.deployment_id)
            if wk is None:
                wk = _wakes[g.deployment_id] = Wake(g.deployment_id, g.name, by)
            lead = not wk.woken and not wk.asking
            if lead:
                wk.asking = True
        if not lead:
            continue
        try:
            outcome = await run_in_threadpool(_wake, g.deployment_id, by)
        finally:
            with _lock:
                wk.asking = False
        if outcome == "awake":
            with _lock:
                wk.woken = True
        elif outcome in ("stopped", "gone"):
            forget(g.deployment_id)


# ------------------------------------------------------------------ how it goes
def diagnose(db, dep_id: int) -> tuple[str, str]:
    """(waiting | cannot | stopped | gone | asleep, why) for a deployment with no ready replica."""
    from forgeyard.workloads import offline
    from forgeyard.workloads.sleep import ASLEEP
    from forgeyard.workloads.state import describe

    d = db.scalar(select(Deployment).where(Deployment.id == dep_id).options(
        selectinload(Deployment.workload).selectinload(Workload.replicas).selectinload(Replica.node)))
    if d is None or d.workload is None:
        return "gone", "its deployment was removed"
    w = d.workload
    if w.desired_state == ASLEEP:
        return "asleep", ""
    if w.desired_state != "running":
        return "stopped", "its deployment was stopped"
    live = [r for r in w.replicas if not r.final and r.generation == w.generation]
    if not live:
        return "cannot", f"{d.name} has no replica left to start"
    pinned = (w.spec or {}).get("machine_id")
    hopeless: list[str] = []
    for r in sorted(live, key=lambda x: x.ordinal):
        node = r.node
        if r.node_id is None:
            if (r.reason or "").startswith("Cannot be placed"):
                hopeless.append(r.reason)
                continue
            return "waiting", ""       # placement has not looked yet
        if node is not None and offline.skipped(node):
            hopeless.append(f"it is pinned to {node.name}, which is {node.status}" if pinned == r.node_id
                            else f"replica {r.ordinal} waits for {node.name}, which is {node.status}")
            continue
        if r.state in FAILING:
            where = node.name if node is not None else f"machine {r.node_id}"
            hopeless.append(f"replica {r.ordinal} on {where}: {describe(r.state, r.reason, r.exit_code, r.pid)}")
            continue
        return "waiting", ""           # placed and starting (downloading, loading the model…)
    return "cannot", "; ".join(hopeless)[:400]


def _look(served_name: str, dep_ids: tuple) -> Look:
    from forgeyard.gateway.route import load_route

    db = new_session()
    try:
        route = load_route(db, served_name)
        if route is None:
            return Look("gone", reason="its route was removed")
        fresh = {g.deployment_id: g for tier in route.tiers for g in tier.locals}
        if any(fresh[i].replicas for i in dep_ids if i in fresh):
            return Look("ready", fresh)
        verdicts = [diagnose(db, i) for i in dep_ids]
    finally:
        db.close()
    states = [s for s, _ in verdicts]
    if any(s in ("waiting", "asleep") for s in states):
        return Look("waiting", fresh)
    if "cannot" in states:
        return Look("cannot", fresh, "; ".join(why for s, why in verdicts if s == "cannot"))
    if "stopped" in states:
        return Look("stopped", fresh, "its deployment was stopped")
    return Look("gone", fresh, "its deployment was removed")


async def look(served_name: str, dep_ids: tuple) -> Look:
    """How the wake goes, shared for LOOK_TTL by everyone waiting for the same deployments."""
    key = (served_name, dep_ids)
    with _lock:
        cached = _looks.get(key)
    if cached is not None and time.monotonic() - cached.at < LOOK_TTL:
        return cached
    found = await run_in_threadpool(_look, served_name, dep_ids)
    with _lock:
        _looks[key] = found
    return found


# ------------------------------------------------------------------ answers
def retry_after(dep_ids) -> int:
    """Seconds until it is expected to be ready: its last start minus what has passed since the wake began."""
    now = time.monotonic()
    with _lock:
        wakes = [_wakes[i] for i in dep_ids if i in _wakes]
    if not wakes:
        return max(RETRY_AFTER_MIN, START_DEFAULT)
    return max(RETRY_AFTER_MIN, round(min(wk.expected - (now - wk.t0) for wk in wakes)))


def _names(groups: list) -> str:
    return ", ".join(dict.fromkeys(g.name for g in groups))


def waking_error(served_name: str, groups: list, *, waited: Optional[float] = None, full: int = 0) -> GatewayError:
    """503 model_waking: it is waking up and not ready yet (the wait ran out, or the line is full)."""
    after = retry_after([g.deployment_id for g in groups])
    if full:
        why = f"{full} requests already wait for it"
    elif waited is not None:
        why = f"it was not ready within {waited:g} s (its deployment's wake_wait)"
    else:
        why = "it is not ready yet"
    return GatewayError(503, "model_waking", f"{served_name!r} is waking up (its deployment {_names(groups)} was asleep "
                                             f"because nobody used it) and {why}. Try again in {after} s.",
                        retry_after=after)


def cannot_wake_error(served_name: str, groups: list, reason: str) -> GatewayError:
    return GatewayError(503, "cannot_wake", f"{served_name!r} cannot wake up now (its deployment {_names(groups)} was "
                                            f"asleep): {reason}.")


async def until_awake(receive, key: str, served_name: str, groups: list, sleepy_groups: list, by: str) -> list:
    """Wait in the wake line `key` until a replica of one of `groups` is ready; returns `groups` with their ready
    replicas (loaded again). Raises GatewayError (cannot_wake, model_waking, no_ready_replica) or ClientGone."""
    from forgeyard.gateway.upstream import unless_gone

    dep_ids = tuple(dict.fromkeys(g.deployment_id for g in groups))
    wait_s = float(max(g.wake_wait for g in sleepy_groups))
    ln = line(key)
    place = ln.join(MAX_WAITERS)
    if place is None:
        raise waking_error(served_name, sleepy_groups, full=len(ln))
    counted = [wake_of(g.deployment_id) for g in sleepy_groups]
    with _lock:
        for wk in counted:
            if wk is not None:
                wk.waiting += 1
    deadline = time.monotonic() + wait_s
    try:
        while True:
            if ln.may_try(place):
                seen = await look(served_name, dep_ids)
                if seen.state == "ready":
                    for i in dep_ids:
                        if seen.fresh.get(i) is not None and seen.fresh[i].replicas:
                            with _lock:
                                _wakes.pop(i, None)   # its wake is over: it serves
                    ln.leave(place)                   # the next one in line looks now
                    place = None
                    out = []
                    for g in groups:
                        f = seen.fresh.get(g.deployment_id)
                        out.append(replace(g, replicas=list(f.replicas), reason=f.reason, asleep=f.asleep,
                                           running=f.running) if f is not None else replace(g, replicas=[]))
                    return out
                if seen.state == "cannot":
                    raise cannot_wake_error(served_name, sleepy_groups, seen.reason)
                if seen.state in ("stopped", "gone"):
                    raise GatewayError(503, "no_ready_replica", f"{served_name!r} has no ready replica: "
                                                                f"{seen.reason}.")
                # Still asleep: the replicas the sweeper stopped had not stopped yet; ask again.
                asleep = [g for g in sleepy_groups if (f := seen.fresh.get(g.deployment_id)) is not None and f.asleep]
                if asleep:
                    await ask(asleep, by)
            left = deadline - time.monotonic()
            if left <= 0:
                raise waking_error(served_name, sleepy_groups, waited=wait_s)
            await unless_gone(receive, ln.wait(place, left))
    finally:
        ln.leave(place)
        with _lock:
            for wk in counted:
                if wk is not None:
                    wk.waiting = max(0, wk.waiting - 1)


def status(d, w) -> Optional[dict]:
    """For the deployment API: since when and by whom it is being woken, and how many requests wait for it; None
    when it is not being woken."""
    from forgeyard.workloads.state import is_ready

    with _lock:
        wk = _wakes.get(d.id)
    if wk is None or w.desired_state != "running":
        return None
    if any(is_ready(r) for r in w.replicas if not r.final and r.generation == w.generation):
        return None
    return {"since": wk.since, "by": wk.by, "waiting": wk.waiting, "expected_seconds": round(wk.expected)}
