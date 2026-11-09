"""Who is calling: the client IP, honouring X-Forwarded-For only from trusted proxies."""

from __future__ import annotations

from fastapi import Request

from forgeyard.config import get_settings


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if peer in get_settings().trusted_proxy_list:
        forwarded = request.headers.get("x-forwarded-for", "")
        # The right-most address not added by one of our proxies is the client.
        for hop in reversed([h.strip() for h in forwarded.split(",") if h.strip()]):
            if hop not in get_settings().trusted_proxy_list:
                return hop
    return peer
