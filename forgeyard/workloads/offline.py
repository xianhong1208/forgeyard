"""Replicas move off a machine that stays offline (1.4.5, #164).

A machine is offline once nothing has been heard from it for AGENT_OFFLINE_SECONDS (forgeyard.machines.liveness).
That alone moves nothing: a reboot, an agent update or a short network drop brings it back within minutes, with
its engines still loaded. Once it has been offline for AGENT_OFFLINE_MOVE_SECONDS (10 minutes by default; 0
turns this off), the liveness sweep takes its replicas off it, and the next placement pass puts each one on a
machine that fits, by the deployment's own rules (spread, fits, the estimate as it is now):

- **A deployment pinned to the machine** (its `machine_id`) keeps its replicas there: they wait for it, and
  say so.
- **Nothing else fits:** the replica stays where it is and waits for the machine (it would only wait unplaced
  otherwise, and the machine may well come back first), saying so; it moves on a later sweep once something fits.
- **Plain workloads** (not a model deployment's; since apps were removed in 1.5.3 only tests make them) follow the
  same rules for services. A task stays: it may still be running there, cut off from the network, and running it again elsewhere could do
  its work twice.

The replica that moves is the same row (one copy, never two counted): it gets a new id, so the machine, when it
comes back, does not find it in its work list and stops the engine it still runs (the agent reconciles to the
list it is sent on connect), and an event says "moved: <machine> offline". Offline time counts from the last
heartbeat, or from the server's own start when that is later: after a server restart (every machine shows
offline until its agent reconnects) nothing moves before agents had the whole grace period to come back.

Routing skips a replica on an offline machine at once (forgeyard.models.routes._ready, `skipped`), long before
this; only right after the server starts, a machine that has not reconnected yet keeps its replicas in the routes
for AGENT_OFFLINE_SECONDS (its agent reconnects within seconds, and its engines still answer meanwhile).
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard.config import get_settings
from forgeyard.db import Node, Replica, Workload, as_utc, utcnow

log = logging.getLogger("forgeyard.machines")

_started: datetime = utcnow()


def server_started(at: Optional[datetime] = None) -> None:
    """The server (re)started: no machine has been offline for longer than this, as far as it can tell."""
    global _started
    _started = at or utcnow()


def offline_for(node: Node, now: datetime) -> Optional[timedelta]:
    """How long the machine has been offline (None while it is online, or revoked)."""
    if node.status != "offline" or node.revoked_at is not None:
        return None
    since = as_utc(_started)
    if node.last_heartbeat_at is not None and as_utc(node.last_heartbeat_at) > since:
        since = as_utc(node.last_heartbeat_at)
    return max(timedelta(0), now - since)


def skipped(node: Node, now: Optional[datetime] = None) -> bool:
    """Does the gateway skip this machine's replicas? Yes while it is offline or revoked, except in the first
    AGENT_OFFLINE_SECONDS after the server started for a machine not heard from since: a server that starts shows
    every machine offline until its agent reconnects (seconds, after its back-off), while the engines there still
    answer through the agents' key guards, so those replicas keep serving meanwhile, as before 1.4.5."""
    if node.status == "online":
        return False
    if node.revoked_at is not None or node.status == "revoked":
        return True
    now = now or utcnow()
    started = as_utc(_started)
    if now - started < timedelta(seconds=get_settings().agent_offline_seconds) and (
            node.last_heartbeat_at is None or as_utc(node.last_heartbeat_at) < started):
        return False
    return True


