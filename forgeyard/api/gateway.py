"""The OpenAI-compatible gateway at /v1, for programs (the official OpenAI SDKs, curl, …).

Only API keys (`Authorization: Bearer fyk_…`) are accepted here, never the console's cookie, so a
web page cannot use a signed-in browser against it; for the same reason the console's CSRF rule
does not apply (clients are not browsers). The host check (421) does. No CORS headers are sent.

The console's playground (`/api/playground/…`) goes through the same code with the signed-in
user, under the console's own rules (cookie + CSRF), counted as "console" in usage. Its Vision tab
(1.3, `/api/playground/vision/…`) reaches exposed vision workloads the way /x/ does
(forgeyard/gateway/vision.py).
"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from forgeyard.db import new_session
from forgeyard.gateway import imagegen, keys, media as gmedia, proxy, speech, vision
from forgeyard.gateway.balancer import balancer
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.keys import KeyCaller
from forgeyard.security import sessions

router = APIRouter(prefix="/v1", tags=["gateway"])
console = APIRouter(prefix="/api/playground", tags=["playground"])


def _authenticate(authorization: Optional[str]) -> KeyCaller:
    db = new_session()
    try:
        caller = keys.authenticate(db, authorization)
        db.commit()  # last_used_at
        return caller
    finally:
        db.close()


def _console_caller(token: Optional[str]) -> tuple[Optional[KeyCaller], Optional[str]]:
    db = new_session()
    try:
        row, reason = sessions.resolve(db, token)
        if row is None:
            return None, reason
        return KeyCaller(source="console", user_id=row.user.id, username=row.user.username,
                         cloud=keys.console_cloud(db, row.user), user_budget_micros=row.user.budget_micros), None
    finally:
        db.close()


def _route(name: str) -> Optional[proxy.Route]:
    db = new_session()
    try:
        return proxy.load_route(db, name)
    finally:
        db.close()


def _models() -> list[tuple[proxy.Route, int]]:
    db = new_session()
    try:
        return proxy.model_names(db)
    finally:
        db.close()


async def handle(request: Request, endpoint: str, caller: KeyCaller) -> JSONResponse:
    started = time.monotonic()
    route = None
    parsed = None
    try:
        kind = proxy.ENDPOINTS[endpoint][1]
        body = await proxy.read_body(request, await run_in_threadpool(proxy.body_ceiling))
        parsed = proxy.parse_body(body, request.headers.get("content-type", ""), kind)
        route = await run_in_threadpool(_route, parsed.model)
        route = proxy.check_access(caller, route, parsed.model, endpoint)
        if endpoint == speech.ENDPOINT:
            route = proxy.fit_speech(route, parsed)  # 1.3: checked, voice cloning refused, counted
        elif endpoint == imagegen.ENDPOINT:
            route = proxy.fit_images(route, parsed)  # 1.3: checked against each deployment's caps
        # Images, audio and video (1.3): parts rebuilt, the route narrowed to what can take them,
        # media URLs fetched by the gateway, and the size checked again with them inlined.
        media = proxy.scan_media(parsed, endpoint)
        route = proxy.fit_route(route, parsed, parsed.size)
        if media is not None and media.fetch:
            await gmedia.fetch_all(media)
            parsed.size = proxy.body_size(parsed)
            route = proxy.fit_route(route, parsed, parsed.size)
    except GatewayError as e:
        if parsed is not None:  # a body without a model is not counted under any model
            proxy.record(caller, parsed.model, route.deployment_id if route else None, e.status, started)
        return e.response()
    try:
        return await proxy.forward(request, caller=caller, route=route, endpoint=endpoint, parsed=parsed, started=started)
    except GatewayError as e:
        return e.response()


async def _api(request: Request, endpoint: str):
    try:
        caller = await run_in_threadpool(_authenticate, request.headers.get("authorization"))
    except GatewayError as e:
        return e.response()
    return await handle(request, endpoint, caller)


@router.post("/chat/completions")
async def chat_completions(request: Request):
    return await _api(request, "chat/completions")


@router.post("/completions")
async def completions(request: Request):
    return await _api(request, "completions")


@router.post("/embeddings")
async def embeddings(request: Request):
    return await _api(request, "embeddings")


@router.post("/rerank")
async def rerank(request: Request):
    return await _api(request, "rerank")


@router.post("/score")
async def score(request: Request):
    return await _api(request, "score")


@router.post("/audio/transcriptions")
async def transcriptions(request: Request):
    return await _api(request, "audio/transcriptions")


@router.post("/audio/translations")
async def translations(request: Request):
    return await _api(request, "audio/translations")


@router.post("/audio/speech")
async def speech_create(request: Request):
    return await _api(request, "audio/speech")


@router.post("/images/generations")
async def images_generate(request: Request):
    return await _api(request, "images/generations")


@router.post("/images/edits")
@router.post("/images/variations")
async def images_edit(request: Request):
    """Image edits and variations: not in 1.3 (vLLM-Omni's edit route fetches any URL it is given, and
    variations are a retired DALL-E 2 feature)."""
    try:
        await run_in_threadpool(_authenticate, request.headers.get("authorization"))
    except GatewayError as e:
        return e.response()
    return GatewayError(404, "endpoint_not_supported", "Forgeyard's gateway does not edit images or make variations "
                                                       "(yet): it serves /v1/images/generations.").response()


def _speaks_out(sp: Optional[speech.Speaks]) -> dict:
    """What a client can ask of the deployment besides the voice."""
    if sp is None:
        return {}
    return {"formats": list(sp.formats), "default_format": sp.default_format, "stream_formats": list(sp.stream_formats),
            "max_input_chars": sp.max_chars}


async def voices(model: Optional[str], caller: KeyCaller):
    """A speech model's voices (1.3), asked of one of its ready replicas (kept a few minutes):
    {"object": "list", "model", "voices": [{"id", "name", "language"?, "gender"?}], "default", and what it
    makes: "formats", "default_format", "stream_formats", "max_input_chars"}."""
    if not model:
        raise GatewayError(400, "invalid_request", "Say which model: GET /v1/audio/voices?model=<model>.", param="model")
    route = await run_in_threadpool(_route, model)
    route = proxy.check_access(caller, route, model, speech.ENDPOINT)
    groups = [g for tier in route.tiers for g in tier.locals]
    ready = [(g, t) for g in groups for t in g.replicas]
    if not ready:
        known = next((g.speaks.model_voices for g in groups if g.speaks and g.speaks.model_voices), ())
        if known:
            sp = next(g.speaks for g in groups if g.speaks)
            default = sp.default_voice if sp.default_voice in known else known[0]
            return {"object": "list", "model": model, "voices": [{"id": v, "name": v} for v in known], "default": default,
                    **_speaks_out(sp)}
        reason = "; ".join(g.reason for g in groups if g.reason)
        raise GatewayError(503, "no_ready_replica", f"{model!r} has no ready replica. {reason}".strip())
    t = balancer.order(f"{route.served_name}#voices", [t for _, t in ready])[0]
    g = next(gr for gr, tr in ready if tr.uid == t.uid)
    found = await speech.voices_for(g.deployment_id, g.speaks, proxy.client(), t.url, t.secret)
    if found is None:
        raise GatewayError(502, "upstream_error", f"{model!r} did not list its voices.")
    ids = [v["id"] for v in found]
    default = g.speaks.default_voice if g.speaks and g.speaks.default_voice in ids else (ids[0] if ids else None)
    return {"object": "list", "model": model, "voices": found, "default": default, **_speaks_out(g.speaks)}


@router.get("/audio/voices")
async def list_voices(request: Request, model: Optional[str] = None):
    try:
        caller = await run_in_threadpool(_authenticate, request.headers.get("authorization"))
        return await voices(model, caller)
    except GatewayError as e:
        return e.response()


@router.post("/audio/voices")
@router.delete("/audio/voices/{name:path}")
async def upload_voice(request: Request, name: str = ""):
    """Uploading or deleting a voice (vLLM-Omni's voice cloning): not in 1.3."""
    try:
        await run_in_threadpool(_authenticate, request.headers.get("authorization"))
    except GatewayError as e:
        return e.response()
    return GatewayError(400, "voice_cloning_not_supported", "Voice cloning is not available: voices cannot be "
                                                            "uploaded. GET /v1/audio/voices?model=<model> lists the "
                                                            "model's own voices.").response()


def _model_out(name: str, created: int) -> dict:
    return {"id": name, "object": "model", "created": created, "owned_by": "forgeyard"}


@router.get("/models")
async def list_models(request: Request):
    """The models this key may use (every route, or the key's allowlist; cloud-only models only with
    the key's cloud setting on; not a model whose deployments were all stopped)."""
    try:
        caller = await run_in_threadpool(_authenticate, request.headers.get("authorization"))
    except GatewayError as e:
        return e.response()
    rows = await run_in_threadpool(_models)
    data = [_model_out(r.served_name, c) for r, c in rows
            if proxy.visible(caller.cloud, r.has_local) and proxy.serving(r, caller.cloud)
            and (caller.models is None or r.served_name in caller.models)]
    return {"object": "list", "data": data}


@router.get("/models/{model:path}")
async def get_model(model: str, request: Request):
    try:
        caller = await run_in_threadpool(_authenticate, request.headers.get("authorization"))
    except GatewayError as e:
        return e.response()
    rows = {r.served_name: (r, c) for r, c in await run_in_threadpool(_models) if proxy.visible(caller.cloud, r.has_local)}
    if model not in rows:
        return GatewayError(404, "model_not_found", f"The model {model!r} does not exist.", param="model").response()
    if caller.models is not None and model not in caller.models:
        return GatewayError(403, "model_not_allowed", f"This API key may not use the model {model!r}.",
                            param="model").response()
    route, created = rows[model]
    if not proxy.serving(route, caller.cloud):
        return GatewayError(404, "model_not_found", f"The model {model!r} is stopped. Start its deployment to use it.",
                            param="model").response()
    return _model_out(model, created)


@router.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def unknown(rest: str):
    return GatewayError(404, "unknown_endpoint", f"Forgeyard's gateway has no /v1/{rest}. It serves /v1/models, "
                                                 "/v1/chat/completions, /v1/completions, /v1/embeddings, /v1/rerank, "
                                                 "/v1/score, /v1/audio/transcriptions, /v1/audio/speech, "
                                                 "/v1/audio/voices and /v1/images/generations.").response()


# ------------------------------------------------------------------ the console's playground
def image_options(model: Optional[str], caller: KeyCaller) -> dict:
    """What an image model's deployments take (1.3), for the Playground's Image tab: the most images per
    request, the largest side, the default size, the multiple sizes must be of, the formats, the most
    steps, the longest prompt (over every deployment of the route: what at least one takes)."""
    if not model:
        raise GatewayError(400, "invalid_request", "Say which model: ?model=<model>.", param="model")
    route = proxy.check_access(caller, _route(model), model, imagegen.ENDPOINT)
    ds = [g.draws for tier in route.tiers for g in tier.locals if g.draws is not None]
    if not ds:
        raise GatewayError(503, "no_target", f"{model!r} has no image deployment.")
    first = ds[0]
    return {"model": model, "max_n": max(d.max_n for d in ds), "max_side": max(d.max_side for d in ds),
            "default_size": f"{first.width}x{first.height}", "multiple": min(d.multiple for d in ds),
            "formats": sorted({f for d in ds for f in d.formats}, key=imagegen.FORMATS.index),
            "max_steps": max(d.max_steps for d in ds), "max_prompt_chars": max(d.max_prompt for d in ds)}


@console.get("/images/options")
async def playground_image_options(request: Request, model: Optional[str] = None):
    caller, reason = await run_in_threadpool(_console_caller, request.cookies.get(sessions.COOKIE))
    if caller is None:
        return JSONResponse({"detail": {"code": "not_signed_in" if reason == "no_session" else "session_ended",
                                        "message": "Sign in to continue.", "reason": reason}}, status_code=401)
    try:
        return await run_in_threadpool(image_options, model, caller)
    except GatewayError as e:
        return e.response()


@console.get("/audio/voices")
async def playground_voices(request: Request, model: Optional[str] = None):
    """The playground's voice list for a speech model, as the signed-in user."""
    caller, reason = await run_in_threadpool(_console_caller, request.cookies.get(sessions.COOKIE))
    if caller is None:
        return JSONResponse({"detail": {"code": "not_signed_in" if reason == "no_session" else "session_ended",
                                        "message": "Sign in to continue.", "reason": reason}}, status_code=401)
    try:
        return await voices(model, caller)
    except GatewayError as e:
        return e.response()


def _not_signed_in(reason: Optional[str]) -> JSONResponse:
    return JSONResponse({"detail": {"code": "not_signed_in" if reason == "no_session" else "session_ended",
                                    "message": "Sign in to continue.", "reason": reason}}, status_code=401)


def _vision_options() -> dict:
    db = new_session()
    try:
        return vision.options(db)
    finally:
        db.close()


@console.get("/vision/options")
async def playground_vision_options(request: Request):
    """The exposed vision workloads (1.3), for the Playground's Vision tab."""
    caller, reason = await run_in_threadpool(_console_caller, request.cookies.get(sessions.COOKIE))
    if caller is None:
        return _not_signed_in(reason)
    return await run_in_threadpool(_vision_options)


@console.post("/vision/{workload}/{task}")
async def playground_vision(workload: str, task: str, request: Request):
    """An image to an exposed vision workload's /v1/<task> (1.3), as the signed-in user: the /x/ rules."""
    caller, reason = await run_in_threadpool(_console_caller, request.cookies.get(sessions.COOKIE))
    if caller is None:
        return _not_signed_in(reason)
    return await vision.handle(request, workload, task, caller)


@console.post("/{endpoint:path}")
async def playground(endpoint: str, request: Request):
    """The playground's requests: the same routing as /v1, as the signed-in user, without an API
    key (none is created or shown). Errors keep the gateway's shape; 401 sends the UI to sign-in."""
    if endpoint not in proxy.ENDPOINTS:
        return JSONResponse({"detail": {"code": "not_found", "message": "No such playground endpoint."}}, status_code=404)
    caller, reason = await run_in_threadpool(_console_caller, request.cookies.get(sessions.COOKIE))
    if caller is None:
        return JSONResponse({"detail": {"code": "not_signed_in" if reason == "no_session" else "session_ended",
                                        "message": "Sign in to continue.", "reason": reason}}, status_code=401)
    return await handle(request, endpoint, caller)
