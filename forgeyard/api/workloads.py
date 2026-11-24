"""Workloads: what runs on the machines. Since 1.5.3 (#89) every workload is a model deployment's (or an engine
trial's, which is one too): the console reads them here to show what runs where and a replica's output.

Everyone signed in can look; people with the `user` role see workloads and their status, but not environment
values or logs (either may hold secrets). A workload is stopped, started and deleted through its deployment
(/api/deployments/{id}/stop|start, DELETE /api/deployments/{id}).

Apps, the workloads that were not models, were deprecated in 1.4 (#88) and removed in 1.5.3: creating,
exposing, stopping, starting or deleting one here, and the templates, answer 410 Gone (forgeyard/removed.py).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import audit, removed
from forgeyard.api.deps import Caller, can_run_workloads, current, fail, require_operator
from forgeyard.db import Replica, Workload, WorkloadEvent, get_db, utcnow
from forgeyard.gateway import waking
from forgeyard.machines.hub import hub
from forgeyard.models import reach
from forgeyard.machines.protocol import FEATURE_WORKLOADS, PROTOCOL_VERSION
from forgeyard.workloads import placement, state as wstate
from forgeyard.workloads.spec import env_masked

router = APIRouter(prefix="/api/workloads", tags=["workloads"])

LOG_TIMEOUT = 5.0
MAX_LOG_LINES = 2000


def replica_out(r: Replica) -> dict:
    node = r.node
    rt = ((r.spec or {}).get("runtime") or {})
    return {
        "ordinal": r.ordinal, "id": r.uid, "generation": r.generation,
        "machine": {"id": node.id, "name": node.name, "status": node.status, "connected": hub.connected(node.id)}
        if node else None,
        "gpus": [{"index": g.get("index"), "vendor": g.get("vendor"), "model": g.get("model"), "key": g.get("key")}
                 for g in r.gpus or []],
        "state": r.state, "reason": r.reason, "exit_code": r.exit_code, "attempt": r.attempt, "restarts": r.restarts,
        "final": r.final, "pid": r.pid, "container": r.container, "ports": r.ports or [],
        "started_at": r.started_at, "finished_at": r.finished_at, "updated_at": r.updated_at,
        "vram": r.vram, "download": r.download, "ready_at": r.ready_at,
        "runs": {"type": rt.get("type"), "image": rt.get("image"), "command": rt.get("command")} if rt else None,
        # 1.3.1: placed on a connected machine whose agent has not taken it (state.not_picked_up).
        "not_picked_up": wstate.not_picked_up(r, utcnow(), hub.connected) if node is not None else None,
        # 1.4: why it is on this machine, when a machine that could not fetch what it needs decided it.
        "placement_note": reach.note_of(r.id),
    }


def workload_out(w: Workload, caller: Caller, *, detail: bool = False) -> dict:
    spec = dict(w.spec)
    if not can_run_workloads(caller.user):
        spec["env"] = env_masked(spec.get("env") or {})
    out = {
        "id": w.id, "name": w.name, "kind": w.kind, "runtime": w.runtime, "desired_state": w.desired_state,
        "generation": w.generation, "created_by": w.created_by, "created_at": w.created_at,
        "updated_at": w.updated_at, "spec": spec, **wstate.summary(w),
        "replica_list": [replica_out(r) for r in w.replicas],
    }
    if w.runtime == "model":
        dep = deployment_of(w)
        out["deployment_id"] = dep.id if dep else None
        # 1.4: the deployment that created this workload, for "created by deployment X" on its pages.
        # The GPUs per replica come from the deployment (its spec's own count stays 0): 0 means the CPU.
        out["deployment"] = {"id": dep.id, "name": dep.name, "gpus": int((dep.params or {}).get("gpus") or 0)} if dep else None
    return out


def _get(db: DbSession, workload_id: int) -> Workload:
    w = db.scalar(select(Workload).where(Workload.id == workload_id)
                  .options(selectinload(Workload.replicas).selectinload(Replica.node)))
    if w is None:
        raise fail(404, "not_found", "No such workload.")
    return w


def deployment_of(w: Workload):
    """(id, name, params) of the deployment that created this model workload, or None."""
    if w.runtime != "model":
        return None
    from sqlalchemy.orm import object_session

    from forgeyard.db import Deployment

    db = object_session(w)
    return db.execute(select(Deployment.id, Deployment.name, Deployment.params)
                      .where(Deployment.workload_id == w.id)).first() if db else None


def _events(db: DbSession, w: Workload, limit: int = 100) -> list[dict]:
    rows = db.scalars(select(WorkloadEvent).where(WorkloadEvent.workload_id == w.id)
                      .order_by(WorkloadEvent.id.desc()).limit(limit)).all()
    return [{"at": e.at, "kind": e.kind, "replica": e.replica, "message": e.message} for e in rows]


def _poke(nodes) -> None:
    for node_id in nodes:
        hub.poke(node_id)


def _gone(db: DbSession, workload_id: int):
    """410 for the removed app routes; for a model deployment's workload, naming its deployment."""
    from forgeyard.db import Deployment

    dep = db.execute(select(Deployment.id, Deployment.name).where(Deployment.workload_id == workload_id)).first()
    extra = {"deployment": {"id": dep[0], "name": dep[1]}} if dep is not None else {}
    return removed.gone("apps", removed.deployment_hint(dep), **extra)