def _minutes(d: timedelta) -> str:
    m = int(d.total_seconds() // 60)
    if m < 1:
        return f"{int(d.total_seconds())} seconds"
    return f"{m} minute{'s' if m != 1 else ''}"


def _say(db: DbSession, r: Replica, reason: str, message: str) -> None:
    """The replica waits on the offline machine: its reason says why, and one event (not one per sweep)."""
    from forgeyard.workloads.state import event

    if r.reason == reason:
        return
    r.reason, r.updated_at = reason, utcnow()
    event(db, r.workload, "waiting", message, r.ordinal)


def _model_alternative(db: DbSession, r: Replica, node: Node) -> tuple[Optional[bool], Optional[str]]:
    """(pinned?, the machine that would take it) for a model deployment's replica; (None, None) when its deployment
    or recipe is gone (it is left alone: placement says why when it is placed)."""
    from forgeyard.models import scheduler
    from forgeyard.models.catalog import context_for, deployment_of

    dep = deployment_of(db, r.workload_id)
    ctx = context_for(db, dep) if dep is not None else None
    if ctx is None:
        return None, None
    if ctx.machine_id is not None:
        return True, None
    siblings = {o.node_id for o in r.workload.replicas if o.id != r.id and o.node_id is not None and not o.final}
    # (choose() takes only online machines for an unpinned deployment, so never this one.)
    opt = scheduler.choose(scheduler.evaluate(db, ctx, exclude=r.id), None, siblings=siblings)
    if opt is None:
        return False, None
    return False, opt.node.name


def _app_alternative(db: DbSession, r: Replica, node: Node) -> tuple[Optional[bool], Optional[str]]:
    from forgeyard.workloads import placement
    from forgeyard.workloads.spec import WorkloadSpec

    try:
        spec = WorkloadSpec.model_validate(r.workload.spec)
    except ValueError:
        return None, None
    if spec.machine_id is not None:
        return True, None
    for other in placement._candidates(db, spec):  # online machines, by name
        if other.id == node.id:
            continue
        gpus, _ = placement.fit(db, other, spec, exclude=r.id)
        if gpus is not None:
            return False, other.name
    return False, None


def move_off(db: DbSession, now: Optional[datetime] = None) -> list[str]:
    """Take the replicas of unpinned deployments off every machine offline for AGENT_OFFLINE_MOVE_SECONDS, when
    another machine can take them (the caller's placement pass then places them). Returns their workloads' names
    (one per replica moved). Commits when something changed."""
    from forgeyard.workloads import placement
    from forgeyard.workloads.state import desired_replicas, event, reset_replica

    after = get_settings().agent_offline_move_seconds
    if after <= 0:
        return []
    now = now or utcnow()
    moved: list[str] = []
    with placement._lock:
        for node in db.scalars(select(Node).where(Node.status == "offline").order_by(Node.name, Node.id)).all():
            gone = offline_for(node, now)
            if gone is None or gone < timedelta(seconds=after):
                continue
            for r in desired_replicas(db, node.id):
                w: Workload = r.workload
                kind = "deployment" if w.runtime == "model" else "workload"
                if w.runtime == "model":
                    pinned, target = _model_alternative(db, r, node)
                elif w.kind == "task":
                    _say(db, r, f"waiting for {node.name} to come online (a task is not run again elsewhere)",
                         f"Replica {r.ordinal}: {node.name} has been offline for {_minutes(gone)}; the task stays there "
                         "(it may still be running, and running it again elsewhere could do its work twice).")
                    continue
                else:
                    pinned, target = _app_alternative(db, r, node)
                if pinned is None:
                    continue
                if pinned:
                    _say(db, r, f"waiting for {node.name} to come online (the {kind} is pinned to it)",
                         f"Replica {r.ordinal}: {node.name} has been offline for {_minutes(gone)}; the {kind} is "
                         f"pinned to it, so the replica waits for it.")
                    continue
                if target is None:
                    _say(db, r, f"waiting for {node.name} to come online (no other machine can take it)",
                         f"Replica {r.ordinal}: {node.name} has been offline for {_minutes(gone)}; no other machine "
                         "can take it, so it waits for that one (or for room elsewhere).")
                    continue
                event(db, w, "moved", f"Replica {r.ordinal}: moved: {node.name} offline for {_minutes(gone)}; "
                                      f"placing it on another machine ({target} fits).", r.ordinal)
                log.info("Replica %s of %r moved off %r: offline for %s.", r.ordinal, w.name, node.name,
                         _minutes(gone))
                placement.forget(r.id)  # never "back where it ran before": that machine is offline
                reset_replica(r, r.generation)
                # A new id: the machine, back, finds the old one missing from its work list and stops it. (With the
                # same id, a later move back there could meet an engine still holding the old internal key.)
                r.uid = uuid.uuid4().hex
                r.reason = f"moved: {node.name} offline; being placed again"
                moved.append(w.name)
        db.commit()  # (cheap when nothing changed; a stored estimate that changed is kept too)
    return moved
