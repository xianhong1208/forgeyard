"""The generic passthrough (1.3): any service workload's HTTP API behind the gateway at /x/<name>/….

An admin or operator exposes a service workload (forgeyard/workloads/expose.py). Programs then call
`https://<server>/x/<name>/<path>?<query>` with an API key whose `workloads` setting allows it, and the
gateway passes the request to one of the workload's ready replicas as `/<path>?<query>`.

1. **Auth:** `Authorization: Bearer fyk_…` only: never the console's cookie, never a key in the query.
   A key with `workloads: none` (every key from before 1.3) gets 403 `workloads_not_allowed`; a
   workload it may not call, or that does not exist or is not exposed, is 404 `workload_not_found`
   (it cannot learn which names exist).
2. **Path:** read from the raw request path, never a decoded or normalised one. The prefix `/x/<name>`
   is stripped (`/x/<name>` alone becomes `/`). Dot segments (`.`, `..`), encoded `.`, `/` or `\\`
   (`%2e`, `%2f`, `%5c`), a backslash, control characters (raw or encoded) and non-ASCII bytes are
   refused with 400 `invalid_path`, not normalised: what the gateway checks is what the program gets.
   The query string is passed on as it came.
3. **Request headers:** hop-by-hop headers and the ones `Connection` names, `Authorization`, `Cookie`,
   `Host`, `Forwarded`, `X-Forwarded-*`, `X-Real-IP` and `X-Forgeyard-*` are dropped; fresh
   `X-Forwarded-For`, `-Proto`, `-Host`, `X-Forwarded-Prefix: /x/<name>` and `X-Request-Id` are added,
   and the replica's internal key, which the agent's guard checks and removes. Everything else (the
   content type, `Accept`, `Accept-Encoding`, the app's own headers) passes unchanged.
4. **Body:** read up to the workload's `max_body_mb` (413 `request_too_large` above), so a replica
   that cannot be connected to can be skipped for the next one.
5. **Answer:** passed through as raw bytes, as they arrive (compressed bodies stay compressed, SSE and
   chunked answers stream). Hop-by-hop, `Set-Cookie`, `Access-Control-*`, `Strict-Transport-Security`,
   `Clear-Site-Data`, `Alt-Svc`, `Service-Worker-Allowed`, reporting headers, `X-Forgeyard-*`, `Date` and
   `Server` (the server sends its own) and the security headers below are dropped; these are **forced**, so nothing a workload serves can run as
   the console's origin:
       Content-Security-Policy: sandbox; default-src 'none'; frame-ancestors 'none'
       X-Content-Type-Options: nosniff, X-Frame-Options: DENY, Cache-Control: no-store,
       Cross-Origin-Resource-Policy: same-origin, Referrer-Policy: no-referrer
   Redirects are never followed; `Location` and `Content-Location` that point at the workload are
   rewritten under `/x/<name>`. `X-Forgeyard-Target: workload:<name>#<replica>` and `X-Request-Id`
   say who answered.
6. **Timeouts:** 5 s to connect (GATEWAY_CONNECT_TIMEOUT), then the workload's `read_timeout` for each
   piece of the answer (504 `upstream_timeout` before the first byte). A client that goes away closes
   the upstream request at once.
7. **Limits:** the key's requests per minute (429 `rate_limit_exceeded`; tokens per minute do not
   apply), at most `concurrency` requests at once per replica (429 `workload_busy` when every replica
   is full) and `key_concurrency` per key (429 `key_busy`), with `Retry-After`.
8. **Usage:** one row per day, key and workload, as the model `x:<name>`: requests, errors, latency,
   bytes in (request bodies) and out (answer bodies, as sent), and busy milliseconds (from sending the
   request until the answer ended).

Not in 1.3: WebSockets (an `Upgrade` is dropped) and gRPC (HTTP/2 trailers), browser UIs (the forced
sandbox), bodies above the limit streamed through, and cloud spill.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response

from forgeyard.config import get_settings
from forgeyard.db import Replica, Workload, new_session
from forgeyard.gateway import keys
from forgeyard.gateway.balancer import Target, balancer
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.keys import KeyCaller
from forgeyard.gateway.limits import Refusal, key_limiter
from forgeyard.gateway.proxy import CLIENT_CLOSED, RETRYABLE, ClientGone, client, unless_gone, _until_disconnect
from forgeyard.gateway.usage import recorder
from forgeyard.security.client import client_ip
from forgeyard.workloads import expose
from forgeyard.workloads.spec import NAME_RE

log = logging.getLogger("forgeyard.gateway")

PREFIX = b"/x/"
TARGET_HEADER = "X-Forgeyard-Target"
REQUEST_ID = "X-Request-Id"
GUARD_REALM = 'realm="forgeyard-replica"'
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "proxy-authorization", "proxy-authenticate", "te",
              "trailer", "transfer-encoding", "upgrade", "expect"}
DROP_REQUEST = HOP_BY_HOP | {"authorization", "cookie", "host", "content-length", "forwarded", "x-real-ip"}
DROP_REQUEST_PREFIXES = ("x-forwarded-", "x-forgeyard-")
FORCED = (
    ("content-security-policy", "sandbox; default-src 'none'; frame-ancestors 'none'"),
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("cache-control", "no-store"),
    ("cross-origin-resource-policy", "same-origin"),
    ("referrer-policy", "no-referrer"),
)
DROP_RESPONSE = HOP_BY_HOP | {name for name, _ in FORCED} | {
    "set-cookie", "set-cookie2", "strict-transport-security", "content-security-policy-report-only",
    "clear-site-data", "alt-svc", "service-worker-allowed", "report-to", "reporting-endpoints", "nel",
    "origin-agent-cluster", "cross-origin-opener-policy", "cross-origin-embedder-policy", "pragma", "set-login",
    "date", "server"}  # the server sends its own Date and Server: never two of each
DROP_RESPONSE_PREFIXES = ("access-control-", "x-forgeyard-")
# Encoded '.', '/', '\' and control characters, which a program would decode into something the gateway
# did not check.
_ENCODED = re.compile(rb"%(?:2e|2f|5c|[01][0-9a-f]|7f)", re.IGNORECASE)
_DOTS = re.compile(rb"^\.\.?(?:;.*)?$")


# ------------------------------------------------------------------ the path
def _bad_bytes(value: bytes) -> bool:
    return any(b < 0x21 or b > 0x7e for b in value)


def parse_path(raw_path: bytes) -> tuple[str, bytes]:
    """(the workload's name, the path the workload gets) from the raw request path, or GatewayError."""
    if raw_path == b"/x" or not raw_path.startswith(PREFIX):
        raise GatewayError(404, "workload_not_found", "Call a workload as /x/<name>/<path>.")
    rest = raw_path[len(PREFIX):]
    raw_name, sep, tail = rest.partition(b"/")
    try:
        name = raw_name.decode("ascii")
    except UnicodeDecodeError:
        name = ""
    if not NAME_RE.match(name):
        raise GatewayError(404, "workload_not_found", "A workload's name has a-z, 0-9 and '-' only.")
    path = b"/" + tail if sep else b"/"
    if _bad_bytes(path) or b"\\" in path or _ENCODED.search(path):
        raise GatewayError(400, "invalid_path", "The path has a character the gateway does not pass on: a "
                                                "control character, a backslash, non-ASCII text or an encoded "
                                                "'.', '/' or '\\'.")
    if any(_DOTS.match(seg) for seg in path.split(b"/")):
        raise GatewayError(400, "invalid_path", "The path has a '.' or '..' segment; the gateway does not pass "
                                                "those on.")
    return name, path


def check_query(query: bytes) -> bytes:
    if _bad_bytes(query):
        raise GatewayError(400, "invalid_path", "The query string has a control character or non-ASCII text.")
    return query


# ------------------------------------------------------------------ the workload
@dataclass
class XRoute:
    name: str
    workload_id: int
    settings: expose.Exposure
    replicas: list[Target] = field(default_factory=list)
    reason: Optional[str] = None


def _not_ready(w: Workload) -> str:
    if w.desired_state != "running":
        return f"The workload {w.name!r} is stopped."
    parts = []
    for r in w.replicas:
        where = f" on {r.node.name}" if r.node else " (not placed)"
        parts.append(f"replica {r.ordinal}{where}: {r.reason or r.state}")
    return (f"The workload {w.name!r} has no ready replica: " + "; ".join(parts))[:400] if parts \
        else f"The workload {w.name!r} has no replicas."


def load(db: DbSession, name: str) -> Optional[XRoute]:
    """The exposed workload `name` and its ready replicas (behind a guard that strips the key), or None."""
    from forgeyard.models.routes import _ready

    w = db.scalar(select(Workload).where(Workload.name == name)
                  .options(selectinload(Workload.replicas).selectinload(Replica.node)))
    settings = expose.of(w) if w is not None else None
    if w is None or settings is None:
        return None
    port_name = expose.port_name(w, settings)
    out = XRoute(name=w.name, workload_id=w.id, settings=settings)
    skipped = []
    for r in w.replicas:
        if not _ready(r, w):
            continue
        if not r.secret or not expose.can_guard(r.node):
            # Never through an unguarded port: its program would answer anyone, and the key would reach it.
            skipped.append(f"replica {r.ordinal} runs without the agent's key guard (restart the workload)")
            continue
        port = next((p for p in r.ports or [] if p.get("name") == port_name), None)
        if not port or not port.get("host_port") or not port.get("address"):
            continue
        host = port["address"]
        host = f"[{host}]" if ":" in host else host
        out.replicas.append(Target(uid=r.uid, ordinal=r.ordinal, url=f"http://{host}:{port['host_port']}",
                                   machine=r.node.name if r.node else None, secret=r.secret, protected=True))
    if not out.replicas:
        out.reason = "; ".join(skipped) if skipped else _not_ready(w)
    return out


def _load(name: str) -> Optional[XRoute]:
    db = new_session()
    try:
        return load(db, name)
    finally:
        db.close()


def _authenticate(authorization: Optional[str]) -> KeyCaller:
    db = new_session()
    try:
        caller = keys.authenticate(db, authorization)
        db.commit()  # last_used_at
        return caller
    finally:
        db.close()


# ------------------------------------------------------------------ requests in flight
class InFlight:
    """Requests in flight per API key and workload (key_concurrency), in this process."""

    def __init__(self):
        self._lock = threading.Lock()
        self.counts: dict[tuple, int] = {}

    def reset(self) -> None:
        with self._lock:
            self.counts.clear()

    def enter(self, key: tuple, cap: Optional[int]) -> bool:
        with self._lock:
            n = self.counts.get(key, 0)
            if cap and n >= cap:
                return False
            self.counts[key] = n + 1
            return True

    def leave(self, key: tuple) -> None:
        with self._lock:
            n = self.counts.get(key, 0) - 1
            if n > 0:
                self.counts[key] = n
            else:
                self.counts.pop(key, None)


in_flight = InFlight()


# ------------------------------------------------------------------ headers
def _named_by_connection(headers) -> set[str]:
    named = set()
    for k, v in headers:
        if k.lower() == "connection":
            named.update(t.strip().lower() for t in v.split(",") if t.strip())
    return named


def request_headers(request: Request, name: str, secret: str, request_id: str) -> list[tuple[str, str]]:
    incoming = list(request.headers.items())
    named = _named_by_connection(incoming)
    out = [(k, v) for k, v in incoming
           if (lk := k.lower()) not in DROP_REQUEST and lk not in named and not lk.startswith(DROP_REQUEST_PREFIXES)]
    from forgeyard.api.deps import request_is_https

    out += [("Authorization", f"Bearer {secret}"),   # checked and removed by the agent's guard
            ("X-Forwarded-For", client_ip(request)),
            ("X-Forwarded-Proto", "https" if request_is_https(request) else "http"),
            ("X-Forwarded-Host", request.headers.get("host", "")),
            ("X-Forwarded-Prefix", expose.url_path(name)),
            (REQUEST_ID, request_id)]
    return out


def rewrite_location(value: str, name: str, upstream: str, public_host: str) -> str:
    """A Location or Content-Location from the workload, under /x/<name>: a path on the workload gets
    the prefix; an absolute URL at the workload's own address (or at this server without the prefix)
    becomes that path under the prefix. Other URLs (another site) and relative references stay."""
    prefix = expose.url_path(name)
    if value.startswith("//"):
        return value
    if value.startswith("/"):
        return prefix + value
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    if parts.scheme not in ("http", "https"):
        return value
    netloc = parts.netloc.lower()
    ours = {upstream.lower(), public_host.lower()}
    if netloc not in ours and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        return value
    path = parts.path or "/"
    if netloc == public_host.lower() and (path == prefix or path.startswith(prefix + "/")):
        out = path
    else:
        out = prefix + path
    return out + (f"?{parts.query}" if parts.query else "") + (f"#{parts.fragment}" if parts.fragment else "")


def response_headers(upstream: httpx.Response, name: str, upstream_netloc: str, public_host: str,
                     extra: dict) -> list[tuple[bytes, bytes]]:
    raw = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in upstream.headers.raw]
    named = _named_by_connection(raw)
    out = []
    for k, v in raw:
        lk = k.lower()
        if lk in DROP_RESPONSE or lk in named or lk.startswith(DROP_RESPONSE_PREFIXES):
            continue
        if lk in ("location", "content-location"):
            v = rewrite_location(v, name, upstream_netloc, public_host)
        out.append((lk.encode("latin-1"), v.encode("latin-1")))
    out += [(k.encode(), v.encode()) for k, v in FORCED]
    out += [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in extra.items()]
    return out


