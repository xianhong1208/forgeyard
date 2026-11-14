"""The generic passthrough at /x/<name> (1.3): routing through the agent's key guard to a workload's
own HTTP API, prefix and path handling, refused paths, header stripping and forcing, Location rewrites,
the body limit, timeouts, client disconnects, streaming, usage rows, rpm and concurrency, the key's
`workloads` setting (none / listed / all, existing keys none, admin lock), exposing a workload, and the
guard that strips the key (FY-X-*)."""

from __future__ import annotations

import gzip
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Optional

import httpx
import pytest
from alembic import command
from sqlalchemy import select, text

from forgeyard.db import ApiKey, Replica, Workload, get_engine, new_session
from forgeyard.gateway import passthrough as px
from forgeyard.gateway.balancer import balancer
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.usage import recorder
from forgeyard.migrate import alembic_config, upgrade
from forgeyard.workloads import state as wstate
from tests.conftest import add_user, sign_in
from tests.gateway_helpers import LiveServer, api_client, free_port, new_key
from tests.guard_helpers import KEY, GuardThread
from tests.workload_helpers import make_node, service

GUARDED = ("workloads", "replica_keys", "replica_guard", "workload_guard")
PW = "quiet-meadow-4821"


class App:
    """A workload's own HTTP API on 127.0.0.1:<port>, answering without a key (the guard is in front).
    /echo: what reached it; /headers: hostile response headers; /redirect?to=…: 302 there; /sse: one
    event, waits for `release`, a second one; /forever: streams until the client goes away (`gone`);
    /slow: answers after `slow` s; /gzip: a gzip body; /status/<n>: that status; /auth401: the app's
    own 401."""

    def __init__(self):
        self.seen: list[dict] = []
        self.release = threading.Event()
        self.gone = threading.Event()
        self.slow = 3.0
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, status, body: bytes, headers=None, ctype="application/json"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or []):
                    self.send_header(k, v)
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                entry = {"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                         "raw": [(k.lower(), v) for k, v in self.headers.items()], "body": body}
                app.seen.append(entry)
                path = self.path.split("?")[0]
                if path == "/headers":
                    return self._send(200, b"{}", [
                        ("Set-Cookie", "fy_session=stolen; Path=/"), ("Access-Control-Allow-Origin", "*"),
                        ("Access-Control-Allow-Credentials", "true"), ("Strict-Transport-Security", "max-age=1"),
                        ("Content-Security-Policy", "default-src *"), ("X-Frame-Options", "ALLOWALL"),
                        ("Cache-Control", "public, max-age=999"), ("X-Forgeyard-Target", "forged"),
                        ("Clear-Site-Data", '"*"'), ("Alt-Svc", 'h3=":443"'), ("Service-Worker-Allowed", "/"),
                        ("Connection", "X-Hidden"), ("X-Hidden", "1"), ("X-App", "kept"),
                        ("Referrer-Policy", "unsafe-url"), ("X-Content-Type-Options", "sniff-away")])
                if path == "/redirect":
                    to = self.path.split("to=", 1)[1] if "to=" in self.path else "/login"
                    return self._send(302, b"", [("Location", to)])
                if path == "/sse":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    self._chunk(b"data: one\n\n")
                    app.release.wait(10)
                    self._chunk(b"data: two\n\n")
                    self._chunk(b"")
                    return
                if path == "/forever":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()
                    try:
                        for i in range(2000):
                            self._chunk(f"data: {i}\n\n".encode())
                            time.sleep(0.02)
                    except OSError:
                        app.gone.set()
                    return
                if path == "/slow":
                    time.sleep(app.slow)
                    return self._send(200, b'{"slow": true}')
                if path == "/gzip":
                    return self._send(200, gzip.compress(b'{"zipped": true}' * 100), [("Content-Encoding", "gzip")])
                if path.startswith("/status/"):
                    return self._send(int(path.rsplit("/", 1)[1]), b'{"own": true}')
                if path == "/auth401":
                    return self._send(401, b'{"app": "says no"}', [("WWW-Authenticate", 'Bearer realm="app"')])
                out = json.dumps({"method": self.command, "path": self.path, "len": len(body),
                                  "auth": entry["headers"].get("authorization")}).encode()
                self._send(200, out)

            def _chunk(self, data: bytes):
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

            do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = do_HEAD = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def expose_workload(owner, name: str, node: int, gateway: Optional[dict] = None, replicas: int = 1) -> dict:
    r = owner.post("/api/workloads", json={**service(name, machine_id=node, replicas=replicas),
                                           "gateway": {"expose": True, **(gateway or {})}})
    assert r.status_code == 201, r.text
    return r.json()["workload"]


def ready_behind(name: str, ports: list[int], secret: str = KEY) -> None:
    """The workload's replicas: healthy, their published port (the guard's) at `ports`, key `secret`."""
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == name))
        for r, port in zip(sorted(w.replicas, key=lambda r: r.ordinal), ports):
            r.state, r.reason, r.final = "running", None, False
            r.ports = [{"name": "http", "port": None, "host_port": port, "address": "127.0.0.1"}]
            r.secret = secret
        db.commit()
    finally:
        db.close()


def set_gateway(owner, name: str, **gateway) -> dict:
    wid = next(w["id"] for w in owner.get("/api/workloads").json()["workloads"] if w["name"] == name)
    r = owner.put(f"/api/workloads/{wid}/gateway", json=gateway)
    assert r.status_code == 200, r.text
    return r.json()["workload"]