@router.get("/templates", include_in_schema=False)
def list_templates():
    """Removed in 1.5.3 (#89): 410."""
    return removed.gone("templates")


@router.get("")
def list_workloads(caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    rows = db.scalars(select(Workload).options(selectinload(Workload.replicas).selectinload(Replica.node))
                      .order_by(Workload.created_at.desc(), Workload.id.desc())).all()
    return {"workloads": [workload_out(w, caller) for w in rows], "server_time": utcnow(),
            "can_manage": can_run_workloads(caller.user)}


@router.post("", include_in_schema=False)
def create_workload():
    """Removed in 1.5.3 (#89): apps were the only workloads made here (models are deployments). 410."""
    return removed.gone("apps")


@router.get("/{workload_id}")
def get_workload(workload_id: int, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    w = _get(db, workload_id)
    return {"workload": workload_out(w, caller), "events": _events(db, w), "server_time": utcnow(),
            "can_manage": can_run_workloads(caller.user)}


@router.put("/{workload_id}/gateway", include_in_schema=False)
def set_gateway(workload_id: int, db: DbSession = Depends(get_db)):
    """Removed in 1.5.3 (#89): /x/ is gone. 410."""
    return _gone(db, workload_id)


@router.post("/{workload_id}/stop", include_in_schema=False)
def stop_workload_route(workload_id: int, db: DbSession = Depends(get_db)):
    """Removed in 1.5.3 (#89): a deployment's workload is stopped through its deployment. 410."""
    return _gone(db, workload_id)


@router.post("/{workload_id}/start", include_in_schema=False)
def start_workload_route(workload_id: int, db: DbSession = Depends(get_db)):
    """Removed in 1.5.3 (#89): a deployment's workload is started through its deployment. 410."""
    return _gone(db, workload_id)


@router.delete("/{workload_id}", include_in_schema=False)
def delete_workload(workload_id: int, db: DbSession = Depends(get_db)):
    """Removed in 1.5.3 (#89): a deployment's workload is deleted with its deployment. 410."""
    return _gone(db, workload_id)


def stop_workload(workload_id: int, caller: Caller, db: DbSession) -> dict:
    """Stop it (also for its deployment: /api/deployments/{id}/stop)."""
    w = _get(db, workload_id)
    if w.desired_state == "stopped":
        raise fail(409, "already_stopped", "This workload is already stopped.")
    was_asleep = w.desired_state == "asleep"
    # 1.5.0: an asleep deployment can be stopped too (its replicas are stopped already): a person's Stop stays
    # stopped, while an asleep one is woken by Start and by a request.
    nodes = wstate.stop(db, w, hub.connected)
    wstate.event(db, w, "stopped", f"Stopped by {caller.user.username}" + (" (it was asleep)." if was_asleep else "."))
    audit.record(db, "workload.stopped", actor=caller.user, target=w.name, ip=caller.ip, workload_id=w.id)
    db.commit()
    waking.forget_workload(w.id)   # 1.5.0: requests waiting for its wake answer that it was stopped
    _poke(nodes)
    placement.place_and_poke(db)  # its cards are free once confirmed; anything waiting gets a try
    return {"workload": workload_out(_get(db, workload_id), caller)}


def start_workload(workload_id: int, caller: Caller, db: DbSession) -> dict:
    """Start it again (also for its deployment: /api/deployments/{id}/start)."""
    w = _get(db, workload_id)
    if w.desired_state == "running":
        raise fail(409, "already_running", "This workload is already running.")
    if any(r.state == "stopping" for r in w.replicas):
        raise fail(409, "still_stopping", "Some replicas are still stopping. Start it again once they have stopped.")
    woken = w.desired_state == "asleep"   # 1.5.0: Start on an asleep deployment wakes it
    w.desired_state = "running"
    w.generation += 1
    w.updated_at = utcnow()
    for r in w.replicas:
        placement.remember(r)  # 1.3.1: back to the machine it ran on, when that machine still takes it
        wstate.reset_replica(r, w.generation)
    if woken:
        wstate.event(db, w, "woken", f"Woken by {caller.user.username} (generation {w.generation}).")
    else:
        wstate.event(db, w, "started", f"Started again by {caller.user.username} (generation {w.generation}).")
    audit.record(db, "workload.started", actor=caller.user, target=w.name, ip=caller.ip, workload_id=w.id,
                 generation=w.generation, **({"woken": True} if woken else {}))
    db.commit()
    if woken:
        waking.woken_by_person(db, w, caller.user.username)   # 1.5.0: requests arriving meanwhile wait for it
    placement.place_and_poke(db)
    db.expire_all()
    return {"workload": workload_out(_get(db, workload_id), caller)}


@router.get("/{workload_id}/replicas/{ordinal}/logs")
def replica_logs(workload_id: int, ordinal: int, request: Request, lines: int = Query(default=200, ge=1, le=MAX_LOG_LINES),
                 caller: Caller = Depends(require_operator), db: DbSession = Depends(get_db)):
    """The last lines of a replica's output, live from its agent; the lines kept from when it
    ended when the agent cannot be asked."""
    w = _get(db, workload_id)
    r = next((x for x in w.replicas if x.ordinal == ordinal), None)
    if r is None:
        raise fail(404, "not_found", "No such replica.")
    stored = {"source": "stored", "text": r.log_tail or "", "truncated": False, "state": r.state}
    conn = hub.get(r.node_id) if r.node_id is not None else None
    if conn is None or FEATURE_WORKLOADS not in conn.features:
        why = "not placed on a machine" if r.node_id is None else "its machine is not connected"
        return {**stored, "note": f"Live output is not available: {why}. Showing the lines kept when it last ended."}
    fut = conn.request({"v": PROTOCOL_VERSION, "type": "logs_request", "replica": r.uid, "lines": lines})
    try:
        answer = fut.result(timeout=LOG_TIMEOUT)
    except Exception:
        return {**stored, "note": "The agent did not answer in time. Showing the lines kept when it last ended."}
    finally:
        conn.forget(fut)
    if not answer.get("ok"):
        return {**stored, "note": answer.get("reason") or "The agent has no output for this replica."}
    return {"source": "live", "text": answer.get("text", ""), "truncated": bool(answer.get("truncated")),
            "state": r.state}