def forced_error(e: GatewayError, extra: Optional[dict] = None) -> Response:
    """A gateway error at /x/: OpenAI's error shape, with the forced headers too."""
    r = e.response()
    for k, v in FORCED:
        r.headers[k] = v
    for k, v in (extra or {}).items():
        r.headers[k] = v
    return r


# ------------------------------------------------------------------ the answer
class Relay(Response):
    """The workload's answer, raw bytes as they arrive. Closes the upstream request as soon as the
    client goes away; counts the bytes sent."""

    def __init__(self, upstream: httpx.Response, headers: list[tuple[bytes, bytes]], *, head: bool,
                 done: Callable[[int, int], None]):
        self.status_code = upstream.status_code
        self.background = None
        self.raw_headers = headers
        self.upstream = upstream
        self.head = head
        self.done = done
        self.sent = 0

    async def __call__(self, scope, receive, send):
        status = self.upstream.status_code
        broken: Optional[Exception] = None
        await send({"type": "http.response.start", "status": status, "headers": self.raw_headers})

        async def pump():
            nonlocal status, broken
            if self.head or status in (204, 304):
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            try:
                async for chunk in self.upstream.aiter_raw():
                    if chunk:
                        self.sent += len(chunk)
                        await send({"type": "http.response.body", "body": chunk, "more_body": True})
            except httpx.HTTPError as e:
                status, broken = 502, e
                log.warning("An answer from a workload broke off: %s", type(e).__name__)
                return
            await send({"type": "http.response.body", "body": b"", "more_body": False})

        pumping = asyncio.ensure_future(pump())
        watching = asyncio.ensure_future(_until_disconnect(receive))
        try:
            done, _ = await asyncio.wait({pumping, watching}, return_when=asyncio.FIRST_COMPLETED)
            if pumping not in done:
                status = CLIENT_CLOSED
            else:
                pumping.result()
        except OSError:
            status = CLIENT_CLOSED
        finally:
            for t in (pumping, watching):
                t.cancel()
            for t in (pumping, watching):
                with contextlib.suppress(BaseException):
                    await t
            await self.upstream.aclose()  # the workload sees the connection close
            self.done(status, self.sent)
        if broken is not None:
            # The answer is incomplete: end the connection without finishing it, so the client cannot take
            # a cut-off body for a whole one.
            raise UpstreamBroken(str(type(broken).__name__))


