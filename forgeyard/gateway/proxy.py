"""Forwarding a gateway request to whoever serves its model: a deployment's replicas, or (1.2) a
cloud provider.

1. The request's `model` (a route's served name) picks the route. A route has targets in tiers:
   local deployments and provider models. Tier 0 is tried first.
2. Within a tier, a deployment's ready replicas are tried in the balancer's order (least in flight
   first). If connecting to a replica fails before anything was sent back, the next one is tried, and
   the failed one is tried last for a while. Provider targets of a tier are tried by weight; one
   that answers 429 or 5xx or cannot be reached cools down and the next is tried.
3. **Spilling** to the next tier happens only before the first byte reaches the client, and only
   when this tier is
   - *unavailable*: no ready replica, or no replica could be connected to;
   - *busy*: every ready replica already has as many requests in flight through this server as its
     deployment's `concurrency` (after waiting up to the route's `wait_ms` for a slot);
   - *refusing*: the replica answered 429 or 503;
   - *too small* (only when the route opts in): the prompt, estimated from its size, plus
     `max_tokens` does not fit the deployment's `context`.
   Never on other answers (a 400 would be the same anywhere), a timeout after the request was sent,
   the replica refusing the gateway's key, a client that went away, or once streaming has begun.
4. Which tiers a request may use: a route marked local only, a request with
   `X-Forgeyard-Local-Only: 1`, and a key whose `cloud` setting is off never reach a provider outside
   your network; with `fallback` only as a spill target (after a local tier); with `on` also cloud-only
   models. A model a key may not reach at all is "not found" for it.
5. The upstream request is built from the client's body only: `model` becomes the name the upstream
   expects, the replica's internal key or the provider key is added, and no other client header (its
   API key, cookies) is passed on. For a provider, the kind's quirks apply (forgeyard/gateway/quirks.py).
6. Streamed answers (SSE) are passed through as they arrive, never buffered. For a streamed chat or
   completion the gateway asks for a final usage chunk (`stream_options.include_usage`) to count
   tokens, and removes it again if the client did not ask for it.
7. If the client goes away, the upstream request is closed at once, so the engine stops generating.
8. The answer names who served it: `X-Forgeyard-Target: local:<deployment>#<replica>` or
   `provider:<provider>/<model>`, never a key; `X-Forgeyard-Spilled` says why it left tier 0.
"""

from __future__ import annotations

import asyncio
import contextlib
import email.utils
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload
from starlette.requests import Request
from starlette.responses import Response

from forgeyard.config import get_settings
from forgeyard.db import Deployment, GatewayRoute, ProviderModel, Replica, RouteTarget, Workload, as_utc
from forgeyard.gateway import egress, multipart, quirks
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
CLOUD_APIS = ("openai-chat", "openai-embeddings")      # what provider targets serve in 1.2
CLOUD_ENDPOINTS = ("chat/completions", "completions", "embeddings")
CLIENT_CLOSED = 499  # nginx's code for "the client went away": counted, but not as an error
LOCAL_ONLY_HEADER = "x-forgeyard-local-only"
MAX_WAIT_MS = 10_000
BYTES_PER_TOKEN = 3     # a prompt's size in tokens, estimated on the high side (CJK is ~3 bytes a token)

# Connecting failed, or the connection broke before the replica answered: the request did not get
# anywhere (or the engine died with it), so another replica may take it.
RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.ReadError,
             httpx.WriteError, httpx.PoolTimeout)


# ------------------------------------------------------------------ routes
@dataclass
class LocalGroup:
    """A deployment in a tier: its ready replicas."""

    target_id: int
    deployment_id: int
    name: str
    upstream_model: str
    replicas: list[Target] = field(default_factory=list)
    reason: Optional[str] = None          # why none is ready
    concurrency: Optional[int] = None     # requests one replica takes at once (its `concurrency`)
    context: Optional[int] = None         # tokens per request (its `context`)


