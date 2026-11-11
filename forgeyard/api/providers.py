"""Cloud providers (1.2): model APIs outside Forgeyard that routes may send requests to.

Admins add, change, test, clone and delete providers and their models. A provider's API key is
write-only: it is stored encrypted, never returned (only its last four characters are shown), never
logged or audited, and never copied when a provider is cloned. Everyone signed in may list the
providers (without their keys), so the route pages can name them.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import audit
from forgeyard.api.deps import Caller, current, fail, require_admin
from forgeyard.config import get_settings
from forgeyard.db import GatewayRoute, Provider, ProviderModel, RouteTarget, get_db, utcnow
from forgeyard.gateway import budgets as gbudgets, egress, quirks
from forgeyard.gateway.balancer import balancer
from forgeyard.gateway.pricing import PRICE_FIELDS

router = APIRouter(prefix="/api", tags=["providers"])

NAME_RE = r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$"
HEADER_NAME_RE = r"^[A-Za-z0-9-]{1,64}$"
# Headers a provider's extra headers may not set: the key goes in its own field, and the rest is
# the gateway's.
RESERVED_HEADERS = {"authorization", "proxy-authorization", "api-key", "x-api-key", "x-goog-api-key", "host",
                    "content-length", "content-type", "transfer-encoding", "connection", "cookie", "accept-encoding",
                    "anthropic-version", "user-agent"}
MAX_HEADERS = 16
APIS = ("openai-chat", "openai-embeddings")
TEST_TIMEOUT = 15.0


class ProviderIn(BaseModel):
    name: str = Field(pattern=NAME_RE)
    kind: str = Field(max_length=24)
    base_url: Optional[str] = Field(default=None, max_length=512)
    api_key: Optional[str] = Field(default=None, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict)
    external: Optional[bool] = None
    allow_http: bool = False
    enabled: bool = True
    timeout_s: Optional[int] = Field(default=None, ge=1, le=3600)


class ProviderPatch(BaseModel):
    name: Optional[str] = Field(default=None, pattern=NAME_RE)
    base_url: Optional[str] = Field(default=None, max_length=512)
    # Absent: keep the key. A string: replace it. clear_api_key: remove it.
    api_key: Optional[str] = Field(default=None, max_length=4096)
    clear_api_key: bool = False
    headers: Optional[dict[str, str]] = None
    external: Optional[bool] = None
    allow_http: Optional[bool] = None
    enabled: Optional[bool] = None
    timeout_s: Optional[int] = Field(default=None, ge=1, le=3600)


class CloneIn(BaseModel):
    name: str = Field(pattern=NAME_RE)


class ModelIn(BaseModel):
    upstream_model: str = Field(min_length=1, max_length=256)
    api: str = "openai-chat"
    display_name: Optional[str] = Field(default=None, max_length=128)
    context_length: Optional[int] = Field(default=None, ge=1, le=100_000_000)
    drop_params: list[str] = Field(default_factory=list, max_length=64)
    # Micro-USD per million tokens (forgeyard/gateway/pricing.py); None: unknown.
    price_input: Optional[int] = Field(default=None, ge=0, le=10**12)
    price_output: Optional[int] = Field(default=None, ge=0, le=10**12)
    price_cache_read: Optional[int] = Field(default=None, ge=0, le=10**12)
    price_cache_write: Optional[int] = Field(default=None, ge=0, le=10**12)


class ModelsIn(BaseModel):
    models: list[ModelIn] = Field(min_length=1, max_length=200)


class ModelPatch(BaseModel):
    display_name: Optional[str] = Field(default=None, max_length=128)
    context_length: Optional[int] = Field(default=None, ge=1, le=100_000_000)
    drop_params: Optional[list[str]] = Field(default=None, max_length=64)
    price_input: Optional[int] = Field(default=None, ge=0, le=10**12)
    price_output: Optional[int] = Field(default=None, ge=0, le=10**12)
    price_cache_read: Optional[int] = Field(default=None, ge=0, le=10**12)
    price_cache_write: Optional[int] = Field(default=None, ge=0, le=10**12)


# ------------------------------------------------------------------ output
def model_out(pm: ProviderModel) -> dict:
    key = f"pm:{pm.id}"
    return {"id": pm.id, "provider_id": pm.provider_id, "upstream_model": pm.upstream_model, "api": pm.api,
            "display_name": pm.display_name, "context_length": pm.context_length, "drop_params": pm.drop_params or [],
            "price_input": pm.price_input, "price_output": pm.price_output, "price_cache_read": pm.price_cache_read,
            "price_cache_write": pm.price_cache_write, "price_source": pm.price_source,
            "price_checked_at": pm.price_checked_at, "price_ref": pm.price_ref,
            "cooldown_s": round(balancer.cooldown_left(key), 1), "in_flight": balancer.snapshot(key)["in_flight"],
            "sent": balancer.snapshot(key)["served"], "created_at": pm.created_at}


def used_by(db: DbSession, model_ids: list[int]) -> dict[int, list[str]]:
    if not model_ids:
        return {}
    rows = db.execute(select(RouteTarget.provider_model_id, GatewayRoute.served_name)
                      .join(GatewayRoute, GatewayRoute.id == RouteTarget.route_id)
                      .where(RouteTarget.provider_model_id.in_(model_ids))).all()
    out: dict[int, list[str]] = {}
    for pm_id, name in rows:
        out.setdefault(pm_id, [])
        if name not in out[pm_id]:
            out[pm_id].append(name)
    return out


def provider_out(db: DbSession, p: Provider, *, admin: bool = True) -> dict:
    kind = quirks.get(p.kind)
    uses = used_by(db, [m.id for m in p.models])
    out = {"id": p.id, "name": p.name, "kind": p.kind, "kind_label": kind.label, "base_url": p.base_url,
           "has_key": bool(p.api_key), "key_hint": f"…{p.key_last4}" if p.key_last4 else None,
           "external": p.external, "allow_http": p.allow_http, "enabled": p.enabled, "timeout_s": p.timeout_s,
           "created_by": p.created_by, "created_at": p.created_at, "updated_at": p.updated_at,
           "models": [{**model_out(m), "routes": uses.get(m.id, [])} for m in p.models]}
    if p.external:
        from forgeyard.api.keys import budget_out

        spend = gbudgets.ledger.snapshot()
        out["budget"] = budget_out(p.budget_micros, spend.spent.get(("provider", p.id), 0),
                                   spend.held.get(("provider", p.id), 0))
    if admin:
        out["headers"] = dict(p.headers or {})
    return out


def kinds_out() -> list[dict]:
    return [{"kind": k.name, "label": k.label, "base_url": k.base_url, "base_url_hint": k.base_url_hint,
             "auth": k.auth, "embeddings": k.embeddings, "models_endpoint": k.models_endpoint,
             "default_external": k.default_external, "key_required": k.key_required, "note": k.note}
            for k in quirks.KINDS.values()]


def egress_out() -> dict:
    s = get_settings()
    return {"proxy": bool(s.egress_proxy.strip()), "allowed_hosts": s.egress_allowed_host_list,
            "ca_file": bool(s.egress_ca_file.strip()), "private_hosts": s.provider_allow_private_hosts,
            "cooldown_s": s.provider_cooldown_seconds, "read_timeout_s": s.provider_read_timeout}


# ------------------------------------------------------------------ checks
def _get(db: DbSession, provider_id: int) -> Provider:
    p = db.scalar(select(Provider).where(Provider.id == provider_id).options(selectinload(Provider.models)))
    if p is None:
        raise fail(404, "not_found", "No such provider.")
    return p


def _clean_headers(headers: dict[str, str]) -> dict[str, str]:
    if len(headers) > MAX_HEADERS:
        raise fail(422, "bad_headers", f"At most {MAX_HEADERS} extra headers.")
    out = {}
    for name, value in headers.items():
        name = name.strip()
        if not re.match(HEADER_NAME_RE, name):
            raise fail(422, "bad_headers", f"{name!r} is not a header name.")
        if name.lower() in RESERVED_HEADERS:
            raise fail(422, "bad_headers", f"{name} cannot be set here; the API key has its own field.")
        value = str(value)
        if len(value) > 512 or any(c in value for c in "\r\n\x00"):
            raise fail(422, "bad_headers", f"The value of {name} must be one line of at most 512 characters.")
        out[name] = value.strip()
    return out


def _clean_key(key: Optional[str]) -> Optional[str]:
    if key is None:
        return None
    key = key.strip()
    if not key:
        return None
    if any(c.isspace() for c in key) or not key.isprintable() or not key.isascii():
        raise fail(422, "bad_key", "The API key must be one word of printable characters.")
    return key


def _check_destination(kind: str, base_url: str, external: bool, allow_http: bool) -> None:
    if allow_http and external:
        raise fail(422, "bad_base_url", "Plain HTTP is only for a provider inside your network (not external).")
    try:
        egress.check_url(base_url, external=external, allow_http=allow_http)
    except egress.EgressError as e:
        raise fail(422, "bad_base_url", f"base_url: {e}.") from None


def _taken(db: DbSession, name: str, but: Optional[int] = None) -> None:
    q = select(Provider.id).where(Provider.name == name)
    if but is not None:
        q = q.where(Provider.id != but)
    if db.scalar(q) is not None:
        raise fail(409, "name_taken", f"A provider named {name} already exists.")


# ------------------------------------------------------------------ providers
@router.get("/providers")
def list_providers(caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    admin = caller.user.role == "admin"
    rows = db.scalars(select(Provider).options(selectinload(Provider.models)).order_by(Provider.name)).all()
    return {"providers": [provider_out(db, p, admin=admin) for p in rows], "kinds": kinds_out(),
            "egress": egress_out(), "is_admin": admin}


@router.get("/providers/{provider_id}")
def get_provider(provider_id: int, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    admin = caller.user.role == "admin"
    return {"provider": provider_out(db, _get(db, provider_id), admin=admin), "kinds": kinds_out(),
            "egress": egress_out(), "is_admin": admin}


@router.post("/providers", status_code=201)
def create_provider(body: ProviderIn, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    if body.kind not in quirks.KINDS:
        raise fail(422, "bad_kind", f"kind: one of {', '.join(quirks.KINDS)}")
    kind = quirks.KINDS[body.kind]
    name = body.name.strip()
    _taken(db, name)
    base_url = (body.base_url or "").strip().rstrip("/") or kind.base_url
    if not base_url:
        raise fail(422, "bad_base_url", f"{kind.label} needs your own base URL, like {kind.base_url_hint}.")
    external = kind.default_external if body.external is None else body.external
    _check_destination(body.kind, base_url, external, body.allow_http)
    key = _clean_key(body.api_key)
    p = Provider(name=name, kind=body.kind, base_url=base_url, api_key=key, key_last4=key[-4:] if key else None,
                 headers=_clean_headers(body.headers), external=external, allow_http=body.allow_http,
                 enabled=body.enabled, timeout_s=body.timeout_s, created_by=caller.user.username)
    db.add(p)
    db.flush()
    audit.record(db, "provider.created", actor=caller.user, target=name, ip=caller.ip, provider_id=p.id, kind=p.kind,
                 base_url=base_url, external=external, allow_http=body.allow_http or None, key=bool(key),
                 headers=sorted(p.headers) or None)
    db.commit()
    return {"provider": provider_out(db, _get(db, p.id))}


@router.patch("/providers/{provider_id}")
def change_provider(provider_id: int, body: ProviderPatch, caller: Caller = Depends(require_admin),
                    db: DbSession = Depends(get_db)):
    p = _get(db, provider_id)
    fields = body.model_fields_set
    changes: dict[str, Any] = {}
    if "name" in fields and body.name and body.name.strip() != p.name:
        _taken(db, body.name.strip(), p.id)
        changes["name"] = {"before": p.name, "after": body.name.strip()}
        p.name = body.name.strip()
    base_url = (body.base_url.strip().rstrip("/") if "base_url" in fields and body.base_url else p.base_url)
    external = body.external if "external" in fields and body.external is not None else p.external
    allow_http = body.allow_http if "allow_http" in fields and body.allow_http is not None else p.allow_http
    if external and "allow_http" not in fields:
        allow_http = False      # outside your network: https only
    _check_destination(p.kind, base_url, external, allow_http)
    for name, value in (("base_url", base_url), ("external", external), ("allow_http", allow_http)):
        if getattr(p, name) != value:
            changes[name] = {"before": getattr(p, name), "after": value}
            setattr(p, name, value)
    if "headers" in fields and body.headers is not None:
        headers = _clean_headers(body.headers)
        if headers != (p.headers or {}):
            changes["headers"] = {"before": sorted(p.headers or {}), "after": sorted(headers)}
            p.headers = headers
    for name in ("enabled", "timeout_s"):
        if name in fields and (name != "enabled" or getattr(body, name) is not None) and getattr(body, name) != getattr(p, name):
            changes[name] = {"before": getattr(p, name), "after": getattr(body, name)}
            setattr(p, name, getattr(body, name))
    if body.clear_api_key and p.api_key:
        p.api_key, p.key_last4 = None, None
        changes["api_key"] = "removed"
    elif "api_key" in fields and body.api_key is not None:
        key = _clean_key(body.api_key)
        if key:
            p.api_key, p.key_last4 = key, key[-4:]
            changes["api_key"] = "replaced"     # never the key itself
    if changes:
        p.updated_at = utcnow()
        audit.record(db, "provider.changed", actor=caller.user, target=p.name, ip=caller.ip, provider_id=p.id,
                     changes=changes)
    db.commit()
    return {"provider": provider_out(db, _get(db, p.id))}


@router.post("/providers/{provider_id}/clone", status_code=201)
def clone_provider(provider_id: int, body: CloneIn, caller: Caller = Depends(require_admin),
                   db: DbSession = Depends(get_db)):
    """A copy of the settings and models, **without the key** (enter one for the copy), disabled."""
    src = _get(db, provider_id)
    name = body.name.strip()
    _taken(db, name)
    p = Provider(name=name, kind=src.kind, base_url=src.base_url, api_key=None, key_last4=None,
                 headers=dict(src.headers or {}), external=src.external, allow_http=src.allow_http, enabled=False,
                 timeout_s=src.timeout_s, budget_micros=src.budget_micros, created_by=caller.user.username)
    for m in src.models:
        p.models.append(ProviderModel(upstream_model=m.upstream_model, api=m.api, display_name=m.display_name,
                                      context_length=m.context_length, drop_params=list(m.drop_params or []),
                                      price_input=m.price_input, price_output=m.price_output,
                                      price_cache_read=m.price_cache_read, price_cache_write=m.price_cache_write,
                                      price_source=m.price_source, price_checked_at=m.price_checked_at,
                                      price_ref=m.price_ref))
    db.add(p)
    db.flush()
    audit.record(db, "provider.cloned", actor=caller.user, target=name, ip=caller.ip, provider_id=p.id,
                 source=src.name, models=len(p.models))
    db.commit()
    return {"provider": provider_out(db, _get(db, p.id))}


@router.delete("/providers/{provider_id}")
def delete_provider(provider_id: int, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    p = _get(db, provider_id)
    uses = sorted({n for names in used_by(db, [m.id for m in p.models]).values() for n in names})
    if uses:
        raise fail(409, "in_use", f"Routes use this provider's models: {', '.join(uses)}. Remove them from those "
                                  "routes first, or disable the provider.", routes=uses)
    name = p.name
    db.delete(p)
    audit.record(db, "provider.deleted", actor=caller.user, target=name, ip=caller.ip, provider_id=provider_id)
    db.commit()
    return {"ok": True}


async def _upstream_get(p: Provider, path: str) -> tuple[int, Any, float]:
    """GET {base}/{path} from the provider, with its key, through the egress rules."""
    try:
        url, host_headers, extensions = await egress.prepare(p.base_url, path, external=p.external,
                                                             allow_http=p.allow_http)
    except egress.EgressError as e:
        raise fail(422, "egress_refused", f"{e}.") from None
    headers = {k: str(v) for k, v in (p.headers or {}).items()}
    headers.update({"Accept": "application/json", "User-Agent": "forgeyard-gateway", **host_headers})
    headers.update(quirks.auth_headers(p.kind, p.api_key))
    started = time.monotonic()
    try:
        r = await egress.client().get(url, headers=headers, extensions=extensions,
                                      timeout=httpx.Timeout(TEST_TIMEOUT, connect=min(TEST_TIMEOUT, 10.0)))
        data = r.content[:4 * 1024**2]
    except httpx.HTTPError as e:
        raise fail(502, "provider_unreachable", f"{p.name} could not be reached ({type(e).__name__}).") from None
    ms = (time.monotonic() - started) * 1000
    if 300 <= r.status_code < 400:
        return r.status_code, None, ms
    try:
        return r.status_code, json.loads(data), ms
    except ValueError:
        return r.status_code, data[:300].decode("utf-8", "replace"), ms


def _listed(kind: str, obj) -> list[dict]:
    items = obj.get("data") if isinstance(obj, dict) else obj if isinstance(obj, list) else None
    out = []
    for m in items or []:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str):
            continue
        mid = quirks.model_id(kind, m["id"])[:256]
        ctx = m.get("context_length")
        if not isinstance(ctx, int) and isinstance(m.get("top_provider"), dict):
            ctx = m["top_provider"].get("context_length")
        name = m.get("display_name") or m.get("name")
        guess = "openai-embeddings" if "embed" in mid.lower() else "openai-chat"
        out.append({"id": mid, "owned_by": m.get("owned_by") if isinstance(m.get("owned_by"), str) else None,
                    "context_length": ctx if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0 else None,
                    "api": guess, "display_name": name[:128] if isinstance(name, str) else None})
    return out


def _problem(p: Provider, status: int, obj) -> str:
    from forgeyard.gateway.proxy import CloudTarget, provider_error

    stub = CloudTarget(target_id=0, provider_model_id=0, provider_id=p.id, provider=p.name, kind=p.kind,
                       base_url=p.base_url, upstream_model="", external=p.external, allow_http=p.allow_http,
                       enabled=p.enabled, api_key=p.api_key)
    raw = json.dumps(obj).encode() if not isinstance(obj, str) else obj.encode()
    if status in (401, 403):
        return f"{p.name} refused the key (HTTP {status}). Check the key{' and its workspace' if p.kind == 'anthropic' else ''}."
    if 300 <= status < 400:
        return f"{p.name} answered with a redirect ({status}); Forgeyard does not follow them. Check the base URL."
    return provider_error(stub, status, raw).message


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: int, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    """GET {base_url}/models with the provider's key: can Forgeyard reach it, and does it take the key?"""
    p = _get(db, provider_id)
    status, obj, ms = await _upstream_get(p, "models")
    ok = 200 <= status < 300
    models = _listed(p.kind, obj) if ok else []
    return {"ok": ok, "status": status, "ms": round(ms), "models": len(models),
            "sample": [m["id"] for m in models[:5]], "message": None if ok else _problem(p, status, obj)}


