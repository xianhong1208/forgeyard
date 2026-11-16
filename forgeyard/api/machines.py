"""Machines: everyone signed in can look; admins add (join tokens), revoke and remove, and choose
which GPUs Forgeyard may assign on each machine and how it reaches the internet (`fetch_via`, 1.4).
Operators and admins remove models from a machine's model cache (1.4). Admins upload an image file to a
machine (1.4, forgeyard/machines/upload.py)."""

from __future__ import annotations

from typing import Optional
from urllib.parse import unquote

from fastapi import APIRouter, Depends, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import audit
from forgeyard.api.deps import Caller, current, fail, public_base, require_admin, require_operator
from forgeyard.api.tls import agent_commands
from forgeyard.config import get_settings
from forgeyard.db import Accelerator, JoinToken, Node, get_db, new_session, utcnow
from forgeyard.install import commands as install_commands
from forgeyard.machines import cache, store, upload
from forgeyard.machines.hub import hub
from forgeyard.machines.protocol import CLOSE_REVOKED, FETCH_VIA
from forgeyard.models import prefetch
from forgeyard.security import tls
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
        "fetch_via": n.fetch_via or "auto",
        # 1.4: the largest image file an admin may upload to a machine (IMAGE_UPLOAD_MAX_GB).
        "image_upload_max_bytes": get_settings().image_upload_max_gb * upload.UNIT,
        # The per-model listing (1.4) is served by /api/machines/{id}/cache, not with every machine.
        "model_cache": {k: v for k, v in (n.model_cache or {}).items() if k not in ("models", "engines_detail")},
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
        "heartbeat_interval": settings.heartbeat_seconds,
        "offline_after": settings.agent_offline_seconds,
        "can_manage": caller.user.role == "admin",
    }


