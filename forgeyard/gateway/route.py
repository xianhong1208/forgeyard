"""A route and its targets (forgeyard/gateway/proxy.py, steps 1 and 4): the route of a served name loaded
from the database, with its tiers of local deployments (their ready replicas) and provider models; who may
see and call it; and which tiers a request may use."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload
from starlette.requests import Request

from forgeyard.db import Deployment, GatewayRoute, ProviderModel, Replica, RouteTarget, Workload, as_utc
from forgeyard.gateway import classify, imagegen, pricing, speech, videogen
from forgeyard.gateway.balancer import Target
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.keys import KeyCaller
from forgeyard.machines.protocol import FEATURE_REPLICA_KEYS
from forgeyard.models import modalities
from forgeyard.security import replica_tls
from forgeyard.workloads import offline

# /v1/<endpoint> -> (the route protocol it needs, the body it takes)
ENDPOINTS: dict[str, tuple[str, str]] = {
    "chat/completions": ("openai-chat", "json"),
    "completions": ("openai-chat", "json"),
    "embeddings": ("openai-embeddings", "json"),
    "rerank": ("rerank", "json"),
    "score": ("rerank", "json"),
    "audio/transcriptions": ("openai-audio", "multipart"),
    "audio/translations": ("openai-audio", "multipart"),
    "audio/speech": ("openai-speech", "json"),
    "images/generations": ("openai-images", "json"),
    "videos": ("openai-videos", "video"),   # asynchronous jobs (forgeyard/gateway/videojobs.py), not forward()
    # 1.4: vision models, one image per request (multipart or JSON)
    "vision/detect": ("vision-detect", "vision"),
    "vision/segment": ("vision-segment", "vision"),
    "vision/depth": ("vision-depth", "vision"),
    "vision/classify": ("vision-classify", "vision"),   # 1.4.4: image classification (#141)
    # 1.4.4: text classification (forgeyard/gateway/classify.py)
    "classify": ("classify", "json"),
}
# Where an engine serves an endpoint when it is not /v1/<endpoint>: vLLM's /classify has no /v1.
UPSTREAM_PATHS = {"classify": classify.UPSTREAM_PATH}
CLOUD_APIS = ("openai-chat", "openai-embeddings")      # what provider targets serve in 1.2
CLOUD_ENDPOINTS = ("chat/completions", "completions", "embeddings")
LOCAL_ONLY_HEADER = "x-forgeyard-local-only"
MAX_WAIT_MS = 10_000
FIRST_ANSWER_DEFAULT = 120     # seconds a replica has to start answering a request it was sent (1.4.5)


# ------------------------------------------------------------------ routes
@dataclass
class LocalGroup:
    """A deployment in a tier: its ready replicas."""

    target_id: int
    deployment_id: int
    name: str
    upstream_model: str
    replicas: list[Target] = field(default_factory=list)
    reason: Optional[str] = None          # why none is ready
    concurrency: Optional[int] = None     # requests one replica takes at once (its `concurrency`)
    context: Optional[int] = None         # tokens per request (its `context`)
    running: bool = True                  # False once someone stopped the deployment
    accepts: Optional[modalities.Accepts] = None   # media and body size it takes (1.3)
    speaks: Optional[speech.Speaks] = None          # text to speech: formats, voices, input cap (1.3)
    draws: Optional[imagegen.Draws] = None          # image generation: caps, default size, queue (1.3)
    films: Optional[videogen.Films] = None          # video generation: caps, default size and length (1.3)
    first_answer: float = FIRST_ANSWER_DEFAULT      # seconds to start answering once sent (1.4.5)


@dataclass
class CloudTarget:
    """A provider's model in a tier. `external` False: a server inside your network, not cloud."""

    target_id: int
    provider_model_id: int
    provider_id: int
    provider: str
    kind: str
    base_url: str
    upstream_model: str
    external: bool
    allow_http: bool
    enabled: bool
    weight: int = 1
    timeout_s: Optional[int] = None
    headers: dict = field(default_factory=dict)
    drop_params: list = field(default_factory=list)
    api_key: Optional[str] = field(default=None, repr=False)   # never logged or returned
    prices: pricing.Prices = field(default_factory=pricing.Prices)
    budget_micros: Optional[int] = None   # the provider's monthly budget (None: none)

    @property
    def key(self) -> str:
        return f"pm:{self.provider_model_id}"

    @property
    def label(self) -> str:
        return f"provider:{self.provider}/{self.upstream_model}"


@dataclass
class Tier:
    index: int
    locals: list[LocalGroup] = field(default_factory=list)
    clouds: list[CloudTarget] = field(default_factory=list)


