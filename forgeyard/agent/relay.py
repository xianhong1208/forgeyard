"""The agent's side of the server's relay (1.4, issue #35; protocol feature "relay").

A model or engine block from the server may carry `relay: {path, mode}`. The bytes then come from the
agent's **own** server (the URL it joined, never a host the server names), over the same HTTPS and pinned
CA as its WebSocket, with its machine credential: `GET <server><path>[/<file path>]`, sending only `Range`.
No other token goes with it (a Hugging Face token is the server's business there). Nothing else about a
download changes: resume from the `.partial` file, retries, cancel, progress and every checksum are the
model cache's (forgeyard.agent.models), so bytes changed on the way are refused like any others.

- mode "server": always through the server;
- mode "auto": directly first, and through the server after a *network* failure reaching the source (the
  name did not resolve, the connection was refused, reset or timed out, the network was unreachable:
  forgeyard.models.fetch.NETWORK_ERRORS); never after an HTTP error or a checksum mismatch, which the server
  would get too. The host is then remembered for UNREACHABLE_TTL, so the next files and downloads go through
  the server at once;
- no `relay` (a machine set to "direct only", or a server without the feature): directly, as before.

Engine images (#35 PR 4, "relay_images") come the same way, as `image_relay: {path, mode}` beside the spec
(parse_image); agent/imagerelay.py fetches and loads them. An uploaded image file (an admin's, "image_load")
is read from IMAGE_UPLOAD_PATH/<ticket> with the same client. (1.4.1, "relay_source") A cached model file another
machine needs is sent with `post` to RELAY_SEND_PATH/<ticket> (agent/share.py).
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import ssl
import threading
import time
from typing import Optional
from urllib.parse import urlsplit

from forgeyard import __version__
from forgeyard.models import fetch

PATH_RE = re.compile(r"^/agent/v1/relay/(replica|download)/[A-Za-z0-9_-]{1,64}/(model|engine)$")
IMAGE_PATH_RE = re.compile(r"^/agent/v1/relay/(replica|download)/[A-Za-z0-9_-]{1,64}/image$")
UPLOAD_PATH_RE = re.compile(r"^/agent/v1/image-upload/[A-Za-z0-9_-]{16,64}$")
IMAGE_SEND_PATH_RE = re.compile(r"^/agent/v1/image-send/[A-Za-z0-9_-]{16,64}$")
MODES = ("auto", "server")
UNREACHABLE_TTL = 3600.0


class RelayRefused(Exception):
    """The server will not relay this (not wanted any more, not allowed, the source refused access): retrying
    would get the same answer. The message is the server's."""


class SendStopped(Exception):
    """A send this agent stopped itself (cancelled, or the file ended early)."""


class RelayBusy(Exception):
    """The server relays as many downloads as it may right now; try again after `wait` seconds."""

    def __init__(self, message: str, wait: float):
        super().__init__(message)
        self.wait = wait


def parse(block) -> Optional[dict]:
    """The `relay` of a model or engine block, checked: {path, mode}, or None (fetched directly)."""
    r = block.get("relay") if isinstance(block, dict) else None
    if not isinstance(r, dict):
        return None
    path, mode = r.get("path"), r.get("mode")
    if not isinstance(path, str) or not PATH_RE.match(path) or mode not in MODES:
        return None
    return {"path": path, "mode": mode}


def parse_image(item) -> Optional[dict]:
    """The `image_relay` of a desired replica or download, checked: {path, mode}, or None."""
    r = item.get("image_relay") if isinstance(item, dict) else None
    if not isinstance(r, dict):
        return None
    path, mode = r.get("path"), r.get("mode")
    if not isinstance(path, str) or not IMAGE_PATH_RE.match(path) or mode not in MODES:
        return None
    return {"path": path, "mode": mode}


def failure_is_network(e: BaseException) -> bool:
    """A failure to reach the source at all (switch to the server), not an answer from it."""
    if isinstance(e, fetch.BlockedAddress):
        return False
    if isinstance(e, (socket.gaierror, ConnectionRefusedError, ConnectionResetError, TimeoutError)):
        return True
    return fetch.network_error(str(e))