def err(r) -> dict:
    body = r.json()
    assert set(body) == {"error"} and {"message", "type", "code"} <= set(body["error"]), body
    return body["error"]


@pytest.fixture()
def xw(owner):
    node = make_node("cpu-a", "nvidia-rtx4090x2", features=GUARDED)
    app = App()
    guard = GuardThread(app.port, strip_auth=True)
    expose_workload(owner, "web", node)
    ready_behind("web", [guard.port])
    key = new_key(owner, "laptop", workloads="all")["key"]
    ns = SimpleNamespace(owner=owner, node=node, app=app, guard=guard, key=key, api=api_client(owner, key))
    yield ns
    guard.close()
    app.close()


def usage_row(owner, model: str) -> dict:
    recorder.flush()
    rows = owner.get("/api/usage").json()["rows"]
    return next(r for r in rows if r["id"] == model)


# ------------------------------------------------------------------ routing and the path
def test_fy_x_001_a_request_reaches_the_workload_through_its_guard_without_any_key(xw):
    """FY-X-001: GET /x/web/hello?a=1&b=%20c reaches the workload as /hello?a=1&b=%20c through the
    agent's guard, which checked the internal key and removed it: the program sees no Authorization
    at all, neither the client's API key nor the internal one."""
    r = xw.api.get("/x/web/hello?a=1&b=%20c")
    assert r.status_code == 200, r.text
    assert r.json() == {"method": "GET", "path": "/hello?a=1&b=%20c", "len": 0, "auth": None}
    seen = xw.app.seen[-1]
    assert "authorization" not in seen["headers"] and "cookie" not in seen["headers"]
    assert xw.guard.guard.forwarded == 1 and xw.guard.guard.refused == 0
    assert r.headers["x-forgeyard-target"] == "workload:web#0"
    assert len(r.headers["x-request-id"]) == 32 and seen["headers"]["x-request-id"] == r.headers["x-request-id"]
    r = xw.api.post("/x/web/v1/predict", content=b"x" * 1000, headers={"Content-Type": "application/octet-stream"})
    assert r.json() == {"method": "POST", "path": "/v1/predict", "len": 1000, "auth": None}
    assert xw.app.seen[-1]["headers"]["content-type"] == "application/octet-stream"
    for method in ("PUT", "PATCH", "DELETE", "OPTIONS"):
        assert xw.api.request(method, "/x/web/thing").json()["method"] == method
    r = xw.api.head("/x/web/thing")
    assert r.status_code == 200 and r.content == b"" and xw.app.seen[-1]["method"] == "HEAD"


@pytest.mark.parametrize("path,upstream", [("/x/web", "/"), ("/x/web/", "/"), ("/x/web/a/b/", "/a/b/"),
                                           ("/x/web/a//b", "/a//b"), ("/x/web/v2/models/m/infer", "/v2/models/m/infer"),
                                           ("/x/web/x/web/z", "/x/web/z"), ("/x/web/%41%20b", "/%41%20b")])
def test_fy_x_002_the_prefix_is_stripped_and_the_rest_passed_as_it_came(xw, path, upstream):
    """FY-X-002: only /x/<name> is stripped (alone it becomes /); the rest, encoded characters
    included, is passed on byte for byte, and the workload is told its prefix."""
    r = xw.api.get(path)
    assert r.status_code == 200, r.text
    assert r.json()["path"] == upstream
    assert xw.app.seen[-1]["headers"]["x-forwarded-prefix"] == "/x/web"


@pytest.mark.parametrize("raw", [b"/x/web/../admin", b"/x/web/..", b"/x/web/./a", b"/x/web/a/%2e%2e/b", b"/x/web/%2E/a",
                                 b"/x/web/a%2fb", b"/x/web/a%2Fb", b"/x/web/a%5cb", b"/x/web/a\\b", b"/x/web/a%00b",
                                 b"/x/web/a%0d%0ab", b"/x/web/a%7f", b"/x/web/..;/a", b"/x/web/a\x01b",
                                 "/x/web/café".encode()])
def test_fy_x_003_dot_segments_and_encoded_separators_are_refused(raw):
    """FY-X-003: what the gateway would have to normalise (dot segments, encoded '.', '/', '\\',
    control characters raw or encoded, non-ASCII) is refused with 400 invalid_path."""
    with pytest.raises(GatewayError) as e:
        px.parse_path(raw)
    assert (e.value.status, e.value.code) == (400, "invalid_path")
    for bad_name in (b"/x/Web/a", b"/x/-web", b"/x/w%65b/a", b"/x/", b"/x"):
        with pytest.raises(GatewayError) as e:
            px.parse_path(bad_name)
        assert e.value.code == "workload_not_found"
    with pytest.raises(GatewayError):
        px.check_query(b"a=\x00")
    assert px.parse_path(b"/x/web/a.b/.well-known/..x") == ("web", b"/a.b/.well-known/..x")


