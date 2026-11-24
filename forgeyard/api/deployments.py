"""Model deployments: a catalog model + a recipe + parameters, expanded into a service workload
(placed by the model scheduler) and a gateway route (served at /v1 by the gateway, M4).

Admins and operators estimate, create, change (1.3.1), stop, start and delete deployments; everyone
signed in sees them and can send a short test request ("try it") to a ready replica, and use them
through the gateway with an API key or the playground.

Changing a deployment (PATCH, 1.3.1) checks the new estimate and placement like a deploy, saves, and
restarts the replicas on the new settings: one at a time when there are several (the next one once the
restarted one is ready), so the model keeps answering; a single replica is stopped and started again.
Each replica goes back to the machine it ran on when that machine still takes it.

1.5.2 (#95): a change may also move the deployment to another version of its engine: the built-in engine and its
copies ("Try this version"). The name, served name, keys and routes stay; the replicas restart as for any other
change. A change that restarts the only replica of a running deployment says first about how long the model will
not answer, and how to avoid it (`outage`).
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from pathlib import Path
from typing import Annotated, Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import audit
from forgeyard.api.deps import Caller, can_run_workloads, current, fail, require_operator
from forgeyard.api.workloads import _events, _get as get_workload, workload_out
from forgeyard.db import (Accelerator, CatalogModel, Deployment, GatewayRoute, Node, Replica, RouteTarget, Workload, get_db,
                          utcnow)
from forgeyard.gateway import imagegen, proxy, waking
from forgeyard.gateway.speech import wav_ms
from forgeyard.machines.hub import hub
from forgeyard.models import catalog, encoders, modalities, routes, scheduler, textcls, trials, vision
from forgeyard.models.catalog import Context
from forgeyard.models.estimate import EstimateError, estimate as compute_estimate
from forgeyard.models.recipes import Recipe
from forgeyard.security import replica_tls
from forgeyard.workloads import autoscale, placement, sleep, starts, state as wstate
from forgeyard.workloads.spec import NAME_RE, WorkloadSpec, parse_bytes

router = APIRouter(prefix="/api", tags=["deployments"])

SERVED_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
CLIP = Path(__file__).resolve().parent.parent / "models" / "assets" / "jfk-ask-not.mp3"
TRY_TIMEOUT = 120.0
TRY_IMAGE_TIMEOUT = 300.0     # an image on a CPU takes a while


class DeploymentIn(BaseModel):
    name: str = Field(max_length=48)
    model: str = Field(max_length=64)               # catalog name (or id)
    recipe: str = Field(max_length=40)
    served_name: Optional[str] = Field(default=None, max_length=128)
    params: dict[str, Any] = Field(default_factory=dict)
    replicas: int = Field(default=1, ge=1, le=16)
    machine_id: Optional[int] = Field(default=None, ge=1)
    # For gated repositories: sent only to the agents that download the model.
    hf_token: Optional[str] = Field(default=None, max_length=512)
    # Create it even though nothing fits right now; it waits as pending.
    wait: bool = False


def _model(db: DbSession, ref: str) -> CatalogModel:
    m = db.scalar(select(CatalogModel).where(CatalogModel.name == ref))
    if m is None and ref.isdigit():
        m = db.get(CatalogModel, int(ref))
    if m is None:
        raise fail(404, "model_not_found", f"No model {ref!r} in the catalog.")
    return m


# 1.4.5: how long a replica has to start answering a request it was sent (the gateway's, not the engine's:
# changing it restarts nothing). Unset: the gateway's default, 120 s (forgeyard/gateway/proxy.py).
FIRST_ANSWER_MIN, FIRST_ANSWER_MAX = 5, 3600
# 1.5.0 (#37): sleep when idle and the idle time are the server's too (forgeyard/workloads/sleep.py), and so are
# automatic replicas (forgeyard/workloads/autoscale.py).
GATEWAY_PARAMS = ("first_answer_timeout",) + sleep.PARAMS + autoscale.PARAMS


def normalise_params(recipe: Recipe, raw: dict) -> dict:
    """The deployment's parameters, checked against the recipe: its own (context, concurrency…)
    and the common ones (gpus, share, vram, extra_args, first_answer_timeout, and 1.5.0's sleep_when_idle,
    idle_minutes and automatic replicas). Unknown names are refused."""
    out: dict = {}
    has_gpu = any(v.gpu for v in recipe.variants)
    has_cpu = any(not v.gpu for v in recipe.variants)
    for name, p in recipe.params.items():
        try:
            out[name] = p.coerce(name, raw.get(name, p.default))
        except ValueError as e:
            raise fail(422, "invalid_params", str(e)) from None
    gpus = raw.get("gpus", 1 if has_gpu else 0)
    try:
        gpus = int(gpus)
    except (TypeError, ValueError):
        raise fail(422, "invalid_params", "gpus must be a whole number") from None
    if not 0 <= gpus <= 16:
        raise fail(422, "invalid_params", "gpus: 0 (CPU) to 16")
    if gpus == 0 and not has_cpu:
        raise fail(422, "invalid_params", f"{recipe.name} runs on GPUs only: gpus must be at least 1")
    if gpus > 0 and not has_gpu:
        raise fail(422, "invalid_params", f"{recipe.name} runs on the CPU only: gpus must be 0")
    out["gpus"] = gpus
    share = raw.get("share") or recipe.share
    if share not in ("exclusive", "vram"):
        raise fail(422, "invalid_params", "share: exclusive or vram")
    out["share"] = share
    if raw.get("vram") not in (None, "", 0):
        try:
            out["vram"] = parse_bytes(raw["vram"])
        except ValueError as e:
            raise fail(422, "invalid_params", f"vram: {e}") from None
    extra = raw.get("extra_args") or []
    if not isinstance(extra, list) or len(extra) > 32 or not all(isinstance(a, str) for a in extra):
        raise fail(422, "invalid_params", "extra_args: a list of at most 32 strings")
    if any(len(a) > 1024 or any(c in a for c in "\x00\r\n") for a in extra):
        raise fail(422, "invalid_params", "extra_args: one line each, at most 1024 characters")
    if extra:
        out["extra_args"] = list(extra)
    wait = raw.get("first_answer_timeout")
    if wait not in (None, ""):
        if all(t in ("image", "video") for t in recipe.tasks):
            raise fail(422, "invalid_params", "first_answer_timeout: image and video deployments use their own "
                                              "timeout")
        try:
            wait = int(str(wait).strip())   # True, 1.5 and "soon" are not whole numbers of seconds
        except ValueError:
            raise fail(422, "invalid_params", "first_answer_timeout must be a whole number of seconds") from None
        if not FIRST_ANSWER_MIN <= wait <= FIRST_ANSWER_MAX:
            raise fail(422, "invalid_params", f"first_answer_timeout: {FIRST_ANSWER_MIN} to {FIRST_ANSWER_MAX} seconds")
        out["first_answer_timeout"] = wait
    if why := sleep.normalise(raw, out):
        raise fail(422, "invalid_params", why)
    if why := autoscale.normalise(raw, out, video=autoscale.is_video(recipe)):
        raise fail(422, "invalid_params", why)
    unknown = sorted(set(raw) - set(out) - {"vram", "extra_args", "first_answer_timeout", *sleep.PARAMS,
                                            *autoscale.PARAMS})
    if unknown:
        raise fail(422, "invalid_params", f"{recipe.name} has no parameter {unknown[0]!r}")
    return out


def build_context(db: DbSession, body: DeploymentIn) -> tuple[Context, Recipe, CatalogModel]:
    m = _model(db, body.model)
    recipe = catalog.get_recipe(db, body.recipe)
    if recipe is None:
        raise fail(404, "recipe_not_found", f"No recipe {body.recipe!r}.")
    if m.format not in recipe.formats:
        raise fail(422, "incompatible", f"{recipe.name} reads {', '.join(recipe.formats)} models; {m.name} is {m.format}.")
    if m.task not in recipe.tasks:
        raise fail(422, "incompatible", f"{recipe.name} serves {', '.join(recipe.tasks)}; {m.name} is a {m.task} model.")
    if why := vision.deploy_refusal(m.task, m.meta or {}, recipe):
        # 1.4.4 (#142): a zero-shot detector the vision server cannot load: one that needs timm (OmDet-Turbo, added
        # before 1.4.3), or one on a vision image from before 1.4.4; an image classifier (#141) on such an image too.
        raise fail(422, *why)
    if why := encoders.deploy_refusal(m.task, m.meta or {}, recipe):
        # 1.5.4 (#141, #144): the transformers server runs image embedders and text classifiers only, and on a vision
        # image from 1.5.4 on; vLLM's pooling runner no NLI or other non-vLLM classifier, and no image embedder
        # (an entry added before these rules).
        raise fail(422, *why)
    params = normalise_params(recipe, body.params)
    fit_default_context(recipe, body.params, params, m.meta or {})
    device = "gpu" if params["gpus"] > 0 else "cpu"
    try:
        est = compute_estimate(m.meta or {}, recipe, params, device=device, gpu_context=scheduler.gpu_context(),
                               task=m.task)
    except EstimateError as e:
        raise fail(422, "cannot_estimate", str(e)) from None
    if body.machine_id is not None and db.get(Node, body.machine_id) is None:
        raise fail(404, "machine_not_found", "No such machine.")
    ctx = Context(None, m, recipe, params, est, device, params["share"], params["gpus"], body.machine_id, body.replicas)
    return ctx, recipe, m


def _served_name(body: DeploymentIn) -> str:
    name = (body.served_name or body.name).strip()
    if not SERVED_NAME_RE.match(name):
        raise fail(422, "bad_served_name", "served_name: letters, digits and . _ : / - (at most 128)")
    return name


def _previewing(ctx: Context, body: DeploymentIn, m: CatalogModel) -> None:
    """A context for a deployment that does not exist yet: rendering it needs a served name only."""
    served = (body.served_name or body.name or m.name).strip() or m.name

    class _Dep:
        served_name = served

    ctx.deployment = _Dep()  # type: ignore[assignment]


# The deploy form's context hint (D9) rounds the largest context that fits down to this many tokens.
CONTEXT_STEP = 1024


def max_context_of(meta: dict) -> Optional[int]:
    """The longest context a model itself supports (config.json or the GGUF metadata), if known."""
    n = (meta.get("config") or {}).get("max_context") or (meta.get("gguf") or {}).get("context_length")
    try:
        return int(n) if n and int(n) > 0 else None
    except (TypeError, ValueError):
        return None


def model_max_context(m: CatalogModel) -> Optional[int]:
    """The longest context the model itself supports (config.json or the GGUF metadata), if known."""
    return max_context_of(m.meta or {})


def fit_default_context(recipe: Recipe, raw: Optional[dict], params: dict, meta: dict) -> None:
    """1.4.4: a context the request does not name is the recipe's default, or the model's own maximum when that is
    smaller (a 512-token BERT classifier on a recipe whose default is 8192), so the default is one the model takes.
    A context the request names is checked by the estimate as before (D9)."""
    p = recipe.params.get("context")
    if p is None or "context" in (raw or {}) or not isinstance(params.get("context"), int):
        return
    top = max_context_of(meta)
    if top and params["context"] > top:
        params["context"] = max(int(p.min or 1), top)


def context_hint(db: DbSession, body: DeploymentIn, recipe: Recipe, m: CatalogModel) -> Optional[dict]:
    """For a recipe with a `context` parameter: the model's maximum, and the largest context (a multiple
    of CONTEXT_STEP, at most the model's maximum) with which every replica still fits right now, on
    the chosen machine or anywhere. The default context stays as it is (D9); the form offers these as
    one click. `fits_max` is None when nothing fits at the smallest step, or the memory is overridden
    (then the context does not change what is reserved)."""
    p = recipe.params.get("context")
    if p is None or p.type != "int":
        return None
    top = model_max_context(m)
    out = {"param": "context", "model_max": top, "fits_max": None}
    if p.max is not None:
        top = min(top, int(p.max)) if top else None
    if not top or (body.params or {}).get("vram") not in (None, "", 0):
        return out
    low = max(int(p.min or 1), 1)

    def fits(n: int) -> bool:
        trial = body.model_copy(update={"params": {**(body.params or {}), "context": n}})
        try:
            c, _, _ = build_context(db, trial)
        except HTTPException:  # the estimate refuses this size: it does not fit
            return False
        _previewing(c, body, m)
        return bool(scheduler.preview(db, c)["fits"])

    if fits(top):
        out["fits_max"] = top
        return out
    lo, hi = 0, top // CONTEXT_STEP          # in steps: lo fits (0 = nothing yet), hi does not
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if mid * CONTEXT_STEP >= low and fits(mid * CONTEXT_STEP):
            lo = mid
        else:
            hi = mid
    out["fits_max"] = lo * CONTEXT_STEP if lo else None
    return out


@router.post("/deployments/estimate")
def estimate(body: DeploymentIn, _: Caller = Depends(require_operator), db: DbSession = Depends(get_db)):
    """The estimate and where the replicas would go right now, before anything is created; for a model
    with a context length, also the model's maximum and the largest context that fits now."""
    ctx, recipe, m = build_context(db, body)
    if autoscale.setting(ctx.params).on:
        ctx.replicas = autoscale.setting(ctx.params).floor   # 1.5.0: it starts with its minimum
    _previewing(ctx, body, m)
    return {"estimate": ctx.estimate.as_dict(), "placement": scheduler.preview(db, ctx), "params": ctx.params,
            "recipe": recipe.name, "model": m.name, "context_hint": context_hint(db, body, recipe, m)}