class RelayClient:
    """GET <server><path> with the machine credential, verified with the agent's TLS context."""

    def __init__(self, server: str, ssl_context: Optional[ssl.SSLContext], credential: str, *, timeout: float = 60.0):
        parts = urlsplit(server)
        self.scheme = parts.scheme
        self.host = parts.hostname or ""
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.base = parts.path.rstrip("/")
        self.ssl_context = ssl_context
        self._credential = credential
        self.timeout = timeout
        self._lock = threading.Lock()
        self._unreachable: dict[str, float] = {}   # source host -> when a direct fetch could not reach it

    # ------------------------------------------------------------------ hosts a direct fetch could not reach
    def unreachable(self, host: str) -> bool:
        with self._lock:
            at = self._unreachable.get(host)
            if at is not None and time.monotonic() - at > UNREACHABLE_TTL:
                self._unreachable.pop(host, None)
                return False
            return at is not None

    def mark_unreachable(self, host: str) -> None:
        with self._lock:
            if len(self._unreachable) > 1000:
                self._unreachable.clear()
            self._unreachable[host] = time.monotonic()

    def route(self, relay: Optional[dict], source_url: str) -> str:
        """"server" or "direct" for this fetch, before it starts."""
        if relay is None:
            return "direct"
        if relay["mode"] == "server":
            return "server"
        host = (urlsplit(source_url).hostname or "").lower()
        return "server" if host and self.unreachable(host) else "direct"

    # ------------------------------------------------------------------ the request
    def _connection(self) -> http.client.HTTPConnection:
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout,
                                               context=self.ssl_context or ssl.create_default_context())
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)

    def post(self, path: str, chunks, length: Optional[int]) -> dict:
        """1.4.1 ("relay_source"): POST `length` bytes (from the iterable `chunks`) to <server><path> with the machine
        credential; the server's JSON answer. `length` None ("image_save": a `docker save` stream): chunked, and
        ended cleanly only when `chunks` ends without an error. Raises RelayRefused (the server refused or ended
        the send) or fetch.FetchError (the connection failed); an error from `chunks` ends it (and goes on up)."""
        conn = self._connection()
        try:
            try:
                conn.putrequest("POST", self.base + path, skip_accept_encoding=True)
                size = ("Content-Length", str(length)) if length is not None else ("Transfer-Encoding", "chunked")
                for k, v in (("User-Agent", f"forgeyard-agent/{__version__}"), ("Authorization", f"Bearer {self._credential}"),
                             ("Content-Type", "application/octet-stream"), size):
                    conn.putheader(k, v)
                conn.endheaders()
                for chunk in chunks:
                    if length is not None:
                        conn.send(chunk)
                    elif chunk:
                        conn.send(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                if length is None:
                    conn.send(b"0\r\n\r\n")
                resp = conn.getresponse()
                body = resp.read(16 * 1024)
            except (OSError, http.client.HTTPException) as e:
                raise fetch.FetchError(f"the server ({self.host}): {getattr(e, 'strerror', None) or e or type(e).__name__}") \
                    from None
            try:
                answer = json.loads(body or b"{}")
            except ValueError:
                answer = {}
            if resp.status == 200:
                return answer if isinstance(answer, dict) else {}
            detail = answer.get("detail") if isinstance(answer, dict) else None
            said = detail.get("message") if isinstance(detail, dict) else None
            raise RelayRefused(str(said or "")[:300] or f"HTTP {resp.status}")
        finally:
            conn.close()

    def open(self, path: str, headers: Optional[dict] = None) -> fetch.Response:
        """Open the relay. Raises fetch.FetchError (retry), RelayBusy (wait, then retry) or RelayRefused (stop);
        a 2xx or 416 answer is returned for the caller to read."""
        h = {"User-Agent": f"forgeyard-agent/{__version__}", "Accept-Encoding": "identity",
             **{k: v for k, v in (headers or {}).items() if k.lower() == "range"},
             "Authorization": f"Bearer {self._credential}"}
        conn = self._connection()
        try:
            conn.request("GET", self.base + path, headers=h)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as e:
            conn.close()
            raise fetch.FetchError(f"the server ({self.host}): {getattr(e, 'strerror', None) or e or type(e).__name__}") \
                from None
        out = fetch.Response(conn, resp, self.base + path)
        if resp.status in (200, 206, 416):
            return out
        try:
            detail = json.loads(resp.read(16 * 1024) or b"{}").get("detail") or {}
        except (ValueError, OSError, http.client.HTTPException):
            detail = {}
        finally:
            out.close()
        said = detail.get("message") if isinstance(detail, dict) else None
        message = str(said or "")[:300] or f"HTTP {resp.status}"
        if resp.status == 429:
            try:
                wait = float(resp.headers.get("Retry-After") or 5)
            except ValueError:
                wait = 5.0
            raise RelayBusy(message, max(1.0, min(wait, 30.0)))
        if resp.status in (401, 403, 404):
            raise RelayRefused(message)
        raise fetch.FetchError(message, resp.status)