def test_fy_x_004_refused_paths_never_reach_the_workload_on_a_real_server(xw):
    """FY-X-004: on a real HTTP server (raw request lines, which no client library would send as they
    are), /x/web/../… and /x/web/%2e%2e/… get 400 invalid_path and the workload sees nothing."""
    live = LiveServer()
    try:
        for target in (b"/x/web/../api/users", b"/x/web/%2e%2e/%2e%2e/api/users", b"/x/web/a%2f..%2fb"):
            with socket.create_connection(("127.0.0.1", live.port), timeout=10) as s:
                s.sendall(b"GET " + target + b" HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer "
                          + xw.key.encode() + b"\r\nConnection: close\r\n\r\n")
                data = b""
                while chunk := s.recv(65536):
                    data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            assert head.startswith(b"HTTP/1.1 400"), (target, head)
            assert json.loads(body)["error"]["code"] == "invalid_path"
    finally:
        live.close()
    assert xw.app.seen == []


# ------------------------------------------------------------------ headers
def test_fy_x_005_request_headers_credentials_and_forwarding_are_replaced(xw):
    """FY-X-005: the client's API key, cookies, forwarded headers (X-Forwarded-*, Forwarded,
    X-Real-IP), X-Forgeyard-* and hop-by-hop headers (and what Connection names) never reach the
    workload; fresh X-Forwarded-For/-Proto/-Host/-Prefix are set; the app's own headers pass."""
    r = xw.api.get("/x/web/h", headers={
        "Cookie": "fy_session=abc", "X-Forwarded-For": "6.6.6.6", "X-Forwarded-Host": "evil.example",
        "Forwarded": "for=6.6.6.6", "X-Real-IP": "6.6.6.6", "X-Forgeyard-Local-Only": "1", "Connection": "X-Drop",
        "X-Drop": "1", "Upgrade": "websocket", "X-App-Token": "kept", "Accept": "application/json"})
    assert r.status_code == 200
    h = xw.app.seen[-1]["headers"]
    for gone in ("cookie", "forwarded", "x-real-ip", "x-forgeyard-local-only", "x-drop", "upgrade", "authorization"):
        assert gone not in h, gone
    assert h["x-forwarded-for"] == "testclient"
    assert h["x-forwarded-host"] == "localhost" and h["x-forwarded-proto"] == "http"
    assert h["x-forwarded-prefix"] == "/x/web"
    assert h["x-app-token"] == "kept" and h["accept"] == "application/json"
    raw = json.dumps(xw.app.seen[-1]["raw"])
    assert "evil" not in raw and "6.6.6.6" not in raw  # not even as a second copy
    assert [k for k, _ in xw.app.seen[-1]["raw"]].count("x-forwarded-for") == 1


def test_fy_x_006_response_headers_are_stripped_and_the_sandbox_forced(xw):
    """FY-X-006: Set-Cookie, Access-Control-*, HSTS, Clear-Site-Data, Alt-Svc, Service-Worker-Allowed,
    X-Forgeyard-* and what Connection names are dropped; CSP sandbox, nosniff, DENY, no-store, CORP
    and no-referrer are forced over the workload's own; errors carry them too."""
    r = xw.api.get("/x/web/headers")
    assert r.status_code == 200
    h = r.headers
    for gone in ("set-cookie", "access-control-allow-origin", "access-control-allow-credentials",
                 "strict-transport-security", "clear-site-data", "alt-svc", "service-worker-allowed", "x-hidden"):
        assert gone not in h, gone
    assert h["content-security-policy"] == "sandbox; default-src 'none'; frame-ancestors 'none'"
    assert h["x-content-type-options"] == "nosniff" and h["x-frame-options"] == "DENY"
    assert h["cache-control"] == "no-store" and h["cross-origin-resource-policy"] == "same-origin"
    assert h["referrer-policy"] == "no-referrer"
    assert h["x-forgeyard-target"] == "workload:web#0" and h["x-app"] == "kept"
    assert len(h.get_list("content-security-policy")) == 1 and len(h.get_list("cache-control")) == 1
    assert "BaseHTTP" not in h.get("server", "") and len(h.get_list("date")) <= 1
    r = xw.api.get("/x/nothing-here/a")
    assert r.status_code == 404 and r.headers["content-security-policy"].startswith("sandbox")


