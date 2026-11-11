"""The OpenAI-compatible gateway at /v1, for programs (the official OpenAI SDKs, curl, …).

Only API keys (`Authorization: Bearer fyk_…`) are accepted here, never the console's cookie, so a
web page cannot use a signed-in browser against it; for the same reason the console's CSRF rule
does not apply (clients are not browsers). The host check (421) does. No CORS headers are sent.

The console's playground (`/api/playground/…`) goes through the same code with the signed-in
user, under the console's own rules (cookie + CSRF), counted as "console" in usage.
"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from forgeyard.db import new_session
from forgeyard.gateway import keys, proxy
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
                         cloud=keys.console_cloud(db, row.user)), None
    finally:
        db.close()


def _route(name: str) -> Optional[proxy.Route]:
    db = new_session()
    try:
        return proxy.load_route(db, name)
    finally:
        db.close()


def _models() -> list[tuple[str, str, int, bool, bool]]:
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
        body = await proxy.read_body(request)
        parsed = proxy.parse_body(body, request.headers.get("content-type", ""), kind)
        route = await run_in_threadpool(_route, parsed.model)
        route = proxy.check_access(caller, route, parsed.model, endpoint)
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


def _model_out(name: str, created: int) -> dict:
    return {"id": name, "object": "model", "created": created, "owned_by": "forgeyard"}


@router.get("/models")
async def list_models(request: Request):
    """The models this key may use (every route, or the key's allowlist; cloud-only models only with
    the key's cloud setting on)."""
    try:
        caller = await run_in_threadpool(_authenticate, request.headers.get("authorization"))
    except GatewayError as e:
        return e.response()
    rows = await run_in_threadpool(_models)
    data = [_model_out(n, c) for n, _a, c, local, _cloud in rows
            if proxy.visible(caller.cloud, local) and (caller.models is None or n in caller.models)]
    return {"object": "list", "data": data}


@router.get("/models/{model:path}")
async def get_model(model: str, request: Request):
    try:
        caller = await run_in_threadpool(_authenticate, request.headers.get("authorization"))
    except GatewayError as e:
        return e.response()
    rows = {n: c for n, _a, c, local, _cloud in await run_in_threadpool(_models) if proxy.visible(caller.cloud, local)}
    if model not in rows:
        return GatewayError(404, "model_not_found", f"The model {model!r} does not exist.", param="model").response()
    if caller.models is not None and model not in caller.models:
        return GatewayError(403, "model_not_allowed", f"This API key may not use the model {model!r}.",
                            param="model").response()
    return _model_out(model, rows[model])


@router.api_route("/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def unknown(rest: str):
    return GatewayError(404, "unknown_endpoint", f"Forgeyard's gateway has no /v1/{rest}. It serves /v1/models, "
                                                 "/v1/chat/completions, /v1/completions, /v1/embeddings, /v1/rerank, "
                                                 "/v1/score and /v1/audio/transcriptions.").response()


# ------------------------------------------------------------------ the console's playground
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