@dataclass
class CloudTarget:
    """A provider's model in a tier. `external` False: a server inside your network, not cloud."""

    target_id: int
    provider_model_id: int
    provider_id: int
    provider: str
    kind: str
    base_url: str
    upstream_model: str
    external: bool
    allow_http: bool
    enabled: bool
    weight: int = 1
    timeout_s: Optional[int] = None
    headers: dict = field(default_factory=dict)
    drop_params: list = field(default_factory=list)
    api_key: Optional[str] = field(default=None, repr=False)   # never logged or returned

    @property
    def key(self) -> str:
        return f"pm:{self.provider_model_id}"

    @property
    def label(self) -> str:
        return f"provider:{self.provider}/{self.upstream_model}"


@dataclass
class Tier:
    index: int
    locals: list[LocalGroup] = field(default_factory=list)
    clouds: list[CloudTarget] = field(default_factory=list)


@dataclass
class Route:
    served_name: str
    api: str
    id: int = 0
    deployment_id: Optional[int] = None   # the deployment that created it
    local_only: bool = False
    spill_on_long_prompt: bool = False
    wait_ms: int = 0
    tiers: list[Tier] = field(default_factory=list)
    has_local: bool = False               # a target inside your network (deployment or own server)
    has_cloud: bool = False               # a provider outside your network

    @property
    def targets(self) -> list[Target]:
        """Every ready replica of every tier (what the route has locally right now)."""
        return [t for tier in self.tiers for g in tier.locals for t in g.replicas]

    @property
    def reason(self) -> Optional[str]:
        reasons = [g.reason for tier in self.tiers for g in tier.locals if g.reason]
        return "; ".join(reasons)[:400] if reasons else None


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


def _int(value) -> Optional[int]:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def local_group(t: RouteTarget, d: Deployment) -> LocalGroup:
    w = d.workload
    replicas = []
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
        replicas.append(Target(uid=r.uid, ordinal=r.ordinal, url=f"http://{host}:{port['host_port']}",
                               machine=r.node.name if r.node else None, secret=r.secret, protected=protected,
                               ready_since=since))
    params = d.params or {}
    return LocalGroup(target_id=t.id, deployment_id=d.id, name=d.name, upstream_model=t.upstream_model or d.served_name,
                      replicas=replicas, reason=None if replicas else not_ready_reason(w),
                      concurrency=_int(params.get("concurrency")), context=_int(params.get("context")))


def cloud_target(t: RouteTarget, pm: ProviderModel) -> CloudTarget:
    p = pm.provider
    return CloudTarget(target_id=t.id, provider_model_id=pm.id, provider_id=p.id, provider=p.name, kind=p.kind,
                       base_url=p.base_url, upstream_model=pm.upstream_model, external=p.external,
                       allow_http=p.allow_http, enabled=p.enabled, weight=max(1, t.weight or 1),
                       timeout_s=p.timeout_s, headers=dict(p.headers or {}), drop_params=list(pm.drop_params or []),
                       api_key=p.api_key)


def _route_query():
    return (select(GatewayRoute).options(
        selectinload(GatewayRoute.targets).selectinload(RouteTarget.deployment).selectinload(Deployment.workload)
        .selectinload(Workload.replicas).selectinload(Replica.node),
        selectinload(GatewayRoute.targets).selectinload(RouteTarget.provider_model).selectinload(ProviderModel.provider)))


def build_route(row: GatewayRoute) -> Route:
    route = Route(served_name=row.served_name, api=row.api, id=row.id, deployment_id=row.deployment_id,
                  local_only=bool(row.local_only), spill_on_long_prompt=bool(row.spill_on_long_prompt),
                  wait_ms=max(0, min(int(row.wait_ms or 0), MAX_WAIT_MS)))
    tiers: dict[int, Tier] = {}
    for t in row.targets:
        # Whether the model is visible to a key depends on what the route has, enabled or not.
        if t.kind == "local" and t.deployment is not None:
            route.has_local = True
        elif t.kind == "provider" and t.provider_model is not None:
            if t.provider_model.provider.external:
                route.has_cloud = True
            else:
                route.has_local = True
        if not t.enabled:
            continue
        tier = tiers.setdefault(t.tier, Tier(t.tier))
        if t.kind == "local" and t.deployment is not None:
            tier.locals.append(local_group(t, t.deployment))
        elif t.kind == "provider" and t.provider_model is not None and row.api in CLOUD_APIS:
            ct = cloud_target(t, t.provider_model)
            if ct.enabled:
                tier.clouds.append(ct)
    route.tiers = [tiers[i] for i in sorted(tiers)]
    return route