def test_fy_x_007_locations_are_rewritten_under_the_prefix(xw):
    """FY-X-007: redirects are passed on, never followed; a Location on the workload (a path, or its
    own address) is rewritten under /x/<name>; another site's and relative ones stay."""
    upstream = f"127.0.0.1:{xw.guard.port}"
    cases = {"/login": "/x/web/login", "/a?b=1#c": "/x/web/a?b=1#c", f"http://{upstream}/docs": "/x/web/docs",
             "http://127.0.0.1:9/x": "/x/web/x", "http://localhost/y": "/x/web/y", "https://example.com/z": "https://example.com/z",
             "next": "next", "//cdn.example/a": "//cdn.example/a", "http://localhost/x/web/done": "/x/web/x/web/done"}
    for raw, want in cases.items():
        assert px.rewrite_location(raw, "web", upstream, "forge.example:7860") == want, raw
    assert px.rewrite_location("https://forge.example:7860/x/web/ok", "web", upstream, "forge.example:7860") == "/x/web/ok"
    assert px.rewrite_location("https://forge.example:7860/ok", "web", upstream, "forge.example:7860") == "/x/web/ok"
    r = xw.api.get("/x/web/redirect?to=/login", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/x/web/login"
    assert len(xw.app.seen) == 1  # not followed
    r = xw.api.get(f"/x/web/redirect?to=http://{upstream}/next", follow_redirects=False)
    assert r.headers["location"] == "/x/web/next"


# ------------------------------------------------------------------ body, timeouts, failover
def test_fy_x_008_the_body_limit(xw):
    """FY-X-008: a body above the workload's max_body_mb is 413 request_too_large (whether it says its
    length or not) and never reaches the workload; one at the limit passes."""
    set_gateway(xw.owner, "web", expose=True, max_body_mb=1)
    assert xw.api.post("/x/web/up", content=b"x" * (1024**2)).json()["len"] == 1024**2
    r = xw.api.post("/x/web/up", content=b"x" * (1024**2 + 1))
    assert r.status_code == 413 and err(r)["code"] == "request_too_large"

    def chunks():
        for _ in range(3):
            yield b"y" * (600 * 1024)

    r = xw.api.post("/x/web/up", content=chunks())
    assert r.status_code == 413
    assert [s["path"] for s in xw.app.seen] == ["/up"]
    assert usage_row(xw.owner, "x:web")["errors"] == 2


def test_fy_x_009_read_timeout_and_unreachable_replicas(xw):
    """FY-X-009: a workload slower than its read_timeout is 504 upstream_timeout; a replica that cannot
    be reached is skipped for the next one; with none reachable, 502 upstream_unreachable."""
    set_gateway(xw.owner, "web", expose=True, read_timeout=1)
    started = time.monotonic()
    r = xw.api.get("/x/web/slow")
    assert r.status_code == 504 and err(r)["code"] == "upstream_timeout"
    assert time.monotonic() - started < 2.9
    dead = GuardThread(free_port(), strip_auth=True)  # a guard whose program is down: closes without an answer
    try:
        db = new_session()
        w = db.scalar(select(Workload).where(Workload.name == "web"))
        db.add(Replica(workload_id=w.id, ordinal=1, generation=w.generation, state="running", node_id=xw.node,
                       secret=KEY, gpus=[], ports=[{"name": "http", "host_port": dead.port, "address": "127.0.0.1"}]))
        db.commit()
        db.close()
        for _ in range(4):
            assert xw.api.get("/x/web/ok").status_code == 200
        ready_behind("web", [dead.port, dead.port])
        r = xw.api.get("/x/web/ok")
        assert r.status_code == 502 and err(r)["code"] == "upstream_unreachable"
    finally:
        dead.close()


def test_fy_x_010_a_client_that_goes_away_closes_the_upstream(xw):
    """FY-X-010: on a real server, reading one chunk of an endless stream and hanging up closes the
    workload's connection at once; nothing stays in flight, and it counts as 499, not an error."""
    live = LiveServer()
    try:
        with httpx.Client(base_url=live.url, headers={"Authorization": f"Bearer {xw.key}"}, timeout=10) as c:
            with c.stream("GET", "/x/web/forever") as resp:
                assert resp.status_code == 200
                assert b"data: 0" in next(resp.iter_raw())
        assert xw.app.gone.wait(10)
        deadline = time.monotonic() + 5
        while (sum(balancer.inflight.values()) or px.in_flight.counts) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sum(balancer.inflight.values()) == 0 and px.in_flight.counts == {}
    finally:
        live.close()
    row = usage_row(xw.owner, "x:web")
    assert row["requests"] == 1 and row["errors"] == 0


def test_fy_x_011_answers_stream_as_raw_bytes(xw):
    """FY-X-011: an SSE event reaches the client before the workload has finished; a gzip body passes
    as it came (still compressed, same length)."""
    live = LiveServer()
    try:
        with httpx.Client(base_url=live.url, headers={"Authorization": f"Bearer {xw.key}"}, timeout=10) as c:
            with c.stream("GET", "/x/web/sse") as resp:
                it = resp.iter_raw()
                assert next(it) == b"data: one\n\n"   # while the workload still waits
                xw.app.release.set()
                assert b"".join(it) == b"data: two\n\n"
            r = c.get("/x/web/gzip", headers={"Accept-Encoding": "gzip"})
            assert r.headers["content-encoding"] == "gzip"
            assert int(r.headers["content-length"]) == len(gzip.compress(b'{"zipped": true}' * 100))
            assert r.content == b'{"zipped": true}' * 100  # decoded by the client, not by the gateway
    finally:
        live.close()


# ------------------------------------------------------------------ usage and limits
def test_fy_x_012_usage_rows_count_requests_bytes_busy_and_latency(xw):
    """FY-X-012: each request is counted under the model x:<name>: requests, errors (an answer's own 5xx
    too), bytes in and out, busy milliseconds and latency."""
    xw.api.post("/x/web/a", content=b"z" * 300)
    xw.api.get("/x/web/status/500")
    xw.app.slow = 0.2
    xw.api.get("/x/web/slow")
    row = usage_row(xw.owner, "x:web")
    assert row["requests"] == 3 and row["errors"] == 1
    assert row["bytes_in"] == 300
    assert row["bytes_out"] == len(b'{"method": "POST", "path": "/a", "len": 300, "auth": null}') + len(b'{"own": true}') \
        + len(b'{"slow": true}')
    assert row["busy_ms"] >= 200 and row["latency_avg_ms"] > 0
    assert row["prompt_tokens"] == row["completion_tokens"] == 0


def test_fy_x_013_requests_per_minute(xw):
    """FY-X-013: the key's rpm applies at /x/ (the 3rd request in a minute with rpm 2 is 429 with
    Retry-After), and the refusal is counted as limited."""
    key = new_key(xw.owner, "rpm", workloads="all", rpm=2)["key"]
    c = api_client(xw.owner, key)
    assert [c.get("/x/web/a").status_code for _ in range(2)] == [200, 200]
    r = c.get("/x/web/a")
    assert r.status_code == 429 and err(r)["code"] == "rate_limit_exceeded" and int(r.headers["retry-after"]) >= 1
    assert usage_row(xw.owner, "x:web")["limited"] == 1


def test_fy_x_014_concurrency_per_replica_and_per_key(xw):
    """FY-X-014: with concurrency 1 a second request while one is in flight is 429 workload_busy; with
    key_concurrency 1 the same key gets 429 key_busy while another key still gets through; both carry
    Retry-After, and the slots are free again afterwards."""
    xw.app.slow = 1.0
    live = LiveServer()
    other = new_key(xw.owner, "other", workloads="all")["key"]
    try:
        def call(key, path="/x/web/slow"):
            return httpx.get(live.url + path, headers={"Authorization": f"Bearer {key}"}, timeout=10)

        set_gateway(xw.owner, "web", expose=True, concurrency=1)
        t = threading.Thread(target=call, args=(xw.key,))
        t.start()
        time.sleep(0.4)
        r = call(other, "/x/web/fast")
        assert r.status_code == 429 and r.json()["error"]["code"] == "workload_busy" and r.headers["retry-after"] == "1"
        t.join()
        assert call(other, "/x/web/fast").status_code == 200
        set_gateway(xw.owner, "web", expose=True, key_concurrency=1)
        t = threading.Thread(target=call, args=(xw.key,))
        t.start()
        time.sleep(0.4)
        r = call(xw.key, "/x/web/fast")
        assert r.status_code == 429 and r.json()["error"]["code"] == "key_busy" and r.headers["retry-after"] == "1"
        assert call(other, "/x/web/fast").status_code == 200
        t.join()
        assert call(xw.key, "/x/web/fast").status_code == 200
    finally:
        live.close()
    assert px.in_flight.counts == {} and sum(balancer.inflight.values()) == 0


# ------------------------------------------------------------------ the key's workloads setting
def test_fy_x_015_a_keys_workloads_setting(xw):
    """FY-X-015: a new key may call no workload (403 workloads_not_allowed); `listed` only the names on
    its list (others, unknown or unexposed ones are 404 workload_not_found); `all` every exposed one.
    The list is checked: names must exist, `listed` needs one, other levels take none."""
    plain = new_key(xw.owner, "plain")
    assert plain["api_key"]["workloads"] == "none" and plain["api_key"]["workload_list"] is None
    r = api_client(xw.owner, plain["key"]).get("/x/web/a")
    assert r.status_code == 403 and err(r)["code"] == "workloads_not_allowed"
    xw.owner.post("/api/workloads", json=service("hidden", machine_id=xw.node))   # not exposed
    listed = new_key(xw.owner, "listed", workloads="listed", workload_list=["hidden"])["key"]
    r = api_client(xw.owner, listed).get("/x/web/a")
    assert r.status_code == 404 and err(r)["code"] == "workload_not_found"
    r = api_client(xw.owner, listed).get("/x/hidden/a")
    assert r.status_code == 404
    kid = new_key(xw.owner, "two", workloads="listed", workload_list=["web", "hidden"])["api_key"]["id"]
    assert xw.owner.get("/api/keys").json()["workloads"] == [
        {"name": "hidden", "exposed": False, "path": "/x/hidden"}, {"name": "web", "exposed": True, "path": "/x/web"}]
    for body, code in (({"workloads": "listed", "workload_list": []}, "no_workloads"),
                       ({"workloads": "listed", "workload_list": ["nope"]}, "unknown_workload"),
                       ({"workloads": "all", "workload_list": ["web"]}, "workload_list_unused")):
        r = xw.owner.patch(f"/api/keys/{kid}", json=body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == code, body
    r = xw.owner.patch(f"/api/keys/{kid}", json={"workload_list": ["web"]})
    assert r.json()["api_key"]["workload_list"] == ["web"]
    assert xw.api.get("/x/unknown-one/a").status_code == 404
    assert xw.api.get("/x/hidden/a").status_code == 404
    r = xw.owner.patch(f"/api/keys/{kid}", json={"workloads": "none"})
    assert r.json()["api_key"]["workloads"] == "none" and r.json()["api_key"]["workload_list"] is None


def test_fy_x_016_existing_keys_get_none_through_the_migration(owner):
    """FY-X-016: keys from before 0013 come through the upgrade with workloads none (and workloads not
    exposed); down drops the columns without touching the rows; up again adds them back."""
    owner.post("/api/keys", json={"name": "old"})
    owner.post("/api/workloads", json=service("old-web"))
    cfg = alembic_config()
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0012_media_usage")
    with get_engine().begin() as conn:
        conn.execute(text("UPDATE api_keys SET name = 'before 0013'"))
    upgrade()
    with get_engine().connect() as conn:
        assert tuple(conn.execute(text("SELECT name, workloads, workload_list, workloads_locked_by FROM api_keys")).one()) \
            == ("before 0013", "none", None, None)
        assert conn.execute(text("SELECT gateway FROM workloads WHERE name = 'old-web'")).scalar() is None
    with get_engine().begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "0012_media_usage")
    with get_engine().connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM api_keys")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM workloads")).scalar() == 1
    upgrade()


