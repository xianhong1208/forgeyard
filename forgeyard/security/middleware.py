"""Request guards that run before any route.

1. Host check: a Host that is not ours gets 421 (DNS rebinding: a page on evil.example
   that resolves to 127.0.0.1 must not be able to talk to the API with the browser's
   cookies). Allowed: the PUBLIC_URL host, ALLOWED_HOSTS, localhost / *.localhost and
   IP addresses.
2. CSRF on writes to /api: the request must carry `X-Forgeyard: 1` (a custom header a
   cross-site form cannot send without a CORS preflight, which we never grant) and, when
   the browser sends Origin or Referer, it must be our own origin.
3. A per-IP request budget for /api (429, never a sign-out).

GuardMiddleware only sees HTTP requests; WebSocketHostGuard applies the same host check to
WebSocket handshakes (the agent stream), answering 421 before anything else runs.
"""

from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from forgeyard.config import get_settings
from forgeyard.security.client import client_ip
from forgeyard.security.ratelimit import limiter, retry_after

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
CSRF_HEADER = "x-forgeyard"


def _host_name(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:7860
        return host[1 : host.find("]")] if "]" in host else host
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host


def host_allowed(host_header: str) -> bool:
    name = _host_name(host_header)
    if not name:
        return False
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    settings = get_settings()
    allowed = set(settings.allowed_host_list)
    if settings.public_url:
        public = urlsplit(settings.public_url).hostname
        if public:
            allowed.add(public.lower())
    return name in allowed


def same_origin(request: Request) -> bool:
    """True unless the browser says the request came from another origin."""
    source = request.headers.get("origin") or request.headers.get("referer")
    if not source or source == "null":
        return source is None  # an explicit "null" origin (sandboxed frame, file://) is foreign
    parts = urlsplit(source)
    ours = {f"{request.url.scheme}://{request.headers.get('host', '').lower()}"}
    settings = get_settings()
    if settings.public_url:
        p = urlsplit(settings.public_url)
        ours.add(f"{p.scheme}://{p.netloc.lower()}")
    return f"{parts.scheme}://{parts.netloc.lower()}" in ours


def _error(status: int, code: str, message: str, headers: dict | None = None) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status, headers=headers)


class GuardMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not host_allowed(request.headers.get("host", "")):
            return _error(421, "host_not_allowed", "This server does not answer to that host name.")

        if request.url.path.startswith("/api/"):
            if request.method not in SAFE_METHODS:
                if request.headers.get(CSRF_HEADER) != "1" or not same_origin(request):
                    return _error(403, "csrf", "Cross-site request refused.")
            wait = limiter.hit(f"ip:{client_ip(request)}", get_settings().rate_limit_per_minute)
            if wait:
                return _error(429, "rate_limited", "Too many requests. Try again shortly.",
                              {"Retry-After": retry_after(wait)})

        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response


class WebSocketHostGuard:
    """Pure ASGI: the host check for WebSocket handshakes (BaseHTTPMiddleware skips them)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            host = dict(scope.get("headers") or []).get(b"host", b"").decode("latin-1")
            if not host_allowed(host):
                await receive()  # websocket.connect
                if "websocket.http.response" in scope.get("extensions", {}):
                    body = json.dumps({"detail": {"code": "host_not_allowed",
                                                  "message": "This server does not answer to that host name."}}).encode()
                    await send({"type": "websocket.http.response.start", "status": 421,
                                "headers": [(b"content-type", b"application/json"),
                                            (b"content-length", str(len(body)).encode())]})
                    await send({"type": "websocket.http.response.body", "body": body})
                else:
                    await send({"type": "websocket.close", "code": 1008})
                return
        await self.app(scope, receive, send)
