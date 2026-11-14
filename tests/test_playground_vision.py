"""The Playground's Vision tab (1.3): the console's routes to exposed vision workloads, which list only
exposed workloads started with a vision task, take the console session and its CSRF rule (never an API
key), reach only the workload's /v1/<task> through its key guard under the /x/ header rules, apply the
workload's body limit and concurrency, and count usage as "console" under x:<name> (FY-PGV-*). /x/ itself
still takes API keys only."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from forgeyard.gateway import passthrough as px, vision
from forgeyard.gateway.balancer import balancer
from forgeyard.workloads import templates
from tests.conftest import OWNER, add_user, sign_in
from tests.gateway_helpers import LiveServer, api_client, new_key
from tests.guard_helpers import GuardThread
from tests.test_passthrough import GUARDED, ready_behind, set_gateway
from tests.workload_helpers import make_node, service

PW = "quiet-meadow-4821"
DETECTED = {"model": "PekingU/rtdetr_v2_r18vd", "width": 640, "height": 480,
            "detections": [{"label": "cat", "score": 0.97, "box": [10.0, 20.0, 300.0, 400.0]}], "ms": 12.5}


class VisionApp:
    """A vision server's HTTP API on 127.0.0.1:<port> (the guard is in front): POST /v1/<task> records what
    reached it and answers like images/vision; `hold` s before answering; `release` lets held ones go."""

    def __init__(self):
        self.seen: list[dict] = []
        self.hold = 0.0
        app = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _any(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                app.seen.append({"method": self.command, "path": self.path, "body": body,
                                 "headers": {k.lower(): v for k, v in self.headers.items()}})
                if app.hold:
                    time.sleep(app.hold)
                out = json.dumps(DETECTED).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.send_header("Set-Cookie", "fy_session=stolen; Path=/")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(out)

            do_GET = do_POST = do_PUT = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def vision_service(name: str, task_args: list[str], node: int) -> dict:
    return service(name, machine_id=node, runtime={"type": "process", "command": "/opt/vision/bin/python",
                                                   "args": ["app.py", *task_args, "--port", "${PORT}"]})


def create(owner, spec: dict, expose: bool = True) -> dict:
    body = {**spec, "gateway": {"expose": True}} if expose else spec
    r = owner.post("/api/workloads", json=body)
    assert r.status_code == 201, r.text
    return r.json()["workload"]


def image_form() -> dict:
    return {"files": {"image": ("cat.png", b"\x89PNG fake image bytes", "image/png")}, "data": {"threshold": "0.1"}}


@pytest.fixture()
def vw(owner):
    node = make_node("cpu-a", "nvidia-rtx4090x2", features=GUARDED)
    app = VisionApp()
    guard = GuardThread(app.port, strip_auth=True)
    create(owner, vision_service("detector", ["--task", "detect"], node))
    ready_behind("detector", [guard.port])
    yield SimpleNamespace(owner=owner, node=node, app=app, guard=guard)
    guard.close()
    app.close()


# ------------------------------------------------------------------ which workloads
def test_fy_pgv_001_options_list_only_exposed_vision_workloads(vw):
    """FY-PGV-001: the options list the exposed services started with a vision task (`--task x` or
    `--task=x`), with the task's title, model and licence, ready replicas or why none, and the body limit;
    not an unexposed vision workload, an exposed workload without a vision task, or an unknown task."""
    o = vw.owner
    create(o, vision_service("segmenter", ["--task=segment"], vw.node))      # exposed, not ready yet
    create(o, vision_service("hidden-depth", ["--task", "depth"], vw.node), expose=False)
    create(o, service("web", machine_id=vw.node))                            # exposed, no vision task
    create(o, vision_service("classify", ["--task", "classify"], vw.node))   # not a vision server task
    set_gateway(o, "segmenter", expose=True, max_body_mb=4)
    r = o.get("/api/playground/vision/options")
    assert r.status_code == 200, r.text
    ws = {w["name"]: w for w in r.json()["workloads"]}
    assert sorted(ws) == ["detector", "segmenter"]
    d = ws["detector"]
    assert (d["task"], d["title"], d["model"], d["licence"], d["endpoint"], d["path"]) == \
        ("detect", "Object detection", "PekingU/rtdetr_v2_r18vd", "apache-2.0", "/v1/detect", "/x/detector")
    assert d["ready"] == 1 and d["reason"] is None and d["max_body_mb"] == 32
    s = ws["segmenter"]
    assert s["task"] == "segment" and s["ready"] == 0 and "no ready replica" in s["reason"] and s["max_body_mb"] == 4
    assert [t["template"] for t in r.json()["tasks"]] == ["object-detection", "segmentation", "depth"]


def test_fy_pgv_002_the_task_a_workload_declares():
    """FY-PGV-002: the task comes from the program's arguments (a container's command too); every vision
    template's specs (CPU and CUDA) declare their own task."""
    def spec(**rt):
        return {"runtime": {"type": "container", "image": "x", **rt}}

    assert vision.declared_task(spec(args=["--task", "depth"])) == "depth"
    assert vision.declared_task(spec(command=["python", "app.py", "--task=segment"])) == "segment"
    assert vision.declared_task(spec(args=["--task"])) is None
    assert vision.declared_task(spec(args=["--task", "classify", "--task", "detect"])) is None  # the first counts
    assert vision.declared_task(spec(args=["--tasks", "detect", "detect"])) is None
    assert vision.declared_task({"runtime": {"type": "process", "command": "--task", "args": ["detect"]}}) is None
    assert vision.declared_task(None) is None
    for t in templates.TEMPLATES:
        for variant in ("cpu", "cuda"):
            assert vision.declared_task(templates.spec(t, gpu=variant == "cuda")) == t.task