def _endpoints(w: Workload) -> list[dict]:
    """Each replica's own address, with what the gateway sent it since the server started. Whether
    it requires the internal key (protected), and whether on every path (guarded: its agent's key
    guard) or only on the engine's own /v1 routes, is shown; the key never is."""
    from forgeyard.gateway.balancer import balancer
    from forgeyard.machines.protocol import FEATURE_REPLICA_GUARD, FEATURE_REPLICA_KEYS

    by_ordinal = {r.ordinal: r for r in w.replicas}
    out = []
    for e in routes.endpoints(w, tls=True):
        r = by_ordinal.get(e["replica"])
        features = (r.node.features or []) if r and r.node else []
        protected = bool(r and r.secret and FEATURE_REPLICA_KEYS in features)
        guarded = protected and FEATURE_REPLICA_GUARD in features
        # 1.4.5 (#163): whether the gateway reaches it over TLS (the binding name itself stays on the server).
        shown = {k: v for k, v in e.items() if k != "tls_name"}
        out.append({**shown, "protected": protected, "guarded": guarded, "encrypted": e.get("tls_name") is not None,
                    **(balancer.snapshot(r.uid) if r else {})})
    return out


def _pinned(db: DbSession, w: Workload) -> Optional[dict]:
    """The machine a person chose for the deployment (pinned), or None (Forgeyard chooses)."""
    machine_id = (w.spec or {}).get("machine_id")
    if machine_id is None:
        return None
    node = db.get(Node, machine_id)
    return {"id": machine_id, "name": node.name if node is not None else None}