class UpstreamBroken(Exception):
    pass


# ------------------------------------------------------------------ one request
def record(caller: KeyCaller, name: str, status: int, started: float, *, limited: bool = False,
           bytes_in: int = 0, bytes_out: int = 0, busy_ms: int = 0) -> None:
    recorder.record(source=caller.source, key_id=caller.key_id, user_id=caller.user_id, username=caller.username,
                    model=f"x:{name}", deployment_id=None, status=status,
                    latency_ms=(time.monotonic() - started) * 1000, limited=limited,
                    units={"bytes_in": bytes_in, "bytes_out": bytes_out, "busy_ms": busy_ms})


async def read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    mb = limit // 1024**2
    if declared and declared.isdigit() and int(declared) > limit:
        raise GatewayError(413, "request_too_large", f"The request is larger than {mb} MiB, the most this workload takes.")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise GatewayError(413, "request_too_large", f"The request is larger than {mb} MiB, the most this workload "
                                                         "takes.")
        chunks.append(chunk)
    return b"".join(chunks)


async def handle(request: Request) -> Response:
    started = time.monotonic()
    request_id = uuid.uuid4().hex
    ids = {REQUEST_ID: request_id}
    try:
        caller = await run_in_threadpool(_authenticate, request.headers.get("authorization"))
        name, path = parse_path(request.scope.get("raw_path") or request.url.path.encode("utf-8"))
        query = check_query(request.scope.get("query_string") or b"")
        if caller.workloads == "none":
            raise GatewayError(403, "workloads_not_allowed", "This API key may not call workloads. Change its "
                                                             "'Workloads' setting to allow this one.")
        if not keys.may_call(caller.workloads, caller.workload_list, name):
            raise GatewayError(404, "workload_not_found", f"No workload {name!r} is exposed to this API key.")
        route = await run_in_threadpool(_load, name)
        if route is None:
            raise GatewayError(404, "workload_not_found", f"No workload {name!r} is exposed to this API key.")
    except GatewayError as e:
        return forced_error(e, ids)
    try:
        return await _forward(request, caller, route, path, query, started, request_id)
    except GatewayError as e:
        return forced_error(e, ids)


