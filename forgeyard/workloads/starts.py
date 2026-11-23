"""How long a model replica took to start, recorded where its events are (1.5.0, issue #37, "start_times").

When a replica of a model deployment becomes ready (healthy, or running without a health check), the server
writes one event of kind READY_KIND:

    Replica 1 on box: ready in 74.2 s (model 20.0 s, image 3.1 s, engine 51.1 s)

from the agent's own measurement (`status.replicas[].start`, agent/workloads/starts.py): from the moment it began
starting the replica to its first healthy answer, and the parts it tells apart. An agent older than 1.5.0 sends
none; the server then measures the engine's part only, from the agent's `started_at` (when it launched the engine)
to the status that says the replica is ready, and says so:

    Replica 1 on box: ready in 51.0 s (engine 51.0 s, measured by the server)

The same seconds go to the histogram `forgeyard_replica_start_seconds{recipe, model}` (forgeyard/metrics.py), and
the latest event is the deployment API's `last_start` (`last_start`, read back by `parse`): no new column, no
migration. It is what 1.5.0's console hint ("the last start took 74 s") and the `Retry-After` of a request waiting
for a model to wake will use.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard.db import Deployment, Node, Replica, WorkloadEvent, as_utc
from forgeyard.machines.messages import MAX_START_SECONDS, START_PHASES

READY_KIND = "ready"
BY_SERVER = "measured by the server"
_READY_RE = re.compile(r": ready in (\d+(?:\.\d+)?) s(?: \(([^()]*)\))?$")
_PART_RE = re.compile(r"^([a-z]+) (\d+(?:\.\d+)?) s$")


def _s(seconds: float) -> str:
    return f"{seconds:.1f} s"


def message(ordinal: int, machine: str, start: dict, *, by_server: bool = False) -> str:
    parts = [f"{k} {_s(v)}" for k, v in (start.get("phases") or {}).items() if k in START_PHASES]
    if by_server:
        parts.append(BY_SERVER)
    text = f"Replica {ordinal} on {machine}: ready in {_s(float(start['seconds']))}"
    return text + (f" ({', '.join(parts)})" if parts else "")


def parse(text: str) -> Optional[dict]:
    """A READY_KIND event's message back as {seconds, phases, source} (agent, or server for an older agent)."""
    m = _READY_RE.search(text or "")
    if m is None:
        return None
    phases: dict[str, float] = {}
    source = "agent"
    for item in (m.group(2) or "").split(", "):
        if item == BY_SERVER:
            source = "server"
            continue
        p = _PART_RE.match(item)
        if p and p.group(1) in START_PHASES:
            phases[p.group(1)] = float(p.group(2))
    return {"seconds": float(m.group(1)), "phases": phases, "source": source}


def measured(r: Replica, start: Optional[dict], now: datetime) -> Optional[tuple[dict, bool]]:
    """The start of `r` that just became ready: the agent's measurement, else the server's (the engine's part, from
    the agent's started_at to now). None when neither can be told (no started_at, or clocks that disagree)."""
    if start is not None:
        return start, False
    if r.started_at is None:
        return None
    seconds = (now - as_utc(r.started_at)).total_seconds()
    if not 0 <= seconds <= MAX_START_SECONDS:
        return None
    seconds = round(seconds, 1)
    return {"seconds": seconds, "phases": {"engine": seconds}}, True


def ready(db: DbSession, r: Replica, node: Node, start: Optional[dict], now: datetime) -> Optional[dict]:
    """`r`, a replica of a model deployment, became ready on `node`: its start goes into the events and /metrics."""
    from forgeyard.workloads.state import event

    found = measured(r, start, now)
    if found is None:
        return None
    start, by_server = found
    event(db, r.workload, READY_KIND, message(r.ordinal, node.name, start, by_server=by_server), r.ordinal)
    dep = db.scalar(select(Deployment).where(Deployment.workload_id == r.workload_id))
    from forgeyard.models import trials

    if dep is not None and not trials.is_trial(db, dep.id):   # 1.5.2 (#95): a trial's start is on the trial
        from forgeyard import metrics

        metrics.replica_started(dep.recipe, dep.served_name, float(start["seconds"]))
    return start


def last_start(db: DbSession, workload_id: int) -> Optional[dict]:
    """The deployment's latest start, from its events: {seconds, phases, source, at, replica}, or None (it has not
    become ready since 1.5.0, or that event was trimmed with the oldest)."""
    e = db.scalars(select(WorkloadEvent).where(WorkloadEvent.workload_id == workload_id,
                                               WorkloadEvent.kind == READY_KIND)
                   .order_by(WorkloadEvent.id.desc()).limit(1)).first()
    if e is None:
        return None
    out = parse(e.message)
    if out is None:
        return None
    return {**out, "at": e.at, "replica": e.replica}