def test_fy_x_017_an_admins_workloads_setting_is_locked_for_the_owner(xw):
    """FY-X-017: an admin's workloads setting on someone else's key is locked: the owner cannot change
    it (403 limit_locked) but may send it back unchanged; the admin clearing it (null) unlocks it and
    sets none."""
    add_user(xw.owner, "ana", "user", PW)
    ana = sign_in(xw.owner, "ana", PW)
    k = new_key(ana, "ana-key")
    kid = k["api_key"]["id"]
    assert ana.patch(f"/api/keys/{kid}", json={"workloads": "all"}).json()["api_key"]["workloads"] == "all"
    r = xw.owner.patch(f"/api/keys/{kid}", json={"workloads": "listed", "workload_list": ["web"]})
    assert r.status_code == 200 and r.json()["api_key"]["workloads_locked_by"] == "owner"
    r = ana.patch(f"/api/keys/{kid}", json={"workloads": "all"})
    assert r.status_code == 403 and r.json()["detail"]["code"] == "limit_locked"
    r = ana.patch(f"/api/keys/{kid}", json={"workload_list": ["web"]})
    assert r.status_code == 200
    assert ana.patch(f"/api/keys/{kid}", json={"workload_list": ["web", "web"]}).status_code == 200
    assert api_client(xw.owner, k["key"]).get("/x/web/a").status_code == 200
    r = xw.owner.patch(f"/api/keys/{kid}", json={"workloads": None})
    assert r.json()["api_key"]["workloads"] == "none" and r.json()["api_key"]["workloads_locked_by"] is None
    assert ana.patch(f"/api/keys/{kid}", json={"workloads": "all"}).status_code == 200
    audit = xw.owner.get("/api/audit").json()
    assert any(e["action"] == "api_key.changed" and "workloads" in (e["detail"].get("locked") or [])
               for e in audit["events"])


