"""`GET /metrics`: Forgeyard's metrics in the Prometheus text format (1.4.5, #165; forgeyard/metrics.py).

Access is the same as Folio's:

- **With METRICS_TOKEN set**, every scrape needs `Authorization: Bearer <token>`, from anywhere; no token or a wrong
  one gets 401 (the same answer either way), and the token is compared in constant time.
- **Without it**, only a direct request from this host may read it: the peer is a loopback address (the server never
  rewrites it from headers: uvicorn runs with proxy_headers off) and the request carries no forwarding header
  (`X-Forwarded-For`, `Forwarded`, `X-Real-IP`). A request through a proxy on this host is not direct: the proxy
  may be serving the whole network. Anyone else gets 403.

The host check (421 for a Host that is not ours) still applies: a Prometheus that scrapes Forgeyard by a Docker
service name needs that name in ALLOWED_HOSTS. It is not an /api route, so neither the console's CSRF header nor its
rate limit applies.
"""

from __future__ import annotations

import hmac
import ipaddress
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from forgeyard import metrics
from forgeyard.config import get_settings

router = APIRouter()

FORWARDING_HEADERS = ("x-forwarded-for", "forwarded", "x-real-ip")


def presented_token(request: Request) -> Optional[str]:
    scheme, _, value = (request.headers.get("authorization") or "").partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def direct_loopback(request: Request) -> bool:
    """A client on this host that did not come through a proxy."""
    if any(request.headers.get(h) for h in FORWARDING_HEADERS):
        return False
    host = getattr(request.client, "host", None)
    if not host:
        return False
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback  # (also ::ffff:127.0.0.1)
    except ValueError:  # a unix socket, or TestClient's "testclient"
        return False


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request):
    expected = get_settings().metrics_token.strip()
    if expected:
        if not hmac.compare_digest((presented_token(request) or "").encode(), expected.encode()):
            return JSONResponse({"detail": {"code": "metrics_token_required",
                                            "message": "GET /metrics needs Authorization: Bearer <METRICS_TOKEN>."}},
                                status_code=401, headers={"WWW-Authenticate": 'Bearer realm="metrics"'})
    elif not direct_loopback(request):
        return JSONResponse({"detail": {"code": "metrics_local_only",
                                        "message": "Without METRICS_TOKEN, GET /metrics answers only a direct request "
                                                   "from the server's own host. Set METRICS_TOKEN to scrape it from "
                                                   "elsewhere."}}, status_code=403)
    body, content_type = await run_in_threadpool(metrics.render)  # (reads the database: off the event loop)
    return Response(content=body, media_type=content_type, headers={"Cache-Control": "no-store"})