def _rollout(w: Workload) -> Optional[dict]:
    """While an edit restarts the replicas: how many run the new settings."""
    live = [r for r in w.replicas if not r.final]
    old = [r for r in live if r.generation < w.generation]
    if not old or w.desired_state != "running":
        return None
    return {"generation": w.generation, "updated": len(live) - len(old), "total": len(live),
            "next": min(r.ordinal for r in old)}


def _languages(meta: dict) -> Optional[list[str]]:
    """The languages a model's card names (as the catalog's `languages`), or None: unknown (1.5.4)."""
    langs = meta.get("languages")
    return [str(x) for x in langs] if isinstance(langs, list) and langs else None


def deployment_out(db: DbSession, d: Deployment, caller: Caller, *, detail: bool = False) -> dict:
    w = d.workload
    route = routes.own_route(db, d.id)
    own = next((t for t in route.targets if t.kind == "local" and t.deployment_id == d.id), None) if route else None
    out = {
        "id": d.id, "name": d.name, "recipe": d.recipe, "served_name": d.served_name, "params": d.params,
        "model": {"id": d.model.id, "name": d.model.name, "source": d.model.source, "task": d.model.task,
                  "format": d.model.format, "revision": d.model.revision, "file": d.model.file,
                  "size_bytes": d.model.size_bytes,
                  # 1.4: what it takes besides text, so the pages list a chat model as multimodal chat.
                  "modalities": modalities.of_model(d.model.meta or {}, d.model.format),
                  # 1.4.4: a text classifier's labels, so the Playground names every probability.
                  **({"classify": textcls.out(d.model.meta or {})} if d.model.task == textcls.TASK else {}),
                  # 1.5.4: what an embedding model takes (text, image or both), for Try it and the Playground.
                  **({"embed": encoders.embed_of(d.model.meta or {})} if d.model.task == "embed" else {}),
                  # 1.5.4: the languages its card names, so the Playground warns before Chinese texts go to an
                  # English-only classifier or CLIP.
                  **({"languages": _languages(d.model.meta or {})} if d.model.task in ("embed", textcls.TASK) else {})},
        "estimate": d.estimate, "has_token": bool(d.hf_token), "created_by": d.created_by, "created_at": d.created_at,
        "route": {"served_name": route.served_name, "api": route.api,
                  "upstream_model": own.upstream_model if own else d.served_name}
        if route else None,
        "endpoints": _endpoints(w),
        "workload": workload_out(w, caller),
        "pinned": _pinned(db, w),
        "rollout": _rollout(w),
        # 1.5.0 (#37): sleep when idle, the last request this server saw, and when it would go to sleep.
        "sleep": sleep.status(db, d),
        # 1.5.0 (#37): how long its latest start took, from its events ({seconds, phases, source, at, replica}).
        "last_start": starts.last_start(db, w.id),
        # 1.5.0 (#37): automatic replicas, the load and the last decision.
        "autoscale": autoscale.autoscaler.status(d),
    }
    if detail:
        from forgeyard.api.keys import deployment_usage

        out["routing"] = routes.route_out(route) if route else None
        # Other routes this deployment is a target of (an admin added it to them).
        out["also_in"] = [r.served_name for r in db.scalars(
            select(GatewayRoute).join(RouteTarget, RouteTarget.route_id == GatewayRoute.id)
            .where(RouteTarget.deployment_id == d.id, GatewayRoute.id != (route.id if route else 0))).all()]

        out["cards"] = live_cards(db, w)
        out["usage"] = deployment_usage(db, d)
        # 1.5.2 (#95): the engine it runs (a copy says which built-in it is based on), and the versions Edit offers.
        recipe = catalog.get_recipe(db, d.recipe)
        out["engine"] = engine_out(recipe) if recipe is not None else None
        out["engines"] = engine_choices(db, d)
    return out


def live_cards(db: DbSession, w: Workload) -> list[dict]:
    """The cards (or machine RAM) the replicas hold, as their agents last reported them."""
    out = []
    for r in w.replicas:
        if r.node_id is None:
            continue
        keys = [g["key"] for g in r.gpus or []]
        rows = db.scalars(select(Accelerator).where(Accelerator.node_id == r.node_id)).all()
        for a in rows:
            if (keys and a.key in keys) or (not keys and a.vendor == "cpu"):
                out.append({"replica": r.ordinal, "machine": r.node.name if r.node else None, "key": a.key,
                            "index": a.index if a.vendor != "cpu" else None, "vendor": a.vendor, "model": a.model,
                            "total": a.vram_total, "free": a.vram_free, "used": a.vram_used, "reserved": r.vram,
                            "updated_at": a.updated_at})
    return out


def _get(db: DbSession, dep_id: int) -> Deployment:
    d = db.scalar(select(Deployment).where(Deployment.id == dep_id)
                  .options(selectinload(Deployment.workload).selectinload(Workload.replicas).selectinload(Replica.node)))
    if d is None or trials.is_trial(db, d.id):
        # 1.5.2 (#95): a trial's temporary deployment is managed by its trial only (the engine page), never here.
        raise fail(404, "not_found", "No such deployment.")
    return d