# ------------------------------------------------------------------ who may call
def test_fy_pgv_003_the_console_session_and_csrf_only(vw, client):
    """FY-PGV-003: without a session both routes are 401 (an API key does not count); a write without
    the CSRF header or from another origin is 403; a signed-in user (not only admins) may use them; and
    /x/ still refuses the console's cookie."""
    o = vw.owner
    key = new_key(o, "laptop", workloads="all")["key"]
    anon = api_client(o, key)  # no cookie, an API key
    assert anon.get("/api/playground/vision/options").status_code == 401
    r = anon.post("/api/playground/vision/detector/detect", headers={"X-Forgeyard": "1"}, **image_form())
    assert r.status_code == 401 and r.json()["detail"]["code"] == "not_signed_in"
    assert vw.app.seen == []
    r = o.post("/api/playground/vision/detector/detect", headers={"X-Forgeyard": ""}, **image_form())
    assert r.status_code == 403 and r.json()["detail"]["code"] == "csrf"
    r = o.post("/api/playground/vision/detector/detect", headers={"Origin": "https://evil.example"}, **image_form())
    assert r.status_code == 403 and r.json()["detail"]["code"] == "csrf"
    assert vw.app.seen == []
    add_user(o, "alice")
    alice = sign_in(o, "alice", PW)
    assert [w["name"] for w in alice.get("/api/playground/vision/options").json()["workloads"]] == ["detector"]
    r = alice.post("/api/playground/vision/detector/detect", **image_form())
    assert r.status_code == 200 and r.json()["detections"][0]["label"] == "cat"
    # /x/ takes API keys only: the console's session cookie gets 401 there and never reaches the workload.
    seen = len(vw.app.seen)
    r = o.post("/x/detector/v1/detect", **image_form())
    assert r.status_code == 401 and len(vw.app.seen) == seen


# ------------------------------------------------------------------ what reaches the workload
def test_fy_pgv_004_forwarded_to_the_tasks_path_through_the_guard_under_the_x_rules(vw):
    """FY-PGV-004: the image reaches the workload as POST /v1/detect (no query) through its key guard,
    which removed the internal key; the console's cookie and CSRF header never reach it; the /x/ answer
    headers apply (the app's Set-Cookie and CORS dropped, the sandbox forced, who answered)."""
    o = vw.owner
    r = o.post("/api/playground/vision/detector/detect?evil=1", **image_form())
    assert r.status_code == 200, r.text
    assert r.json() == DETECTED
    seen = vw.app.seen[-1]
    assert (seen["method"], seen["path"]) == ("POST", "/v1/detect")
    assert seen["headers"]["content-type"].startswith("multipart/form-data")
    assert b'name="image"' in seen["body"] and b"fake image bytes" in seen["body"] and b'name="threshold"' in seen["body"]
    for h in ("cookie", "authorization", "x-forgeyard"):
        assert h not in seen["headers"], h
    assert seen["headers"]["x-forwarded-prefix"] == "/x/detector"
    assert vw.guard.guard.forwarded == 1 and vw.guard.guard.refused == 0
    assert "set-cookie" not in r.headers and "access-control-allow-origin" not in r.headers
    assert r.headers["content-security-policy"].startswith("sandbox") and r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-forgeyard-target"] == "workload:detector#0" and len(r.headers["x-request-id"]) == 32


def test_fy_pgv_005_refused_workloads_and_tasks_never_reach_anything(vw):
    """FY-PGV-005: an unexposed vision workload, an exposed workload without a vision task, another task
    than the workload's, an unknown task or a bad name is 404 in the gateway's error shape, and nothing
    is sent anywhere."""
    o = vw.owner
    create(o, vision_service("hidden-depth", ["--task", "depth"], vw.node), expose=False)
    create(o, service("web", machine_id=vw.node))
    ready_behind("hidden-depth", [vw.guard.port])
    ready_behind("web", [vw.guard.port])
    for path, code in (("hidden-depth/depth", "workload_not_found"), ("web/detect", "workload_not_found"),
                       ("detector/depth", "workload_not_found"), ("detector/classify", "unknown_task"),
                       ("Detector/detect", "workload_not_found"), ("nobody/detect", "workload_not_found")):
        r = o.post(f"/api/playground/vision/{path}", **image_form())
        assert r.status_code == 404, (path, r.text)
        assert r.json()["error"]["code"] == code, path
    assert vw.app.seen == [] and vw.guard.guard.forwarded == 0
    set_gateway(o, "detector", expose=False)
    r = o.post("/api/playground/vision/detector/detect", **image_form())
    assert r.status_code == 404 and vw.app.seen == []
    assert o.get("/api/playground/vision/options").json()["workloads"] == []


