"""A real Forgeyard server listening with HTTPS (what `python -m forgeyard` runs), for the TLS and
agent pinning tests."""

from __future__ import annotations

import json
import socket
import ssl
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

import uvicorn

from forgeyard.config import get_settings
from forgeyard.machines.hub import hub
from forgeyard.security import tls


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LiveTLS:
    """`python -m forgeyard` in a thread: certificates made or renewed, HTTPS on 127.0.0.1:port."""

    def __init__(self, port: int):
        from forgeyard.app import create_app

        self.port = port
        get_settings.cache_clear()
        self.config = tls.uvicorn_config(create_app(), log_level="warning", lifespan="on")
        self.server = uvicorn.Server(self.config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"https://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        for _ in range(250):
            if self.server.started:
                return self
            time.sleep(0.02)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)
        hub.reset()


def ca_context(ca_pem: str) -> ssl.SSLContext:
    return ssl.create_default_context(cadata=ca_pem)


def peer_cert(port: int, ca_pem: Optional[str] = None) -> dict:
    """The certificate the server presents (parsed by ssl when verified against ca_pem)."""
    ctx = ca_context(ca_pem) if ca_pem else ssl._create_unverified_context()
    with socket.create_connection(("127.0.0.1", port), timeout=5) as raw:
        with ctx.wrap_socket(raw, server_hostname="127.0.0.1") as s:
            return {"der": s.getpeercert(binary_form=True), "parsed": s.getpeercert()}


def https(port: int, method: str, path: str, ca_pem: str, body=None, cookie=None):
    req = urllib.request.Request(f"https://127.0.0.1:{port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-Forgeyard": "1",
                                          **({"Cookie": cookie} if cookie else {})})
    with urllib.request.urlopen(req, timeout=5, context=ca_context(ca_pem)) as r:
        return json.loads(r.read() or b"null"), r.headers


def ca_of(data_dir: Path) -> str:
    return (data_dir / "tls" / "ca.crt").read_text()
