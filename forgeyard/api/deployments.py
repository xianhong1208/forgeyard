"""Model deployments: a catalog model + a recipe + parameters, expanded into a service workload
(placed by the model scheduler) and a gateway route (served at /v1 by the gateway, M4).

Admins and operators estimate, create, stop, start and delete deployments; everyone signed in
sees them and can send a short test request ("try it") to a ready replica, and use them through
the gateway with an API key or the playground.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import audit
from forgeyard.api.deps import Caller, can_run_workloads, current, fail, require_operator
from forgeyard.api.workloads import _events, _get as get_workload, workload_out
from forgeyard.db import (Accelerator, CatalogModel, Deployment, GatewayRoute, Node, Replica, RouteTarget, Workload, get_db,
                          utcnow)
from forgeyard.gateway import proxy
from forgeyard.gateway.speech import wav_ms
from forgeyard.machines.hub import hub
from forgeyard.models import catalog, routes, scheduler
from forgeyard.models.catalog import Context
from forgeyard.models.estimate import EstimateError, estimate as compute_estimate
from forgeyard.models.recipes import Recipe
from forgeyard.workloads import placement, state as wstate
from forgeyard.workloads.spec import NAME_RE, WorkloadSpec, parse_bytes

router = APIRouter(prefix="/api", tags=["deployments"])

SERVED_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
CLIP = Path(__file__).resolve().parent.parent / "models" / "assets" / "jfk-ask-not.mp3"
TRY_TIMEOUT = 120.0


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


def normalise_params(recipe: Recipe, raw: dict) -> dict:
    """The deployment's parameters, checked against the recipe: its own (context, concurrency…)
    and the common ones (gpus, share, vram, extra_args). Unknown names are refused."""
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
    unknown = sorted(set(raw) - set(out) - {"vram", "extra_args"})
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
    params = normalise_params(recipe, body.params)
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


@router.post("/deployments/estimate")
def estimate(body: DeploymentIn, _: Caller = Depends(require_operator), db: DbSession = Depends(get_db)):
    """The estimate and where the replicas would go right now, before anything is created."""
    ctx, recipe, m = build_context(db, body)
    served = (body.served_name or body.name or m.name).strip() or m.name

    class _Dep:  # render needs a served name only
        served_name = served

    ctx.deployment = _Dep()  # type: ignore[assignment]
    return {"estimate": ctx.estimate.as_dict(), "placement": scheduler.preview(db, ctx), "params": ctx.params,
            "recipe": recipe.name, "model": m.name}


def _endpoints(w: Workload) -> list[dict]:
    """Each replica's own address, with what the gateway sent it since the server started. Whether
    it requires the internal key (protected), and whether on every path (guarded: its agent's key
    guard) or only on the engine's own /v1 routes, is shown; the key never is."""
    from forgeyard.gateway.balancer import balancer
    from forgeyard.machines.protocol import FEATURE_REPLICA_GUARD, FEATURE_REPLICA_KEYS

    by_ordinal = {r.ordinal: r for r in w.replicas}
    out = []
    for e in routes.endpoints(w):
        r = by_ordinal.get(e["replica"])
        features = (r.node.features or []) if r and r.node else []
        protected = bool(r and r.secret and FEATURE_REPLICA_KEYS in features)
        guarded = protected and FEATURE_REPLICA_GUARD in features
        out.append({**e, "protected": protected, "guarded": guarded, **(balancer.snapshot(r.uid) if r else {})})
    return out