def test_fy_pgv_006_no_ready_replica(vw):
    """FY-PGV-006: an exposed vision workload without a ready replica is 503 no_ready_replica, with why."""
    o = vw.owner
    create(o, vision_service("depth", ["--task", "depth"], vw.node))
    r = o.post("/api/playground/vision/depth/depth", **image_form())
    assert r.status_code == 503 and r.json()["error"]["code"] == "no_ready_replica"
    assert "depth" in r.json()["error"]["message"]


# ------------------------------------------------------------------ limits and usage
def test_fy_pgv_007_the_workloads_body_limit(vw):
    """FY-PGV-007: an image above the workload's max_body_mb is 413 request_too_large and never reaches it."""
    o = vw.owner
    set_gateway(o, "detector", expose=True, max_body_mb=1)
    big = {"files": {"image": ("big.png", b"x" * (1024**2 + 10), "image/png")}}
    r = o.post("/api/playground/vision/detector/detect", **big)
    assert r.status_code == 413 and r.json()["error"]["code"] == "request_too_large"
    assert vw.app.seen == []


def test_fy_pgv_008_concurrency_per_replica_and_per_person(vw):
    """FY-PGV-008: with concurrency 1 a second image while one is in flight is 429 workload_busy; with
    key_concurrency 1 the same person gets 429 key_busy while another person still gets through; the
    slots are free afterwards."""
    o = vw.owner
    add_user(o, "alice")
    vw.app.hold = 1.0
    live = LiveServer()
    try:
        def browser(username, password):
            c = httpx.Client(base_url=live.url, headers={"X-Forgeyard": "1"}, timeout=10)
            assert c.post("/api/auth/login", json={"username": username, "password": password}).status_code == 200
            return c

        me, alice = browser(*OWNER), browser("alice", PW)

        def call(c):
            return c.post("/api/playground/vision/detector/detect", **image_form())

        set_gateway(o, "detector", expose=True, concurrency=1)
        t = threading.Thread(target=call, args=(me,))
        t.start()
        time.sleep(0.4)
        r = call(alice)
        assert r.status_code == 429 and r.json()["error"]["code"] == "workload_busy" and r.headers["retry-after"] == "1"
        t.join()
        set_gateway(o, "detector", expose=True, key_concurrency=1)
        t = threading.Thread(target=call, args=(me,))
        t.start()
        time.sleep(0.4)
        r = call(me)
        assert r.status_code == 429 and r.json()["error"]["code"] == "key_busy"
        assert r.json()["error"]["message"].startswith("You already have 1 request in flight")
        assert call(alice).status_code == 200
        t.join()
        assert call(me).status_code == 200
        me.close()
        alice.close()
    finally:
        live.close()
    assert px.in_flight.counts == {} and sum(balancer.inflight.values()) == 0


def test_fy_pgv_009_usage_counts_as_console_under_the_workload(vw):
    """FY-PGV-009: Playground calls are counted under x:<name> as "console" (no key), for the person who
    made them: requests, errors, refusals as limited, bytes in and out."""
    o = vw.owner
    add_user(o, "alice")
    alice = sign_in(o, "alice", PW)
    form = image_form()
    r = alice.post("/api/playground/vision/detector/detect", **form)
    assert r.status_code == 200
    sent = len(vw.app.seen[-1]["body"])
    set_gateway(o, "detector", expose=True, max_body_mb=1)
    alice.post("/api/playground/vision/detector/detect", files={"image": ("b.png", b"x" * (1024**2 + 10), "image/png")})
    from forgeyard.gateway.usage import recorder

    recorder.flush()
    row = next(r for r in alice.get("/api/usage").json()["rows"] if r["id"] == "x:detector")  # alice's own usage
    assert row["requests"] == 2 and row["errors"] == 1
    assert row["bytes_in"] == sent and row["bytes_out"] == len(json.dumps(DETECTED).encode())
    rows = o.get("/api/usage?group=source&everyone=true").json()["rows"]
    assert [(x["id"], x["requests"]) for x in rows] == [("console", 2)]
    by_user = {x["name"]: x["requests"] for x in o.get("/api/usage?group=user&everyone=true").json()["rows"]}
    assert by_user == {"alice": 2}
    assert not any(r["id"] == "x:detector" for r in o.get("/api/usage").json()["rows"])  # not the owner's