@dataclass
class Route:
    served_name: str
    api: str
    id: int = 0
    deployment_id: Optional[int] = None   # the deployment that created it
    local_only: bool = False
    spill_on_long_prompt: bool = False
    wait_ms: int = 0
    tiers: list[Tier] = field(default_factory=list)
    has_local: bool = False               # a target inside your network (deployment or own server)
    has_cloud: bool = False               # a provider outside your network

    @property
    def targets(self) -> list[Target]:
        """Every ready replica of every tier (what the route has locally right now)."""
        return [t for tier in self.tiers for g in tier.locals for t in g.replicas]

    @property
    def reason(self) -> Optional[str]:
        reasons = [g.reason for tier in self.tiers for g in tier.locals if g.reason]
        return "; ".join(reasons)[:400] if reasons else None


def _ready(r: Replica, w: Workload) -> bool:
    from forgeyard.models.routes import _ready as ready

    return ready(r, w)


def not_ready_reason(w: Workload) -> str:
    if w.desired_state != "running":
        return "The deployment is stopped."
    parts = []
    for r in w.replicas:
        where = f" on {r.node.name}" if r.node else " (not placed)"
        state = r.reason or r.state
        if r.node is not None and offline.skipped(r.node) and not r.final:
            state = f"the machine is {r.node.status}" + (f"; {r.reason}" if r.reason else "")  # 1.4.5 (#164)
        parts.append(f"replica {r.ordinal}{where}: {state}")
    return ("No replica is ready yet: " + "; ".join(parts))[:400] if parts else "The deployment has no replicas."


