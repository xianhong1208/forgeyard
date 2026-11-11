"""API keys for the gateway, and usage statistics.

Everyone signed in creates and revokes their own keys. Admins see and revoke everyone's keys, set
the limits of any key, and set the defaults and caps for keys. A key is shown once, when it is
created; the server keeps only its SHA-256 and its prefix. Creating, changing and revoking keys, and
changing the defaults, are in the audit log.

A limit an admin sets on someone else's key is locked: its owner can no longer change that value
(they can still revoke the key, and change the other limit if no admin set it). The key shows who set
it. An admin unlocks a limit by clearing it (null: back to the server's default, the owner's again).

The same goes for a key's `cloud` setting (1.2): off (the default, and every key from before 1.2),
fallback or on. A user may give their own key at most the server's cap; an admin's value on someone
else's key is locked; null unlocks it and sets the server's default again.

And for a key's monthly cloud budget (`budget_micros`, micro-USD; forgeyard/gateway/budgets.py): its
owner may set or remove it; an admin's value on someone else's key is locked; null removes it (no
budget) and unlocks it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import audit
from forgeyard.api.deps import Caller, current, fail, public_base, require_admin
from forgeyard.api.tls import trust_page
from forgeyard.db import ApiKey, Deployment, GatewayRoute, as_utc, get_db, utcnow
from forgeyard.models import routes as mroutes
from forgeyard.gateway import budgets as gbudgets, keys as gkeys, usage as gusage
from forgeyard.gateway.limits import key_limiter

router = APIRouter(prefix="/api", tags=["keys"])

MAX_MODELS = 64
MAX_LIMIT = 100_000_000
Cloud = Literal["off", "fallback", "on"]
MAX_BUDGET = 10**15        # micro-USD: a billion dollars


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    # None: every deployment the owner may use (now and later). A list: only these served names.
    models: Optional[list[str]] = Field(default=None, max_length=MAX_MODELS)
    rpm: Optional[int] = Field(default=None, ge=1, le=MAX_LIMIT)
    tpm: Optional[int] = Field(default=None, ge=1, le=MAX_LIMIT)
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)
    # None: the server's default for new keys.
    cloud: Optional[Cloud] = None
    # Monthly cloud budget in micro-USD; None: no budget.
    budget_micros: Optional[int] = Field(default=None, ge=0, le=MAX_BUDGET)


class KeyPatchIn(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    models: Optional[list[str]] = Field(default=None, max_length=MAX_MODELS)
    rpm: Optional[int] = Field(default=None, ge=1, le=MAX_LIMIT)
    tpm: Optional[int] = Field(default=None, ge=1, le=MAX_LIMIT)
    cloud: Optional[Cloud] = None
    budget_micros: Optional[int] = Field(default=None, ge=0, le=MAX_BUDGET)


class CloudIn(BaseModel):
    default: Cloud = "off"
    max: Cloud = "off"


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


LIMIT_NAMES = {"rpm": "requests-per-minute", "tpm": "tokens-per-minute", "cloud": "cloud",
               "budget_micros": "monthly budget"}
# A value an admin may lock on someone else's key -> the column that says who locked it.
LOCKS = {"rpm": "rpm_locked_by", "tpm": "tpm_locked_by", "cloud": "cloud_locked_by", "budget_micros": "budget_locked_by"}


def budget_out(budget: Optional[int], spent: int, reserved: int) -> dict:
    """A budget with this month's spend: `used` (0..1+) and `level` ok | warning (80%) | exhausted."""
    used = (spent / budget if budget else (1.0 if spent or budget == 0 else 0.0)) if budget is not None else None
    level = None if used is None else "exhausted" if used >= 1 else "warning" if used >= 0.8 else "ok"
    return {"budget_micros": budget, "spent_micros": spent, "reserved_micros": reserved, "used": used, "level": level}


