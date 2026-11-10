"""Shared pieces for the key guard tests: a stand-in engine that answers every path without asking for
a key (like vLLM's /invocations, /rerank, /pooling) and records what reached it, and a guard running on
its own event loop in a thread, so plain blocking clients can talk to it."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from forgeyard.agent.workloads.guard import Guard
from tests.gateway_helpers import free_port

KEY = "guard-key-0123456789abcdefghijklmnopqrstuvwxyzAB"


class OpenEngine:
    """http://127.0.0.1:<port>: every method on every path answers 200 with what it saw, no key needed.
    /sse sends one event, waits for `release`, then a second one; /forever streams until the client
    goes away (then `gone` is set); /big answers `big` bytes; /slow-upload signals `progress` once
    `threshold` bytes of a body have arrived."""

    def __init__(self, big: int = 20 * 1024 * 1024, threshold: int = 0):
        self.seen: list[dict] = []
        self.big = big
        self.threshold = threshold
        self.progress = threading.Event()
        self.release = threading.Event()
        self.gone = threading.Event()
        self.received = 0
        engine = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _body(self) -> bytes:
                out = bytearray()
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    while True:
                        size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                        if size == 0:
                            self.rfile.readline()
                            break
                        out += self.rfile.read(size)
                        self.rfile.readline()
                        engine._got(len(out))
                    return bytes(out)
                left = int(self.headers.get("Content-Length") or 0)
                while left:
                    piece = self.rfile.read1(min(left, 65536)) if hasattr(self.rfile, "read1") else self.rfile.read(min(left, 65536))
                    if not piece:
                        break
                    out += piece
                    left -= len(piece)
                    engine._got(len(out))
                return bytes(out)

            def _any(self):
                body = self._body()
                entry = {"method": self.command, "path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()},
                         "len": len(body), "sha": hashlib.sha256(body).hexdigest()}
                engine.seen.append(entry)
                if self.path == "/sse":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    self.wfile.write(b"data: one\n\n")
                    self.wfile.flush()
                    engine.release.wait(10)
                    self.wfile.write(b"data: two\n\n")
                    self.wfile.flush()
                    return
                if self.path == "/forever":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    try:
                        for i in range(2000):
                            self.wfile.write(f"data: {i}\n\n".encode())
                            self.wfile.flush()
                            time.sleep(0.02)
                    except OSError:
                        engine.gone.set()
                    return
                if self.path == "/big":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Length", str(engine.big))
                    self.end_headers()
                    block = b"x" * (1024 * 1024)
                    left = engine.big
                    while left:
                        n = min(left, len(block))
                        self.wfile.write(block[:n])
                        left -= n
                    return
                out = json.dumps({k: entry[k] for k in ("method", "path", "len", "sha")} |
                                 {"auth": entry["headers"].get("authorization")}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.send_header("X-Engine", "open")
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(out)

            do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = do_PATCH = do_HEAD = _any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _got(self, n: int) -> None:
        self.received = n
        if self.threshold and n >= self.threshold:
            self.progress.set()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class GuardThread:
    """A Guard on 127.0.0.1:<port> in front of 127.0.0.1:<upstream>, on its own loop in a thread."""

    def __init__(self, upstream: int, key: Optional[str] = KEY, **kw):
        self.loop = asyncio.new_event_loop()
        self.port = free_port()
        self.guard = Guard(listen=("127.0.0.1", self.port), upstream=("127.0.0.1", upstream), key=key, name="test", **kw)
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()
        self.call(self.guard.start())

    def call(self, coro, timeout: float = 10):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self.call(self.guard.close())
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(5)
        self.loop.close()
