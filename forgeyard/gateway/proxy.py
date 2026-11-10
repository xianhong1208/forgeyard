"""Forwarding a gateway request to a replica of its model.

1. The request's `model` (the deployment's served name) picks the route; the route's ready
   replicas are tried in the balancer's order (least in flight first).
2. If connecting to a replica fails (refused, reset, no answer within the connect timeout) before
   anything was sent back to the client, the next replica is tried, and the failed one is tried last
   for a while. Once a replica has answered, its answer is the answer.
3. The upstream request is built from the client's body only: `model` becomes the name the engine
   expects, the replica's internal key goes in `Authorization`, and no other client header (its API
   key, cookies) is passed on.
4. Streamed answers (SSE) are passed through as they arrive, never buffered. For a streamed chat or
   completion the gateway asks the engine for a final usage chunk (`stream_options.include_usage`)
   to count tokens, and removes it again if the client did not ask for it.
5. If the client goes away, the upstream request is closed at once, so the engine stops generating.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload
from starlette.requests import Request
from starlette.responses import Response

from forgeyard.config import get_settings
from forgeyard.db import GatewayRoute, Replica, Workload, as_utc
from forgeyard.gateway import multipart
from forgeyard.gateway.balancer import Target, balancer
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.keys import KeyCaller
from forgeyard.gateway.limits import Admission, Refusal, key_limiter
from forgeyard.gateway.usage import recorder
from forgeyard.machines.protocol import FEATURE_REPLICA_KEYS

log = logging.getLogger("forgeyard.gateway")

# /v1/<endpoint> -> (the route protocol it needs, the body it takes)
ENDPOINTS: dict[str, tuple[str, str]] = {
    "chat/completions": ("openai-chat", "json"),
    "completions": ("openai-chat", "json"),
    "embeddings": ("openai-embeddings", "json"),
    "rerank": ("rerank", "json"),
    "score": ("rerank", "json"),
    "audio/transcriptions": ("openai-audio", "multipart"),
    "audio/translations": ("openai-audio", "multipart"),
}
USAGE_ENDPOINTS = ("chat/completions", "completions")  # streamed ones can report usage at the end
CLIENT_CLOSED = 499  # nginx's code for "the client went away": counted, but not as an error

# Connecting failed, or the connection broke before the replica answered: the request did not get
# anywhere (or the engine died with it), so another replica may take it.
RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.ReadError,
             httpx.WriteError, httpx.PoolTimeout)


# ------------------------------------------------------------------ routes
@dataclass
class Route:
    served_name: str
    api: str
    upstream_model: str
    deployment_id: int
    targets: list[Target] = field(default_factory=list)
    reason: Optional[str] = None   # why nothing is ready


def _ready(r: Replica, w: Workload) -> bool:
    from forgeyard.models.routes import _ready as ready

    return ready(r, w)


def not_ready_reason(w: Workload) -> str:
    if w.desired_state != "running":
        return "The deployment is stopped."
    parts = []
    for r in w.replicas:
        where = f" on {r.node.name}" if r.node else " (not placed)"
        state = r.reason or r.state
        parts.append(f"replica {r.ordinal}{where}: {state}")
    return ("No replica is ready yet: " + "; ".join(parts))[:400] if parts else "The deployment has no replicas."


def load_route(db: DbSession, served_name: str) -> Optional[Route]:
    row = db.execute(select(GatewayRoute, Workload).join(Workload, Workload.id == GatewayRoute.workload_id)
                     .where(GatewayRoute.served_name == served_name)
                     .options(selectinload(Workload.replicas).selectinload(Replica.node))).first()
    if row is None:
        return None
    route, w = row
    targets = []
    for r in w.replicas:
        if not _ready(r, w):
            continue
        port = next(iter(r.ports or []), None)
        if not port or not port.get("host_port") or not port.get("address"):
            continue
        host = port["address"]
        host = f"[{host}]" if ":" in host else host
        protected = bool(r.secret) and r.node is not None and FEATURE_REPLICA_KEYS in (r.node.features or [])
        since = as_utc(r.ready_at).timestamp() if r.ready_at else 0.0
        targets.append(Target(uid=r.uid, ordinal=r.ordinal, url=f"http://{host}:{port['host_port']}",
                              machine=r.node.name if r.node else None, secret=r.secret, protected=protected,
                              ready_since=since))
    return Route(served_name=route.served_name, api=route.api, upstream_model=route.upstream_model,
                 deployment_id=route.deployment_id, targets=targets,
                 reason=None if targets else not_ready_reason(w))


def model_names(db: DbSession) -> list[tuple[str, str, int]]:
    """(served name, protocol, created) of every route."""
    rows = db.execute(select(GatewayRoute.served_name, GatewayRoute.api, GatewayRoute.created_at)
                      .order_by(GatewayRoute.served_name)).all()
    return [(n, a, int(c.timestamp()) if c else 0) for n, a, c in rows]


def endpoint_for(api: str) -> str:
    return {"openai-chat": "/v1/chat/completions", "openai-embeddings": "/v1/embeddings", "rerank": "/v1/rerank",
            "openai-audio": "/v1/audio/transcriptions"}.get(api, "/v1")


def check_access(caller: KeyCaller, route: Optional[Route], model: str, endpoint: str) -> Route:
    if route is None:
        raise GatewayError(404, "model_not_found", f"The model {model!r} does not exist. GET /v1/models lists yours.",
                           param="model")
    if caller.models is not None and route.served_name not in caller.models:
        raise GatewayError(403, "model_not_allowed", f"This API key may not use the model {model!r}.", param="model")
    need = ENDPOINTS[endpoint][0]
    if route.api != need:
        raise GatewayError(400, "wrong_endpoint", f"{model!r} is served at {endpoint_for(route.api)}, "
                                                  f"not /v1/{endpoint}.", param="model")
    return route


# ------------------------------------------------------------------ the request body
@dataclass
class Parsed:
    model: str
    stream: bool = False
    max_tokens: Optional[int] = None
    obj: Optional[dict] = None                     # a JSON body
    parts: Optional[list] = None                   # a multipart body
    boundary: Optional[bytes] = None


async def read_body(request: Request) -> bytes:
    limit = get_settings().gateway_max_body_mb * 1024**2
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise GatewayError(413, "request_too_large", f"The request is larger than {limit // 1024**2} MiB.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise GatewayError(413, "request_too_large", f"The request is larger than {limit // 1024**2} MiB.")
        chunks.append(chunk)
    return b"".join(chunks)


def parse_body(body: bytes, content_type: str, kind: str) -> Parsed:
    if kind == "multipart":
        if "multipart/form-data" not in (content_type or "").lower():
            raise GatewayError(400, "invalid_request", "Send the audio as multipart/form-data.")
        try:
            boundary, parts = multipart.parse(body, content_type)
        except multipart.MultipartError as e:
            raise GatewayError(400, "invalid_request", f"The multipart body could not be read: {e}.") from None
        model = multipart.field(parts, "model")
        if not model:
            raise GatewayError(400, "invalid_request", "The form needs a 'model' field.", param="model")
        stream = (multipart.field(parts, "stream") or "").lower() in ("true", "1")
        return Parsed(model=model, stream=stream, parts=parts, boundary=boundary)
    try:
        obj = json.loads(body or b"null")
    except (ValueError, UnicodeDecodeError):
        raise GatewayError(400, "invalid_json", "The request body is not valid JSON.") from None
    if not isinstance(obj, dict):
        raise GatewayError(400, "invalid_request", "The request body must be a JSON object.")
    model = obj.get("model")
    if not isinstance(model, str) or not model.strip():
        raise GatewayError(400, "invalid_request", "The request needs a 'model'.", param="model")
    max_tokens = obj.get("max_completion_tokens", obj.get("max_tokens"))
    max_tokens = max_tokens if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0 else None
    return Parsed(model=model.strip(), stream=obj.get("stream") is True, max_tokens=max_tokens, obj=obj)


def upstream_body(parsed: Parsed, endpoint: str, upstream_model: str) -> tuple[bytes, bool]:
    """(the body for the engine, whether to strip a usage chunk the gateway asked for)."""
    if parsed.parts is not None:
        return multipart.build(parsed.boundary, parsed.parts, {"model": upstream_model}), False
    obj = dict(parsed.obj or {})
    obj["model"] = upstream_model
    strip = False
    if parsed.stream and endpoint in USAGE_ENDPOINTS:
        options = obj.get("stream_options")
        options = dict(options) if isinstance(options, dict) else {}
        if options.get("include_usage") is not True:
            options["include_usage"] = True
            obj["stream_options"] = options
            strip = True
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), strip


# ------------------------------------------------------------------ usage in answers
def tokens_of(usage) -> tuple[int, int]:
    if not isinstance(usage, dict):
        return 0, 0

    def num(v) -> int:
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else 0

    prompt, completion = num(usage.get("prompt_tokens")), num(usage.get("completion_tokens"))
    if not prompt and not completion:
        prompt = num(usage.get("total_tokens")) or num(usage.get("input_tokens"))
    return prompt, completion


class SSEFilter:
    """Passes server-sent events through as they complete, remembers the last `usage`, and drops
    the usage-only chunk (or the `usage` field) when the gateway asked for it on its own."""

    def __init__(self, strip_usage: bool):
        self.strip = strip_usage
        self.buf = b""
        self.usage: Optional[dict] = None
        self.events = 0

    def feed(self, chunk: bytes) -> bytes:
        self.buf += chunk
        out = []
        while True:
            cut = [(i, len(sep)) for sep in (b"\n\n", b"\r\n\r\n") if (i := self.buf.find(sep)) >= 0]
            if not cut:
                break
            i, n = min(cut)
            event, self.buf = self.buf[:i + n], self.buf[i + n:]
            out.append(self._event(event))
        return b"".join(out)

    def end(self) -> bytes:
        rest, self.buf = self.buf, b""
        return self._event(rest) if rest.strip() else rest

    def _event(self, event: bytes) -> bytes:
        self.events += 1
        if b'"usage"' not in event:
            return event
        data = b"".join(line[5:].lstrip() for line in event.splitlines() if line.startswith(b"data:"))
        try:
            obj = json.loads(data)
        except ValueError:
            return event
        if not isinstance(obj, dict) or not obj.get("usage"):
            return event
        self.usage = obj["usage"]
        if not self.strip:
            return event
        if not obj.get("choices"):
            return b""  # the usage-only chunk the client did not ask for
        obj.pop("usage")
        return b"data: " + json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n\n"


# ------------------------------------------------------------------ the HTTP client
_clients: dict[int, tuple[asyncio.AbstractEventLoop, httpx.AsyncClient]] = {}


def client() -> httpx.AsyncClient:
    loop = asyncio.get_running_loop()
    entry = _clients.get(id(loop))
    if entry is None or entry[0] is not loop or entry[1].is_closed:
        s = get_settings()
        c = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=s.gateway_connect_timeout, read=s.gateway_read_timeout, write=60.0, pool=30.0),
            limits=httpx.Limits(max_connections=1000, max_keepalive_connections=100),
            trust_env=False,          # replicas are on the LAN: never through a proxy from the environment
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


class StreamResponse(Response):
    """Server-sent events from the replica to the client, as they come. Stops the upstream request
    as soon as the client disconnects."""

    def __init__(self, upstream: httpx.Response, sse: SSEFilter, headers: dict, done: Callable[[int, Optional[dict]], None]):
        # Not Response.__init__: that would declare Content-Length: 0 for the empty initial body.
        self.status_code = upstream.status_code
        self.background = None
        ctype = upstream.headers.get("content-type", "text/event-stream")
        self.raw_headers = [(b"content-type", ctype.encode("latin-1"))] + [
            (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
        self.upstream = upstream
        self.sse = sse
        self.done = done

    async def __call__(self, scope, receive, send):
        status = self.upstream.status_code
        await send({"type": "http.response.start", "status": status, "headers": self.raw_headers})

        async def pump():
            nonlocal status
            try:
                async for chunk in self.upstream.aiter_bytes():
                    out = self.sse.feed(chunk)
                    if out:
                        await send({"type": "http.response.body", "body": out, "more_body": True})
                tail = self.sse.end()
            except httpx.HTTPError as e:
                status = 502
                log.warning("A streamed answer broke off: %s", type(e).__name__)
                err = {"error": {"message": f"The replica's stream broke off ({type(e).__name__}).",
                                 "type": "upstream_error", "param": None, "code": "upstream_stream_broken"}}
                tail = b"data: " + json.dumps(err).encode() + b"\n\n"
            await send({"type": "http.response.body", "body": tail, "more_body": False})

        pumping = asyncio.ensure_future(pump())
        watching = asyncio.ensure_future(_until_disconnect(receive))
        try:
            done, _ = await asyncio.wait({pumping, watching}, return_when=asyncio.FIRST_COMPLETED)
            if pumping not in done:
                status = CLIENT_CLOSED
            else:
                pumping.result()
        except OSError:
            status = CLIENT_CLOSED  # the client's socket is gone (send failed)
        finally:
            for t in (pumping, watching):
                t.cancel()
            for t in (pumping, watching):
                with contextlib.suppress(BaseException):
                    await t
            await self.upstream.aclose()  # the engine sees the connection close and stops
            self.done(status, self.sse.usage)


# ------------------------------------------------------------------ one request
async def forward(request: Request, *, caller: KeyCaller, route: Route, endpoint: str, parsed: Parsed,
                  started: float) -> Response:
    """Limits, a replica, the upstream request and the answer. Records usage when it is over."""
    admission: Optional[Admission] = None
    if caller.source == "api":
        verdict = key_limiter.admit(caller.key_id, caller.rpm, caller.tpm, parsed.max_tokens)
        if isinstance(verdict, Refusal):
            what = "requests" if verdict.which == "rpm" else "tokens"
            record(caller, route.served_name, route.deployment_id, 429, started, limited=True)
            raise GatewayError(429, "rate_limit_exceeded",
                               f"This API key may use {verdict.limit} {what} per minute; try again in "
                               f"{verdict.retry_after} s.", retry_after=verdict.retry_after)
        admission = verdict
    settled = False

    def finish(status: int, usage) -> None:
        nonlocal settled
        if settled:
            return
        settled = True
        prompt, completion = tokens_of(usage)
        if admission is not None:
            key_limiter.settle(admission, prompt + completion)
        record(caller, route.served_name, route.deployment_id, status, started, prompt, completion)

    try:
        if not route.targets:
            raise GatewayError(503, "no_ready_replica", f"{route.served_name!r} has no ready replica. {route.reason or ''}".strip())
        body, strip = upstream_body(parsed, endpoint, route.upstream_model)
        ctype = request.headers.get("content-type", "application/json")
        if parsed.parts is None:
            ctype = "application/json"
        http = client()
        response = None
        target: Optional[Target] = None
        last = None
        for t in balancer.order(route.served_name, route.targets):
            headers = {"Content-Type": ctype, "Accept": request.headers.get("accept") or "*/*",
                       "Accept-Encoding": "identity", "User-Agent": "forgeyard-gateway"}
            if t.secret:
                headers["Authorization"] = f"Bearer {t.secret}"
            balancer.acquire(t.uid)
            try:
                req = http.build_request("POST", f"{t.url}/v1/{endpoint}", content=body, headers=headers)
                response = await unless_gone(request.receive, http.send(req, stream=True))
            except ClientGone:
                balancer.release(t.uid)
                finish(CLIENT_CLOSED, None)
                return Response(status_code=CLIENT_CLOSED)
            except RETRYABLE as e:
                balancer.release(t.uid)
                balancer.failed(t.uid)
                last = e
                log.warning("Replica %s of %s did not take the request (%s); trying the next one.",
                            t.ordinal, route.served_name, type(e).__name__)
                continue
            except httpx.TimeoutException:
                balancer.release(t.uid)
                raise GatewayError(504, "upstream_timeout", f"The replica did not answer within "
                                                            f"{get_settings().gateway_read_timeout:g} s.") from None
            except httpx.HTTPError as e:
                balancer.release(t.uid)
                raise GatewayError(502, "upstream_error", f"The request to the replica failed ({type(e).__name__}).") from None
            target = t
            break
        if response is None or target is None:
            raise GatewayError(502, "upstream_unreachable", f"No replica of {route.served_name!r} could be reached "
                                                            f"({type(last).__name__ if last else 'none tried'}).")
        out_headers = {"X-Forgeyard-Replica": f"{route.served_name}#{target.ordinal}"}
        if response.status_code in (401, 403):
            await response.aclose()
            balancer.release(target.uid)
            log.warning("Replica %s of %s refused the gateway's internal key (HTTP %s).", target.ordinal,
                        route.served_name, response.status_code)
            raise GatewayError(502, "replica_refused_key", "The replica refused the gateway's internal key. Stop and "
                                                           "start the deployment to give it a new one.")
        is_sse = "text/event-stream" in response.headers.get("content-type", "")
        if is_sse and response.status_code == 200:
            uid = target.uid

            def done(status: int, usage) -> None:
                balancer.release(uid)
                finish(status, usage)

            out_headers.update({"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
            return StreamResponse(response, SSEFilter(strip), out_headers, done)
        try:
            data = await unless_gone(request.receive, response.aread())
        except ClientGone:
            finish(CLIENT_CLOSED, None)
            return Response(status_code=CLIENT_CLOSED)
        except httpx.TimeoutException:
            raise GatewayError(504, "upstream_timeout", "The replica did not finish its answer in time.") from None
        except httpx.HTTPError as e:
            raise GatewayError(502, "upstream_error", f"The replica's answer broke off ({type(e).__name__}).") from None
        finally:
            await response.aclose()
            balancer.release(target.uid)
        usage = None
        if response.status_code == 200 and "json" in response.headers.get("content-type", ""):
            try:
                answer = json.loads(data)
                usage = answer.get("usage") if isinstance(answer, dict) else None
            except ValueError:
                pass
        finish(response.status_code, usage)
        return Response(content=data, status_code=response.status_code, headers=out_headers,
                        media_type=response.headers.get("content-type") or "application/json")
    except GatewayError as e:
        finish(e.status, None)
        raise


def record(caller: KeyCaller, model: str, deployment_id: Optional[int], status: int, started: float,
           prompt: int = 0, completion: int = 0, limited: bool = False) -> None:
    recorder.record(source=caller.source, key_id=caller.key_id, user_id=caller.user_id, username=caller.username,
                    model=model, deployment_id=deployment_id, status=status,
                    latency_ms=(time.monotonic() - started) * 1000, prompt_tokens=prompt,
                    completion_tokens=completion, limited=limited)
