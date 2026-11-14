"""Workloads: everyone signed in can look; admins and operators create, stop, start and delete.

A service workload can be exposed behind the gateway at /x/<name> (1.3, forgeyard/workloads/expose.py):
`gateway` beside the spec when it is created, or PUT /api/workloads/{id}/gateway at any time. Exposing
a running workload restarts its replicas behind the agent's key guard; turning it off closes /x/ at
once (the guard stays until the workload is restarted). Both are audited.

Creating a workload runs code on a machine, so it is limited to the roles trusted with that.
People with the `user` role see workloads and their status, but not environment values or
logs (either may hold secrets).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Body, Depends, Query, Request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import audit
from forgeyard.api.deps import Caller, can_run_workloads, current, fail, public_base, require_operator
from forgeyard.db import Node, Replica, Workload, WorkloadEvent, get_db, utcnow
from forgeyard.machines.hub import hub
from forgeyard.machines.protocol import FEATURE_WORKLOADS, PROTOCOL_VERSION
from forgeyard.workloads import expose, placement, state as wstate
from forgeyard.workloads.spec import WorkloadSpec, env_masked

router = APIRouter(prefix="/api/workloads", tags=["workloads"])

LOG_TIMEOUT = 5.0
MAX_LOG_LINES = 2000


def _validation_message(e: ValidationError) -> tuple[str, list[dict]]:
    errors = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err.get("loc", ()) if not str(p).startswith(("function-", "tagged-union")))
        msg = str(err.get("msg", "invalid")).removeprefix("Value error, ")
        errors.append({"field": loc, "message": msg})
    first = errors[0] if errors else {"field": "", "message": "invalid"}
    text = f"{first['field']}: {first['message']}" if first["field"] else first["message"]
    return text, errors


def parse_gateway(spec: dict, body: Any) -> expose.Exposure:
    if body is None:
        return expose.Exposure()
    if not isinstance(body, dict):
        raise fail(422, "invalid_gateway", "gateway: send an object such as {\"expose\": true}.")
    try:
        value = expose.Exposure.model_validate(body)
    except ValidationError as e:
        text, errors = _validation_message(e)
        raise fail(422, "invalid_gateway", f"gateway.{text}", errors=errors) from None
    problem = expose.check(spec, value)
    if problem:
        raise fail(422, "cannot_expose", f"gateway: {problem}.")
    return value


def parse_spec(body: Any) -> WorkloadSpec:
    if not isinstance(body, dict):
        raise fail(422, "invalid_spec", "Send the workload as a JSON object.")
    try:
        return WorkloadSpec.model_validate(body)
    except ValidationError as e:
        text, errors = _validation_message(e)
        raise fail(422, "invalid_spec", text, errors=errors) from None


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
    }


def gateway_out(w: Workload) -> Optional[dict]:
    """How the workload is exposed at /x/ (None for a model deployment's workload)."""
    if w.runtime == "model":
        return None
    value = expose.settings_of(w)
    exposed = expose.of(w) is not None
    guarded = [r.ordinal for r in w.replicas if r.secret and expose.can_guard(r.node)]
    unguarded = [r.ordinal for r in w.replicas if r.node is not None and not r.final and exposed
                 and not (r.secret and expose.can_guard(r.node))]
    return {**value.model_dump(), "exposed": exposed, "path": expose.url_path(w.name),
            "port_name": expose.port_name(w, value), "guarded": guarded, "unguarded": unguarded}


def workload_out(w: Workload, caller: Caller, *, detail: bool = False) -> dict:
    spec = dict(w.spec)
    if not can_run_workloads(caller.user):
        spec["env"] = env_masked(spec.get("env") or {})
    out = {
        "id": w.id, "name": w.name, "kind": w.kind, "runtime": w.runtime, "desired_state": w.desired_state,
        "generation": w.generation, "created_by": w.created_by, "created_at": w.created_at,
        "updated_at": w.updated_at, "spec": spec, **wstate.summary(w),
        "replica_list": [replica_out(r) for r in w.replicas], "gateway": gateway_out(w),
    }
    if w.runtime == "model":
        from sqlalchemy.orm import object_session

        from forgeyard.db import Deployment

        db = object_session(w)
        out["deployment_id"] = db.scalar(select(Deployment.id).where(Deployment.workload_id == w.id)) if db else None
    return out


def _get(db: DbSession, workload_id: int) -> Workload:
    w = db.scalar(select(Workload).where(Workload.id == workload_id)
                  .options(selectinload(Workload.replicas).selectinload(Replica.node)))
    if w is None:
        raise fail(404, "not_found", "No such workload.")
    return w


def _events(db: DbSession, w: Workload, limit: int = 100) -> list[dict]:
    rows = db.scalars(select(WorkloadEvent).where(WorkloadEvent.workload_id == w.id)
                      .order_by(WorkloadEvent.id.desc()).limit(limit)).all()
    return [{"at": e.at, "kind": e.kind, "replica": e.replica, "message": e.message} for e in rows]


def _poke(nodes) -> None:
    for node_id in nodes:
        hub.poke(node_id)


@router.get("")
def list_workloads(caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    rows = db.scalars(select(Workload).options(selectinload(Workload.replicas).selectinload(Replica.node))
                      .order_by(Workload.created_at.desc(), Workload.id.desc())).all()
    return {"workloads": [workload_out(w, caller) for w in rows], "server_time": utcnow(),
            "can_manage": can_run_workloads(caller.user)}


@router.post("", status_code=201)
def create_workload(body: Any = Body(...), caller: Caller = Depends(require_operator), db: DbSession = Depends(get_db)):
    gateway_in = body.pop("gateway", None) if isinstance(body, dict) else None
    spec = parse_spec(body)
    if spec.runtime_type == "model":
        raise fail(422, "invalid_spec", "runtime: model workloads are created as deployments (/api/deployments)")
    if db.scalar(select(Workload.id).where(Workload.name == spec.name)) is not None:
        raise fail(409, "name_taken", f"A workload named {spec.name} already exists.")
    if spec.machine_id is not None:
        node = db.scalar(select(Node).where(Node.id == spec.machine_id).options(selectinload(Node.accelerators)))
        if node is None:
            raise fail(404, "machine_not_found", "No such machine.")
    data = spec.model_dump(mode="json")
    gateway = parse_gateway(data, gateway_in) if spec.runtime_type != "model" else expose.Exposure()
    if spec.machine_id is not None:
        problem = placement.runtime_problem(node, spec, gateway.expose)
        if problem is None and spec.resources.gpus.indices is not None:
            _, problem = placement.choose_gpus(node, spec, {})  # cards that do not exist or are not allowed
        if problem:
            raise fail(422, "cannot_place", f"{node.name} cannot run this: {problem}.")
    w = Workload(name=spec.name, kind=spec.kind, runtime=spec.runtime_type, spec=data, desired_state="running",
                 generation=1, owner_id=caller.user.id, created_by=caller.user.username,
                 gateway=gateway.model_dump() if gateway.expose else None)
    db.add(w)
    db.flush()
    for i in range(spec.replicas):
        w.replicas.append(Replica(ordinal=i, generation=1, state="pending", gpus=[], ports=[]))
    wstate.event(db, w, "created", f"Created by {caller.user.username}.")
    gpus = spec.resources.gpus
    audit.record(db, "workload.created", actor=caller.user, target=w.name, ip=caller.ip, workload_id=w.id,
                 kind=w.kind, runtime=w.runtime, machine_id=spec.machine_id, replicas=spec.replicas,
                 gpus=gpus.indices if gpus.indices is not None else gpus.count or 0,
                 exposed=expose.url_path(w.name) if gateway.expose else None)
    db.commit()
    placement.place_and_poke(db)
    db.expire_all()
    w = _get(db, w.id)
    return {"workload": workload_out(w, caller), "events": _events(db, w)}


@router.get("/{workload_id}")
def get_workload(workload_id: int, request: Request, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    from forgeyard.api.tls import trust_page

    w = _get(db, workload_id)
    return {"workload": workload_out(w, caller), "events": _events(db, w), "server_time": utcnow(),
            "can_manage": can_run_workloads(caller.user), "gateway_base": public_base(request),
            "trust_url": trust_page(request)}


@router.put("/{workload_id}/gateway")
def set_gateway(workload_id: int, body: Any = Body(...), caller: Caller = Depends(require_operator),
                db: DbSession = Depends(get_db)):
    """Expose the workload at /x/<name> (or stop exposing it), and its limits. A running workload that
    becomes exposed gets an internal key per replica: its agent restarts it behind the key guard."""
    w = _get(db, workload_id)
    if w.runtime == "model":
        raise fail(422, "cannot_expose", "A model deployment is served at /v1, not /x/.")
    value = parse_gateway(w.spec, body)
    before = expose.settings_of(w).model_dump()
    was = expose.of(w) is not None
    w.gateway = value.model_dump() if value.expose or w.gateway else None
    w.updated_at = utcnow()
    nodes: set[int] = set()
    if value.expose and not was:
        for r in w.replicas:
            if r.node_id is not None and not r.final and expose.can_guard(r.node):
                expose.give_key(r)  # the agent restarts it behind its key guard
                nodes.add(r.node_id)
        wstate.event(db, w, "exposed", f"Exposed at {expose.url_path(w.name)} by {caller.user.username}.")
    elif was and not value.expose:
        wstate.event(db, w, "unexposed", f"No longer exposed at {expose.url_path(w.name)} (by {caller.user.username}); "
                                         "its key guard stays until it is restarted.")
    after = value.model_dump()
    if before != after:
        audit.record(db, "workload.gateway_changed", actor=caller.user, target=w.name, ip=caller.ip, workload_id=w.id,
                     before=before, after=after)
    db.commit()
    _poke(nodes)
    db.expire_all()
    return {"workload": workload_out(_get(db, workload_id), caller)}


@router.post("/{workload_id}/stop")
def stop_workload(workload_id: int, caller: Caller = Depends(require_operator), db: DbSession = Depends(get_db)):
    w = _get(db, workload_id)
    if w.desired_state != "running":
        raise fail(409, "already_stopped", "This workload is already stopped.")
    nodes = wstate.stop(db, w, hub.connected)
    wstate.event(db, w, "stopped", f"Stopped by {caller.user.username}.")
    audit.record(db, "workload.stopped", actor=caller.user, target=w.name, ip=caller.ip, workload_id=w.id)
    db.commit()
    _poke(nodes)
    placement.place_and_poke(db)  # its cards are free once confirmed; anything waiting gets a try
    return {"workload": workload_out(_get(db, workload_id), caller)}


@router.post("/{workload_id}/start")
def start_workload(workload_id: int, caller: Caller = Depends(require_operator), db: DbSession = Depends(get_db)):
    w = _get(db, workload_id)
    if w.desired_state == "running":
        raise fail(409, "already_running", "This workload is already running.")
    if any(r.state == "stopping" for r in w.replicas):
        raise fail(409, "still_stopping", "Some replicas are still stopping. Start it again once they have stopped.")
    w.desired_state = "running"
    w.generation += 1
    w.updated_at = utcnow()
    for r in w.replicas:
        wstate.reset_replica(r, w.generation)
    wstate.event(db, w, "started", f"Started again by {caller.user.username} (generation {w.generation}).")
    audit.record(db, "workload.started", actor=caller.user, target=w.name, ip=caller.ip, workload_id=w.id,
                 generation=w.generation)
    db.commit()
    placement.place_and_poke(db)
    db.expire_all()
    return {"workload": workload_out(_get(db, workload_id), caller)}


@router.delete("/{workload_id}")
def delete_workload(workload_id: int, caller: Caller = Depends(require_operator), db: DbSession = Depends(get_db)):
    w = _get(db, workload_id)
    nodes = {r.node_id for r in w.replicas if r.node_id is not None and not r.final}
    name = w.name
    db.delete(w)
    audit.record(db, "workload.deleted", actor=caller.user, target=name, ip=caller.ip, workload_id=workload_id,
                 was=w.desired_state)
    db.commit()
    _poke(nodes)  # the agents stop and forget its replicas: they are no longer desired
    placement.place_and_poke(db)
    return {"ok": True}


@router.get("/{workload_id}/replicas/{ordinal}/logs")
def replica_logs(workload_id: int, ordinal: int, lines: int = Query(default=200, ge=1, le=MAX_LOG_LINES),
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
