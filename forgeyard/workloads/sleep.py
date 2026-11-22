"""Sleep when idle (1.5.0, #37): a deployment nobody calls stops its replicas and frees what they hold.

- **Settings** (in the deployment's parameters, read by the server only, so changing them restarts nothing):
  `sleep_when_idle` (off by default, for every deployment old and new) and `idle_minutes` (30 by default,
  1 to 1,440).
- **Asleep** is a third desired state of the deployment's workload, beside `running` and `stopped` (no
  migration: the column takes any short text). Its replicas are stopped exactly as for *Stop*: the rows stay,
  the agent stops the engines, and their reservations are free once it confirms; the model files stay in the
  machine's model cache and the image in its engine's store. Everything that asks `!= "running"` (the agents'
  work lists, placement, what a card holds, routing) treats it as stopped; only the console, the API and
  /v1/models tell the two apart: an asleep model stays listed.
- **What counts as use**: a request the gateway takes for a route this deployment serves, from any key or the
  Playground, also one that then went to a cloud tier, while it starts, while it waits in line or in the image
  queue, and when it finishes; a video job asked; *Try it*. Not: /v1/models, health checks, the console's
  polling, polls of a video job.
- **When it sleeps**: the idle sweeper (every SWEEP_SECONDS) puts a deployment to sleep once it is on, the
  deployment is running, and nothing used it for `idle_minutes`, counted from the last use, or from when the
  deployment was started or changed or a replica of it became ready, or from the server's own start, whichever
  is latest (last uses are kept in this server's memory: a restart never puts anything to sleep early). Not
  while a request is in flight or waiting, a replica is starting, an edit is restarting its replicas, or (video)
  a job is waiting or being made, or a finished video is still kept for download.
- **Waking**: *Start* on an asleep deployment wakes it (on its pinned machine when it is pinned, else where it
  ran when that still fits). In 1.5.0 PR 2 a request to an asleep model answers 503 `model_asleep`; waking on a
  request comes next. *Stop* on an asleep deployment makes it stopped: a person's Stop stays stopped.
- **Downgrade**: an older server reads `asleep` as stopped (not listed, 503 "stopped", Start works).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard.db import Deployment, Replica, VideoJob, Workload, as_utc, new_session, utcnow

log = logging.getLogger("forgeyard.workloads")

ASLEEP = "asleep"
IDLE_DEFAULT, IDLE_MIN, IDLE_MAX = 30, 1, 1440   # minutes (Tony, #37)
SWEEP_SECONDS = 30.0
PARAMS = ("sleep_when_idle", "idle_minutes")
GIB = 1024**3


class IdleClock:
    """When each deployment was last used, and how many gateway requests for it are under way (this server's
    memory). Thread-safe: the gateway sets it on the event loop, the sweeper reads it in a thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.reset()

    def reset(self, at: Optional[datetime] = None) -> None:
        with self._lock:
            self._last: dict[int, datetime] = {}
            self._active: dict[int, int] = {}
            self._started = at or utcnow()

    def server_started(self, at: Optional[datetime] = None) -> None:
        """The server (re)started: every idle time counts from now at the earliest."""
        with self._lock:
            self._started = at or utcnow()

    @property
    def started(self) -> datetime:
        return self._started

    def used(self, ids: Iterable[int], at: Optional[datetime] = None) -> None:
        at = at or utcnow()
        with self._lock:
            for i in ids:
                if i is not None:
                    self._last[i] = at

    def begin(self, ids: Iterable[int]) -> list[int]:
        """A request for these deployments starts: used now, and in flight until end()."""
        ids = [i for i in dict.fromkeys(ids) if i is not None]
        now = utcnow()
        with self._lock:
            for i in ids:
                self._last[i] = now
                self._active[i] = self._active.get(i, 0) + 1
        return ids

    def end(self, ids: Iterable[int]) -> None:
        now = utcnow()
        with self._lock:
            for i in ids:
                self._last[i] = now
                left = self._active.get(i, 0) - 1
                if left > 0:
                    self._active[i] = left
                else:
                    self._active.pop(i, None)

    def active(self, dep_id: int) -> int:
        with self._lock:
            return self._active.get(dep_id, 0)

    def last(self, dep_id: int) -> Optional[datetime]:
        """The last use this server saw (None: none since it started)."""
        with self._lock:
            return self._last.get(dep_id)


