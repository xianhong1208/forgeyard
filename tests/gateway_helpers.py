"""Shared pieces for the gateway tests: a fake OpenAI-compatible engine that checks its internal key
and records what reached it, deployments whose replicas point at such engines, API keys, and a real
uvicorn server for what TestClient cannot show (a client that goes away mid-stream)."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from fastapi.testclient import TestClient
from sqlalchemy import select

from forgeyard.db import Replica, Workload, new_session
from forgeyard.gateway.balancer import balancer
from forgeyard.gateway.limits import key_limiter
from forgeyard.gateway.usage import recorder

USAGE = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}


class FakeEngine:
    """http://127.0.0.1:<port>, answering like vLLM / llama-server / speaches. With `secret`, every
    /v1 path needs `Authorization: Bearer <secret>` (401 otherwise), like those engines with their
    API key set."""

    def __init__(self, name: str, secret: Optional[str] = "engine-secret", *, chunks: int = 3, delay: float = 0.0,
                 hold: float = 0.0, usage: Optional[dict] = None):
        self.name = name
        self.secret = secret
        self.chunks = chunks
        self.delay = delay          # between streamed chunks
        self.hold = hold            # before a non-streamed answer
        self.usage = usage or USAGE
        self.status: Optional[int] = None   # answer every request with this status instead
        self.retry_after: Optional[str] = None  # sent with `status`
        self.cut_after: Optional[int] = None  # a stream breaks off after this many chunks (1.2)
        self.seen: list[dict] = []
        self.refused = 0
        self.disconnects = 0
        self.finished_streams = 0
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, status, obj, headers=None):
                body = json.dumps(obj).encode()
                self.send_response(status)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    return self._json(200, {})
                self._json(404, {"error": "not found"})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                auth = self.headers.get("Authorization")
                entry = {"path": self.path, "auth": auth, "headers": dict(self.headers), "raw": raw}
                try:
                    entry["body"] = json.loads(raw)
                except ValueError:
                    entry["body"] = None
                engine.seen.append(entry)
                if engine.secret and auth != f"Bearer {engine.secret}":
                    engine.refused += 1
                    return self._json(401, {"error": "Unauthorized"})
                if engine.status:
                    return self._json(engine.status, {"error": {"message": "engine says no", "type": "BadRequestError",
                                                                "code": engine.status}},
                                      {"Retry-After": engine.retry_after} if engine.retry_after else None)
                body = entry["body"] or {}
                if self.path == "/v1/chat/completions" and body.get("stream"):
                    return self._stream(body)
                if engine.hold:
                    time.sleep(engine.hold)
                if self.path in ("/v1/chat/completions", "/v1/completions"):
                    return self._json(200, {"id": "c1", "object": "chat.completion", "created": 1, "model": body.get("model"),
                                            "choices": [{"index": 0, "finish_reason": "stop",
                                                         "message": {"role": "assistant",
                                                                     "content": f"hello from {engine.name}"}}],
                                            "usage": engine.usage})
                if self.path == "/v1/embeddings":
                    return self._json(200, {"object": "list", "model": body.get("model"),
                                            "data": [{"object": "embedding", "index": 0, "embedding": [0.1, 0.2, 0.3]}],
                                            "usage": {"prompt_tokens": 4, "total_tokens": 4}})
                if self.path in ("/v1/rerank", "/v1/score"):
                    return self._json(200, {"id": "r1", "model": body.get("model"), "usage": {"total_tokens": 12},
                                            "results": [{"index": 0, "relevance_score": 0.98},
                                                        {"index": 1, "relevance_score": 0.1}]})
                if self.path == "/v1/audio/transcriptions":
                    return self._json(200, {"text": "And so my fellow Americans, ask not."})
                self._json(404, {"error": "not found"})

            def _stream(self, body):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                if engine.cut_after is not None:
                    # Promise more than is sent, then hang up: the client sees a broken answer.
                    self.send_header("Content-Length", "1000000")
                self.end_headers()
                usage_asked = (body.get("stream_options") or {}).get("include_usage") is True
                try:
                    for i in range(engine.chunks):
                        if engine.cut_after is not None and i == engine.cut_after:
                            self.wfile.flush()
                            self.close_connection = True
                            return
                        chunk = {"id": "c1", "object": "chat.completion.chunk", "model": body.get("model"),
                                 "choices": [{"index": 0, "delta": {"content": f"t{i} "}, "finish_reason": None}]}
                        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                        self.wfile.flush()
                        if engine.delay:
                            time.sleep(engine.delay)
                    last = {"id": "c1", "object": "chat.completion.chunk", "model": body.get("model"),
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                    self.wfile.write(b"data: " + json.dumps(last).encode() + b"\n\n")
                    if usage_asked:
                        u = {"id": "c1", "object": "chat.completion.chunk", "model": body.get("model"), "choices": [],
                             "usage": engine.usage}
                        self.wfile.write(b"data: " + json.dumps(u).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    engine.finished_streams += 1
                except (BrokenPipeError, ConnectionResetError):
                    engine.disconnects += 1

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def posts(self, path: Optional[str] = None) -> list[dict]:
        return [s for s in self.seen if path is None or s["path"] == path]


def free_port() -> int:
    """A port nothing listens on (bound, then closed)."""
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_ready(workload_name: str, targets: list[tuple[int, int, Optional[str]]]) -> list[str]:
    """Replicas 0..n-1 of a deployment: on machine `node_id`, healthy, answering on 127.0.0.1:port,
    with the internal key `secret`. Returns their uids."""
    db = new_session()
    try:
        w = db.scalar(select(Workload).where(Workload.name == workload_name))
        reps = sorted(w.replicas, key=lambda r: r.ordinal)
        assert len(reps) == len(targets), (len(reps), len(targets))
        for r, (node_id, port, secret) in zip(reps, targets):
            r.node_id = node_id
            r.state = "healthy"
            r.reason = None
            r.final = False
            r.ports = [{"name": "http", "port": 8080, "host_port": port, "address": "127.0.0.1"}]
            r.secret = secret
        db.commit()
        return [r.uid for r in reps]
    finally:
        db.close()


def set_state(workload_name: str, ordinal: int, state: str, reason: Optional[str] = None) -> None:
    db = new_session()
    try:
        r = db.scalar(select(Replica).join(Workload).where(Workload.name == workload_name, Replica.ordinal == ordinal))
        r.state, r.reason = state, reason
        db.commit()
    finally:
        db.close()


def new_key(c: TestClient, name: str = "laptop", status: int = 201, **body) -> dict:
    r = c.post("/api/keys", json={"name": name, **body})
    assert r.status_code == status, r.text
    return r.json()


def api_client(app_client: TestClient, key: Optional[str] = None) -> TestClient:
    """A program calling /v1: no cookies, no console headers."""
    c = TestClient(app_client.app, base_url="http://localhost")
    # The same event loop as the signed-in client (without running the app's startup again).
    c.portal = getattr(app_client, "portal", None)
    if key:
        c.headers["Authorization"] = f"Bearer {key}"
    return c


def reset_gateway() -> None:
    balancer.reset()
    key_limiter.reset()
    recorder.reset()


class LiveServer:
    """The app on a real uvicorn server in a thread (port chosen by the OS)."""

    def __init__(self):
        import uvicorn

        from forgeyard.app import create_app

        self.config = uvicorn.Config(create_app(), host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("uvicorn did not start")
            time.sleep(0.05)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.should_exit = True
        self.thread.join(timeout=20)
