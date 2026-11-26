"""GPU hours (1.6.1, #190): how much GPU time each deployment, team, machine and GPU model held, per UTC day.

**What counts.** Allocation, not utilisation, as Kubernetes, Slurm and Run:ai count it: a replica holds its cards (or its
share of a card) whether it answers one request or a thousand, and nobody else can use that memory.

- **When:** from the moment its engine starts until its reservation is freed: the replica is starting, running, healthy,
  unhealthy or in backoff (`COUNTED`) on a machine. Not while it downloads its model, engine or image (`download` set,
  or a fetching reason from an agent that sends no progress): on a slow network that can take hours, and the team would
  pay for the network. Not once it is asked to stop or its deployment goes to sleep (the replica is then `stopping`,
  then `stopped`): scale to zero is rewarded.
- **How much:** an exclusive card counts 1 per card; a shared card (`share: vram`) its reservation ÷ the card's total
  memory (20 GiB of an 80 GiB card: 0.25), at most 1; a card whose total is unknown counts whole. A CPU replica counts no
  GPU, but its reserved RAM × time (`ram_mib_ms`), so a report can price CPU work too.
- **Busy:** of that time, the part with at least one request in flight through this server (the balancer's
  `take_busy`), weighted like the allocation on GPU rows, so busy share = busy_ms ÷ gpu_ms.
- **Where:** one row per (UTC day, deployment, team, machine, GPU model) in `gpu_usage_daily`. A replica on cards of
  two models adds to two rows. The deployment's team when counted decides the row: moving it to another team starts
  new rows. An interval that crosses midnight (UTC) is split between the two days.

**How.** The usage recorder (forgeyard/gateway/usage.py) calls `Meter.collect` on each write, every FLUSH_SECONDS (5 s)
and before the Usage page reads: each counted replica adds the time since it was last seen counted. No agent change.
A replica seen counted for the first time adds nothing yet (it is counted from the next write on), and one that stopped
between two writes loses the time since the last one: at most one interval (5 s) at each end. **A server that is down
counts nothing** (the replicas may keep running): after a start, counting begins at the first write; a gap longer than
MAX_GAP (a server that was suspended, or could not write for a while) counts only MAX_GAP.

**Reading.** `summary` (the Usage page), `month_total` (a deployment's page; and what an extension that enforces
GPU-hour quotas reads: `month_total(db, team_id=…)`, from the database, at most one write behind).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard.db import Accelerator, Deployment, GpuUsageDaily, Node, Replica, Team, Workload

log = logging.getLogger("forgeyard")

COUNTED = ("starting", "running", "healthy", "unhealthy", "backoff")
# Reasons an agent gives while it fetches what a replica needs (agents older than 1.3.1 send no progress with them).
FETCHING = ("preparing the model", "downloading", "pulling the image", "waiting for another replica's download")
MAX_GAP = 60.0          # seconds: the most one interval between two writes may add
MIB = 1024**2
DAY = 86400.0
GROUPS = ("deployment", "team", "machine", "gpu_model")


def counting(r: Replica) -> bool:
    """Does this replica hold its reservation with its engine started (not fetching, not stopping, not asleep)?"""
    if r.final or r.node_id is None or r.state not in COUNTED:
        return False
    if r.workload.desired_state != "running":   # stopped or asleep (its replicas are "stopping" by then anyway)
        return False
    return r.download is None and not (r.reason or "").startswith(FETCHING)


def by_day(start: float, end: float) -> list[tuple[str, float]]:
    """[(UTC day, seconds)] of the interval start..end (epoch seconds), split at midnight."""
    out = []
    t = start
    while t < end:
        midnight = (math.floor(t / DAY) + 1) * DAY
        cut = min(end, midnight)
        out.append((datetime.fromtimestamp(t, tz=timezone.utc).date().isoformat(), cut - t))
        t = cut
    return out


@dataclass
class _Add:
    vendor: str
    gpu_ms: float = 0.0
    ram_mib_ms: float = 0.0
    busy_ms: float = 0.0


class Meter:
    """GPU time counted in memory between two writes of the usage recorder (which holds its own lock around
    collect/committed; this lock guards the meter's state against a direct caller)."""

    def __init__(self, clock: Callable[[], float] = time.time, busy: Optional[Callable[[str], float]] = None):
        self.clock = clock
        self._busy = busy
        self._lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "_lock", threading.Lock()):
            self._last: dict[str, float] = {}           # replica uid -> when it was last seen counted
            self._pending: dict[tuple, _Add] = {}       # slot -> GPU time not yet written (fractions of a ms carried)
            self._names: dict[int, str] = {}            # deployment id -> name (the /metrics label)
            self._staged: Optional[tuple[dict, dict, dict]] = None
            self.started_at = self.clock()              # counting since (the server's start)

    def _take_busy(self, uid: str) -> float:
        if self._busy is not None:
            return self._busy(uid)
        from forgeyard.gateway.balancer import balancer

        return balancer.take_busy(uid)

    # ---------------------------------------------------------------- counting
    def sample(self, db: DbSession, now: Optional[float] = None) -> int:
        """Add the time since the last sample of every counted replica; returns how many were counted."""
        now = self.clock() if now is None else now
        rows = db.scalars(select(Replica).join(Workload, Workload.id == Replica.workload_id).where(
            Workload.runtime == "model", Replica.final.is_(False), Replica.node_id.is_not(None),
            Replica.state.in_(COUNTED))).all()
        live = [r for r in rows if counting(r)]
        deps = {wid: (did, name, team) for did, wid, name, team in db.execute(select(
            Deployment.id, Deployment.workload_id, Deployment.name, Deployment.team_id).where(
            Deployment.workload_id.in_({r.workload_id for r in live}))).all()} if live else {}
        cards: dict[tuple[int, str], Accelerator] = {}
        cpus: dict[int, Accelerator] = {}
        if live:
            for a in db.scalars(select(Accelerator).where(Accelerator.node_id.in_({r.node_id for r in live}))).all():
                cards[(a.node_id, a.key)] = a
                if a.vendor == "cpu":
                    cpus[a.node_id] = a
        with self._lock:
            seen = set()
            for r in live:
                dep = deps.get(r.workload_id)
                if dep is None:
                    continue
                seen.add(r.uid)
                start = self._last.get(r.uid)
                self._last[r.uid] = now
                busy = self._take_busy(r.uid)
                if start is None:
                    continue   # first seen counted: from now on
                elapsed = min(now - start, MAX_GAP)
                if elapsed <= 0:
                    continue
                did, name, team = dep
                self._names[did] = name
                busy_part = min(max(0.0, busy), elapsed) / elapsed
                for day, secs in by_day(now - elapsed, now):
                    self._add_replica(r, did, team or 0, day, secs, busy_part, cards, cpus)
            for uid in [u for u in self._last if u not in seen]:   # stopped, asleep, fetching or gone
                self._last.pop(uid, None)
                self._take_busy(uid)
            return len(seen)

    def _add_replica(self, r: Replica, did: int, team: int, day: str, secs: float, busy_part: float,
                     cards: dict, cpus: dict) -> None:
        ms = secs * 1000.0
        if r.gpus:
            from forgeyard.workloads.placement import replica_spec

            share = ((replica_spec(r).get("resources") or {}).get("gpus") or {}).get("share") or "exclusive"
            for g in r.gpus:
                a = cards.get((r.node_id, g.get("key")))
                total = a.vram_total if a is not None else None
                weight = min(1.0, r.vram / total) if share == "vram" and r.vram and total else 1.0
                model = (a.model if a is not None else None) or g.get("model") or "unknown GPU"
                vendor = (a.vendor if a is not None else None) or g.get("vendor") or "gpu"
                add = self._slot(day, did, team, r.node_id, model[:160], vendor)
                add.gpu_ms += weight * ms
                add.busy_ms += weight * ms * busy_part
        else:
            a = cpus.get(r.node_id)
            add = self._slot(day, did, team, r.node_id, ((a.model if a is not None else None) or "CPU")[:160], "cpu")
            add.ram_mib_ms += (r.vram or 0) / MIB * ms
            add.busy_ms += ms * busy_part

    def _slot(self, day: str, did: int, team: int, node: int, model: str, vendor: str) -> _Add:
        key = (day, did, team, node, model)
        add = self._pending.get(key)
        if add is None:
            add = self._pending[key] = _Add(vendor)
        return add

    # ---------------------------------------------------------------- writing
    def write(self, db: DbSession) -> int:
        """Add what was counted to its rows; the caller commits and then calls `committed`, or `failed` when the commit
        did not happen. Returns the rows touched."""
        with self._lock:
            taken, self._pending = self._pending, {}
            self._staged = None
        if not taken:
            return 0
        days = {k[0] for k in taken}
        deps = {k[1] for k in taken}
        have = {(r.day, r.deployment_id, r.team_id, r.node_id, r.gpu_model): r for r in db.scalars(
            select(GpuUsageDaily).where(GpuUsageDaily.day.in_(days), GpuUsageDaily.deployment_id.in_(deps))).all()}
        left: dict[tuple, _Add] = {}
        seconds: dict[tuple[str, str], float] = {}
        touched = 0
        for key, add in taken.items():
            gpu, ram, busy = int(add.gpu_ms), int(add.ram_mib_ms), int(add.busy_ms)
            left[key] = _Add(add.vendor, add.gpu_ms - gpu, add.ram_mib_ms - ram, add.busy_ms - busy)
            if not (gpu or ram or busy):
                continue   # less than a millisecond so far: carried to the next write
            touched += 1
            row = have.get(key)
            if row is None:
                day, did, team, node, model = key
                row = GpuUsageDaily(day=day, deployment_id=did, team_id=team, node_id=node, vendor=add.vendor,
                                    gpu_model=model, gpu_ms=0, ram_mib_ms=0, busy_ms=0)
                db.add(row)
                have[key] = row
            row.gpu_ms = (row.gpu_ms or 0) + gpu
            row.ram_mib_ms = (row.ram_mib_ms or 0) + ram
            row.busy_ms = (row.busy_ms or 0) + busy
            if gpu:
                label = (self._names.get(key[1], f"#{key[1]}"), key[4])
                seconds[label] = seconds.get(label, 0.0) + gpu / 1000.0
        with self._lock:
            self._staged = (taken, left, seconds)
        return touched

    def _merge(self, adds: dict[tuple, _Add]) -> None:
        for key, add in adds.items():
            have = self._pending.get(key)
            if have is None:
                self._pending[key] = _Add(add.vendor, add.gpu_ms, add.ram_mib_ms, add.busy_ms)
            else:
                have.gpu_ms += add.gpu_ms
                have.ram_mib_ms += add.ram_mib_ms
                have.busy_ms += add.busy_ms

    def committed(self) -> None:
        """The write is in the database: only the fractions of a millisecond are carried (yesterday's and today's), and
        /metrics counts the GPU seconds written."""
        with self._lock:
            staged, self._staged = self._staged, None
            if staged is None:
                return
            _, left, seconds = staged
            keep = (datetime.fromtimestamp(self.clock(), tz=timezone.utc).date() - timedelta(days=1)).isoformat()
            self._merge({k: v for k, v in left.items() if k[0] >= keep})
        from forgeyard import metrics

        for (deployment, model), secs in seconds.items():
            metrics.gpu_seconds(deployment, model, secs)

    def failed(self) -> None:
        """The write was rolled back: everything it took is counted again at the next one."""
        with self._lock:
            staged, self._staged = self._staged, None
            if staged is not None:
                self._merge(staged[0])

    def collect(self, db: DbSession, now: Optional[float] = None) -> int:
        """Sample, then write: what the usage recorder calls on each write."""
        self.sample(db, now)
        return self.write(db)