idle = IdleClock()


def route_deployments(route) -> list[int]:
    """The deployments a gateway route serves locally (every tier)."""
    return [g.deployment_id for tier in route.tiers for g in tier.locals]


# ------------------------------------------------------------------ settings
@dataclass
class Setting:
    on: bool
    idle_minutes: int


def setting(params: Optional[dict]) -> Setting:
    p = params or {}
    minutes = p.get("idle_minutes")
    ok = isinstance(minutes, int) and not isinstance(minutes, bool) and IDLE_MIN <= minutes <= IDLE_MAX
    return Setting(on=p.get("sleep_when_idle") is True, idle_minutes=minutes if ok else IDLE_DEFAULT)


def normalise(raw: dict, out: dict) -> Optional[str]:
    """Check the two parameters into `out`. Returns why they are refused, or None. `sleep_when_idle` is kept only
    when on; `idle_minutes` is kept as given (also while off, for when it is turned on)."""
    on = raw.get("sleep_when_idle")
    if on not in (None, ""):
        if not isinstance(on, bool):
            return "sleep_when_idle: true or false"
        if on:
            out["sleep_when_idle"] = True
    minutes = raw.get("idle_minutes")
    if minutes not in (None, ""):
        try:
            minutes = int(str(minutes).strip())   # true, 1.5 and "soon" are not whole numbers of minutes
        except ValueError:
            return "idle_minutes must be a whole number of minutes"
        if not IDLE_MIN <= minutes <= IDLE_MAX:
            return f"idle_minutes: {IDLE_MIN} to {IDLE_MAX:,} minutes"
        out["idle_minutes"] = minutes
    return None


# ------------------------------------------------------------------ is it idle?
def idle_since(d: Deployment, w: Workload) -> datetime:
    """From when its idle time counts: its last use, its last start or change, a replica becoming ready, or the
    server's start, whichever is latest."""
    times = [as_utc(idle.started)]
    if (last := idle.last(d.id)) is not None:
        times.append(as_utc(last))
    if w.updated_at is not None:
        times.append(as_utc(w.updated_at))
    times += [as_utc(r.ready_at) for r in w.replicas if r.ready_at is not None and not r.final]
    return max(times)


def in_flight(d: Deployment, w: Workload) -> int:
    """Requests for it under way in this server: in the gateway (also waiting in line or in the image queue) and
    on its replicas (a stream still being passed on)."""
    from forgeyard.gateway.balancer import balancer

    return idle.active(d.id) + sum(balancer.snapshot(r.uid)["in_flight"] for r in w.replicas)


def video_hold(db: DbSession, d: Deployment, now: datetime) -> Optional[str]:
    """Why a video deployment must stay awake: a job waiting or being made on it, or a finished video still kept
    for download (stopping the replica would delete it). None otherwise (#37, Q8)."""
    from forgeyard.gateway.videogen import ACTIVE

    rows = db.scalars(select(VideoJob).where(VideoJob.deployment_id == d.id, VideoJob.removed.is_(False))).all()
    if any(j.status in ACTIVE for j in rows):
        return "a video is waiting or being made"
    kept = [as_utc(j.expires_at) for j in rows
            if j.status == "completed" and j.expires_at is not None and as_utc(j.expires_at) > now]
    if kept:
        return f"a finished video is kept until {max(kept).strftime('%Y-%m-%d %H:%M')} UTC"
    return None


def hold(db: DbSession, d: Deployment, w: Workload, now: datetime) -> Optional[str]:
    """Why it does not go to sleep right now although it may be idle long enough; None when nothing holds it."""
    if in_flight(d, w):
        return "a request is in flight"
    live = [r for r in w.replicas if not r.final]
    if any(r.generation < w.generation for r in live):
        return "an edit is restarting its replicas"
    if any(r.state == "starting" or (r.state == "pending" and r.node_id is not None) for r in live):
        return "a replica is starting"
    return video_hold(db, d, now)


