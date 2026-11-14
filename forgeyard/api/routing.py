"""Gateway routes (1.2): a model name, its targets in tiers, and the spill rules.

Everyone signed in sees the routes (the playground and the deployment pages use them). Admins add
tiers to a deployment's route (a cloud model to spill to), make routes of their own (a cloud-only
model, or several deployments behind one name), and change the flags. A deployment's own route keeps
its deployment as a target and goes away with it.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import audit
from forgeyard.api.deployments import SERVED_NAME_RE
from forgeyard.api.deps import Caller, current, fail, require_admin
from forgeyard.db import Deployment, GatewayRoute, Provider, ProviderModel, RouteTarget, get_db
from forgeyard.gateway import keys as gkeys
from forgeyard.gateway.proxy import CLOUD_APIS, MAX_WAIT_MS, visible
from forgeyard.models import routes

router = APIRouter(prefix="/api", tags=["routes"])

APIS = ("openai-chat", "openai-embeddings", "rerank", "openai-audio", "openai-speech", "openai-images", "openai-videos")
MAX_TARGETS = 32
MAX_TIERS = 8


class TargetIn(BaseModel):
    tier: int = Field(default=0, ge=0, le=99)
    kind: str = Field(pattern=r"^(local|provider)$")
    deployment_id: Optional[int] = Field(default=None, ge=1)
    provider_model_id: Optional[int] = Field(default=None, ge=1)
    weight: int = Field(default=1, ge=1, le=100)
    enabled: bool = True


class RouteSettings(BaseModel):
    local_only: bool = False
    spill_on_long_prompt: bool = False
    wait_ms: int = Field(default=0, ge=0, le=MAX_WAIT_MS)
    targets: list[TargetIn] = Field(min_length=1, max_length=MAX_TARGETS)


class RouteIn(RouteSettings):
    served_name: str = Field(max_length=128)
    api: str = "openai-chat"


def _route(db: DbSession, route_id: int) -> GatewayRoute:
    row = db.scalar(routes.query().where(GatewayRoute.id == route_id))
    if row is None:
        raise fail(404, "not_found", "No such route.")
    return row


def deployment_api(db: DbSession, d: Deployment) -> Optional[str]:
    own = db.scalar(select(GatewayRoute.api).where(GatewayRoute.deployment_id == d.id))
    return own


def _targets(db: DbSession, api: str, body: RouteSettings, owner: Optional[int]) -> list[RouteTarget]:
    """The checked targets, tiers renumbered 0, 1, 2… in their order."""
    tiers = sorted({t.tier for t in body.targets})
    if len(tiers) > MAX_TIERS:
        raise fail(422, "too_many_tiers", f"At most {MAX_TIERS} tiers.")
    number = {t: i for i, t in enumerate(tiers)}
    seen: set[tuple[str, int]] = set()
    out = []
    for t in body.targets:
        if t.kind == "local":
            if t.deployment_id is None or t.provider_model_id is not None:
                raise fail(422, "bad_target", "A local target names a deployment (deployment_id).")
            d = db.get(Deployment, t.deployment_id)
            if d is None:
                raise fail(404, "deployment_not_found", f"No deployment #{t.deployment_id}.")
            if deployment_api(db, d) != api:
                raise fail(422, "wrong_api", f"{d.name} does not serve {api}.")
            own = db.scalar(select(RouteTarget.upstream_model).join(GatewayRoute, GatewayRoute.id == RouteTarget.route_id)
                            .where(GatewayRoute.deployment_id == d.id, RouteTarget.deployment_id == d.id))
            key = ("local", d.id)
            target = RouteTarget(tier=number[t.tier], kind="local", deployment_id=d.id, upstream_model=own or d.served_name,
                                 weight=t.weight, enabled=t.enabled)
        else:
            if t.provider_model_id is None or t.deployment_id is not None:
                raise fail(422, "bad_target", "A provider target names a provider model (provider_model_id).")
            pm = db.scalar(select(ProviderModel).where(ProviderModel.id == t.provider_model_id)
                           .options(selectinload(ProviderModel.provider)))
            if pm is None:
                raise fail(404, "provider_model_not_found", f"No provider model #{t.provider_model_id}.")
            if api not in CLOUD_APIS:
                raise fail(422, "wrong_api", "Provider models serve chat and embeddings only; this route serves "
                                             f"{api}.")
            if pm.api != api:
                raise fail(422, "wrong_api", f"{pm.provider.name}/{pm.upstream_model} is a {pm.api} model, not {api}.")
            key = ("provider", pm.id)
            target = RouteTarget(tier=number[t.tier], kind="provider", provider_model_id=pm.id,
                                 upstream_model=pm.upstream_model, weight=t.weight, enabled=t.enabled)
        if key in seen:
            raise fail(422, "duplicate_target", "The same target is listed twice.")
        seen.add(key)
        out.append(target)
    if owner is not None and ("local", owner) not in seen:
        raise fail(422, "owner_required", "A deployment's own route keeps that deployment as a target; delete the "
                                          "deployment to remove it.")
    return out


def _summary(targets: list[RouteTarget]) -> list[str]:
    return [f"{t.tier}:{t.kind}:{t.deployment_id or t.provider_model_id}{'' if t.enabled else ':off'}" for t in targets]


def _usable(db: DbSession, caller: Caller, out: list[dict]) -> list[dict]:
    cloud = gkeys.console_cloud(db, caller.user)
    return [{**r, "usable": visible(cloud, r["has_local"])} for r in out]


@router.get("/routes")
def list_routes(caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    """Every route with its targets; `usable`: whether the playground may call it (cloud-only models
    need the cloud setting the signed-in person may give a key)."""
    return {"routes": _usable(db, caller, routes.all_routes(db)), "cloud": gkeys.console_cloud(db, caller.user),
            "is_admin": caller.user.role == "admin"}


@router.get("/route-options")
def route_options(_: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    """What a route may point at: deployments (with the protocol they serve) and provider models."""
    deps = []
    for d in db.scalars(select(Deployment).order_by(Deployment.name)).all():
        deps.append({"id": d.id, "name": d.name, "served_name": d.served_name, "api": deployment_api(db, d)})
    models = []
    for pm in db.scalars(select(ProviderModel).options(selectinload(ProviderModel.provider))
                         .join(Provider, Provider.id == ProviderModel.provider_id)
                         .order_by(Provider.name, ProviderModel.upstream_model)).all():
        p = pm.provider
        models.append({"id": pm.id, "upstream_model": pm.upstream_model, "display_name": pm.display_name,
                       "api": pm.api, "provider": {"id": p.id, "name": p.name, "kind": p.kind, "external": p.external,
                                                   "enabled": p.enabled}})
    return {"deployments": deps, "provider_models": models}


@router.get("/routes/{route_id}")
def get_route(route_id: int, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    return {"route": _usable(db, caller, [routes.route_out(_route(db, route_id))])[0],
            "is_admin": caller.user.role == "admin"}


@router.post("/routes", status_code=201)
def create_route(body: RouteIn, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    """A route of its own: a cloud-only model, or several targets behind one new name."""
    name = body.served_name.strip()
    if not SERVED_NAME_RE.match(name):
        raise fail(422, "bad_served_name", "served_name: letters, digits and . _ : / - (at most 128)")
    if body.api not in APIS:
        raise fail(422, "bad_api", f"api: one of {', '.join(APIS)}")
    if db.scalar(select(GatewayRoute.id).where(GatewayRoute.served_name == name)) is not None:
        raise fail(409, "served_name_taken", f"Another deployment or route already serves the model name {name}.")
    targets = _targets(db, body.api, body, None)
    row = GatewayRoute(served_name=name, api=body.api, deployment_id=None, local_only=body.local_only,
                       spill_on_long_prompt=body.spill_on_long_prompt, wait_ms=body.wait_ms,
                       created_by=caller.user.username)
    row.targets.extend(targets)
    db.add(row)
    db.flush()
    audit.record(db, "route.created", actor=caller.user, target=name, ip=caller.ip, route_id=row.id, api=body.api,
                 targets=_summary(targets), local_only=body.local_only or None)
    db.commit()
    db.expire_all()
    return {"route": routes.route_out(_route(db, row.id))}


@router.put("/routes/{route_id}")
def change_route(route_id: int, body: RouteSettings, caller: Caller = Depends(require_admin),
                 db: DbSession = Depends(get_db)):
    """Replace the route's targets and flags (its name and protocol stay)."""
    row = _route(db, route_id)
    before = {"targets": _summary(row.targets), "local_only": row.local_only,
              "spill_on_long_prompt": row.spill_on_long_prompt, "wait_ms": row.wait_ms}
    targets = _targets(db, row.api, body, row.deployment_id)
    row.targets.clear()
    db.flush()
    row.targets.extend(targets)
    row.local_only, row.spill_on_long_prompt, row.wait_ms = body.local_only, body.spill_on_long_prompt, body.wait_ms
    db.flush()
    after = {"targets": _summary(targets), "local_only": row.local_only,
             "spill_on_long_prompt": row.spill_on_long_prompt, "wait_ms": row.wait_ms}
    changes = {k: {"before": before[k], "after": after[k]} for k in before if before[k] != after[k]}
    if changes:
        audit.record(db, "route.changed", actor=caller.user, target=row.served_name, ip=caller.ip, route_id=row.id,
                     changes=changes)
    db.commit()
    db.expire_all()
    return {"route": routes.route_out(_route(db, route_id))}


@router.delete("/routes/{route_id}")
def delete_route(route_id: int, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    row = _route(db, route_id)
    if row.deployment_id is not None:
        raise fail(409, "deployment_route", "This is a deployment's own route: it goes away with the deployment.")
    name = row.served_name
    db.delete(row)
    audit.record(db, "route.deleted", actor=caller.user, target=name, ip=caller.ip, route_id=route_id)
    db.commit()
    return {"ok": True}