def _int(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def local_group(t: RouteTarget, d: Deployment, recipes: Optional[dict] = None) -> LocalGroup:
    w = d.workload
    replicas = []
    for r in w.replicas:
        if not _ready(r, w):
            continue
        port = next(iter(r.ports or []), None)
        if not port or not port.get("host_port") or not port.get("address"):
            continue
        dialed = replica_tls.dial(r, port)   # 1.4.5: https:// with the machine's binding, or an older agent's http://
        if dialed is None:
            continue
        protected = bool(r.secret) and r.node is not None and FEATURE_REPLICA_KEYS in (r.node.features or [])
        since = as_utc(r.ready_at).timestamp() if r.ready_at else 0.0
        replicas.append(Target(uid=r.uid, ordinal=r.ordinal, url=dialed[0],
                               machine=r.node.name if r.node else None, secret=r.secret, protected=protected,
                               ready_since=since, tls_name=dialed[1]))
    params = d.params or {}
    found = (recipes or {}).get(d.recipe)
    model = d.model
    accepts = modalities.accepts(found[0], model.meta or {}, model.format, params) \
        if found is not None and model is not None else None
    speaks = speech.speaks(found[0], params, model.meta if model is not None else None) \
        if found is not None and "tts" in found[0].tasks else None
    draws = imagegen.draws(found[0], params, model.meta if model is not None else None) \
        if found is not None and "image" in found[0].tasks else None
    films = videogen.films(found[0], params, model.meta if model is not None else None) \
        if found is not None and "video" in found[0].tasks else None
    return LocalGroup(target_id=t.id, deployment_id=d.id, name=d.name, upstream_model=t.upstream_model or d.served_name,
                      replicas=replicas, reason=None if replicas else not_ready_reason(w),
                      concurrency=draws.concurrency if draws else _int(params.get("concurrency")),
                      context=_int(params.get("context")), running=w.desired_state == "running", accepts=accepts,
                      speaks=speaks, draws=draws, films=films,
                      first_answer=float(_int(params.get("first_answer_timeout")) or FIRST_ANSWER_DEFAULT))


def cloud_target(t: RouteTarget, pm: ProviderModel) -> CloudTarget:
    p = pm.provider
    return CloudTarget(target_id=t.id, provider_model_id=pm.id, provider_id=p.id, provider=p.name, kind=p.kind,
                       base_url=p.base_url, upstream_model=pm.upstream_model, external=p.external,
                       allow_http=p.allow_http, enabled=p.enabled, weight=max(1, t.weight or 1),
                       timeout_s=p.timeout_s, headers=dict(p.headers or {}), drop_params=list(pm.drop_params or []),
                       api_key=p.api_key, prices=pricing.Prices.of(pm), budget_micros=p.budget_micros)


def _route_query():
    return (select(GatewayRoute).options(
        selectinload(GatewayRoute.targets).selectinload(RouteTarget.deployment).selectinload(Deployment.workload)
        .selectinload(Workload.replicas).selectinload(Replica.node),
        selectinload(GatewayRoute.targets).selectinload(RouteTarget.deployment).selectinload(Deployment.model),
        selectinload(GatewayRoute.targets).selectinload(RouteTarget.provider_model).selectinload(ProviderModel.provider)))


def build_route(row: GatewayRoute, recipes: Optional[dict] = None) -> Route:
    route = Route(served_name=row.served_name, api=row.api, id=row.id, deployment_id=row.deployment_id,
                  local_only=bool(row.local_only), spill_on_long_prompt=bool(row.spill_on_long_prompt),
                  wait_ms=max(0, min(int(row.wait_ms or 0), MAX_WAIT_MS)))
    tiers: dict[int, Tier] = {}
    for t in row.targets:
        # Whether the model is visible to a key depends on what the route has, enabled or not.
        if t.kind == "local" and t.deployment is not None:
            route.has_local = True
        elif t.kind == "provider" and t.provider_model is not None:
            if t.provider_model.provider.external:
                route.has_cloud = True
            else:
                route.has_local = True
        if not t.enabled:
            continue
        tier = tiers.setdefault(t.tier, Tier(t.tier))
        if t.kind == "local" and t.deployment is not None:
            tier.locals.append(local_group(t, t.deployment, recipes))
        elif t.kind == "provider" and t.provider_model is not None and row.api in CLOUD_APIS:
            ct = cloud_target(t, t.provider_model)
            if ct.enabled:
                tier.clouds.append(ct)
    route.tiers = [tiers[i] for i in sorted(tiers)]
    return route


def load_route(db: DbSession, served_name: str) -> Optional[Route]:
    from forgeyard.models.catalog import all_recipes

    row = db.scalar(_route_query().where(GatewayRoute.served_name == served_name))
    return build_route(row, all_recipes(db)) if row is not None else None


def model_names(db: DbSession) -> list[tuple[Route, int]]:
    """Every route, with the time it was created."""
    return [(build_route(row), int(row.created_at.timestamp()) if row.created_at else 0)
            for row in db.scalars(_route_query().order_by(GatewayRoute.served_name)).all()]


def endpoint_for(api: str) -> str:
    return {"openai-chat": "/v1/chat/completions", "openai-embeddings": "/v1/embeddings", "rerank": "/v1/rerank",
            "openai-audio": "/v1/audio/transcriptions", "openai-speech": "/v1/audio/speech",
            "openai-images": "/v1/images/generations", "openai-videos": "/v1/videos",
            "vision-detect": "/v1/vision/detect", "vision-segment": "/v1/vision/segment",
            "vision-depth": "/v1/vision/depth", "vision-classify": "/v1/vision/classify",
            "classify": "/v1/classify"}.get(api, "/v1")


def visible(cloud: str, has_local: bool) -> bool:
    """May a caller with this cloud setting see a route at all? A cloud-only model exists only for `on`."""
    return has_local or cloud == "on"


def serving(route: Route, cloud: str) -> bool:
    """Can this route answer a caller with this cloud setting, now or once its replicas are ready?

    Not when every deployment in it was stopped and nothing else in it is open to the caller: such a
    model is left out of /v1/models, so clients do not offer it. A deployment that is starting or
    restarting still counts. Calling a stopped model still answers 503 with the reason.
    """
    endpoint = {"openai-chat": "chat/completions", "openai-embeddings": "embeddings"}.get(route.api, "")
    # The same tiers a request with this cloud setting may use (a `fallback` key reaches the cloud only
    # after a local tier), so a listed model is one that can be called.
    return any(any(g.running for g in tier.locals) or tier.clouds
               for tier in allowed_tiers(route, cloud, False, endpoint))


def check_access(caller: KeyCaller, route: Optional[Route], model: str, endpoint: str) -> Route:
    if route is None or not visible(caller.cloud, route.has_local):
        raise GatewayError(404, "model_not_found", f"The model {model!r} does not exist. GET /v1/models lists yours.",
                           param="model")
    if caller.models is not None and route.served_name not in caller.models:
        raise GatewayError(403, "model_not_allowed", f"This API key may not use the model {model!r}.", param="model")
    need = ENDPOINTS[endpoint][0]
    if route.api != need:
        hint = (" A chat model classifies text with structured outputs: give the labels as "
                "structured_outputs.choice at /v1/chat/completions.") if endpoint == "classify" and \
            route.api == "openai-chat" else ""
        raise GatewayError(400, "wrong_endpoint", f"{model!r} is served at {endpoint_for(route.api)}, "
                                                  f"not /v1/{endpoint}.{hint}", param="model")
    return route


def wants_local_only(request: Request) -> bool:
    return (request.headers.get(LOCAL_ONLY_HEADER) or "").strip().lower() in ("1", "true", "yes", "on")


def allowed_tiers(route: Route, cloud: str, local_only: bool, endpoint: str) -> list[Tier]:
    """The tiers this request may use, with the targets it may not use removed."""
    no_cloud = route.local_only or local_only or cloud not in ("fallback", "on")
    out: list[Tier] = []
    seen_local = False
    for tier in route.tiers:
        own = [c for c in tier.clouds if not c.external]
        clouds = [c for c in tier.clouds if c.external]
        if no_cloud or (cloud == "fallback" and not seen_local):
            clouds = []
        providers = own + clouds if endpoint in CLOUD_ENDPOINTS else []
        if tier.locals or own:
            seen_local = True
        if tier.locals or providers:
            out.append(Tier(tier.index, list(tier.locals), providers))
    return out