@router.post("/join-tokens", status_code=201)
def create_join_token(request: Request, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    token, row = store.issue_join_token(db, caller.user.username)
    audit.record(db, "machine.join_token_created", actor=caller.user, ip=caller.ip, token=row.public_id)
    db.commit()
    server = public_base(request)
    anchor = tls.trust_anchor_pem()
    fingerprint = tls.fingerprint(anchor) if anchor and server.startswith("https://") else None
    return {
        **token_out(row),
        "token": token,
        "server_url": server,
        # Every way to add the machine pins the CA by its fingerprint: the agent trusts that CA only.
        **agent_commands(server, token),
        **install_commands.all_commands(server, token, fingerprint),
        "ca_fingerprint": fingerprint,
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
    prefetch.fail_all(db, node.id, f"{node.name} was revoked")
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


FETCH_VIA_TEXT = {"auto": "Auto (directly, through the server after a network failure)",
                  "server": "Always through the server", "direct": "Direct only"}


class FetchViaIn(BaseModel):
    fetch_via: str = Field(max_length=16)


@router.put("/{machine_id}/fetch-via")
def set_fetch_via(machine_id: int, body: FetchViaIn, caller: Caller = Depends(require_admin),
                  db: DbSession = Depends(get_db)):
    """How this machine fetches models and engine archives (1.4, D3/D9: admins only): "auto" (directly, and
    through the server after a network failure), "server" (always through the server) or "direct" (never
    through it). Its agent gets the new way with its next desired state, sent now; placement no longer
    avoids a machine that relays for a host it could not reach."""
    if body.fetch_via not in FETCH_VIA:
        raise fail(422, "bad_fetch_via", f"fetch_via is one of: {', '.join(FETCH_VIA)}.")
    node = _get(db, machine_id)
    before = node.fetch_via or "auto"
    if before != body.fetch_via:
        node.fetch_via = body.fetch_via
        audit.record(db, "node.fetch_via_changed", actor=caller.user, target=node.name, ip=caller.ip,
                     machine_id=node.id, before=before, after=body.fetch_via)
        db.commit()
        hub.poke(node.id)  # its replicas' and downloads' blocks say how to fetch them
        placement.place_and_poke(db)  # a replica waiting for a machine that can fetch it may now go here
    db.expire_all()
    node = db.scalar(select(Node).where(Node.id == machine_id).options(selectinload(Node.accelerators)))
    return {"machine": machine_out(node, db)}


# ---------------------------------------------------------------------------- the model cache (1.4)
@router.get("/{machine_id}/cache")
def machine_cache(machine_id: int, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    """The models in this machine's model cache, with their sizes, the catalog models they are, and
    the deployments there that use them (as its agent last reported)."""
    return cache.view(db, _get(db, machine_id), caller.user)


class CacheRemoveIn(BaseModel):
    key: str = Field(min_length=1, max_length=800)


@router.post("/{machine_id}/cache/remove")
def remove_from_cache(machine_id: int, body: CacheRemoveIn, caller: Caller = Depends(require_operator),
                      db: DbSession = Depends(get_db)):
    """Delete one model (or engine) from this machine's cache. Refused while a deployment there uses
    it: stop that first. Its agent checks again before it deletes anything."""
    node = _get(db, machine_id)
    found = cache.find(node.model_cache or {}, body.key)
    if found is None:
        raise fail(404, "not_in_cache", f"{node.name} does not report that model in its cache (any more).")
    users = cache.users_of(db, node.id, found.target)
    if users:
        names = ", ".join(u["name"] for u in users)
        raise fail(409, "model_in_use", f"{found.title} is in use by {names} on {node.name}. Stop "
                                        f"{'it' if len(users) == 1 else 'them'} first.", in_use=users)
    answer = cache.ask(node, {"type": "cache_remove", "key": body.key})
    audit.record(db, "model.cache_removed", actor=caller.user, target=node.name, ip=caller.ip, machine_id=node.id,
                 key=body.key, model=found.title, freed_bytes=answer.get("freed_bytes") or 0)
    db.commit()
    db.expire_all()
    return {"ok": True, "freed_bytes": answer.get("freed_bytes") or 0, "cache": cache.view(db, _get(db, machine_id), caller.user)}


@router.post("/{machine_id}/cache/remove-partial")
def remove_partial_downloads(machine_id: int, caller: Caller = Depends(require_operator),
                             db: DbSession = Depends(get_db)):
    """Delete what unfinished downloads left in this machine's cache (refused by the agent while a
    download runs there)."""
    node = _get(db, machine_id)
    answer = cache.ask(node, {"type": "cache_prune"})
    audit.record(db, "model.cache_removed", actor=caller.user, target=node.name, ip=caller.ip, machine_id=node.id,
                 key=None, model="partial downloads", freed_bytes=answer.get("freed_bytes") or 0)
    db.commit()
    db.expire_all()
    return {"ok": True, "freed_bytes": answer.get("freed_bytes") or 0, "cache": cache.view(db, _get(db, machine_id), caller.user)}


# ---------------------------------------------------------------------------- uploading an image file (1.4)
def _upload_caller(request: Request, machine_id: int) -> tuple[Caller, Node]:
    """In a thread, with a session of its own (an upload lasts minutes; no database connection is held)."""
    db = new_session()
    try:
        caller = require_admin(current(request, db))
        node = _get(db, machine_id)
        _ = (caller.user.id, caller.user.username, node.id, node.name)  # loaded before the session closes
        return caller, node
    finally:
        db.close()


def _upload_audit(caller: Caller, node: Node, **detail) -> None:
    db = new_session()
    try:
        audit.record(db, "node.image_uploaded", actor=caller.user, target=node.name, ip=caller.ip, machine_id=node.id,
                     **detail)
        db.commit()
    finally:
        db.close()


def _matches(items: list[dict]) -> list[dict]:
    db = new_session()
    try:
        return [{**i, "matches": upload.recipe_matches(db, i)} for i in items]
    finally:
        db.close()


@router.post("/{machine_id}/images")
async def upload_image(machine_id: int, request: Request):
    """Upload an image file (`docker save`, optionally gzip) to this machine (1.4, admins only): streamed through
    the server to its agent, which loads it into its container engine. The body is the file; Content-Length is
    required; `X-Filename` names it in the audit log. Answers what was loaded, and which recipe image each one
    is (checked by its recorded ids, or named by a tag-only recipe image)."""
    caller, node = await run_in_threadpool(_upload_caller, request, machine_id)
    limit = get_settings().image_upload_max_gb * upload.UNIT
    length = request.headers.get("content-length") or ""
    if not length.isdigit():
        raise fail(411, "length_required", "The upload must say its size (Content-Length).")
    size = int(length)
    if size <= 0:
        raise fail(400, "upload_empty", "The file is empty.")
    if size > limit:
        raise fail(413, "upload_too_large", f"The file is {size / upload.UNIT:.1f} GiB; the limit is "
                                            f"{get_settings().image_upload_max_gb} GiB (IMAGE_UPLOAD_MAX_GB).")
    name = unquote(request.headers.get("x-filename") or "")[:200] or "image.tar"
    try:
        upload.precheck(node)  # refused before a byte moves: not an upload, not audited
    except upload.UploadFailed as e:
        raise fail(e.status, e.code, e.message) from None
    try:
        answer = await upload.receive(request, node, size, name)
    except upload.UploadFailed as e:
        await run_in_threadpool(_upload_audit, caller, node, file=name, bytes=size, ok=False, reason=e.message[:200])
        raise fail(e.status, e.code, e.message) from None
    if not answer.get("ok"):
        reason = str(answer.get("reason") or "the agent could not load it")[:400]
        await run_in_threadpool(_upload_audit, caller, node, file=name, bytes=size, ok=False, reason=reason[:200])
        raise fail(422, "image_load_failed", f"{node.name}: {reason}")
    items = await run_in_threadpool(_matches, list(answer.get("images") or []))
    await run_in_threadpool(_upload_audit, caller, node, file=name, bytes=size, ok=True,
                            images=[{"id": i.get("id"), "tags": i.get("tags")} for i in items][:8])
    return {"ok": True, "bytes": size, "images": items}
