"""The FastAPI application: API routes, request guards and the web UI."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from forgeyard import __version__
from forgeyard.api import (
    audit_log,
    auth,
    deployments,
    gateway,
    keys,
    machines,
    models,
    onboarding,
    tls as tls_api,
    users,
    workloads,
)
from forgeyard.bootstrap import bootstrap
from forgeyard.config import get_settings
from forgeyard.db import get_engine, new_session
from forgeyard.gateway import proxy as gateway_proxy
from forgeyard.gateway.usage import recorder as usage_recorder
from forgeyard.machines import agent_api, liveness
from forgeyard.machines.hub import hub
from forgeyard.machines.protocol import CLOSE_RESTART
from forgeyard.migrate import upgrade
from forgeyard.security import crypto, tls
from forgeyard.security.middleware import GuardMiddleware, WebSocketHostGuard

STATIC = Path(__file__).with_name("static")
log = logging.getLogger("forgeyard")


def _base_url() -> str:
    s = get_settings()
    scheme = "https" if s.tls_on else "http"
    return s.public_url.rstrip("/") or f"{scheme}://{'localhost' if s.host in ('0.0.0.0', '::') else s.host}:{s.port}"


def check_secret_key() -> None:
    with get_engine().connect() as conn:
        crypto.check_database(conn)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    check_secret_key()  # before migrations: never make a new key for a database that names one
    upgrade()
    check_secret_key()
    bootstrap(_base_url())
    settings = get_settings()
    if settings.public_url_upgraded:
        log.warning("PUBLIC_URL says http:// but this server speaks HTTPS: using %s. Change PUBLIC_URL to it.",
                    settings.public_url)
    if not settings.tls_on:
        log.warning("TLS=off: Forgeyard speaks plain HTTP. Only run it like this behind a proxy that terminates "
                    "TLS (docs/https-and-trust.md); admins see a warning while requests arrive over plain HTTP.")
    db = new_session()
    try:
        liveness.mark_all_offline(db)  # no agent is connected to a server that just started
    finally:
        db.close()
    sweeper = asyncio.create_task(liveness.run_forever())
    usage_writer = asyncio.create_task(usage_recorder.run_forever(get_settings().usage_flush_seconds))
    renewer = asyncio.create_task(tls.live.run_forever())  # does nothing unless the server listens with TLS
    try:
        yield
    finally:
        hub.close_all(CLOSE_RESTART, "server restarting")  # agents reconnect on their own
        for task in (sweeper, usage_writer, renewer):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await gateway_proxy.close_client()
        try:
            await asyncio.to_thread(usage_recorder.flush)  # what was counted since the last write
        except Exception:
            log.exception("Writing gateway usage on shutdown failed")


def create_app() -> FastAPI:
    app = FastAPI(title="Forgeyard", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.add_middleware(GuardMiddleware)
    app.add_middleware(WebSocketHostGuard)

    @app.exception_handler(HTTPException)
    async def _http_error(_request: Request, exc: HTTPException):
        headers = dict(exc.headers or {})
        if isinstance(exc.detail, dict) and exc.detail.get("retry_after"):
            headers["Retry-After"] = str(exc.detail["retry_after"])
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    app.include_router(auth.router)
    app.include_router(users.router)
    app.include_router(audit_log.router)
    app.include_router(machines.router)
    app.include_router(workloads.router)
    app.include_router(models.router)
    app.include_router(deployments.router)
    app.include_router(keys.router)
    app.include_router(onboarding.router)
    app.include_router(tls_api.router)
    app.include_router(gateway.console)
    app.include_router(gateway.router)
    app.include_router(agent_api.router)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def api_not_found(rest: str):
        return JSONResponse({"detail": {"code": "not_found", "message": "No such API route."}}, status_code=404)

    if (STATIC / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str):
        candidate = (STATIC / path).resolve()
        if path and candidate.is_file() and STATIC.resolve() in candidate.parents:
            return FileResponse(candidate)
        index = STATIC / "index.html"
        if index.is_file():
            # The shell is tiny and changes with every build: never cache it.
            return FileResponse(index, headers={"Cache-Control": "no-cache"})
        return PlainTextResponse("The web UI is not built. Run: cd web && npm ci && npm run build", status_code=503)

    return app