# ------------------------------------------------------------------ reading
def _empty(gid: str, name: str, **extra) -> dict:
    return {"id": gid, "name": name, **extra, "gpu_ms": 0, "ram_mib_ms": 0, "busy_ms": 0, "cpu_busy_ms": 0}


def _add(g: dict, r: GpuUsageDaily) -> None:
    g["gpu_ms"] += r.gpu_ms or 0
    g["ram_mib_ms"] += r.ram_mib_ms or 0
    if r.vendor == "cpu":
        g["cpu_busy_ms"] += r.busy_ms or 0
    else:
        g["busy_ms"] += r.busy_ms or 0


def _finish(g: dict) -> dict:
    g["busy_share"] = round(g["busy_ms"] / g["gpu_ms"], 4) if g["gpu_ms"] else None
    return g


def _rows(db: DbSession, first: str, last: str, team_id: Optional[int], deployment_id: Optional[int]):
    q = select(GpuUsageDaily).where(GpuUsageDaily.day >= first, GpuUsageDaily.day <= last)
    if team_id is not None:
        q = q.where(GpuUsageDaily.team_id == team_id)
    if deployment_id is not None:
        q = q.where(GpuUsageDaily.deployment_id == deployment_id)
    return db.scalars(q).all()


def summary(db: DbSession, *, days: int = 7, group_by: str = "deployment", team_id: Optional[int] = None,
            today: Optional[str] = None) -> dict:
    """GPU time over the last `days` UTC days (today included), in total and per deployment, team, machine or GPU
    model, largest first."""
    end = datetime.fromisoformat(today).date() if today else datetime.now(timezone.utc).date()
    first = (end - timedelta(days=max(1, days) - 1)).isoformat()
    rows = _rows(db, first, end.isoformat(), team_id, None)
    label = _labeler(db, rows, group_by)
    groups: dict[str, dict] = {}
    total = _empty("total", "Total")
    for r in rows:
        gid, name, extra = label(r)
        g = groups.setdefault(gid, _empty(gid, name, **extra))
        _add(g, r)
        _add(total, r)
    out = sorted((_finish(g) for g in groups.values()), key=lambda g: (-g["gpu_ms"], -g["ram_mib_ms"], g["name"]))
    return {"from": first, "to": end.isoformat(), "days": days, "group_by": group_by, "total": _finish(total),
            "rows": out}