async def _forward(request: Request, caller: KeyCaller, route: XRoute, path: bytes, query: bytes, started: float,
                   request_id: str) -> Response:
    s = route.settings
    name = route.name
    try:
        body = await read_body(request, s.max_body_mb * 1024**2)
    except ClientDisconnect:
        record(caller, name, CLIENT_CLOSED, started)
        return Response(status_code=CLIENT_CLOSED)
    except GatewayError as e:
        record(caller, name, e.status, started)
        raise
    if not route.replicas:
        record(caller, name, 503, started, bytes_in=len(body))
        raise GatewayError(503, "no_ready_replica", route.reason or f"The workload {name!r} has no ready replica.")
    if caller.source == "api":
        verdict = key_limiter.admit(caller.key_id, caller.rpm, None)
        if isinstance(verdict, Refusal):
            record(caller, name, 429, started, limited=True)
            raise GatewayError(429, "rate_limit_exceeded", f"This API key may send {verdict.limit} requests per "
                                                           f"minute; try again in {verdict.retry_after} s.",
                               retry_after=verdict.retry_after)
    slot = ("x", name, caller.source, caller.key_id)
    if not in_flight.enter(slot, s.key_concurrency):
        record(caller, name, 429, started, limited=True)
        raise GatewayError(429, "key_busy", f"This API key already has {s.key_concurrency} request"
                                            f"{'s' if s.key_concurrency != 1 else ''} in flight to {name!r}, the most "
                                            "it may have at once.", retry_after=1)
    handed_over = False
    try:
        response = await _send(request, caller, route, path, query, body, started, request_id, slot)
        handed_over = isinstance(response, Relay)
        return response
    finally:
        if not handed_over:
            in_flight.leave(slot)