def status(db: DbSession, d: Deployment, now: Optional[datetime] = None) -> dict:
    """The deployment's sleep setting and where it stands, for the API (the console shows it)."""
    now = now or utcnow()
    w = d.workload
    s = setting(d.params)
    last = idle.last(d.id)
    out = {"on": s.on, "idle_minutes": s.idle_minutes, "asleep": w.desired_state == ASLEEP,
           "last_request_at": last, "since": idle_since(d, w), "sleeps_at": None, "held_by": None}
    if s.on and w.desired_state == "running":
        out["sleeps_at"] = out["since"] + timedelta(minutes=s.idle_minutes)
        out["held_by"] = hold(db, d, w, now)
    return out


# ------------------------------------------------------------------ going to sleep
def _frees(w: Workload) -> str:
    parts = []
    for r in sorted(w.replicas, key=lambda x: x.ordinal):
        if r.final or r.node_id is None or r.state == "stopped" or not r.vram:
            continue
        where = r.node.name if r.node is not None else f"machine {r.node_id}"
        size = f"{r.vram / GIB:.1f} GiB"
        cards = [f"#{g.get('index')}" for g in r.gpus or [] if g.get("vendor") != "cpu"]
        parts.append(f"{size} on {where} GPU {', '.join(cards)}" if cards else f"{size} of RAM on {where}")
    return "; ".join(parts) if parts else "nothing it held"


def put_to_sleep(db: DbSession, d: Deployment, connected, minutes: int) -> set[int]:
    """Stop its replicas as Stop does, with the desired state `asleep`, and say so in its events (not audited: no
    person did it). Returns the machines to poke."""
    from forgeyard.workloads import state as wstate

    w = d.workload
    frees = _frees(w)
    nodes = wstate.stop(db, w, connected, to=ASLEEP)
    wstate.event(db, w, ASLEEP, f"Asleep: no requests for {minutes} min; frees {frees}. "
                                "The model files stay on the machine; Start wakes it.")
    return nodes


def sweep_once(now: Optional[datetime] = None) -> list[str]:
    """One pass: every running deployment with sleep when idle on that has been idle long enough goes to sleep.
    Returns their names."""
    from forgeyard.machines.hub import hub
    from forgeyard.workloads import placement

    now = now or utcnow()
    slept: list[str] = []
    nodes: set[int] = set()
    db = new_session()
    try:
        ids = db.scalars(select(Deployment.id).join(Workload, Deployment.workload_id == Workload.id)
                         .where(Workload.desired_state == "running")).all()
        for dep_id in ids:
            with placement._lock:   # not while a placement pass places its replicas
                db.expire_all()
                d = db.scalar(select(Deployment).where(Deployment.id == dep_id).options(
                    selectinload(Deployment.workload).selectinload(Workload.replicas).selectinload(Replica.node)))
                if d is None or d.workload is None or d.workload.desired_state != "running":
                    continue
                s = setting(d.params)
                if not s.on or now - idle_since(d, d.workload) < timedelta(minutes=s.idle_minutes):
                    continue
                if hold(db, d, d.workload, now) is not None:
                    continue
                seen = idle.last(d.id)
                stopping = put_to_sleep(db, d, hub.connected, s.idle_minutes)
                if idle.last(d.id) != seen or idle.active(d.id):
                    db.rollback()   # a request came in meanwhile: it stays awake
                    continue
                db.commit()
                nodes |= stopping
                slept.append(d.name)
                log.info("Deployment %s is asleep: no requests for %s min.", d.name, s.idle_minutes)
        if slept:
            for node_id in nodes:
                hub.poke(node_id)
            placement.place_and_poke(db)  # its memory is free once confirmed; anything waiting gets a try
    finally:
        db.close()
    return slept


async def run_forever(interval: float = SWEEP_SECONDS) -> None:
    while True:
        try:
            await asyncio.to_thread(sweep_once)
        except Exception:  # keep sweeping whatever one round hit (a locked database, say)
            log.exception("Idle sweep failed")
        await asyncio.sleep(interval)
