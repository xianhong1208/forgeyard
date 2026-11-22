"""The connection to an upstream (forgeyard/gateway/proxy.py, steps 2, 3 and 7): the gateway's one HTTP client to
the replicas, which failures prove a request never reached one (and may move on), a client that went away,
and what a tier that did not answer leaves behind (`Failed`)."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from typing import Optional

import httpx
from starlette.responses import Response

from forgeyard.config import get_settings
from forgeyard.gateway import egress
from forgeyard.gateway.errors import GatewayError
from forgeyard.security import replica_tls

CLIENT_CLOSED = 499  # nginx's code for "the client went away": counted, but not as an error

# The request provably never reached the replica (1.4.5): the connection could not be made, or none
# was free in the pool. Only these move a request to another replica or provider.
RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)
# The connection broke while the request was being written or its answer awaited. The engine may have
# read it and be generating (an answer it could not send, a crash midway), so it is never tried again:
# that could run it twice. A write that broke partway is counted here too (conservatively): which bytes
# the engine read cannot be told from this side.
SENT_BROKE = (httpx.ReadError, httpx.WriteError, httpx.RemoteProtocolError)
# A replica's key guard (1.4.5) answers 503 with this header when its engine is not running: the request
# provably reached no engine, so it is tried on the next replica like a connect error.
NOT_TAKEN_HEADER = "x-forgeyard-not-taken"


class EngineUnreachable(Exception):
    """What a replica's guard said: its engine is not running (named in upstream_unreachable)."""


def not_taken(response: httpx.Response) -> bool:
    return response.status_code == 503 and bool(response.headers.get(NOT_TAKEN_HEADER))


# ------------------------------------------------------------------ the HTTP client
_clients: dict[int, tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}


def client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    entry = _clients.get(id(loop))
    if entry is None or entry[0] is not loop or entry[1].is_closed:
        s = get_settings()
        c = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=s.gateway_connect_timeout, read=s.gateway_read_timeout, write=60.0, pool=30.0),
            # 1.4.5 (#163): TLS to every replica whose agent serves it, checked against Forgeyard's CA and the
            # machine's binding name; plain HTTP only to older agents (security/replica_tls.py).
            transport=replica_tls.ReplicaTransport(
                limits=httpx.Limits(max_connections=1000, max_keepalive_connections=100)),
            trust_env=False,          # replicas are on the LAN: never through a proxy from the environment
            cookies=egress.no_cookies(),  # shared by every request: a cookie one upstream set is never kept
            follow_redirects=False)
        _clients[id(loop)] = (loop, c)
        entry = (loop, c)
    return entry[1]


async def close_client() -> None:
    entry = _clients.pop(id(asyncio.get_running_loop()), None)
    if entry is not None:
        await entry[1].aclose()


# ------------------------------------------------------------------ client disconnects
class ClientGone(Exception):
    pass


async def _empty():
    return
    yield  # an async iterator with nothing in it


async def _until_disconnect(receive) -> None:
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            return


async def unless_gone(receive, coro):
    """Await `coro`, but cancel it (and raise ClientGone) if the client disconnects first."""
    task = asyncio.ensure_future(coro)
    watch = asyncio.ensure_future(_until_disconnect(receive))
    try:
        done, _ = await asyncio.wait({task, watch}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return task.result()
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        raise ClientGone()
    finally:
        watch.cancel()
        with contextlib.suppress(BaseException):
            await watch


# ------------------------------------------------------------------ one request
@dataclass
class Failed:
    """A tier (or part of one) did not answer; the request may move on."""

    reason: str                                # unavailable | busy | waking | refused | too_long | budget
    error: Optional[GatewayError] = None
    answer: Optional[Response] = None          # an upstream answer to pass on if nothing else serves
    retry_after: Optional[float] = None
    status: Optional[int] = None


def upstream_retry_after(response: httpx.Response) -> dict:
    """An engine's or provider's own Retry-After (on a 429 or 503), passed on with its answer, so
    clients back off as long as the upstream asked."""
    if response.status_code < 400:
        return {}
    return {k: v for k in ("retry-after", "retry-after-ms") if (v := response.headers.get(k))}