def _labeler(db: DbSession, rows, group_by: str) -> Callable[[GpuUsageDaily], tuple[str, str, dict]]:
    if group_by == "deployment":
        names = dict(db.execute(select(Deployment.id, Deployment.name).where(
            Deployment.id.in_({r.deployment_id for r in rows}))).all()) if rows else {}
        return lambda r: (str(r.deployment_id), names.get(r.deployment_id, f"deleted deployment #{r.deployment_id}"),
                          {"deleted": r.deployment_id not in names})
    if group_by == "team":
        teams = dict(db.execute(select(Team.id, Team.title).where(
            Team.id.in_({r.team_id for r in rows if r.team_id}))).all()) if rows else {}
        return lambda r: ("none", "No team", {}) if not r.team_id else (
            str(r.team_id), teams.get(r.team_id, f"deleted team #{r.team_id}"), {})
    if group_by == "machine":
        nodes = dict(db.execute(select(Node.id, Node.name).where(Node.id.in_({r.node_id for r in rows}))).all()) \
            if rows else {}
        return lambda r: (str(r.node_id), nodes.get(r.node_id, f"removed machine #{r.node_id}"), {})
    return lambda r: (f"{r.vendor}:{r.gpu_model}", r.gpu_model, {"vendor": r.vendor})


def since(meter: Meter) -> str:
    """When this server began counting (its start): GPU time before it, while the server was down, is not counted."""
    return datetime.fromtimestamp(meter.started_at, tz=timezone.utc).isoformat()


def month_of(at: Optional[date] = None) -> str:
    return (at or datetime.now(timezone.utc).date()).isoformat()[:7]


def month_total(db: DbSession, *, team_id: Optional[int] = None, deployment_id: Optional[int] = None,
                month: Optional[str] = None) -> dict:
    """GPU time in one UTC calendar month ("YYYY-MM", this one by default), for a team, a deployment or everything.
    From the database: at most one write of the usage recorder (5 s) behind. This is what an extension that enforces
    GPU-hour quotas reads (`month_total(db, team_id=…)["gpu_ms"]`)."""
    month = month or month_of()
    rows = _rows(db, f"{month}-01", f"{month}-31", team_id, deployment_id)
    total = _empty("total", "Total")
    for r in rows:
        _add(total, r)
    out = _finish(total)
    out.pop("id")
    out.pop("name")
    return {"month": month, **out}