def test_fy_x_018_only_api_keys_and_no_csrf_but_the_host_check(xw):
    """FY-X-018: the console's cookie opens nothing at /x/ (401), a key in the query is not a key, a
    program needs no CSRF header for a write, and a foreign host name is 421 in the gateway's shape."""
    r = xw.owner.get("/x/web/a")
    assert r.status_code == 401 and err(r)["code"] == "missing_api_key"
    anon = api_client(xw.owner)
    assert anon.get(f"/x/web/a?api_key={xw.key}").status_code == 401
    assert anon.get("/x/web/a", headers={"Authorization": f"Basic {xw.key}"}).status_code == 401
    r = xw.api.post("/x/web/write", content=b"{}", headers={"Origin": "https://evil.example"})
    assert r.status_code == 200 and "access-control-allow-origin" not in r.headers
    r = xw.api.get("/x/web/a", headers={"Host": "evil.example"})
    assert r.status_code == 421 and err(r)["code"] == "host_not_allowed"
    assert xw.app.seen[-1]["path"] == "/write"


# ------------------------------------------------------------------ exposing and the guard
def test_fy_x_019_an_exposed_workload_runs_only_behind_a_guard_that_strips_the_key(owner):
    """FY-X-019: an exposed workload is placed only on an agent with `workload_guard` (elsewhere it waits
    with a reason); its replica gets an internal key, sent with `strip: true` and no variable; a replica
    without a key or on an agent without the feature is never routed to."""
    old = make_node("old", "nvidia-rtx4090x2", features=("workloads", "replica_keys", "replica_guard"))
    r = owner.post("/api/workloads", json={**service("pinned", machine_id=old), "gateway": {"expose": True}})
    assert r.status_code == 422 and "cannot guard a workload exposed at /x/" in r.json()["detail"]["message"]
    r = owner.post("/api/workloads", json={**service("anywhere"), "gateway": {"expose": True}})
    w = r.json()["workload"]
    assert r.status_code == 201 and w["replica_list"][0]["machine"] is None  # it waits for an updated agent
    assert "cannot guard a workload exposed at /x/" in w["replica_list"][0]["reason"]
    new = make_node("new", "nvidia-rtx4090x2", features=GUARDED)
    expose_workload(owner, "web", new)
    db = new_session()
    try:
        rep = db.scalar(select(Replica).join(Workload).where(Workload.name == "web"))
        assert rep.node_id == new and rep.secret and len(rep.secret) >= 40
        msg = wstate.desired_message(db, new, 1)
        entry = next(x for x in msg["replicas"] if x["workload"] == "web")
        assert entry["replica_key"] == {"env": None, "value": rep.secret, "strip": True}
        assert "gateway" not in entry["spec"] and "FORGEYARD" not in json.dumps(entry["spec"]["env"] if "env" in entry["spec"] else {})
        plain = owner.post("/api/workloads", json=service("plain", machine_id=new)).json()["workload"]
        msg = wstate.desired_message(db, new, 2)
        assert "replica_key" not in next(x for x in msg["replicas"] if x["workload"] == "plain")
        assert plain["gateway"]["exposed"] is False
    finally:
        db.close()
    key = new_key(owner, "k", workloads="all")["key"]
    ready_behind("web", [free_port()], secret=None)
    r = api_client(owner, key).get("/x/web/a")
    assert r.status_code == 503 and err(r)["code"] == "no_ready_replica" and "key guard" in err(r)["message"]