def load_route(db: DbSession, served_name: str) -> Optional[Route]:
    row = db.scalar(_route_query().where(GatewayRoute.served_name == served_name))
    return build_route(row) if row is not None else None


def model_names(db: DbSession) -> list[tuple[str, str, int, bool, bool]]:
    """(served name, protocol, created, has a target inside your network, has a cloud target) of
    every route."""
    out = []
    for row in db.scalars(_route_query().order_by(GatewayRoute.served_name)).all():
        r = build_route(row)
        out.append((r.served_name, r.api, int(row.created_at.timestamp()) if row.created_at else 0,
                    r.has_local, r.has_cloud))
    return out


def endpoint_for(api: str) -> str:
    return {"openai-chat": "/v1/chat/completions", "openai-embeddings": "/v1/embeddings", "rerank": "/v1/rerank",
            "openai-audio": "/v1/audio/transcriptions"}.get(api, "/v1")


def visible(cloud: str, has_local: bool) -> bool:
    """May a caller with this cloud setting see a route at all? A cloud-only model exists only for `on`."""
    return has_local or cloud == "on"


def check_access(caller: KeyCaller, route: Optional[Route], model: str, endpoint: str) -> Route:
    if route is None or not visible(caller.cloud, route.has_local):
        raise GatewayError(404, "model_not_found", f"The model {model!r} does not exist. GET /v1/models lists yours.",
                           param="model")
    if caller.models is not None and route.served_name not in caller.models:
        raise GatewayError(403, "model_not_allowed", f"This API key may not use the model {model!r}.", param="model")
    need = ENDPOINTS[endpoint][0]
    if route.api != need:
        raise GatewayError(400, "wrong_endpoint", f"{model!r} is served at {endpoint_for(route.api)}, "
                                                  f"not /v1/{endpoint}.", param="model")
    return route


def wants_local_only(request: Request) -> bool:
    return (request.headers.get(LOCAL_ONLY_HEADER) or "").strip().lower() in ("1", "true", "yes", "on")


def allowed_tiers(route: Route, cloud: str, local_only: bool, endpoint: str) -> list[Tier]:
    """The tiers this request may use, with the targets it may not use removed."""
    no_cloud = route.local_only or local_only or cloud not in ("fallback", "on")
    out: list[Tier] = []
    seen_local = False
    for tier in route.tiers:
        own = [c for c in tier.clouds if not c.external]
        clouds = [c for c in tier.clouds if c.external]
        if no_cloud or (cloud == "fallback" and not seen_local):
            clouds = []
        providers = own + clouds if endpoint in CLOUD_ENDPOINTS else []
        if tier.locals or own:
            seen_local = True
        if tier.locals or providers:
            out.append(Tier(tier.index, list(tier.locals), providers))
    return out


