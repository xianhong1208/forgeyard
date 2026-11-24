"""The Playground's Vision tab: vision models (1.4), tried from the console as the signed-in user.

Vision models are deployments of detection, SAM 2, depth and image classification models
(forgeyard/models/vision.py); a zero-shot detector (1.4.4, #142) is marked `zero_shot`, and the tab asks what to
find. The tab sends their requests to `/api/playground/vision/detect|segment|depth|classify` with the model's name,
which is the /v1 gateway path for the signed-in person (forgeyard/api/gateway.py); `options()` lists them with how
many replicas are ready.

The exposed vision apps of 1.3 (made from the workload templates, reached through
`/api/playground/vision/<name>/<task>`) were removed in 1.5.3 (#89): that route answers 410 (forgeyard/removed.py).
"""

from __future__ import annotations

from sqlalchemy.orm import Session as DbSession

from forgeyard.db import Deployment, GatewayRoute
from forgeyard.gateway import proxy
from forgeyard.models import vision as vision_models
from forgeyard.models.catalog import all_recipes


def model_options(db: DbSession) -> list[dict]:
    """The vision models (1.4): every route of a vision task, with its ready replicas (or why none) and the
    catalog model behind its deployment."""
    recipes = all_recipes(db)
    out = []
    apis = {api: task for task, api in vision_models.API.items()}
    for row in db.scalars(proxy._route_query().where(GatewayRoute.api.in_(list(apis)))
                          .order_by(GatewayRoute.served_name)).all():
        route = proxy.build_route(row, recipes)
        ready = len(route.targets)
        running = any(g.running for tier in route.tiers for g in tier.locals)
        d = db.get(Deployment, row.deployment_id) if row.deployment_id else None
        m = d.model if d is not None else None
        task = apis[row.api]
        out.append({"name": row.served_name, "task": task, "title": vision_models.LABEL[task],
                    "endpoint": f"/v1/{vision_models.ENDPOINT[task]}", "deployment_id": row.deployment_id,
                    "model": ((m.meta or {}).get("repo") or m.source) if m is not None else None,
                    "licence": (m.meta or {}).get("license") if m is not None else None,
                    # 1.4.4 (#142): a zero-shot detector finds what the request names (`labels`)
                    # (1.4.4, #141) and a zero-shot image classifier the labels to choose from
                    "zero_shot": bool(m is not None and task in ("detect", "image-classify")
                                      and vision_models.is_zero_shot(m.meta or {})),
                    "ready": ready, "running": running, "reason": None if ready else route.reason})
    return out


def options(db: DbSession) -> dict:
    """The vision models, for the Playground's Vision tab."""
    return {"models": model_options(db)}