def key_out(k: ApiKey, limits: dict, spend: Optional[gbudgets.Spend] = None) -> dict:
    requests, tokens = key_limiter.used(k.id)
    spend = spend or gbudgets.ledger.snapshot()
    return {
        "id": k.public_id, "name": k.name, "prefix": k.prefix, "models": k.models,
        "rpm": k.rpm, "tpm": k.tpm, "rpm_locked_by": k.rpm_locked_by, "tpm_locked_by": k.tpm_locked_by,
        "cloud": gkeys.cloud_level(k.cloud), "cloud_locked_by": k.cloud_locked_by,
        "effective_rpm": gkeys.effective(k.rpm, limits["default_rpm"], limits["max_rpm"]),
        "effective_tpm": gkeys.effective(k.tpm, limits["default_tpm"], limits["max_tpm"]),
        "expires_at": k.expires_at, "created_at": k.created_at, "last_used_at": k.last_used_at,
        "revoked_at": k.revoked_at, "revoked_by": k.revoked_by, "status": key_status(k),
        "user": {"id": k.user_id, "username": k.user.username if k.user else None},
        "this_minute": {"requests": requests, "tokens": tokens},
        "budget_locked_by": k.budget_locked_by,
        "budget": budget_out(k.budget_micros, spend.spent.get(("key", k.id), 0), spend.held.get(("key", k.id), 0)),
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


def _check_own(caller: Caller, owner_id: int, limits: dict, rpm: Optional[int], tpm: Optional[int],
               cloud: Optional[str] = None, cloud_cap: str = "on") -> None:
    if caller.user.role == "admin":
        return  # admins may give any key any limit
    for name, value, cap in (("rpm", rpm, limits["max_rpm"]), ("tpm", tpm, limits["max_tpm"])):
        problem = gkeys.check_own_limit(name, value, cap)
        if problem:
            raise fail(422, "over_cap", problem)
    if cloud is not None and gkeys.cloud_above(cloud, cloud_cap):
        raise fail(422, "over_cap", f"cloud: at most {cloud_cap!r} on your own keys; an admin can allow more")


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
    rows = db.scalars(mroutes.query().order_by(GatewayRoute.served_name)).all()
    spend = gbudgets.ledger.snapshot()
    me = caller.user
    return {"keys": [key_out(k, limits, spend) for k in db.scalars(q).all()], "limits": limits, "is_admin": admin,
            "month": spend.month, "resets_at": gbudgets.resets_at(spend.month),
            "person_budget": budget_out(me.budget_micros, spend.spent.get(("person", me.id), 0),
                                        spend.held.get(("person", me.id), 0)),
            "cloud": gkeys.get_cloud(db), "gateway_url": gateway_url(request), "trust_url": trust_page(request),
            "models": [{"name": r.served_name, "api": r.api, "kind": mroutes.route_kind(r)} for r in rows]}


@router.post("/keys", status_code=201)
def create_key(body: KeyIn, request: Request, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    limits = gkeys.get_limits(db)
    cloud_settings = gkeys.get_cloud(db)
    _check_own(caller, caller.user.id, limits, body.rpm, body.tpm, body.cloud, cloud_settings["max"])
    models = _models(db, body.models)
    cloud = body.cloud or cloud_settings["default"]
    raw, prefix, digest = gkeys.new_key()
    k = ApiKey(user_id=caller.user.id, name=body.name.strip(), prefix=prefix, hash=digest, models=models,
               rpm=body.rpm, tpm=body.tpm, cloud=cloud, budget_micros=body.budget_micros,
               expires_at=utcnow() + timedelta(days=body.expires_in_days) if body.expires_in_days else None)
    db.add(k)
    db.flush()
    audit.record(db, "api_key.created", actor=caller.user, target=k.name, ip=caller.ip, key=k.public_id,
                 prefix=prefix, models=models, rpm=body.rpm, tpm=body.tpm, cloud=cloud,
                 budget_micros=body.budget_micros, expires_at=k.expires_at.isoformat()
                 if k.expires_at else None)
    db.commit()
    db.refresh(k)
    # The only time the key itself leaves the server.
    return {"key": raw, "api_key": key_out(k, limits), "gateway_url": gateway_url(request),
            "trust_url": trust_page(request)}


@router.patch("/keys/{key_id}")
def change_key(key_id: str, body: KeyPatchIn, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    k = _get(db, key_id, caller)
    own = k.user_id == caller.user.id
    fields = body.model_fields_set
    if not own and fields - set(LOCKS):
        raise fail(403, "forbidden", "On someone else's key an admin changes only the limits, the cloud setting and "
                                     "the budget.")
    if k.revoked_at is not None:
        raise fail(409, "revoked", "This key was revoked.")
    limits = gkeys.get_limits(db)
    admin = caller.user.role == "admin"
    fields_ = ("name", "models", *LOCKS, *LOCKS.values())
    before = {f: getattr(k, f) for f in fields_}
    cloud_settings = gkeys.get_cloud(db)
    new = {name: getattr(body, name) for name in LOCKS if name in fields}
    if "cloud" in new and new["cloud"] is None and not admin:
        new["cloud"] = cloud_settings["default"]  # "the default", for a user
    if not admin:
        # Only values that change count: a form that sends a locked value back unchanged is fine.
        new = {name: v for name, v in new.items() if v != getattr(k, name)}
        for name in new:
            by = getattr(k, LOCKS[name])
            if by:
                what = {"cloud": "cloud setting", "budget_micros": "monthly budget"}.get(name, f"{LIMIT_NAMES[name]} limit")
                raise fail(403, "limit_locked", f"An admin ({by}) set this key's {what}; only an admin can change "
                                                "it. You can still revoke the key.")
    _check_own(caller, k.user_id, limits, new.get("rpm"), new.get("tpm"), new.get("cloud"), cloud_settings["max"])
    if "name" in fields and body.name:
        k.name = body.name.strip()
    if "models" in fields:
        k.models = _models(db, body.models)
    for name, value in new.items():
        # A key always has a cloud setting: clearing it means the server's default for new keys.
        setattr(k, name, cloud_settings["default"] if name == "cloud" and value is None else value)
        if admin:
            # On someone else's key an admin's value is locked for its owner; clearing it unlocks.
            # On their own key there is nobody to lock it against.
            setattr(k, LOCKS[name], caller.user.username if (not own and value is not None) else None)
    after = {f: getattr(k, f) for f in fields_}
    changed = {f: {"before": before[f], "after": after[f]} for f in before if before[f] != after[f]}
    if changed:
        locked = [n for n, col in LOCKS.items() if after[col] and after[col] != before[col]]
        unlocked = [n for n, col in LOCKS.items() if before[col] and not after[col]]
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


@router.put("/keys/cloud")
def set_key_cloud(body: CloudIn, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    """The cloud setting a new key gets, and the most a user may give their own key. Existing keys keep
    theirs: raising the default never sends anything out for a key that was off."""
    if gkeys.cloud_above(body.default, body.max):
        raise fail(422, "default_over_max", "The default for new keys cannot be more than what users may choose.")
    before = gkeys.get_cloud(db)
    gkeys.set_cloud(db, body.model_dump(), caller.user.username)
    db.flush()
    after = gkeys.get_cloud(db)
    audit.record(db, "api_key_cloud.changed", actor=caller.user, ip=caller.ip, before=before, after=after)
    db.commit()
    return {"cloud": after}


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
