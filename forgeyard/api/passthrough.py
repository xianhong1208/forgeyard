"""The gateway's /x/<name>/… passthrough to apps (1.3) was removed in 1.5.3 (#89): every request there, any method,
answers 410 Gone in the gateway's error shape, saying what to use instead (forgeyard/removed.py). Like /v1, the
console's CSRF rule does not apply here (forgeyard/security/middleware.py), so a program learns why whatever it sends.
"""

from __future__ import annotations

from fastapi import APIRouter

from forgeyard import removed

router = APIRouter(tags=["passthrough"])

METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]


@router.api_route("/x", methods=METHODS, include_in_schema=False)
@router.api_route("/x/{rest:path}", methods=METHODS, include_in_schema=False)
async def x(rest: str = ""):
    return removed.gone_gateway("x")
