"""Stand-ins for cloud providers (1.2): an OpenAI-compatible HTTP server that records what reached it
and answers like OpenAI, or like Anthropic's compatibility endpoint (x-api-key, empty usage details),
plus helpers to register it as a provider and put it behind a route. No real provider is ever called."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from tests.gateway_helpers import new_key

CLOUD_KEY = "sk-test-cloud-0123456789abcdef"
USAGE = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18,
         "prompt_tokens_details": {"cached_tokens": 4}, "completion_tokens_details": {"reasoning_tokens": 2}}
ANTHROPIC_USAGE = {"prompt_tokens": 13, "completion_tokens": 5, "total_tokens": 18,
                   "prompt_tokens_details": {}, "completion_tokens_details": {}}


class FakeCloud:
    """http://127.0.0.1:<port>/v1 answering /models, /chat/completions, /completions and /embeddings.

    `statuses`: answers to give first, one per request (e.g. [429, 200]); after them `status` (None:
    a normal answer). `style`: "openai" or "anthropic" (which auth header it requires, and its usage)."""

    def __init__(self, name: str = "cloud", key: Optional[str] = CLOUD_KEY, *, style: str = "openai",
                 chunks: int = 3, delay: float = 0.0, hold: float = 0.0, tls_dir=None):
        self.name = name
        self.key = key
        self.style = style
        self.chunks = chunks
        self.delay = delay
        self.hold = hold
        self.status: Optional[int] = None
        self.statuses: list[int] = []
        self.headers_out: dict = {}          # extra headers on error answers (Retry-After…)
        self.error_message = "the upstream says no"
        self.redirect_to: Optional[str] = None
        self.models = [{"id": "gpt-mini", "object": "model", "owned_by": "stub"},
                       {"id": "text-embed-small", "object": "model", "owned_by": "stub"}]
        self.seen: list[dict] = []
        self.lock = threading.Lock()
        cloud = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _json(self, status, obj, extra=None):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("x-request-id", f"req_{len(cloud.seen)}")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _authorised(self) -> bool:
                if not cloud.key:
                    return True
                if cloud.style == "anthropic":
                    return self.headers.get("x-api-key") == cloud.key
                return self.headers.get("Authorization") == f"Bearer {cloud.key}"

            def _record(self, raw: bytes) -> dict:
                entry = {"method": self.command, "path": self.path, "headers": dict(self.headers), "raw": raw}
                try:
                    entry["body"] = json.loads(raw) if raw else None
                except ValueError:
                    entry["body"] = None
                with cloud.lock:
                    cloud.seen.append(entry)
                return entry

            def do_GET(self):
                self._record(b"")
                if not self._authorised():
                    return self._json(401, {"error": {"message": "invalid x-api-key", "type": "authentication_error"}})
                if self.path.endswith("/models"):
                    return self._json(200, {"object": "list", "data": cloud.models})
                self._json(404, {"error": {"message": "not found"}})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                entry = self._record(raw)
                if cloud.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", cloud.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not self._authorised():
                    return self._json(401, {"error": {"message": "Incorrect API key provided: sk-te****cdef",
                                                      "type": "invalid_request_error", "code": "invalid_api_key"}})
                with cloud.lock:
                    status = cloud.statuses.pop(0) if cloud.statuses else cloud.status
                if status and status != 200:
                    return self._json(status, {"error": {"message": cloud.error_message, "type": "upstream",
                                                         "code": f"e{status}", "param": "messages"}},
                                      cloud.headers_out)
                body = entry["body"] or {}
                usage = ANTHROPIC_USAGE if cloud.style == "anthropic" else USAGE
                if self.path.endswith("/chat/completions") and body.get("stream"):
                    return self._stream(body, usage)
                if cloud.hold:
                    time.sleep(cloud.hold)
                if self.path.endswith("/chat/completions") or self.path.endswith("/completions"):
                    return self._json(200, {"id": "cmpl-1", "object": "chat.completion", "created": 1,
                                            "model": body.get("model"),
                                            "choices": [{"index": 0, "finish_reason": "stop",
                                                         "message": {"role": "assistant",
                                                                     "content": f"hello from {cloud.name}"}}],
                                            "usage": usage})
                if self.path.endswith("/embeddings"):
                    return self._json(200, {"object": "list", "model": body.get("model"),
                                            "data": [{"object": "embedding", "index": 0, "embedding": [0.5, 0.25]}],
                                            "usage": {"prompt_tokens": 3, "total_tokens": 3}})
                self._json(404, {"error": {"message": "not found"}})

            def _stream(self, body, usage):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                want_usage = (body.get("stream_options") or {}).get("include_usage") is True
                try:
                    for i in range(cloud.chunks):
                        chunk = {"id": "c1", "object": "chat.completion.chunk", "model": body.get("model"),
                                 "choices": [{"index": 0, "delta": {"content": f"c{i} "}, "finish_reason": None}]}
                        self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
                        self.wfile.flush()
                        if cloud.delay:
                            time.sleep(cloud.delay)
                    last = {"id": "c1", "object": "chat.completion.chunk", "model": body.get("model"),
                            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
                    self.wfile.write(b"data: " + json.dumps(last).encode() + b"\n\n")
                    if want_usage:
                        u = {"id": "c1", "object": "chat.completion.chunk", "model": body.get("model"), "choices": [],
                             "usage": usage}
                        self.wfile.write(b"data: " + json.dumps(u).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.scheme = "http"
        if tls_dir is not None:
            # HTTPS with a certificate from a throwaway CA (tls_dir/ca.crt), for 127.0.0.1.
            import ssl

            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(tls_dir / "server.crt"), str(tls_dir / "server.key"))
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
            self.scheme = "https"
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://127.0.0.1:{self.port}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def posts(self, suffix: Optional[str] = None) -> list[dict]:
        with self.lock:
            return [s for s in self.seen if s["method"] == "POST" and (suffix is None or s["path"].endswith(suffix))]


def add_provider(c, cloud: FakeCloud, name: str = "cloudy", kind: str = "openai", models=(("gpt-mini", "openai-chat"),),
                 **extra) -> dict:
    body = {"name": name, "kind": kind, "base_url": cloud.base_url, "api_key": cloud.key, **extra}
    r = c.post("/api/providers", json=body)
    assert r.status_code == 201, r.text
    p = r.json()["provider"]
    if models:
        r = c.post(f"/api/providers/{p['id']}/models",
                   json={"models": [{"upstream_model": m, "api": a} for m, a in models]})
        assert r.status_code == 201, r.text
        p = r.json()["provider"]
    return p


def model_id(provider: dict, upstream: str) -> int:
    return next(m["id"] for m in provider["models"] if m["upstream_model"] == upstream)


def route_id(c, served_name: str) -> int:
    return next(r["id"] for r in c.get("/api/routes").json()["routes"] if r["served_name"] == served_name)


def deployment_id(c, name: str) -> int:
    return next(d["id"] for d in c.get("/api/deployments").json()["deployments"] if d["name"] == name)


def set_tiers(c, served_name: str, tiers: list[list[tuple[str, int]]], status: int = 200, **flags) -> dict:
    """tiers: [[("local", deployment id), …], [("provider", provider model id), …], …]"""
    targets = [{"tier": i, "kind": kind, ("deployment_id" if kind == "local" else "provider_model_id"): ref}
               for i, tier in enumerate(tiers) for kind, ref in tier]
    r = c.put(f"/api/routes/{route_id(c, served_name)}", json={"targets": targets, **flags})
    assert r.status_code == status, r.text
    return r.json()


def cloud_key(c, name: str, cloud: str, **body) -> str:
    return new_key(c, name, cloud=cloud, **body)["key"]