@router.post("/deployments", status_code=201)
def create_deployment(body: DeploymentIn, caller: Caller = Depends(require_operator), db: DbSession = Depends(get_db)):
    name = body.name.strip()
    if not NAME_RE.match(name):
        raise fail(422, "bad_name", "name: 1-48 characters, a-z, 0-9 and '-', not starting or ending with '-'")
    body.name = name
    if db.scalar(select(Workload.id).where(Workload.name == name)) is not None:
        raise fail(409, "name_taken", f"A workload or deployment named {name} already exists.")
    served = _served_name(body)
    if db.scalar(select(GatewayRoute.id).where(GatewayRoute.served_name == served)) is not None:
        raise fail(409, "served_name_taken", f"Another deployment or route already serves the model name {served}.")
    ctx, recipe, m = build_context(db, body)
    if autoscale.setting(ctx.params).on:
        # 1.5.0 (#37): automatic replicas start with their minimum (at least 1); the autoscaler adds the rest.
        body.replicas = ctx.replicas = autoscale.setting(ctx.params).floor

    class _Dep:
        served_name = served

    ctx.deployment = _Dep()  # type: ignore[assignment]
    with placement._lock:
        plan = scheduler.preview(db, ctx)
    if not plan["fits"] and not body.wait:
        raise fail(409, "does_not_fit", plan["summary"], machines=plan["machines"], estimate=ctx.estimate.as_dict())
    spec = WorkloadSpec.model_validate({
        "name": name, "kind": "service", "runtime": {"type": "model", "recipe": recipe.name, "model": m.name},
        "ports": [{"name": "http"}],
        "health": {"type": "http", "path": recipe.health.path, "interval": recipe.health.interval,
                   "timeout": recipe.health.timeout, "failure_threshold": recipe.health.failure_threshold,
                   "start_period": recipe.health.start_period},
        "replicas": body.replicas, "machine_id": body.machine_id,
    }).model_dump(mode="json")
    w = Workload(name=name, kind="service", runtime="model", spec=spec, desired_state="running", generation=1,
                 owner_id=caller.user.id, created_by=caller.user.username)
    db.add(w)
    db.flush()
    for i in range(body.replicas):
        w.replicas.append(Replica(ordinal=i, generation=1, state="pending", gpus=[], ports=[]))
    d = Deployment(name=name, model_id=m.id, recipe=recipe.name, params=ctx.params, served_name=served,
                   workload_id=w.id, estimate=ctx.estimate.as_dict(), hf_token=(body.hf_token or "").strip() or None,
                   created_by=caller.user.username)
    db.add(d)
    db.flush()
    upstream = (m.meta or {}).get("repo") or served if recipe.upstream_model == "repo" else served
    route = GatewayRoute(served_name=served, api=recipe.api_for(m.task), deployment_id=d.id,
                         created_by=caller.user.username)
    route.targets.append(RouteTarget(tier=0, kind="local", deployment_id=d.id, upstream_model=upstream, weight=1,
                                     enabled=True))
    db.add(route)
    what = f"{ctx.estimate.amount / 1024**3:.2f} GiB {'per GPU' if ctx.device == 'gpu' else 'of RAM'}"
    wstate.event(db, w, "created", f"Deployment of {m.name} with {recipe.name} by {caller.user.username}; "
                                   f"estimated {what}.")
    audit.record(db, "deployment.created", actor=caller.user, target=name, ip=caller.ip, deployment_id=d.id,
                 workload_id=w.id, model=m.name, recipe=recipe.name, served_name=served, replicas=body.replicas,
                 machine_id=body.machine_id, gpus=ctx.gpus, share=ctx.share, estimate=ctx.estimate.amount,
                 token=bool(d.hf_token) or None)
    db.commit()
    sleep.idle.forget(d.id)   # 1.5.0: a removed deployment's id may come back (SQLite): start from nothing
    waking.forget(d.id)
    proxy.forget_ceiling()  # its max_body_mb may raise how much the gateway reads
    placement.place_and_poke(db)
    db.expire_all()
    d = _get(db, d.id)
    return {"deployment": deployment_out(db, d, caller), "placement": plan}


