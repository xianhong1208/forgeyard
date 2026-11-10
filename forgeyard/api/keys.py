"""API keys for the gateway, and usage statistics.

Everyone signed in creates and revokes their own keys. Admins see and revoke everyone's keys, set
the limits of any key, and set the defaults and caps for keys. A key is shown once, when it is
created; the server keeps only its SHA-256 and its prefix. Creating, changing and revoking keys, and
changing the defaults, are in the audit log.

A limit an admin sets on someone else's key is locked: its owner can no longer change that value
(they can still revoke the key, and change the other limit if no admin set it). The key shows who set
it. An admin unlocks a limit by clearing it (null: back to the server's default, the owner's again).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import audit
from forgeyard.api.deps import Caller, current, fail, public_base, require_admin
from forgeyard.db import ApiKey, Deployment, GatewayRoute, as_utc, get_db, utcnow
from forgeyard.gateway import keys as gkeys, usage as gusage
from forgeyard.gateway.limits import key_limiter

router = APIRouter(prefix="/api", tags=["keys"])

MAX_MODELS = 64
MAX_LIMIT = 100_000_000


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # None: every deployment the owner may use (now and later). A list: only these served names.
    models: Optional[list[str]] = Field(default=None, max_length=MAX_MODELS)
    rpm: Optional[int] = Field(default=None, ge=1, le=MAX_LIMIT)
    tpm: Optional[int] = Field(default=None, ge=1, le=MAX_LIMIT)
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)


class KeyPatchIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    models: Optional[list[str]] = Field(default=None, max_length=MAX_MODELS)
    rpm: Optional[int] = Field(default=None, ge=1, le=MAX_LIMIT)
    tpm: Optional[int] = Field(default=None, ge=1, le=MAX_LIMIT)


class LimitsIn(BaseModel):
    default_rpm: Optional[int] = Field(default=None, ge=0, le=MAX_LIMIT)
    default_tpm: Optional[int] = Field(default=None, ge=0, le=MAX_LIMIT)
    max_rpm: Optional[int] = Field(default=None, ge=0, le=MAX_LIMIT)
    max_tpm: Optional[int] = Field(default=None, ge=0, le=MAX_LIMIT)


def gateway_url(request: Request) -> str:
    return public_base(request) + "/v1"


def key_status(k: ApiKey) -> str:
    if k.revoked_at is not None:
        return "revoked"
    exp = as_utc(k.expires_at)
    if exp is not None and exp <= utcnow():
        return "expired"
    return "active"


LIMIT_NAMES = {"rpm": "requests-per-minute", "tpm": "tokens-per-minute"}


def key_out(k: ApiKey, limits: dict) -> dict:
    requests, tokens = key_limiter.used(k.id)
    return {
        "id": k.public_id, "name": k.name, "prefix": k.prefix, "models": k.models,
        "rpm": k.rpm, "tpm": k.tpm, "rpm_locked_by": k.rpm_locked_by, "tpm_locked_by": k.tpm_locked_by,
        "effective_rpm": gkeys.effective(k.rpm, limits["default_rpm"], limits["max_rpm"]),
        "effective_tpm": gkeys.effective(k.tpm, limits["default_tpm"], limits["max_tpm"]),
        "expires_at": k.expires_at, "created_at": k.created_at, "last_used_at": k.last_used_at,
        "revoked_at": k.revoked_at, "revoked_by": k.revoked_by, "status": key_status(k),
        "user": {"id": k.user_id, "username": k.user.username if k.user else None},
        "this_minute": {"requests": requests, "tokens": tokens},
    }


def _models(db: DbSession, names: Optional[list[str]]) -> Optional[list[str]]:
    if names is None:
        return None
    clean = sorted({n.strip() for n in names if n and n.strip()})
    if not clean:
        raise fail(422, "no_models", "Choose at least one model, or allow all of them.")
    known = set(db.scalars(select(GatewayRoute.served_name).where(GatewayRoute.served_name.in_(clean))).all())
    unknown = [n for n in clean if n not in known]
    if unknown:
        raise fail(422, "unknown_model", f"No deployment serves {unknown[0]!r}.")
    return clean


def _get(db: DbSession, key_id: str, caller: Caller) -> ApiKey:
    k = db.scalar(select(ApiKey).where(ApiKey.public_id == key_id).options(selectinload(ApiKey.user)))
    # Someone else's key is "not found" for a non-admin: they cannot even learn that it exists.
    if k is None or (k.user_id != caller.user.id and caller.user.role != "admin"):
        raise fail(404, "not_found", "No such API key.")
    return k


def _check_own(caller: Caller, owner_id: int, limits: dict, rpm: Optional[int], tpm: Optional[int]) -> None:
    if caller.user.role == "admin":
        return  # admins may give any key any limit
    for name, value, cap in (("rpm", rpm, limits["max_rpm"]), ("tpm", tpm, limits["max_tpm"])):
        problem = gkeys.check_own_limit(name, value, cap)
        if problem:
            raise fail(422, "over_cap", problem)


@router.get("/keys")
def list_keys(request: Request, all: bool = Query(default=False), caller: Caller = Depends(current),
              db: DbSession = Depends(get_db)):
    """Your keys; admins with ?all=true: everyone's."""
    admin = caller.user.role == "admin"
    q = select(ApiKey).options(selectinload(ApiKey.user)).order_by(ApiKey.revoked_at.is_not(None),
                                                                     ApiKey.created_at.desc(), ApiKey.id.desc())
    if not (all and admin):
        q = q.where(ApiKey.user_id == caller.user.id)
    limits = gkeys.get_limits(db)
    routes = db.execute(select(GatewayRoute.served_name, GatewayRoute.api).order_by(GatewayRoute.served_name)).all()
    return {"keys": [key_out(k, limits) for k in db.scalars(q).all()], "limits": limits, "is_admin": admin,
            "gateway_url": gateway_url(request), "models": [{"name": n, "api": a} for n, a in routes]}