def prompt_estimate(parsed: "Parsed") -> int:
    """Tokens the request needs, estimated on the high side: the prompt's size plus max_tokens."""
    obj = parsed.obj or {}
    text = obj.get("messages") if "messages" in obj else obj.get("prompt", obj.get("input", ""))
    size = len(json.dumps(text, ensure_ascii=False).encode("utf-8")) if text is not None else 0
    return size // BYTES_PER_TOKEN + (parsed.max_tokens or 0)


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
    """Server-sent events from the replica (or provider) to the client, as they come. Stops the upstream request
    as soon as the client disconnects."""

    def __init__(self, upstream: httpx.Response, sse: SSEFilter, headers: dict, done: Callable[[int, Optional[dict]], None],
                 who: str = "replica"):
        # Not Response.__init__: that would declare Content-Length: 0 for the empty initial body.
        self.status_code = upstream.status_code
        self.background = None
        ctype = upstream.headers.get("content-type", "text/event-stream")
        self.raw_headers = [(b"content-type", ctype.encode("latin-1"))] + [
            (k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()]
        self.upstream = upstream
        self.sse = sse
        self.done = done
        self.who = who

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
                source = "The replica's stream" if self.who == "replica" else f"The stream from {self.who}"
                err = {"error": {"message": f"{source} broke off ({type(e).__name__}).",
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
@dataclass
class Failed:
    """A tier (or part of one) did not answer; the request may move on."""

    reason: str                                # unavailable | busy | refused | too_long
    error: Optional[GatewayError] = None
    answer: Optional[Response] = None          # an upstream answer to pass on if nothing else serves
    retry_after: Optional[float] = None
    status: Optional[int] = None


rng = random.Random()
_SECRETISH = re.compile(r"\b(sk|rk|pk|key|sess)-[A-Za-z0-9_\-*.]{6,}|\bAIza[0-9A-Za-z_\-]{10,}", re.IGNORECASE)


def order_clouds(targets: list[CloudTarget]) -> list[CloudTarget]:
    """By weight (a weighted shuffle), the ones cooling down last, the longest cooldown last of all."""
    keyed = [(balancer.cooldown_left(t.key), -(rng.random() ** (1.0 / max(1, t.weight))), i, t)
             for i, t in enumerate(targets)]
    return [t for *_, t in sorted(keyed, key=lambda k: (k[0] > 0, k[0], k[1], k[2]))]


def retry_after(headers: httpx.Headers) -> Optional[float]:
    ms = headers.get("retry-after-ms")
    if ms:
        try:
            return max(0.0, float(ms) / 1000)
        except ValueError:
            pass
    value = (headers.get("retry-after") or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, when.timestamp() - time.time())


def provider_error(c: CloudTarget, status: int, data: bytes, ra: Optional[float] = None) -> GatewayError:
    """A provider's error answer in Forgeyard's shape. Its message is kept (it says what was wrong
    with the request) without anything that looks like a key; 401/403 are never passed on as such,
    since a client would blame its own Forgeyard key."""
    message, code, param = "", None, None
    try:
        obj = json.loads(data or b"null")
        if isinstance(obj, list) and obj:
            obj = obj[0]
        err = obj.get("error") if isinstance(obj, dict) else None
        if isinstance(err, dict):
            message, code, param = str(err.get("message") or ""), err.get("code") or err.get("type"), err.get("param")
        elif isinstance(err, str):
            message = err
        elif isinstance(obj, dict) and obj.get("message"):
            message = str(obj["message"])
    except (ValueError, UnicodeDecodeError):
        message = data[:300].decode("utf-8", "replace") if data else ""
    if c.api_key:
        message = message.replace(c.api_key, "[redacted]")
    message = _SECRETISH.sub("[redacted]", " ".join(message.split()))[:500] or f"HTTP {status}"
    code = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(code))[:64] if code not in (None, "") else None
    param = str(param)[:64] if isinstance(param, str) else None
    if status == 429:
        return GatewayError(429, "provider_rate_limited", f"{c.provider} is rate limiting this model: {message}",
                            retry_after=max(1, int(ra + 0.999)) if ra else None)
    if status >= 500:
        return GatewayError(502, "provider_error", f"{c.provider} answered {status}: {message}")
    return GatewayError(status, code or "provider_error", f"{c.provider}: {message}", param=param)


class Job:
    """One request on its way through the tiers. Usage is counted once, for whoever answered."""

    def __init__(self, request: Request, caller: KeyCaller, route: Route, endpoint: str, parsed: "Parsed",
                 started: float, admission: Optional[Admission]):
        self.request = request
        self.caller = caller
        self.route = route
        self.endpoint = endpoint
        self.parsed = parsed
        self.started = started
        self.admission = admission
        self.settled = False
        self.spilled: Optional[str] = None
        self.target = "local"                   # who the usage is counted for
        self.deployment_id = route.deployment_id
        self.settings = get_settings()

    # -------------------------------------------------------------- bookkeeping
    def finish(self, status: int, usage) -> None:
        if self.settled:
            return
        self.settled = True
        u = quirks.read_usage(usage)
        if self.admission is not None:
            key_limiter.settle(self.admission, u.prompt + u.completion)
        record(self.caller, self.route.served_name, self.deployment_id, status, self.started, u.prompt, u.completion,
               target=self.target, cached=u.cached, reasoning=u.reasoning, spilled=self.spilled is not None)

    def headers(self, target: str) -> dict:
        out = {"X-Forgeyard-Target": target}
        if self.spilled:
            out["X-Forgeyard-Spilled"] = self.spilled
        return out

    def gone(self) -> Response:
        self.finish(CLIENT_CLOSED, None)
        return Response(status_code=CLIENT_CLOSED)

    async def pause(self, seconds: float) -> None:
        await unless_gone(self.request.receive, asyncio.sleep(seconds))

    # -------------------------------------------------------------- the tiers
    async def run(self, tiers: list[Tier]) -> Response:
        failure: Optional[Failed] = None
        busy: Optional[Tier] = None
        for i, tier in enumerate(tiers):
            more = i < len(tiers) - 1
            result = await self.tier(tier, more)
            if isinstance(result, Response):
                return result
            if result.reason == "busy" and busy is None:
                busy = tier
            failure = result
        if busy is not None:
            # Nothing after the busy tier could take it either: it waits for a local slot after all.
            self.spilled = None
            result = await self.local(busy, can_move_on=False)
            if isinstance(result, Response):
                return result
            failure = result
        assert failure is not None
        if failure.answer is not None:
            self.finish(failure.answer.status_code, None)
            return failure.answer
        raise failure.error or GatewayError(502, "upstream_unreachable", "Nothing could answer.")

    async def tier(self, tier: Tier, more: bool) -> Union[Response, Failed]:
        failed: Optional[Failed] = None
        if tier.locals:
            result = await self.local(tier, can_move_on=more or bool(tier.clouds))
            if isinstance(result, Response):
                return result
            failed = result
            if more or tier.clouds:
                self.spilled = self.spilled or result.reason
        if tier.clouds:
            result = await self.cloud(tier, more)
            if isinstance(result, Response):
                return result
            if failed is None or failed.reason != "busy":
                failed = result
            elif result.error is not None:
                failed = Failed("busy", error=result.error)
            if more and self.spilled is None:
                self.spilled = result.reason
        assert failed is not None
        return failed

    # -------------------------------------------------------------- local replicas
    async def local(self, tier: Tier, *, can_move_on: bool) -> Union[Response, Failed]:
        route, parsed = self.route, self.parsed
        pairs = [(g, t) for g in tier.locals for t in g.replicas]
        if not pairs:
            reason = "; ".join(g.reason for g in tier.locals if g.reason)
            return Failed("unavailable", GatewayError(503, "no_ready_replica",
                                                      f"{route.served_name!r} has no ready replica. {reason}".strip()))
        if can_move_on and route.spill_on_long_prompt:
            need = prompt_estimate(parsed)
            fits = [(g, t) for g, t in pairs if not g.context or need <= g.context]
            if not fits:
                most = max(g.context or 0 for g, _ in pairs)
                return Failed("too_long", GatewayError(400, "context_length_exceeded",
                                                       f"The request needs about {need} tokens; the local deployments "
                                                       f"of {route.served_name!r} take at most {most}."))
            pairs = fits
        group_of = {t.uid: g for g, t in pairs}
        remaining = balancer.order(f"{route.served_name}#{tier.index}", [t for _, t in pairs])
        caps = {t.uid: group_of[t.uid].concurrency for t in remaining} if can_move_on else {}
        body_for: dict[str, tuple[bytes, bool]] = {}
        last_exc: Optional[Exception] = None
        refused: Optional[Response] = None
        deadline: Optional[float] = None
        while remaining:
            pick = next((t for t in remaining if balancer.try_acquire(t.uid, caps.get(t.uid))), None)
            if pick is None:
                # Every replica left is at its deployment's concurrency.
                if deadline is None:
                    deadline = time.monotonic() + route.wait_ms / 1000
                if time.monotonic() < deadline:
                    try:
                        await self.pause(min(0.025, max(0.0, deadline - time.monotonic())))
                    except ClientGone:
                        return self.gone()
                    continue
                if refused is not None:
                    return Failed("refused", answer=refused)
                return Failed("busy", GatewayError(503, "local_busy", f"Every replica of {route.served_name!r} is busy."))
            remaining.remove(pick)
            g = group_of[pick.uid]
            self.target, self.deployment_id = "local", g.deployment_id
            if g.upstream_model not in body_for:
                body_for[g.upstream_model] = upstream_body(parsed, self.endpoint, g.upstream_model)
            body, strip = body_for[g.upstream_model]
            ctype = self.request.headers.get("content-type", "application/json") if parsed.parts is not None \
                else "application/json"
            headers = {"Content-Type": ctype, "Accept": self.request.headers.get("accept") or "*/*",
                       "Accept-Encoding": "identity", "User-Agent": "forgeyard-gateway"}
            if pick.secret:
                headers["Authorization"] = f"Bearer {pick.secret}"
            http = client()
            try:
                req = http.build_request("POST", f"{pick.url}/v1/{self.endpoint}", content=body, headers=headers)
                response = await unless_gone(self.request.receive, http.send(req, stream=True))
            except ClientGone:
                balancer.release(pick.uid)
                return self.gone()
            except RETRYABLE as e:
                balancer.release(pick.uid)
                balancer.failed(pick.uid)
                last_exc = e
                log.warning("Replica %s of %s did not take the request (%s); trying the next one.",
                            pick.ordinal, g.name, type(e).__name__)
                continue
            except httpx.TimeoutException:
                balancer.release(pick.uid)
                raise GatewayError(504, "upstream_timeout", f"The replica did not answer within "
                                                            f"{self.settings.gateway_read_timeout:g} s.") from None
            except httpx.HTTPError as e:
                balancer.release(pick.uid)
                raise GatewayError(502, "upstream_error", f"The request to the replica failed ({type(e).__name__}).") from None
            out_headers = {"X-Forgeyard-Replica": f"{route.served_name}#{pick.ordinal}",
                           **self.headers(f"local:{g.name}#{pick.ordinal}")}
            if response.status_code in (401, 403):
                await response.aclose()
                balancer.release(pick.uid)
                log.warning("Replica %s of %s refused the gateway's internal key (HTTP %s).", pick.ordinal, g.name,
                            response.status_code)
                raise GatewayError(502, "replica_refused_key", "The replica refused the gateway's internal key. Stop and "
                                                               "start the deployment to give it a new one.")
            if can_move_on and response.status_code in (429, 503):
                # The engine is overloaded: another replica, or the next tier, may take it.
                try:
                    data = await unless_gone(self.request.receive, response.aread())
                except ClientGone:
                    return self.gone()
                except httpx.HTTPError:
                    data = b""
                finally:
                    await response.aclose()
                    balancer.release(pick.uid)
                log.info("Replica %s of %s answered %s; moving on.", pick.ordinal, g.name, response.status_code)
                refused = Response(content=data, status_code=response.status_code, headers=out_headers,
                                   media_type=response.headers.get("content-type") or "application/json")
                continue
            return await self.serve(response, out_headers, strip, release=pick.uid, who="replica")
        if refused is not None:
            return Failed("refused", answer=refused)
        return Failed("unavailable", GatewayError(502, "upstream_unreachable",
                                                  f"No replica of {route.served_name!r} could be reached "
                                                  f"({type(last_exc).__name__ if last_exc else 'none tried'})."))

    # -------------------------------------------------------------- providers
    async def cloud(self, tier: Tier, more: bool) -> Union[Response, Failed]:
        failures: list[tuple[CloudTarget, Failed]] = []
        for c in order_clouds(tier.clouds):
            result = await self.send_cloud(c)
            if isinstance(result, Response):
                return result
            failures.append((c, result))
        cap = self.settings.provider_retry_after_cap
        waits = [(f.retry_after, i, c) for i, (c, f) in enumerate(failures) if f.retry_after is not None
                 and f.retry_after <= cap]
        if waits and not more:
            # Nothing else is left: wait the short Retry-After once.
            delay, _, c = min(waits, key=lambda w: (w[0], w[1]))
            try:
                await self.pause(delay)
            except ClientGone:
                return self.gone()
            result = await self.send_cloud(c, again=True)
            if isinstance(result, Response):
                return result
            failures.append((c, result))
        return failures[-1][1]

    async def send_cloud(self, c: CloudTarget, again: bool = False) -> Union[Response, Failed]:
        parsed, endpoint = self.parsed, self.endpoint
        self.target, self.deployment_id = c.key, None
        kind = quirks.get(c.kind)
        obj = dict(parsed.obj or {})
        obj["model"] = c.upstream_model
        strip = False
        if parsed.stream and endpoint in USAGE_ENDPOINTS and kind.stream_usage:
            options = obj.get("stream_options")
            options = dict(options) if isinstance(options, dict) else {}
            if options.get("include_usage") is not True:
                options["include_usage"] = True
                obj["stream_options"] = options
                strip = True
        obj, _dropped = quirks.outgoing(c.kind, obj, endpoint, c.drop_params)
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        cooldown = self.settings.provider_cooldown_seconds
        try:
            url, host_headers, extensions = await egress.prepare(c.base_url, endpoint, external=c.external,
                                                                 allow_http=c.allow_http)
        except egress.EgressError as e:
            log.warning("Provider %s: the request was not sent (%s).", c.provider, e)
            balancer.cool_down(c.key, cooldown)
            return Failed("unavailable", GatewayError(502, "provider_blocked", f"{c.provider}: {e}."))
        headers = {k: str(v) for k, v in (c.headers or {}).items()}
        headers.update({"Content-Type": "application/json", "Accept": self.request.headers.get("accept") or "*/*",
                        "Accept-Encoding": "identity", "User-Agent": "forgeyard-gateway"})
        headers.update(host_headers)
        headers.update(quirks.auth_headers(c.kind, c.api_key))
        http = egress.client()
        balancer.acquire(c.key)
        try:
            req = http.build_request("POST", url, content=body, headers=headers, extensions=extensions,
                                     timeout=egress.timeout_for(c.timeout_s) or httpx.USE_CLIENT_DEFAULT)
            response = await unless_gone(self.request.receive, http.send(req, stream=True))
        except ClientGone:
            balancer.release(c.key)
            return self.gone()
        except RETRYABLE as e:
            balancer.release(c.key)
            balancer.cool_down(c.key, cooldown)
            log.warning("Provider %s (%s) could not be reached (%s); trying the next target.", c.provider,
                        c.upstream_model, type(e).__name__)
            return Failed("unavailable", GatewayError(502, "provider_unreachable",
                                                      f"{c.provider} could not be reached ({type(e).__name__})."))
        except httpx.TimeoutException:
            balancer.release(c.key)
            raise GatewayError(504, "provider_timeout", f"{c.provider} did not answer in time.") from None
        except httpx.HTTPError as e:
            balancer.release(c.key)
            balancer.cool_down(c.key, cooldown)
            return Failed("unavailable", GatewayError(502, "provider_error",
                                                      f"The request to {c.provider} failed ({type(e).__name__})."))
        status = response.status_code
        rid = response.headers.get("x-request-id") or response.headers.get("request-id") or "-"
        if 300 <= status < 400:
            await response.aclose()
            balancer.release(c.key)
            balancer.cool_down(c.key, cooldown)
            log.warning("Provider %s answered a redirect (%s); not followed.", c.provider, status)
            return Failed("unavailable", GatewayError(502, "provider_redirect",
                                                      f"{c.provider} answered with a redirect ({status}); Forgeyard does "
                                                      "not follow redirects to providers. Check its base URL."))
        if status in (401, 403, 407):
            await response.aclose()
            balancer.release(c.key)
            log.warning("Provider %s refused Forgeyard's key for it (HTTP %s, request id %s).", c.provider, status, rid)
            raise GatewayError(502, "provider_auth_failed", f"The provider {c.provider} refused Forgeyard's key for it "
                                                            f"(HTTP {status}). An admin needs to check the provider.")
        if status >= 400:
            try:
                data = await unless_gone(self.request.receive, response.aread())
            except ClientGone:
                return self.gone()
            except httpx.HTTPError:
                data = b""
            finally:
                await response.aclose()
                balancer.release(c.key)
            ra = retry_after(response.headers)
            err = provider_error(c, status, data[:65536], ra)
            if status == 429 or status >= 500:
                balancer.cool_down(c.key, max(cooldown, min(ra or 0.0, 300.0)))
                log.warning("Provider %s (%s) answered %s (request id %s)%s.", c.provider, c.upstream_model, status, rid,
                            "" if again else "; trying the next target")
                return Failed("refused" if status in (429, 503) else "unavailable", err, retry_after=ra, status=status)
            raise err
        return await self.serve(response, self.headers(c.label), strip, release=c.key, who=c.provider)

    # -------------------------------------------------------------- the answer
    async def serve(self, response: httpx.Response, out_headers: dict, strip: bool, *, release: str,
                    who: str) -> Response:
        is_sse = "text/event-stream" in response.headers.get("content-type", "")
        if is_sse and response.status_code == 200:
            def done(status: int, usage) -> None:
                balancer.release(release)
                self.finish(status, usage)

            out_headers = {**out_headers, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
            return StreamResponse(response, SSEFilter(strip), out_headers, done, who=who)
        try:
            data = await unless_gone(self.request.receive, response.aread())
        except ClientGone:
            return self.gone()
        except httpx.TimeoutException:
            raise GatewayError(504, "upstream_timeout", f"The answer from {who} did not finish in time.") from None
        except httpx.HTTPError as e:
            raise GatewayError(502, "upstream_error", f"The answer from {who} broke off ({type(e).__name__}).") from None
        finally:
            await response.aclose()
            balancer.release(release)
        usage = None
        if response.status_code == 200 and "json" in response.headers.get("content-type", ""):
            try:
                answer = json.loads(data)
                usage = answer.get("usage") if isinstance(answer, dict) else None
            except ValueError:
                pass
        self.finish(response.status_code, usage)
        return Response(content=data, status_code=response.status_code, headers=out_headers,
                        media_type=response.headers.get("content-type") or "application/json")


async def forward(request: Request, *, caller: KeyCaller, route: Route, endpoint: str, parsed: Parsed,
                  started: float) -> Response:
    """Limits, a target, the upstream request and the answer. Records usage when it is over."""
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
    job = Job(request, caller, route, endpoint, parsed, started, admission)
    try:
        local_only = wants_local_only(request)
        tiers = allowed_tiers(route, caller.cloud, local_only, endpoint)
        if not tiers:
            if route.has_cloud and (local_only or route.local_only):
                raise GatewayError(400, "cloud_only_model", f"{route.served_name!r} is served only by cloud providers, "
                                                            "and this request may not leave your network.")
            raise GatewayError(503, "no_target", f"{route.served_name!r} has no target that can take this request "
                                                 "(every target is disabled, or none serves this endpoint).")
        return await job.run(tiers)
    except GatewayError as e:
        job.finish(e.status, None)
        raise


def record(caller: KeyCaller, model: str, deployment_id: Optional[int], status: int, started: float,
           prompt: int = 0, completion: int = 0, limited: bool = False, *, target: str = "local", cached: int = 0,
           reasoning: int = 0, spilled: bool = False) -> None:
    recorder.record(source=caller.source, key_id=caller.key_id, user_id=caller.user_id, username=caller.username,
                    model=model, deployment_id=deployment_id, status=status,
                    latency_ms=(time.monotonic() - started) * 1000, prompt_tokens=prompt,
                    completion_tokens=completion, limited=limited, target=target, cached_tokens=cached,
                    reasoning_tokens=reasoning, spilled=spilled)
