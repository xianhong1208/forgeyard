"""Gateway routes as the console shows them: a model name, its protocol, its targets in tiers (local
deployments and provider models, 1.2) and the ready replicas behind it. The gateway itself loads
routes with forgeyard/gateway/proxy.py (`load_route`).
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard.db import Deployment, GatewayRoute, ProviderModel, Replica, RouteTarget, Workload


def _ready(r: Replica, w: Workload) -> bool:
    if w.desired_state != "running" or r.final:
        return False
    health = (r.spec or w.spec or {}).get("health")
    return r.state == "healthy" or (r.state == "running" and not health)


def endpoints(w: Workload, *, ready_only: bool = False) -> list[dict]:
    """Each replica's own address (its first port), ready or not."""
    out = []
    for r in w.replicas:
        port = next(iter(r.ports or []), None)
        if not port or not port.get("host_port") or not port.get("address"):
            continue
        ready = _ready(r, w)
        if ready_only and not ready:
            continue
        host = port["address"]
        host = f"[{host}]" if ":" in host else host
        out.append({"replica": r.ordinal, "machine": r.node.name if r.node else None, "state": r.state,
                    "ready": ready, "url": f"http://{host}:{port['host_port']}/"})
    return out


def is_cloud(t: RouteTarget) -> bool:
    """A provider target outside your network (what a key's `cloud` setting is about)."""
    return t.kind == "provider" and t.provider_model is not None and bool(t.provider_model.provider.external)


def route_kind(route: GatewayRoute) -> str:
    """local: only targets inside your network; cloud: only cloud targets; mixed: both."""
    cloud = any(is_cloud(t) for t in route.targets)
    local = any(not is_cloud(t) for t in route.targets)
    return "mixed" if cloud and local else "cloud" if cloud else "local"


def target_out(t: RouteTarget) -> dict:
    out = {"id": t.id, "tier": t.tier, "kind": t.kind, "weight": t.weight, "enabled": t.enabled,
           "upstream_model": t.upstream_model, "cloud": is_cloud(t), "deployment": None, "provider_model": None}
    if t.kind == "local" and t.deployment is not None:
        d = t.deployment
        ready = endpoints(d.workload, ready_only=True) if d.workload else []
        params = d.params or {}
        out["deployment"] = {"id": d.id, "name": d.name, "served_name": d.served_name, "ready": len(ready),
                             "replicas": len(d.workload.replicas) if d.workload else 0,
                             "concurrency": params.get("concurrency"), "context": params.get("context")}
    elif t.kind == "provider" and t.provider_model is not None:
        pm = t.provider_model
        p = pm.provider
        out["upstream_model"] = pm.upstream_model
        out["provider_model"] = {"id": pm.id, "upstream_model": pm.upstream_model, "display_name": pm.display_name,
                                 "api": pm.api, "context_length": pm.context_length,
                                 "provider": {"id": p.id, "name": p.name, "kind": p.kind, "external": p.external,
                                              "enabled": p.enabled}}
    return out


def route_out(route: GatewayRoute) -> dict:
    ready: list[dict] = []
    owner_upstream: Optional[str] = None
    for t in route.targets:
        if t.kind == "local" and t.deployment is not None:
            if t.enabled:
                ready += endpoints(t.deployment.workload, ready_only=True)
            if owner_upstream is None or t.deployment_id == route.deployment_id:
                owner_upstream = t.upstream_model
    kind = route_kind(route)
    return {"id": route.id, "served_name": route.served_name, "api": route.api, "upstream_model": owner_upstream,
            "deployment_id": route.deployment_id, "local_only": bool(route.local_only),
            "spill_on_long_prompt": bool(route.spill_on_long_prompt), "wait_ms": route.wait_ms or 0,
            "kind": kind, "has_local": kind != "cloud", "has_cloud": kind != "local",
            "created_by": route.created_by, "endpoints": ready, "targets": [target_out(t) for t in route.targets]}


def query():
    return (select(GatewayRoute).options(
        selectinload(GatewayRoute.targets).selectinload(RouteTarget.deployment).selectinload(Deployment.workload)
        .selectinload(Workload.replicas).selectinload(Replica.node),
        selectinload(GatewayRoute.targets).selectinload(RouteTarget.provider_model).selectinload(ProviderModel.provider)))


def all_routes(db: DbSession) -> list[dict]:
    return [route_out(r) for r in db.scalars(query().order_by(GatewayRoute.served_name)).all()]


def resolve(db: DbSession, served_name: str) -> Optional[dict]:
    """A route by its model name, as the console shows it; None if nothing serves that name."""
    row = db.scalar(query().where(GatewayRoute.served_name == served_name))
    return route_out(row) if row is not None else None


def own_route(db: DbSession, deployment_id: int) -> Optional[GatewayRoute]:
    """The route a deployment was created with."""
    return db.scalar(query().where(GatewayRoute.deployment_id == deployment_id))