def test_fy_x_020_the_guard_strips_the_key_only_for_exposed_workloads():
    """FY-X-020: with strip_auth the guard checks the internal key and removes it; without (a model
    replica) the key goes on as before; a wrong key never reaches the program either way."""
    app = App()
    try:
        for strip, seen in ((True, None), (False, f"Bearer {KEY}")):
            g = GuardThread(app.port, strip_auth=strip)
            try:
                r = httpx.get(f"{g.url}/echo", headers={"Authorization": f"Bearer {KEY}"})
                assert r.json()["auth"] == seen
                assert httpx.get(f"{g.url}/echo", headers={"Authorization": "Bearer wrong"}).status_code == 401
            finally:
                g.close()
        assert len(app.seen) == 2
    finally:
        app.close()


def test_fy_x_021_the_supervisor_keeps_strip_per_replica():
    """FY-X-021: the agent reads `strip` from replica_key (only with no variable), keeps it per replica in
    memory, and forgets it when the key no longer says so."""
    from forgeyard.agent.workloads.supervisor import Supervisor

    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        sup = Supervisor(Path(d), {})
        spec = service("web")
        base = {"workload_id": 1, "workload": "web", "ordinal": 0, "generation": 1, "kind": "service", "spec": spec, "gpus": []}
        sup.set_desired({"revision": 1, "replicas": [{**base, "id": "r1", "replica_key": {"env": None, "value": "k", "strip": True}},
                                                     {**base, "id": "r2", "replica_key": {"env": "VLLM_API_KEY", "value": "k",
                                                                                          "strip": True}},
                                                     {**base, "id": "r3", "replica_key": {"env": None, "value": "k"}}]})
        assert sup.strip == {"r1"} and sup.desired["r1"]["guarded"] is True
        sup.set_desired({"revision": 2, "replicas": [{**base, "id": "r1", "replica_key": {"env": None, "value": "k"}}]})
        assert sup.strip == set()


def test_fy_x_022_exposing_and_its_checks_in_the_workloads_api(xw):
    """FY-X-022: a task, an unknown port or a bad value cannot be exposed (422); people with the user role
    cannot expose; exposing a running workload gives its placed replicas a key (the agent restarts them
    behind the guard) and is audited; turning it off closes /x/ at once and keeps the settings."""
    r = xw.owner.post("/api/workloads", json={**service("t", kind="task", ports=[], restart="never",
                                                        runtime={"type": "process", "command": "true"}),
                                              "gateway": {"expose": True}})
    assert r.status_code == 422 and "only services can be exposed" in r.json()["detail"]["message"]
    for body, code in (
                       ({**service("p"), "gateway": {"expose": True, "port": "admin"}}, "cannot_expose"),
                       ({**service("q"), "gateway": {"expose": True, "read_timeout": 0}}, "invalid_gateway"),
                       ({**service("r"), "gateway": {"expose": True, "surprise": 1}}, "invalid_gateway")):
        r = xw.owner.post("/api/workloads", json=body)
        assert r.status_code == 422 and r.json()["detail"]["code"] == code, (body, r.text)
    w = xw.owner.post("/api/workloads", json=service("later", machine_id=xw.node)).json()["workload"]
    db = new_session()
    assert db.scalar(select(Replica.secret).where(Replica.workload_id == w["id"])) is None
    db.close()
    add_user(xw.owner, "ola", "user", PW)
    ola = sign_in(xw.owner, "ola", PW)
    assert ola.put(f"/api/workloads/{w['id']}/gateway", json={"expose": True}).status_code == 403
    out = set_gateway(xw.owner, "later", expose=True, concurrency=2)
    assert out["gateway"]["exposed"] is True and out["gateway"]["path"] == "/x/later" and out["gateway"]["guarded"] == [0]
    db = new_session()
    assert db.scalar(select(Replica.secret).where(Replica.workload_id == w["id"]))
    db.close()
    out = set_gateway(xw.owner, "web", expose=False, concurrency=3)
    assert out["gateway"]["exposed"] is False and out["gateway"]["concurrency"] == 3
    r = xw.api.get("/x/web/a")
    assert r.status_code == 404
    events = xw.owner.get(f"/api/workloads/{w['id']}").json()["events"]
    assert any(e["kind"] == "exposed" for e in events)
    audit = xw.owner.get("/api/audit").json()["events"]
    assert sum(e["action"] == "workload.gateway_changed" for e in audit) == 2


def test_fy_x_023_a_guard_refusal_is_the_gateways_error_an_apps_own_401_is_passed_on(xw):
    """FY-X-023: the guard refusing the gateway's key (a stale key) is 502 replica_refused_key; the
    workload's own 401 and 5xx are passed on as they are."""
    r = xw.api.get("/x/web/auth401")
    assert r.status_code == 401 and r.json() == {"app": "says no"}
    assert r.headers["www-authenticate"] == 'Bearer realm="app"'
    assert xw.api.get("/x/web/status/503").status_code == 503
    ready_behind("web", [xw.guard.port], secret="stale-key")
    r = xw.api.get("/x/web/a")
    assert r.status_code == 502 and err(r)["code"] == "replica_refused_key"


def test_fy_x_024_a_stopped_or_not_ready_workload(xw):
    """FY-X-024: an exposed workload with no ready replica, or stopped, is 503 no_ready_replica with the
    reason; an unexposed or unknown one 404."""
    db = new_session()
    w = db.scalar(select(Workload).where(Workload.name == "web"))
    w.replicas[0].state = "starting"
    db.commit()
    db.close()
    r = xw.api.get("/x/web/a")
    assert r.status_code == 503 and err(r)["code"] == "no_ready_replica" and "starting" in err(r)["message"]
    wid = next(x["id"] for x in xw.owner.get("/api/workloads").json()["workloads"] if x["name"] == "web")
    xw.owner.post(f"/api/workloads/{wid}/stop")
    r = xw.api.get("/x/web/a")
    assert r.status_code == 503 and "stopped" in err(r)["message"]