@router.get("/deployments")
def list_deployments(caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    rows = db.scalars(select(Deployment).where(trials.not_trial())
                      .options(selectinload(Deployment.workload).selectinload(Workload.replicas)
                               .selectinload(Replica.node))
                      .order_by(Deployment.created_at.desc(), Deployment.id.desc())).all()
    # 1.5.2 (#95): trials of engines that run now are not deployments; Serving and the machine pages name them.
    return {"deployments": [deployment_out(db, d, caller) for d in rows], "server_time": utcnow(),
            "can_manage": can_run_workloads(caller.user),
            "trials": [trials.brief_out(t) for t in trials.running(db)]}


@router.get("/deployments/{dep_id}")
def get_deployment(dep_id: int, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    d = _get(db, dep_id)
    return {"deployment": deployment_out(db, d, caller, detail=True), "events": _events(db, d.workload),
            "server_time": utcnow(), "can_manage": can_run_workloads(caller.user)}


@router.post("/deployments/{dep_id}/stop")
def stop_deployment(dep_id: int, caller: Caller = Depends(require_operator), db: DbSession = Depends(get_db)):
    from forgeyard.api.workloads import stop_workload

    d = _get(db, dep_id)
    stop_workload(d.workload_id, caller, db)
    audit.record(db, "deployment.stopped", actor=caller.user, target=d.name, ip=caller.ip, deployment_id=d.id)
    db.commit()
    db.expire_all()
    return {"deployment": deployment_out(db, _get(db, dep_id), caller)}


@router.post("/deployments/{dep_id}/start")
def start_deployment(dep_id: int, caller: Caller = Depends(require_operator), db: DbSession = Depends(get_db)):
    from forgeyard.api.workloads import start_workload

    d = _get(db, dep_id)
    start_workload(d.workload_id, caller, db)
    audit.record(db, "deployment.started", actor=caller.user, target=d.name, ip=caller.ip, deployment_id=d.id)
    db.commit()
    db.expire_all()
    return {"deployment": deployment_out(db, _get(db, dep_id), caller)}


# ------------------------------------------------------------------ change (1.3.1)
COMMON_PARAMS = ("gpus", "share", "vram", "extra_args") + GATEWAY_PARAMS


class DeploymentEdit(BaseModel):
    """What a change may set. A field left out keeps its value; in `params`, only the names sent
    change, and a name sent as null goes back to the recipe's default (`vram`: back to the estimate).
    The model, the name and the served name stay: deploy a new one to change them. The engine (`recipe`) may move
    between a built-in engine and its copies (1.5.2, #95)."""
    model_config = ConfigDict(extra="forbid")

    params: dict[str, Any] = Field(default_factory=dict)
    # 1.5.2 (#95): another version of the same engine: the built-in one or a copy of it ("Try this version").
    recipe: Optional[str] = Field(default=None, max_length=40)
    replicas: Optional[int] = Field(default=None, ge=1, le=16)
    # Sent as null: Forgeyard chooses (automatic). Sent as an id: pinned to that machine.
    machine_id: Optional[int] = Field(default=None, ge=1)
    # Save even though it does not fit now: the restarted replicas wait for room.
    wait: bool = False
    # Only check: the estimate, where it would go and what would restart. Nothing is saved.
    dry_run: bool = False


# ------------------------------------------------------------------ another version of the engine (1.5.2, #95)
def engine_family(r: Recipe) -> str:
    """The built-in engine a recipe is (its own name) or was copied from (`based_on`)."""
    return r.based_on or r.name


def switch_problem(current: Recipe, new: Recipe, m: CatalogModel) -> Optional[tuple[str, str]]:
    """Why a deployment on `current` cannot move to `new` (code, text), or None. Only between a built-in engine and its
    copies, and only to one that serves this model the same way (the route, the keys and the served name stay)."""
    family = engine_family(current)
    if engine_family(new) != family:
        return ("engine_not_related", f"{new.name} is not {family} or a copy of it: a deployment moves only between a "
                                      f"built-in engine and its copies. Deploy {m.name} again to use another engine.")
    if m.format not in new.formats or m.task not in new.tasks:
        return ("incompatible", f"{new.name} does not serve {m.name} ({m.task}, {m.format}).")
    if new.api_for(m.task) != current.api_for(m.task) or new.upstream_model != current.upstream_model:
        return ("engine_not_compatible", f"{new.name} answers {m.task} requests another way than {current.name}, so the "
                                         "deployment's route would change. Deploy the model again with it.")
    return None


def engine_out(r: Recipe) -> dict:
    return {"name": r.name, "title": r.title, "version": r.version, "builtin": r.builtin, "based_on": r.based_on}


def engine_choices(db: DbSession, d: Deployment) -> list[dict]:
    """The engines the deployment can move to in Edit (its own first): its built-in engine and the copies of it that
    serve its model the same way."""
    recipe = catalog.get_recipe(db, d.recipe)
    if recipe is None or d.model is None:
        return []
    out = []
    for r, _ in catalog.all_recipes(db).values():
        if r.name == recipe.name or switch_problem(recipe, r, d.model) is None:
            # 1.5.2 (#95, PR 4): its latest trial (with this model first), so Edit can say how it went. Never a gate.
            out.append({**engine_out(r), "current": r.name == recipe.name,
                        "last_trial": trials.last_for(db, r.name, d.model_id)})
    out.sort(key=lambda e: (not e["builtin"], _version_key(e["version"]), e["name"]))
    return out


def _version_key(text: str) -> tuple:
    from forgeyard.models.engineversions import version_key

    return version_key(text or "")


# What a start is expected to take when this deployment has none measured and no other deployment of the engine
# has either: rough figures from the real runs (vLLM on an A100: 116-126 s for a 4B model, 1.5.0 acceptance;
# llama.cpp a few seconds once its image is there). Anything else: the gateway's START_DEFAULT.
TYPICAL_START = {"vllm": 120, "vllm-omni": 150, "llama.cpp": 15, "stable-diffusion.cpp": 30, "speaches": 20,
                 "forgeyard-vision": 20}
TYPICAL_SAMPLES = 20


def engine_start(db: DbSession, engine: str, but: int) -> Optional[float]:
    """The median of the latest starts of the other deployments of this engine on this server, if any."""
    names = {n for n, (r, _) in catalog.all_recipes(db).items() if r.engine == engine}
    ids = [w for w in db.scalars(select(Deployment.workload_id).where(Deployment.recipe.in_(names))).all() if w != but]
    if not ids:
        return None
    from forgeyard.db import WorkloadEvent

    found = []
    for e in db.scalars(select(WorkloadEvent).where(WorkloadEvent.workload_id.in_(ids),
                                                    WorkloadEvent.kind == starts.READY_KIND)
                        .order_by(WorkloadEvent.id.desc()).limit(TYPICAL_SAMPLES)).all():
        got = starts.parse(e.message)
        if got is not None:
            found.append(float(got["seconds"]))
    if not found:
        return None
    found.sort()
    return found[len(found) // 2]


def outage(db: DbSession, d: Deployment, w: Workload, recipe: Recipe, *, switched: bool, auto_on: bool) -> dict:
    """1.5.2 (#95): the one running replica restarts, so the model does not answer for about `seconds`: from its last
    start, else the other deployments of the engine, else a typical figure. Another engine version may take longer
    on its first start (it may compile; its image may have to come first), and the ways to avoid the gap: a second
    replica first, automatic replicas, and, when its route has a cloud tier, keys that may use the cloud are answered
    there meanwhile. Only says so: nothing else changes."""
    from forgeyard.gateway import waking
    from forgeyard.models.pages import engine_on

    last = starts.last_start(db, w.id)
    if last is not None:
        seconds, source = float(last["seconds"]), "last_start"
    elif (seen := engine_start(db, recipe.engine, w.id)) is not None:
        seconds, source = seen, "engine"
    else:
        seconds, source = float(TYPICAL_START.get(recipe.engine, waking.START_DEFAULT)), "typical"
    missing = []
    if switched:
        for r in w.replicas:
            if r.node is not None and not r.final and engine_on(r.node, recipe)["present"] is False:
                missing.append(r.node.name)
    route = routes.own_route(db, d.id)
    cloud = route is not None and any(t.enabled and routes.is_cloud(t) for t in route.targets)
    avoid = ["add_replica", "min_replicas" if auto_on else "autoscale"]
    if autoscale.is_video(recipe):
        avoid = ["add_replica"]
    if cloud:
        avoid.append("cloud")
    secs = max(1, round(seconds))
    text = f"It has one replica: the model does not answer for about {secs} s while it restarts"
    text += (" (its last start)" if source == "last_start" else " (other deployments of this engine)"
             if source == "engine" else " (a typical start of this engine)")
    if switched:
        text += "; a new engine version can take longer on its first start (it may compile first)"
    if missing:
        text += f", and its image is not on {', '.join(sorted(set(missing)))} yet: Download to… fetches it ahead"
    text += ". To avoid it, raise the replicas to 2 in this change when there is room for two (the new one starts first)"
    if "autoscale" in avoid:
        text += " or turn on automatic replicas"
    if "min_replicas" in avoid:
        text += " (a minimum of 2 automatic replicas)"
    if cloud:
        text += "; keys that may use the cloud are answered by its cloud tier meanwhile"
    return {"seconds": secs, "source": source, "first_start": switched, "image_missing": sorted(set(missing)),
            "avoid": avoid, "cloud": cloud, "text": text + "."}


def _restart_mode(running: bool, restart: bool, replicas: int, asleep: bool = False) -> str:
    if not running:
        return "on_wake" if asleep else "on_start"
    if not restart:
        return "none"
    return "rolling" if replicas > 1 else "all"


RESTART_TEXT = {
    "none": "Nothing restarts.",
    "on_start": "It is stopped: the new settings apply when it is started.",
    "on_wake": "It is asleep: the new settings apply when it wakes.",
    "rolling": "The replicas restart one at a time; each waits until the one before it is ready.",
    "all": "Its replica restarts: the model does not answer until the new one is ready.",
}


@router.patch("/deployments/{dep_id}")
def edit_deployment(dep_id: int, body: DeploymentEdit, caller: Caller = Depends(require_operator),
                    db: DbSession = Depends(get_db)):
    """Change GPU sharing, context, concurrency and the other recipe parameters, the number of
    replicas and the machine (pinned or automatic). Checked like a deploy before anything is saved."""
    d = _get(db, dep_id)
    w = d.workload
    current = catalog.get_recipe(db, d.recipe)
    if current is None:
        raise fail(409, "recipe_gone", f"Its recipe {d.recipe} no longer exists, so it cannot be changed. "
                                       "Deploy the model again with another recipe.")
    recipe = current
    if body.recipe is not None and body.recipe != d.recipe:
        # 1.5.2 (#95): another version of its engine (the built-in one, or a copy of it).
        recipe = catalog.get_recipe(db, body.recipe)
        if recipe is None:
            raise fail(404, "recipe_not_found", f"No engine {body.recipe!r}.")
        if why := switch_problem(current, recipe, d.model):
            raise fail(422, *why)
        if why := trials.switch_policy(db, d, recipe):
            # 1.5.2 (#95, PR 4): the seam for a policy on trials (enterprise later); it allows every switch today.
            raise fail(409, *why)
    switched = recipe.name != d.recipe
    known = set(recipe.params) | set(COMMON_PARAMS)
    params = {k: v for k, v in (d.params or {}).items() if k in known}  # names an updated recipe dropped
    # 1.5.2: what the deployment set that this engine does not know (another version may have dropped a parameter).
    dropped = sorted(k for k in (d.params or {}) if k not in known and k not in body.params)
    for k, v in body.params.items():
        if v is None:
            params.pop(k, None)
        else:
            params[k] = v
    spec = dict(w.spec or {})
    machine_id = body.machine_id if "machine_id" in body.model_fields_set else spec.get("machine_id")
    replicas = body.replicas if body.replicas is not None else len(w.replicas)
    ctx, _, m = build_context(db, DeploymentIn(name=d.name, model=d.model.name, recipe=recipe.name, params=params,
                                               replicas=replicas, machine_id=machine_id))
    ctx.deployment = d
    auto = autoscale.setting(ctx.params)
    if auto.on:
        # 1.5.0 (#37): the autoscaler sets the number; a change keeps the current one within the new bounds.
        if body.replicas is not None and body.replicas != len(w.replicas):
            raise fail(422, "replicas_automatic", "The number of replicas is automatic: change min_replicas and "
                                                  "max_replicas, or turn autoscale off to set a number.")
        replicas = ctx.replicas = autoscale.clamp(len(w.replicas), auto)
    running = w.desired_state == "running"
    live = [r for r in w.replicas if not r.final]
    params_changed = ctx.params != (d.params or {})

    def for_engine(p: dict) -> dict:
        return {k: v for k, v in p.items() if k not in GATEWAY_PARAMS}

    # Only the server reads first_answer_timeout (1.4.5) and the sleep settings (1.5.0): changing them alone
    # restarts nothing.
    engine_changed = for_engine(ctx.params) != for_engine(d.params or {})
    moved = machine_id is not None and any(r.node_id is not None and r.node_id != machine_id for r in live)
    restart = running and (engine_changed or moved or switched)
    mode = _restart_mode(running, restart, replicas, asleep=w.desired_state == sleep.ASLEEP)
    changes: dict[str, dict] = {}
    if switched:
        changes["recipe"] = {"before": d.recipe, "after": recipe.name}
    for k in sorted(set(ctx.params) | set(d.params or {})):
        if (d.params or {}).get(k) != ctx.params.get(k):
            changes[k] = {"before": (d.params or {}).get(k), "after": ctx.params.get(k)}
    if replicas != len(w.replicas):
        changes["replicas"] = {"before": len(w.replicas), "after": replicas}
    if machine_id != spec.get("machine_id"):
        changes["machine_id"] = {"before": spec.get("machine_id"), "after": machine_id}
    with placement._lock:
        plan = scheduler.preview(db, ctx, release={r.id for r in w.replicas},
                                 prefer={r.ordinal: r.node_id for r in w.replicas if r.node_id is not None})
    out = {"estimate": ctx.estimate.as_dict(), "placement": plan, "params": ctx.params, "changes": changes,
           "restart": mode, "restart_text": RESTART_TEXT[mode], "recipe": recipe.name, "dropped": dropped,
           # 1.5.2 (#95): its only replica restarts: about how long the model does not answer, and how to avoid it.
           "outage": outage(db, d, w, recipe, switched=switched, auto_on=auto.on) if mode == "all" else None}
    if body.dry_run:
        return out
    if running and (restart or replicas > len(w.replicas)) and not plan["fits"] and not body.wait:
        raise fail(409, "does_not_fit", plan["summary"], machines=plan["machines"], estimate=ctx.estimate.as_dict())
    if not changes:
        return {**out, "deployment": deployment_out(db, d, caller)}
    nodes: set[int] = set()
    if params_changed or switched or ctx.estimate.as_dict() != d.estimate:
        # 1.4.5 (#166): the estimate from the recipe as it is now, even when no parameter changed (no restart for it).
        d.params = ctx.params
        d.estimate = ctx.estimate.as_dict()
    if switched:
        # 1.5.2 (#95): the replicas render the new engine (models/catalog.py reads deployments.recipe); its health
        # check comes with it. The route, its keys and the served name stay.
        d.recipe = recipe.name
        spec["runtime"] = {**(spec.get("runtime") or {}), "recipe": recipe.name}
        spec["health"] = {**(spec.get("health") or {}), "path": recipe.health.path, "interval": recipe.health.interval,
                          "timeout": recipe.health.timeout, "failure_threshold": recipe.health.failure_threshold,
                          "start_period": recipe.health.start_period}
    spec["replicas"], spec["machine_id"] = replicas, machine_id
    w.spec = spec
    w.updated_at = utcnow()
    if restart:
        w.generation += 1
        for r in w.replicas:
            if r.final:  # ended for good: nothing to wait for, it starts again on the new settings now
                placement.remember(r)
                wstate.reset_replica(r, w.generation)
    ordered = sorted(w.replicas, key=lambda r: r.ordinal)
    for r in ordered[replicas:]:
        if r.node_id is not None and not r.final:
            nodes.add(r.node_id)  # its agent stops it: it is no longer desired
        placement.forget(r.id)
        w.replicas.remove(r)  # delete-orphan: the row goes
    for i in range(len(ordered), replicas):
        w.replicas.append(Replica(ordinal=i, generation=w.generation, state="pending" if running else "stopped",
                                  gpus=[], ports=[]))
    def shown(k: str, value) -> str:
        if k == "machine_id":
            node = db.get(Node, value) if value is not None else None
            return "automatic" if value is None else (node.name if node is not None else f"machine {value}")
        return str(value)

    said = ", ".join(f"{'machine' if k == 'machine_id' else 'engine' if k == 'recipe' else k} "
                     f"{shown(k, v['before'])} → {shown(k, v['after'])}"
                     for k, v in changes.items() if k != "extra_args")
    if "extra_args" in changes:
        said = ", ".join(x for x in (said, "extra engine arguments") if x)
    wstate.event(db, w, "changed", f"Changed by {caller.user.username}: {said}. {RESTART_TEXT[mode]}")
    audit.record(db, "deployment.changed", actor=caller.user, target=d.name, ip=caller.ip, deployment_id=d.id,
                 workload_id=w.id, changes=changes, restart=mode, estimate=ctx.estimate.amount, model=m.name,
                 **({"recipe": {"from": changes["recipe"]["before"], "to": recipe.name},
                     "engine_versions": {"from": current.version, "to": recipe.version}} if switched else {}),
                 **({"dropped": dropped} if dropped else {}))
    db.commit()
    proxy.forget_ceiling()  # max_body_mb may have changed
    for node_id in nodes:
        hub.poke(node_id)
    placement.place_and_poke(db)  # the first restart (placement.roll), and new replicas
    db.expire_all()
    return {**out, "deployment": deployment_out(db, _get(db, dep_id), caller)}


@router.delete("/deployments/{dep_id}")
def delete_deployment(dep_id: int, caller: Caller = Depends(require_operator), db: DbSession = Depends(get_db)):
    d = _get(db, dep_id)
    w = get_workload(db, d.workload_id)
    nodes = {r.node_id for r in w.replicas if r.node_id is not None and not r.final}
    name = d.name
    db.delete(d)
    db.delete(w)
    audit.record(db, "deployment.deleted", actor=caller.user, target=name, ip=caller.ip, deployment_id=dep_id)
    db.commit()
    sleep.idle.forget(dep_id)   # 1.5.0: its last request and any wake go with it (a new one may get its id)
    waking.forget(dep_id)
    for node_id in nodes:
        hub.poke(node_id)
    placement.place_and_poke(db)
    return {"ok": True}


# ------------------------------------------------------------------ try it
class TryIn(BaseModel):
    input: Optional[str] = Field(default=None, max_length=500)
    stream: bool = False
    # 1.5.4: the labels a zero-shot text classifier chooses from (default: TRY_ZERO_SHOT_LABELS).
    labels: Optional[list[Annotated[str, Field(max_length=64)]]] = Field(default=None, max_length=32)


# What Try it asks a zero-shot text classifier (1.5.4) when no labels are given: the default text is about technology.
TRY_ZERO_SHOT_LABELS = ["technology", "sports", "cooking"]
TRY_EMBED_TEXT = "Forgeyard runs models on your own machines."


def _post(url: str, body: bytes, content_type: str, secret: Optional[str] = None,
          tls_name: Optional[str] = None) -> tuple[int, dict | str]:
    speech = content_type == SPEECH_CTYPE
    plain = speech or content_type == IMAGE_CTYPE
    headers = {"Content-Type": "application/json" if plain else content_type, "Accept": "*/*" if speech else "application/json",
               "Accept-Encoding": "identity", "User-Agent": "forgeyard-console"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"  # the replica's internal key (M4)
    try:
        # 1.4.5 (#163): the way the gateway reaches it: TLS with the machine's binding, or an older agent's plain HTTP.
        status, raw = replica_tls.request_sync("POST", url, tls_name, headers=headers, content=body,
                                               timeout=TRY_IMAGE_TIMEOUT if content_type == IMAGE_CTYPE else TRY_TIMEOUT)
    except httpx.HTTPError as e:
        raise fail(502, "replica_unreachable", f"The replica did not answer: {e or type(e).__name__}") from None
    if content_type == IMAGE_CTYPE and 200 <= status < 300:
        # Image generation (1.3): what came back, not the image itself.
        try:
            obj = json.loads(raw)
            items = [i for i in obj.get("data") or [] if isinstance(i, dict) and isinstance(i.get("b64_json"), str)]
        except (ValueError, AttributeError):
            return status, raw[:4000].decode("utf-8", "replace")
        dims = imagegen.b64_dims(items[0]["b64_json"]) if items else None
        return status, {"images": len(items), "width": dims[0] if dims else None, "height": dims[1] if dims else None,
                        "format": dims[2] if dims else None,
                        "bytes": sum(len(i["b64_json"]) * 3 // 4 for i in items)}
    if speech and 200 <= status < 300:
        # Text to speech (1.3): a WAV file, summarised by its length (the audio itself is not returned).
        ms = wav_ms(raw[:8192], len(raw))
        return status, {"audio_bytes": len(raw), "audio_ms": ms, "riff": raw[:4] == b"RIFF"}
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text[:4000]


VISION_TRY = {api: task for task, api in vision.API.items()}
SPEECH_CTYPE = "application/json; tts"   # marks the speech test request (sent as plain application/json)
IMAGE_CTYPE = "application/json; image"  # and the image one (1.3)
TRY_IMAGE_SIZE = 256                       # small, so a CPU answers within the time a test waits


def try_request(api: str, upstream: str, text: Optional[str], voice: Optional[str] = None,
                draws: Optional[imagegen.Draws] = None, zero_shot: bool = False, *,
                labels: Optional[list[str]] = None, embeds: Optional[list[str]] = None) -> tuple[str, bytes, str]:
    """(path, body, content type) of a small request in the route's protocol. `zero_shot`: the model is a
    zero-shot detector, which is told what to find (1.4.4), or (1.5.4) a zero-shot text classifier, which is told the
    `labels` to choose from. `embeds` (1.5.4): what an embedding model takes (text, image): the test image goes with
    the text to a model that embeds images, alone to an image-only one."""
    if api in VISION_TRY:
        # 1.4: the test image (two squares); for SAM 2, the box around the red one; for a zero-shot detector, the
        # names of the two squares (or the words typed in Try it, comma-separated).
        task = VISION_TRY[api]
        body: dict = {"model": upstream, "image": base64.b64encode(vision.test_png()).decode("ascii")}
        if task == "segment":
            body["box"] = vision.TEST_BOX
        if task == "detect" and zero_shot:
            body["labels"] = vision.try_labels(text)
        if task == "image-classify" and zero_shot:
            body["labels"] = vision.try_labels(text, vision.TEST_CLASSES)   # 1.4.4 (#141)
        return f"/v1/{vision.ENDPOINT[task]}", json.dumps(body).encode(), "application/json"
    if api == "openai-images":
        # One small image, in the request shape this engine takes.
        side = TRY_IMAGE_SIZE if draws is None else max(draws.multiple, TRY_IMAGE_SIZE // draws.multiple * draws.multiple)
        ask = imagegen.Ask(prompt=text or "A lighthouse on a rocky coast at sunset, oil painting.", n=1, size=(side, side))
        return "/v1/images/generations", json.dumps(imagegen.body_for(ask, draws, upstream)).encode(), IMAGE_CTYPE
    if api == "openai-chat":
        # enable_thinking=False: reasoning models (Qwen3) answer straight away; other templates ignore it.
        body = {"model": upstream, "max_tokens": 128, "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "user", "content": text or "In one short sentence: what is a GPU?"}]}
        return "/v1/chat/completions", json.dumps(body).encode(), "application/json"
    if api == "openai-embeddings":
        mods = embeds or ["text"]
        items: list = [text or TRY_EMBED_TEXT] if "text" in mods else []
        if "image" in mods:   # 1.5.4: the test image (two squares), as a typed item
            items.append({"image": "data:image/png;base64," + base64.b64encode(vision.test_png()).decode("ascii")})
        body = {"model": upstream, "input": items}
        return "/v1/embeddings", json.dumps(body).encode(), "application/json"
    if api == "rerank":
        body = {"model": upstream, "query": text or "What is the capital of France?",
                "documents": ["Paris is the capital and largest city of France.",
                              "The Eiffel Tower was completed in 1889.", "Berlin is the capital of Germany."]}
        return "/v1/rerank", json.dumps(body).encode(), "application/json"
    if api == "classify":
        # 1.4.4: vLLM serves /classify without /v1.
        body = {"model": upstream, "input": [text or "Forgeyard runs models on your own machines, and that is great."]}
        if zero_shot:   # 1.5.4: a zero-shot (NLI) model chooses among the labels sent
            body["labels"] = [x.strip() for x in labels or [] if x.strip()][:32] or list(TRY_ZERO_SHOT_LABELS)
        return "/classify", json.dumps(body).encode(), "application/json"
    if api == "openai-audio":
        boundary = uuid.uuid4().hex
        parts = [f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{upstream}\r\n".encode(),
                 f"--{boundary}\r\nContent-Disposition: form-data; name=\"response_format\"\r\n\r\njson\r\n".encode(),
                 (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{CLIP.name}\"\r\n"
                  "Content-Type: audio/mpeg\r\n\r\n").encode() + CLIP.read_bytes() + b"\r\n",
                 f"--{boundary}--\r\n".encode()]
        return "/v1/audio/transcriptions", b"".join(parts), f"multipart/form-data; boundary={boundary}"
    if api == "openai-speech":
        # WAV: every speech engine makes it, and its header gives the length.
        body = {"model": upstream, "input": text or "Hello from Forgeyard. This is a short test.", "response_format": "wav"}
        if voice:
            body["voice"] = voice
        return "/v1/audio/speech", json.dumps(body).encode(), SPEECH_CTYPE
    raise fail(422, "unsupported", f"No test request for {api}.")


def _try_videos(target: dict, replica) -> dict:
    """Video models (1.3): a video takes minutes, so the check asks the replica's video API for its job list
    (with its internal key) instead of making one; the Playground's Video panel makes one."""
    import time

    url = target["url"].rstrip("/") + "/v1/videos"
    headers = {"Accept": "application/json", "Accept-Encoding": "identity", "User-Agent": "forgeyard-console"}
    if replica is not None and replica.secret:
        headers["Authorization"] = f"Bearer {replica.secret}"
    started = time.monotonic()
    try:
        status, raw = replica_tls.request_sync("GET", url, target.get("tls_name"), headers=headers, timeout=TRY_TIMEOUT,
                                               limit=256 * 1024)
    except httpx.HTTPError as e:
        raise fail(502, "replica_unreachable", f"The replica did not answer: {e or type(e).__name__}") from None
    ms = int((time.monotonic() - started) * 1000)
    try:
        answer = json.loads(raw)
    except ValueError:
        answer = raw[:4000].decode("utf-8", "replace")
    ok = 200 <= status < 300
    n = len(answer.get("data") or []) if ok and isinstance(answer, dict) else 0
    summary = (f"The replica's video API answers ({n} job{'s' if n != 1 else ''} on it). A video takes minutes: make "
               "one in the Playground.") if ok else str(answer)[:500]
    return {"status": status, "ok": ok, "api": "openai-videos", "endpoint": url, "request": {"method": "GET"},
            "summary": summary, "answer": {"jobs": n} if ok else None, "ms": ms}


def summarise(api: str, answer) -> str:
    if not isinstance(answer, dict):
        return str(answer)[:500]
    try:
        if api == "openai-chat":
            msg = answer["choices"][0]["message"]
            text = (msg.get("content") or "").strip()
            return text[:1000] if text else "(reasoning) " + (msg.get("reasoning_content") or "").strip()[:600]
        if api == "openai-embeddings":
            data = answer["data"]
            vec = data[0]["embedding"]
            out = f"{len(vec)} dimensions: [{', '.join(f'{x:.4f}' for x in vec[:4])}, …]"
            if len(data) == 2 and all(isinstance(d.get("embedding"), list) for d in data):
                # 1.5.4: the text and the test image, in one space: how close they are.
                cos = sum(a * b for a, b in zip(data[0]["embedding"], data[1]["embedding"]))
                out = f"2 vectors (the text and the test image), {out}; text-image cosine {cos:.3f}"
            return out
        if api == "rerank":
            res = sorted(answer.get("results") or [], key=lambda r: -r.get("relevance_score", 0))
            return "; ".join(f"#{r['index']} {r['relevance_score']:.3f}" for r in res)
        if api == "classify":
            d = answer["data"][0]
            return f"{d['label']} {max(d['probs']):.3f} ({d['num_classes']} labels)"
        if api == "openai-audio":
            return str(answer.get("text", "")).strip()[:1000]
        if api == "openai-images":
            fmt = (answer.get("format") or "image").upper()
            return (f"{answer.get('images', 0)} image ({answer.get('width')}x{answer.get('height')} {fmt}, "
                    f"{answer.get('bytes', 0) / 1024:.0f} KiB)")
        if api in VISION_TRY:
            return vision.summarise(VISION_TRY[api], answer)
        if api == "openai-speech":
            ms = answer.get("audio_ms")
            return (f"{ms / 1000:.1f} s of audio" if ms else "audio of unknown length") + \
                f" (WAV, {answer.get('audio_bytes', 0) / 1024:.0f} KiB)"
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return json.dumps(answer)[:500]


@router.post("/deployments/{dep_id}/try")
def try_it(dep_id: int, body: TryIn, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    """One small request straight to a ready replica's own endpoint (with its internal key), to check
    that replica; the playground and /v1 go through the gateway instead."""
    import time

    d = _get(db, dep_id)
    sleep.idle.used([d.id])   # 1.5.0: a test request is use, like any other
    route = routes.own_route(db, d.id)
    own = next((t for t in route.targets if t.kind == "local" and t.deployment_id == d.id), None) if route else None
    upstream = own.upstream_model if own and own.upstream_model else d.served_name
    target = next(iter(routes.endpoints(d.workload, ready_only=True, tls=True)), None)
    if route is None or target is None:
        if d.workload.desired_state == sleep.ASLEEP:
            raise fail(409, "model_asleep", f"{d.name} is asleep: nobody used it for a while. Start it to wake it.")
        raise fail(409, "not_ready", "No replica of this deployment is ready yet.")
    recipe = catalog.get_recipe(db, d.recipe)
    voice = recipe.speech.default_voice if recipe is not None and recipe.tts is not None else None
    draws = imagegen.draws(recipe, d.params or {}, d.model.meta if d.model is not None else None) \
        if recipe is not None and "image" in recipe.tasks else None
    replica = next((r for r in d.workload.replicas if r.ordinal == target["replica"]), None)
    if route.api == "openai-videos":
        return _try_videos(target, replica)
    meta = d.model.meta or {} if d.model is not None else {}
    zero_shot = d.model is not None and (vision.is_zero_shot(meta) if route.api in VISION_TRY else
                                         route.api == "classify" and textcls.is_zero_shot(meta))
    embeds = encoders.embed_of(meta).get("modalities") if route.api == "openai-embeddings" and \
        encoders.is_server(recipe, "embed") else None
    path, payload, ctype = try_request(route.api, upstream, body.input, voice, draws, zero_shot, labels=body.labels,
                                       embeds=embeds)
    started = time.monotonic()
    status, answer = _post(target["url"].rstrip("/") + path, payload, ctype, replica.secret if replica else None,
                           target.get("tls_name"))
    ms = int((time.monotonic() - started) * 1000)
    request = json.loads(payload) if ctype in ("application/json", SPEECH_CTYPE, IMAGE_CTYPE) \
        else {"model": upstream, "file": CLIP.name}
    if route.api == "openai-embeddings" and isinstance(request.get("input"), list):
        request["input"] = [{"image": "<the test image: 256x192 PNG, base64>"} if isinstance(x, dict) else x
                            for x in request["input"]]
    if route.api in VISION_TRY:
        request["image"] = "<the test image: 256x192 PNG, base64>"
        if isinstance(answer, dict):   # masks and depth maps are PNGs in base64: not shown
            answer = {k: v for k, v in answer.items() if k != "depth_png"}
            if isinstance(answer.get("masks"), list):
                answer["masks"] = [{k: v for k, v in m.items() if k != "png"} for m in answer["masks"] if isinstance(m, dict)]
    return {"status": status, "ok": 200 <= status < 300, "api": route.api, "endpoint": target["url"] + path.lstrip("/"),
            "request": request,
            "summary": summarise(route.api, answer) if 200 <= status < 300 else str(answer)[:500],
            "answer": answer if isinstance(answer, dict) else None, "ms": ms}
