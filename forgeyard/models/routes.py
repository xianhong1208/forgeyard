"""Gateway routes: model name -> protocol -> ready replicas. Stored with every deployment in M3;
the M4 gateway (`/v1/*`) resolves requests with `resolve()` and balances over its endpoints.
"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard.db import GatewayRoute, Replica, Workload


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


def route_out(route: GatewayRoute, w: Workload) -> dict:
    return {"served_name": route.served_name, "api": route.api, "upstream_model": route.upstream_model,
            "deployment_id": route.deployment_id, "workload_id": route.workload_id,
            "endpoints": endpoints(w, ready_only=True)}


def all_routes(db: DbSession) -> list[dict]:
    rows = db.execute(select(GatewayRoute, Workload).join(Workload, Workload.id == GatewayRoute.workload_id)
                      .options(selectinload(Workload.replicas).selectinload(Replica.node))
                      .order_by(GatewayRoute.served_name)).all()
    return [route_out(route, w) for route, w in rows]


def resolve(db: DbSession, served_name: str) -> Optional[dict]:
    """What the gateway needs for one request's `model`: the protocol, the name to send upstream,
    and the ready replicas' base URLs. None if no deployment serves that name."""
    row = db.execute(select(GatewayRoute, Workload).join(Workload, Workload.id == GatewayRoute.workload_id)
                     .where(GatewayRoute.served_name == served_name)
                     .options(selectinload(Workload.replicas).selectinload(Replica.node))).first()
    if row is None:
        return None
    return route_out(*row)