def test_fy_x_025_keys_api_reports_and_audits_the_setting(xw):
    """FY-X-025: a key's workloads setting is in its listing and in the audit log when created."""
    k = new_key(xw.owner, "listed-one", workloads="listed", workload_list=["web"])
    assert (k["api_key"]["workloads"], k["api_key"]["workload_list"]) == ("listed", ["web"])
    entry = next(e for e in xw.owner.get("/api/audit").json()["events"] if e["action"] == "api_key.created"
                 and e["target"] == "listed-one")
    assert entry["detail"]["workloads"] == "listed" and entry["detail"]["workload_list"] == ["web"]
    db = new_session()
    try:
        assert db.scalar(select(ApiKey.workloads).where(ApiKey.name == "listed-one")) == "listed"
    finally:
        db.close()


APP_SCRIPT = r'''
import json, os, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
seen = []
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        seen.append(self.headers.get("Authorization"))
        body = json.dumps({"path": self.path, "auth_seen": [a for a in seen if a]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
ThreadingHTTPServer((sys.argv[1], int(os.environ["PORT"])), H).serve_forever()
'''


@pytest.mark.skipif(__import__("os").geteuid() != 0, reason="runs the process as the workload user, which needs a root agent")
def test_fy_x_026_a_real_process_behind_a_stripping_guard(tmp_path):
    """FY-X-026: a real process workload exposed at /x/ (replica_key with no variable and strip): it is
    given ${BIND_ADDRESS} = 127.0.0.1 and listens there only; the guard on the published port answers 401
    without the key and passes requests with it, removing it; neither those requests nor the agent's own
    health checks ever bring the key to the program."""
    import asyncio

    from forgeyard.agent.workloads.process import ProcessRuntime, beyond_loopback
    from forgeyard.agent.workloads.records import RecordStore
    from forgeyard.agent.workloads.supervisor import Supervisor
    from tests.workload_helpers import cleanup_world, converge, desired, replica, world_dir

    work = world_dir()
    try:
        script = work / "app.py"
        script.write_text(APP_SCRIPT)
        script.chmod(0o644)
        spec = {"name": "web", "kind": "service",
                "runtime": {"type": "process", "command": "python3", "args": ["-I", str(script), "${BIND_ADDRESS}"]},
                "ports": [{"name": "http"}],
                "health": {"type": "http", "port": "http", "path": "/health", "interval": 1, "timeout": 2}}
        state = tmp_path / "state"

        async def run():
            rt = ProcessRuntime(RecordStore(state), work / "work", workload_user="nobody", systemd=lambda: False)
            sup = Supervisor(state, {"process": rt}, tick=0.05, port_range=(32140, 32170))
            sup.set_desired(desired({**replica("r1", spec), "replica_key": {"env": None, "value": KEY, "strip": True}}))
            await converge(sup, lambda s: s.records["r1"].state == "healthy" and s.records["r1"].health_failures == 0
                           and s.records["r1"].last_probe_at is not None)
            [p] = sup.records["r1"].ports
            await asyncio.sleep(1.2)  # another health check or two
            out = await asyncio.to_thread(lambda: (
                httpx.get(f"http://127.0.0.1:{p['host_port']}/a", trust_env=False, timeout=5).status_code,
                httpx.get(f"http://127.0.0.1:{p['host_port']}/a", headers={"Authorization": f"Bearer {KEY}"},
                          trust_env=False, timeout=5).json()))
            wide = beyond_loopback([p["internal"]])
            sup.set_desired(desired())
            await converge(sup, lambda s: "r1" not in s.records)
            await sup.close()
            return out, wide

        (refused, answer), wide = asyncio.run(run())
        assert refused == 401
        assert answer == {"path": "/a", "auth_seen": []}
        assert wide == []
    finally:
        cleanup_world(work)


def test_fy_x_027_a_cookie_one_upstream_sets_never_reaches_the_next_request(xw):
    """FY-X-027: the gateway's HTTP clients are shared by every request and keep no cookies: after a
    workload answers with Set-Cookie (dropped for the client), no later request (another key's) brings
    that cookie to it. The providers' client keeps none either."""
    import asyncio

    from forgeyard.gateway import egress, proxy

    assert xw.api.get("/x/web/headers").status_code == 200   # Set-Cookie: fy_session=stolen
    other = api_client(xw.owner, new_key(xw.owner, "other", workloads="all")["key"])
    assert other.get("/x/web/echo").status_code == 200
    assert "cookie" not in xw.app.seen[-1]["headers"]

    async def jars():
        a, b = proxy.client(), egress.client()
        r = httpx.Response(200, headers={"set-cookie": "s=1; Path=/"}, request=httpx.Request("GET", "http://127.0.0.1:9/"))
        a.cookies.extract_cookies(r)
        b.cookies.extract_cookies(r)
        out = (list(a.cookies.jar), list(b.cookies.jar))
        await proxy.close_client()
        await egress.close_client()
        return out

    assert asyncio.run(jars()) == ([], [])