def deployment_out(db: DbSession, d: Deployment, caller: Caller, *, detail: bool = False) -> dict:
    w = d.workload
    route = routes.own_route(db, d.id)
    own = next((t for t in route.targets if t.kind == "local" and t.deployment_id == d.id), None) if route else None
    out = {
        "id": d.id, "name": d.name, "recipe": d.recipe, "served_name": d.served_name, "params": d.params,
        "model": {"id": d.model.id, "name": d.model.name, "source": d.model.source, "task": d.model.task,
                  "format": d.model.format, "revision": d.model.revision, "file": d.model.file,
                  "size_bytes": d.model.size_bytes},
        "estimate": d.estimate, "has_token": bool(d.hf_token), "created_by": d.created_by, "created_at": d.created_at,
        "route": {"served_name": route.served_name, "api": route.api,
                  "upstream_model": own.upstream_model if own else d.served_name}
        if route else None,
        "endpoints": _endpoints(w),
        "workload": workload_out(w, caller),
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
    if d is None:
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
    proxy.forget_ceiling()  # its max_body_mb may raise how much the gateway reads
    placement.place_and_poke(db)
    db.expire_all()
    d = _get(db, d.id)
    return {"deployment": deployment_out(db, d, caller), "placement": plan}


@router.get("/deployments")
def list_deployments(caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    rows = db.scalars(select(Deployment).options(selectinload(Deployment.workload).selectinload(Workload.replicas)
                                                 .selectinload(Replica.node))
                      .order_by(Deployment.created_at.desc(), Deployment.id.desc())).all()
    return {"deployments": [deployment_out(db, d, caller) for d in rows], "server_time": utcnow(),
            "can_manage": can_run_workloads(caller.user)}


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
    for node_id in nodes:
        hub.poke(node_id)
    placement.place_and_poke(db)
    return {"ok": True}


# ------------------------------------------------------------------ try it
class TryIn(BaseModel):
    input: Optional[str] = Field(default=None, max_length=500)
    stream: bool = False


def _post(url: str, body: bytes, content_type: str, secret: Optional[str] = None) -> tuple[int, dict | str]:
    speech = content_type == SPEECH_CTYPE
    headers = {"Content-Type": "application/json" if speech else content_type, "Accept": "*/*" if speech else "application/json"}
    if secret:
        headers["Authorization"] = f"Bearer {secret}"  # the replica's internal key (M4)
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TRY_TIMEOUT) as resp:
            raw = resp.read(2 * 1024**2)
            status = resp.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(64 * 1024), e.code
    except (urllib.error.URLError, OSError) as e:
        raise fail(502, "replica_unreachable", f"The replica did not answer: {getattr(e, 'reason', e)}") from None
    if speech and 200 <= status < 300:
        # Text to speech (1.3): a WAV file, summarised by its length (the audio itself is not returned).
        ms = wav_ms(raw[:8192], len(raw))
        return status, {"audio_bytes": len(raw), "audio_ms": ms, "riff": raw[:4] == b"RIFF"}
    text = raw.decode("utf-8", "replace")
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text[:4000]


SPEECH_CTYPE = "application/json; tts"   # marks the speech test request (sent as plain application/json)


def try_request(api: str, upstream: str, text: Optional[str], voice: Optional[str] = None) -> tuple[str, bytes, str]:
    """(path, body, content type) of a small request in the route's protocol."""
    if api == "openai-chat":
        # enable_thinking=False: reasoning models (Qwen3) answer straight away; other templates ignore it.
        body = {"model": upstream, "max_tokens": 128, "temperature": 0,
                "chat_template_kwargs": {"enable_thinking": False},
                "messages": [{"role": "user", "content": text or "In one short sentence: what is a GPU?"}]}
        return "/v1/chat/completions", json.dumps(body).encode(), "application/json"
    if api == "openai-embeddings":
        body = {"model": upstream, "input": [text or "Forgeyard runs models on your own machines."]}
        return "/v1/embeddings", json.dumps(body).encode(), "application/json"
    if api == "rerank":
        body = {"model": upstream, "query": text or "What is the capital of France?",
                "documents": ["Paris is the capital and largest city of France.",
                              "The Eiffel Tower was completed in 1889.", "Berlin is the capital of Germany."]}
        return "/v1/rerank", json.dumps(body).encode(), "application/json"
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


def summarise(api: str, answer) -> str:
    if not isinstance(answer, dict):
        return str(answer)[:500]
    try:
        if api == "openai-chat":
            msg = answer["choices"][0]["message"]
            text = (msg.get("content") or "").strip()
            return text[:1000] if text else "(reasoning) " + (msg.get("reasoning_content") or "").strip()[:600]
        if api == "openai-embeddings":
            vec = answer["data"][0]["embedding"]
            return f"{len(vec)} dimensions: [{', '.join(f'{x:.4f}' for x in vec[:4])}, …]"
        if api == "rerank":
            res = sorted(answer.get("results") or [], key=lambda r: -r.get("relevance_score", 0))
            return "; ".join(f"#{r['index']} {r['relevance_score']:.3f}" for r in res)
        if api == "openai-audio":
            return str(answer.get("text", "")).strip()[:1000]
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
    route = routes.own_route(db, d.id)
    own = next((t for t in route.targets if t.kind == "local" and t.deployment_id == d.id), None) if route else None
    upstream = own.upstream_model if own and own.upstream_model else d.served_name
    target = next(iter(routes.endpoints(d.workload, ready_only=True)), None)
    if route is None or target is None:
        raise fail(409, "not_ready", "No replica of this deployment is ready yet.")
    recipe = catalog.get_recipe(db, d.recipe)
    voice = recipe.speech.default_voice if recipe is not None and recipe.tts is not None else None
    path, payload, ctype = try_request(route.api, upstream, body.input, voice)
    replica = next((r for r in d.workload.replicas if r.ordinal == target["replica"]), None)
    started = time.monotonic()
    status, answer = _post(target["url"].rstrip("/") + path, payload, ctype, replica.secret if replica else None)
    ms = int((time.monotonic() - started) * 1000)
    return {"status": status, "ok": 200 <= status < 300, "api": route.api, "endpoint": target["url"] + path.lstrip("/"),
            "request": json.loads(payload) if ctype in ("application/json", SPEECH_CTYPE)
            else {"model": upstream, "file": CLIP.name},
            "summary": summarise(route.api, answer) if 200 <= status < 300 else str(answer)[:500],
            "answer": answer if isinstance(answer, dict) else None, "ms": ms}