@router.get("/providers/{provider_id}/upstream-models")
async def upstream_models(provider_id: int, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    """What the provider lists at /models, to pick from when adding models."""
    p = _get(db, provider_id)
    status, obj, _ms = await _upstream_get(p, "models")
    if not 200 <= status < 300:
        raise fail(502, "provider_error", _problem(p, status, obj))
    have = {(m.upstream_model, m.api) for m in p.models}
    kind = quirks.get(p.kind)
    out = []
    for m in _listed(p.kind, obj):
        if m["api"] == "openai-embeddings" and not kind.embeddings:
            continue
        out.append({**m, "added": (m["id"], m["api"]) in have})
    return {"models": sorted(out, key=lambda m: m["id"])[:2000]}


# ------------------------------------------------------------------ provider models
def _model_values(p: Provider, m: ModelIn) -> dict:
    if m.api not in APIS:
        raise fail(422, "bad_api", f"api: {' or '.join(APIS)} (cloud models serve chat and embeddings)")
    if m.api == "openai-embeddings" and not quirks.get(p.kind).embeddings:
        raise fail(422, "bad_api", f"{quirks.get(p.kind).label} has no embeddings API.")
    drop = sorted({d.strip() for d in m.drop_params if d and d.strip()})
    if any(len(d) > 64 for d in drop):
        raise fail(422, "bad_params", "drop_params: names of at most 64 characters")
    prices = {k: getattr(m, k) for k in ("price_input", "price_output", "price_cache_read", "price_cache_write")}
    return {"upstream_model": m.upstream_model.strip(), "api": m.api,
            "display_name": (m.display_name or "").strip() or None, "context_length": m.context_length,
            "drop_params": drop, **prices,
            "price_source": "manual" if any(v is not None for v in prices.values()) else None,
            "price_checked_at": utcnow() if any(v is not None for v in prices.values()) else None}


@router.post("/providers/{provider_id}/models", status_code=201)
def add_models(provider_id: int, body: ModelsIn, caller: Caller = Depends(require_admin),
               db: DbSession = Depends(get_db)):
    p = _get(db, provider_id)
    have = {(m.upstream_model, m.api) for m in p.models}
    added = []
    for m in body.models:
        values = _model_values(p, m)
        if (values["upstream_model"], values["api"]) in have:
            continue
        have.add((values["upstream_model"], values["api"]))
        p.models.append(ProviderModel(**values))
        added.append(values["upstream_model"])
    if added:
        audit.record(db, "provider.models_added", actor=caller.user, target=p.name, ip=caller.ip, provider_id=p.id,
                     models=added[:50])
    db.commit()
    return {"provider": provider_out(db, _get(db, p.id)), "added": added}


def _get_model(db: DbSession, model_id: int) -> ProviderModel:
    pm = db.scalar(select(ProviderModel).where(ProviderModel.id == model_id).options(selectinload(ProviderModel.provider)))
    if pm is None:
        raise fail(404, "not_found", "No such provider model.")
    return pm


@router.patch("/provider-models/{model_id}")
def change_model(model_id: int, body: ModelPatch, caller: Caller = Depends(require_admin),
                 db: DbSession = Depends(get_db)):
    pm = _get_model(db, model_id)
    fields = body.model_fields_set
    changes = {}
    for name in ("display_name", "context_length", "drop_params", "price_input", "price_output", "price_cache_read",
                 "price_cache_write"):
        if name not in fields:
            continue
        value = getattr(body, name)
        if name == "drop_params":
            value = sorted({d.strip() for d in (value or []) if d and d.strip()})
        if name == "display_name":
            value = (value or "").strip() or None
        if value != getattr(pm, name):
            changes[name] = {"before": getattr(pm, name), "after": value}
            setattr(pm, name, value)
    if any(n.startswith("price_") for n in changes):
        if all(getattr(pm, n) is None for n in PRICE_FIELDS):
            pm.price_source, pm.price_checked_at = None, None
        else:
            pm.price_source, pm.price_checked_at = "manual", utcnow()
    if changes:
        audit.record(db, "provider.model_changed", actor=caller.user, target=pm.provider.name, ip=caller.ip,
                     model=pm.upstream_model, changes=changes)
    db.commit()
    db.refresh(pm)
    return {"model": model_out(pm)}


@router.delete("/provider-models/{model_id}")
def delete_model(model_id: int, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    pm = _get_model(db, model_id)
    uses = used_by(db, [pm.id]).get(pm.id, [])
    if uses:
        raise fail(409, "in_use", f"Routes use this model: {', '.join(uses)}. Remove it from them first.", routes=uses)
    audit.record(db, "provider.model_removed", actor=caller.user, target=pm.provider.name, ip=caller.ip,
                 model=pm.upstream_model)
    db.delete(pm)
    db.commit()
    return {"ok": True}
