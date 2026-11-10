"""Request guards that run before any route.

1. Host check: a Host that is not ours gets 421 (DNS rebinding: a page on evil.example
   that resolves to 127.0.0.1 must not be able to talk to the API with the browser's
   cookies). Allowed: the PUBLIC_URL host, ALLOWED_HOSTS, localhost / *.localhost and
   IP addresses.
2. CSRF on writes to /api: the request must carry `X-Forgeyard: 1` (a custom header a
   cross-site form cannot send without a CORS preflight, which we never grant) and, when
   the browser sends Origin or Referer, it must be our own origin.
3. A per-IP request budget for /api (429, never a sign-out).
4. Security headers on every answer; Strict-Transport-Security only with your own certificate
   (see hsts()).

The gateway (`/v1/*`, M4) gets only the host check: it takes API keys, never cookies, so a
cross-site page cannot use a browser's credentials there.

GuardMiddleware only sees HTTP requests; WebSocketHostGuard applies the same host check to
WebSocket handshakes (the agent stream), answering 421 before anything else runs.
"""

from __future__ import annotations

import ipaddress
import json
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import JSONResponse

from forgeyard.config import get_settings
from forgeyard.security.client import client_ip
from forgeyard.security.ratelimit import limiter, retry_after

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
HSTS_VALUE = b"max-age=31536000"


def hsts() -> bool:
    """Strict-Transport-Security only with your own certificate (TLS_CERT_FILE). With Forgeyard's
    CA a browser that does not trust it yet must be able to click past the warning once to reach
    the "Trust this server" page; HSTS would turn that warning into a dead end for a year."""
    s = get_settings()
    return s.tls_on and s.own_certificate

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


class GuardMiddleware:
    """Pure ASGI (not BaseHTTPMiddleware), so streamed answers from the gateway pass straight
    through and a client that goes away is seen by the route at once.

    `/v1/*` is the OpenAI-compatible gateway for programs, not browsers: it takes API keys only,
    never the console cookie, so neither the CSRF rule nor the console's per-IP budget applies
    there (API keys have their own limits). The host check applies to everything."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        path = scope.get("path", "")
        if not host_allowed(request.headers.get("host", "")):
            message = "This server does not answer to that host name."
            if path == "/v1" or path.startswith("/v1/"):  # the gateway answers in OpenAI's error shape
                answer = JSONResponse({"error": {"message": message, "type": "invalid_request_error", "param": None,
                                                 "code": "host_not_allowed"}}, status_code=421)
            else:
                answer = _error(421, "host_not_allowed", message)
            await answer(scope, receive, send)
            return
        is_api = path.startswith("/api/")
        if is_api:
            if request.method not in SAFE_METHODS:
                if request.headers.get(CSRF_HEADER) != "1" or not same_origin(request):
                    await _error(403, "csrf", "Cross-site request refused.")(scope, receive, send)
                    return
            wait = limiter.hit(f"ip:{client_ip(request)}", get_settings().rate_limit_per_minute)
            if wait:
                await _error(429, "rate_limited", "Too many requests. Try again shortly.",
                             {"Retry-After": retry_after(wait)})(scope, receive, send)
                return
        extra = [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"same-origin"),
                 (b"x-frame-options", b"DENY")]
        if is_api or path.startswith("/v1/") or path == "/v1":
            extra.append((b"cache-control", b"no-store"))
        if scope.get("scheme") == "https" and hsts():
            extra.append((b"strict-transport-security", HSTS_VALUE))

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                headers += [(k, v) for k, v in extra if k not in present]
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


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
