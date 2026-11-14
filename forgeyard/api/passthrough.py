"""The generic passthrough at /x/<name>/… (1.3): a service workload's own HTTP API behind the gateway,
for programs with an API key (forgeyard/gateway/passthrough.py). Like /v1, only API keys are accepted
here, never the console's cookie, and the console's CSRF rule does not apply; the host check (421) does.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from forgeyard.gateway import passthrough

router = APIRouter(tags=["passthrough"])


@router.api_route("/x", methods=passthrough.METHODS, include_in_schema=False)
@router.api_route("/x/{rest:path}", methods=passthrough.METHODS, include_in_schema=False)
async def x(request: Request, rest: str = ""):
    return await passthrough.handle(request)
