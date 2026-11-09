"""Machines: everyone signed in can look; admins add (join tokens), revoke and remove."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import audit
from forgeyard.api.deps import Caller, current, fail, public_base, require_admin
from forgeyard.config import get_settings
from forgeyard.db import Accelerator, JoinToken, Node, get_db, utcnow
from forgeyard.machines import store
from forgeyard.machines.hub import hub
from forgeyard.machines.protocol import CLOSE_REVOKED

router = APIRouter(prefix="/api/machines", tags=["machines"])


def accelerator_out(a: Accelerator) -> dict:
    return {
        "key": a.key, "vendor": a.vendor, "index": a.index, "model": a.model, "arch": a.arch, "uuid": a.uuid,
        "vram_total": a.vram_total, "vram_free": a.vram_free, "vram_used": a.vram_used,
        "unified_memory": a.unified_memory, "driver": a.driver, "utilization": a.utilization,
        "temperature": a.temperature, "detail": a.detail or {}, "updated_at": a.updated_at,
    }


def machine_out(n: Node) -> dict:
    return {
        "id": n.id, "name": n.name, "hostname": n.hostname, "status": n.status,
        "connected": hub.connected(n.id), "simulated": n.simulated,
        "agent_version": n.agent_version, "protocol": n.protocol, "labels": n.labels or {},
        "runtimes": n.runtimes or [], "platform": n.platform or {}, "collectors": n.collectors or {},
        "ip": n.ip, "joined_at": n.joined_at, "connected_at": n.connected_at,
        "last_heartbeat_at": n.last_heartbeat_at, "revoked_at": n.revoked_at, "revoked_by": n.revoked_by,
        "accelerators": [accelerator_out(a) for a in n.accelerators],
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
        "machines": [machine_out(n) for n in nodes],
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
    audit.record(db, "machine.revoked", actor=caller.user, target=node.name, ip=caller.ip, machine_id=node.id)
    db.commit()
    closed = hub.close(node.id, CLOSE_REVOKED, "revoked by an admin")
    return {"machine": machine_out(node), "connection_closed": closed}


@router.delete("/{machine_id}")
def remove_machine(machine_id: int, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    node = _get(db, machine_id)
    name, was_revoked = node.name, node.revoked_at is not None
    db.delete(node)
    audit.record(db, "machine.removed", actor=caller.user, target=name, ip=caller.ip, machine_id=machine_id,
                 was_revoked=was_revoked)
    db.commit()
    closed = hub.close(machine_id, CLOSE_REVOKED, "removed by an admin")
    return {"ok": True, "connection_closed": closed}
