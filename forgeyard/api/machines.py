"""Machines: everyone signed in can look; admins add (join tokens), revoke and remove, and choose
which GPUs Forgeyard may assign on each machine."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import audit
from forgeyard.api.deps import Caller, current, fail, public_base, require_admin
from forgeyard.config import get_settings
from forgeyard.db import Accelerator, JoinToken, Node, get_db, utcnow
from forgeyard.machines import store
from forgeyard.machines.hub import hub
from forgeyard.machines.protocol import CLOSE_REVOKED
from forgeyard.workloads import placement, state as wstate

router = APIRouter(prefix="/api/machines", tags=["machines"])


def accelerator_out(a: Accelerator, node: Node, assigned: dict[str, str]) -> dict:
    return {
        "key": a.key, "vendor": a.vendor, "index": a.index, "model": a.model, "arch": a.arch, "uuid": a.uuid,
        "vram_total": a.vram_total, "vram_free": a.vram_free, "vram_used": a.vram_used,
        "unified_memory": a.unified_memory, "driver": a.driver, "utilization": a.utilization,
        "temperature": a.temperature, "detail": a.detail or {}, "updated_at": a.updated_at,
        "allowed": placement.is_allowed(node, a), "assigned_to": assigned.get(a.key),
    }


def machine_out(n: Node, db: Optional[DbSession] = None) -> dict:
    assigned = placement.usage(db, n.id).gpu_keys if db is not None else {}
    return {
        "id": n.id, "name": n.name, "hostname": n.hostname, "status": n.status,
        "connected": hub.connected(n.id), "simulated": n.simulated,
        "agent_version": n.agent_version, "protocol": n.protocol, "labels": n.labels or {},
        "runtimes": n.runtimes or [], "platform": n.platform or {}, "collectors": n.collectors or {},
        "ip": n.ip, "joined_at": n.joined_at, "connected_at": n.connected_at,
        "last_heartbeat_at": n.last_heartbeat_at, "revoked_at": n.revoked_at, "revoked_by": n.revoked_by,
        "gpu_allowlist": n.gpu_allowlist, "features": n.features or [], "runtime_info": n.runtime_info or {},
        "accelerators": [accelerator_out(a, n, assigned) for a in n.accelerators],
    }


def token_out(t: JoinToken) -> dict:
    return {"id": t.public_id, "created_at": t.created_at, "expires_at": t.expires_at, "created_by": t.created_by}


def _get(db: DbSession, machine_id: int) -> Node:
    node = db.get(Node, machine_id)
    if node is None:
        raise fail(404, "not_found", "No such machine.")
    return node


@router.get("")
def list_machines(caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    settings = get_settings()
    nodes = db.scalars(select(Node).options(selectinload(Node.accelerators)).order_by(Node.name)).all()
    return {
        "machines": [machine_out(n, db) for n in nodes],
        "server_time": utcnow(),
        "heartbeat_interval": settings.agent_heartbeat_seconds,
        "offline_after": settings.agent_offline_seconds,
        "can_manage": caller.user.role == "admin",
    }


@router.post("/join-tokens", status_code=201)
def create_join_token(request: Request, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    token, row = store.issue_join_token(db, caller.user.username)
    audit.record(db, "machine.join_token_created", actor=caller.user, ip=caller.ip, token=row.public_id)
    db.commit()
    server = public_base(request)
    return {
        **token_out(row),
        "token": token,
        "server_url": server,
        "command": f"uv run --no-sync python -m forgeyard agent --server {server} --join-token {token}",
        "valid_minutes": int(store.JOIN_TOKEN_TTL.total_seconds() // 60),
    }


@router.get("/join-tokens")
def list_join_tokens(_: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    return {"tokens": [token_out(t) for t in store.live_join_tokens(db)]}


@router.delete("/join-tokens/{public_id}")
def cancel_join_token(public_id: str, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    row = db.scalar(select(JoinToken).where(JoinToken.public_id == public_id, JoinToken.used_at.is_(None)))
    if row is None:
        raise fail(404, "not_found", "No such unused join token.")
    row.used_at = utcnow()
    audit.record(db, "machine.join_token_cancelled", actor=caller.user, ip=caller.ip, token=public_id)
    db.commit()
    return {"ok": True}


@router.post("/{machine_id}/revoke")
def revoke_machine(machine_id: int, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    node = _get(db, machine_id)
    if node.revoked_at is not None:
        raise fail(409, "already_revoked", "This machine is already revoked.")
    store.revoke(db, node, caller.user.username)
    wstate.unplace_node(db, node.id, f"{node.name} was revoked")
    audit.record(db, "machine.revoked", actor=caller.user, target=node.name, ip=caller.ip, machine_id=node.id)
    db.commit()
    closed = hub.close(node.id, CLOSE_REVOKED, "revoked by an admin")
    placement.place_and_poke(db)
    return {"machine": machine_out(node, db), "connection_closed": closed}


@router.delete("/{machine_id}")
def remove_machine(machine_id: int, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    node = _get(db, machine_id)
    name, was_revoked = node.name, node.revoked_at is not None
    wstate.unplace_node(db, node.id, f"{name} was removed")
    db.delete(node)
    audit.record(db, "machine.removed", actor=caller.user, target=name, ip=caller.ip, machine_id=machine_id,
                 was_revoked=was_revoked)
    db.commit()
    closed = hub.close(machine_id, CLOSE_REVOKED, "removed by an admin")
    placement.place_and_poke(db)
    return {"ok": True, "connection_closed": closed}


class AllowlistIn(BaseModel):
    # None: every GPU of the machine. A list (possibly empty): only these accelerator keys.
    keys: Optional[list[str]] = Field(default=None, max_length=256)


@router.put("/{machine_id}/gpu-allowlist")
def set_gpu_allowlist(machine_id: int, body: AllowlistIn, caller: Caller = Depends(require_admin),
                      db: DbSession = Depends(get_db)):
    """Choose which GPUs Forgeyard may assign here. Refused while a workload uses a card that
    would be taken off the list: stop it first, so nothing keeps running outside the allowlist."""
    node = db.scalar(select(Node).where(Node.id == machine_id).options(selectinload(Node.accelerators)))
    if node is None:
        raise fail(404, "not_found", "No such machine.")
    gpus = {a.key: a for a in node.accelerators if placement.is_gpu(a)}
    if body.keys is not None:
        unknown = [k for k in body.keys if k not in gpus]
        if unknown:
            raise fail(422, "unknown_gpu", f"{node.name} has no GPU {unknown[0]!r}.")
        new = sorted(set(body.keys), key=lambda k: (gpus[k].vendor, gpus[k].index))
    else:
        new = None
    in_use = placement.usage(db, node.id).gpu_keys
    blocked = sorted({f"#{gpus[k].index} ({w})" for k, w in in_use.items()
                      if k in gpus and new is not None and k not in new})
    if blocked:
        raise fail(409, "gpu_in_use", f"Cannot take {', '.join(blocked)} off the list while it is in use. "
                                      "Stop that workload first.")

    def described(keys):
        if keys is None:
            return "all"
        return [f"#{gpus[k].index}" if k in gpus else k for k in keys]

    before = node.gpu_allowlist
    node.gpu_allowlist = new
    audit.record(db, "machine.gpu_allowlist_changed", actor=caller.user, target=node.name, ip=caller.ip,
                 machine_id=node.id, before=described(before), after=described(new),
                 keys=new if new is not None else "all")
    db.commit()
    placement.place_and_poke(db)
    db.expire_all()
    node = db.scalar(select(Node).where(Node.id == machine_id).options(selectinload(Node.accelerators)))
    return {"machine": machine_out(node, db)}