async def _send(request: Request, caller: KeyCaller, route: XRoute, path: bytes, query: bytes, body: bytes,
                started: float, request_id: str, slot: tuple) -> Response:
    s = route.settings
    name = route.name
    settings = get_settings()
    timeout = httpx.Timeout(connect=settings.gateway_connect_timeout, read=float(s.read_timeout), write=60.0, pool=30.0)
    remaining = balancer.order(f"x:{name}", route.replicas)
    head = request.method == "HEAD"
    last_exc: Optional[Exception] = None
    public_host = request.headers.get("host", "")
    ids = {REQUEST_ID: request_id}
    while remaining:
        pick = next((t for t in remaining if balancer.try_acquire(t.uid, s.concurrency)), None)
        if pick is None:
            record(caller, name, 429, started, limited=True, bytes_in=len(body))
            raise GatewayError(429, "workload_busy", f"Every replica of {name!r} already has {s.concurrency} request"
                                                     f"{'s' if s.concurrency != 1 else ''} in flight. Try again "
                                                     "shortly.", retry_after=1)
        remaining.remove(pick)
        base = httpx.URL(pick.url)
        url = base.copy_with(raw_path=path + (b"?" + query if query else b""))
        http = client()
        req = http.build_request(request.method, url, content=body if body or request.method not in ("GET", "HEAD")
                                 else None, headers=request_headers(request, name, pick.secret or "", request_id),
                                 timeout=timeout)
        sent_at = time.monotonic()
        try:
            response = await unless_gone(request.receive, http.send(req, stream=True, follow_redirects=False))
        except ClientGone:
            balancer.release(pick.uid)
            record(caller, name, CLIENT_CLOSED, started, bytes_in=len(body),
                   busy_ms=int((time.monotonic() - sent_at) * 1000))
            return Response(status_code=CLIENT_CLOSED)
        except RETRYABLE as e:
            balancer.release(pick.uid)
            balancer.failed(pick.uid)
            last_exc = e
            log.warning("Replica %s of workload %s did not take the request (%s); trying the next one.",
                        pick.ordinal, name, type(e).__name__)
            continue
        except httpx.TimeoutException:
            balancer.release(pick.uid)
            record(caller, name, 504, started, bytes_in=len(body), busy_ms=int((time.monotonic() - sent_at) * 1000))
            raise GatewayError(504, "upstream_timeout", f"The workload {name!r} did not answer within "
                                                        f"{s.read_timeout} s.") from None
        except httpx.HTTPError as e:
            balancer.release(pick.uid)
            record(caller, name, 502, started, bytes_in=len(body))
            raise GatewayError(502, "upstream_error", f"The request to the workload failed ({type(e).__name__}).") from None
        if response.status_code == 401 and GUARD_REALM in response.headers.get("www-authenticate", ""):
            await response.aclose()
            balancer.release(pick.uid)
            record(caller, name, 502, started, bytes_in=len(body))
            log.warning("The key guard of replica %s of workload %s refused the gateway's internal key.",
                        pick.ordinal, name)
            raise GatewayError(502, "replica_refused_key", "The replica's key guard refused the gateway's internal "
                                                           "key. Stop and start the workload to give it a new one.")
        target = {TARGET_HEADER: f"workload:{name}#{pick.ordinal}", **ids}
        headers = response_headers(response, name, base.netloc.decode("ascii"), public_host, target)
        uid, n_in = pick.uid, len(body)

        def done(status: int, sent: int, uid=uid, n_in=n_in, sent_at=sent_at) -> None:
            balancer.release(uid)
            in_flight.leave(slot)
            record(caller, name, status, started, bytes_in=n_in, bytes_out=sent,
                   busy_ms=int((time.monotonic() - sent_at) * 1000))

        return Relay(response, headers, head=head, done=done)
    record(caller, name, 502, started, bytes_in=len(body))
    raise GatewayError(502, "upstream_unreachable", f"No replica of the workload {name!r} could be reached "
                                                    f"({type(last_exc).__name__ if last_exc else 'none tried'}).")