@router.post("/keys", status_code=201)
def create_key(body: KeyIn, request: Request, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    limits = gkeys.get_limits(db)
    _check_own(caller, caller.user.id, limits, body.rpm, body.tpm)
    models = _models(db, body.models)
    raw, prefix, digest = gkeys.new_key()
    k = ApiKey(user_id=caller.user.id, name=body.name.strip(), prefix=prefix, hash=digest, models=models,
               rpm=body.rpm, tpm=body.tpm,
               expires_at=utcnow() + timedelta(days=body.expires_in_days) if body.expires_in_days else None)
    db.add(k)
    db.flush()
    audit.record(db, "api_key.created", actor=caller.user, target=k.name, ip=caller.ip, key=k.public_id,
                 prefix=prefix, models=models, rpm=body.rpm, tpm=body.tpm, expires_at=k.expires_at.isoformat()
                 if k.expires_at else None)
    db.commit()
    db.refresh(k)
    # The only time the key itself leaves the server.
    return {"key": raw, "api_key": key_out(k, limits), "gateway_url": gateway_url(request)}


@router.patch("/keys/{key_id}")
def change_key(key_id: str, body: KeyPatchIn, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    k = _get(db, key_id, caller)
    own = k.user_id == caller.user.id
    fields = body.model_fields_set
    if not own and fields - {"rpm", "tpm"}:
        raise fail(403, "forbidden", "On someone else's key an admin changes only the limits.")
    if k.revoked_at is not None:
        raise fail(409, "revoked", "This key was revoked.")
    limits = gkeys.get_limits(db)
    admin = caller.user.role == "admin"
    fields_ = ("name", "models", "rpm", "tpm", "rpm_locked_by", "tpm_locked_by")
    before = {f: getattr(k, f) for f in fields_}
    new = {name: getattr(body, name) for name in ("rpm", "tpm") if name in fields}
    if not admin:
        # Only values that change count: a form that sends a locked value back unchanged is fine.
        new = {name: v for name, v in new.items() if v != getattr(k, name)}
        for name in new:
            by = getattr(k, f"{name}_locked_by")
            if by:
                raise fail(403, "limit_locked", f"An admin ({by}) set this key's {LIMIT_NAMES[name]} limit; only an "
                                                "admin can change it. You can still revoke the key.")
    _check_own(caller, k.user_id, limits, new.get("rpm"), new.get("tpm"))
    if "name" in fields and body.name:
        k.name = body.name.strip()
    if "models" in fields:
        k.models = _models(db, body.models)
    for name, value in new.items():
        setattr(k, name, value)
        if admin:
            # On someone else's key an admin's value is locked for its owner; clearing it unlocks.
            # On their own key there is nobody to lock it against.
            setattr(k, f"{name}_locked_by", caller.user.username if (not own and value is not None) else None)
    after = {f: getattr(k, f) for f in fields_}
    changed = {f: {"before": before[f], "after": after[f]} for f in before if before[f] != after[f]}
    if changed:
        locked = [n for n in ("rpm", "tpm") if after[f"{n}_locked_by"] and after[f"{n}_locked_by"] != before[f"{n}_locked_by"]]
        unlocked = [n for n in ("rpm", "tpm") if before[f"{n}_locked_by"] and not after[f"{n}_locked_by"]]
        audit.record(db, "api_key.changed", actor=caller.user, target=k.name, ip=caller.ip, key=k.public_id,
                     owner=k.user.username if k.user else None, changes=changed, locked=locked or None,
                     unlocked=unlocked or None)
    db.commit()
    db.refresh(k)
    return {"api_key": key_out(k, limits)}


@router.delete("/keys/{key_id}")
def revoke_key(key_id: str, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    """Revoke: the very next request with the key gets 401. The key stays listed as revoked."""
    k = _get(db, key_id, caller)
    if k.revoked_at is None:
        k.revoked_at = utcnow()
        k.revoked_by = caller.user.username
        audit.record(db, "api_key.revoked", actor=caller.user, target=k.name, ip=caller.ip, key=k.public_id,
                     prefix=k.prefix, owner=k.user.username if k.user else None)
        db.commit()
    db.refresh(k)
    return {"api_key": key_out(k, gkeys.get_limits(db))}


@router.get("/keys/limits")
def get_key_limits(_: Caller = Depends(current), db: DbSession = Depends(get_db)):
    return {"limits": gkeys.get_limits(db)}


@router.put("/keys/limits")
def set_key_limits(body: LimitsIn, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    before = gkeys.get_limits(db)
    gkeys.set_limits(db, body.model_dump(), caller.user.username)
    db.flush()
    after = gkeys.get_limits(db)
    audit.record(db, "api_key_limits.changed", actor=caller.user, ip=caller.ip, before=before, after=after)
    db.commit()
    return {"limits": after}


# ------------------------------------------------------------------ usage
@router.get("/usage")
def get_usage(days: int = Query(default=7, ge=1, le=366), group: str = Query(default="model"),
              everyone: bool = Query(default=False), caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    """Your gateway usage (your keys and your playground); admins with ?everyone=true: all of it."""
    if group not in gusage.GROUPS:
        raise fail(422, "bad_group", f"group: one of {', '.join(gusage.GROUPS)}")
    gusage.recorder.flush()
    admin = caller.user.role == "admin"
    scope_user = None if (everyone and admin) else caller.user.id
    out = gusage.summary(db, days=days, user_id=scope_user, group_by=group)
    out["everyone"] = scope_user is None
    out["is_admin"] = admin
    return out


def deployment_usage(db: DbSession, d: Deployment, days: int = 7) -> dict:
    gusage.recorder.flush()
    out = gusage.summary(db, days=days, deployment_id=d.id, group_by="day")
    return {"days": days, "total": out["total"], "by_day": out["rows"]}
