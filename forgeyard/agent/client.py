"""Joining a server and keeping the connection: hello, heartbeats, inventory, reconnects."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import socket
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, InvalidURI

from forgeyard import __version__
from forgeyard.agent.backoff import Backoff
from forgeyard.agent.inventory import Inventory, fingerprint
from forgeyard.machines.protocol import (
    CLOSE_PROTOCOL,
    CLOSE_REPLACED,
    CLOSE_REVOKED,
    FATAL_CLOSE_CODES,
    JOIN_PATH,
    PROTOCOL_VERSION,
    STREAM_PATH,
)

log = logging.getLogger("forgeyard.agent")

# A full inventory is re-read every this many heartbeats (60 s) and sent when it changed.
INVENTORY_EVERY = 6
WELCOME_TIMEOUT = 15.0

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3      # credential refused, revoked or removed
EXIT_JOIN_FAILED = 4
EXIT_INCOMPATIBLE = 5  # protocol mismatch, host not allowed, or a second agent with the same credential


class AgentStop(Exception):
    """The agent must stop instead of reconnecting."""

    def __init__(self, message: str, exit_code: int):
        super().__init__(message)
        self.exit_code = exit_code


class JoinError(Exception):
    pass


def normalise_server(url: str) -> str:
    url = url.strip().rstrip("/")
    if "://" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(f"--server must be an http:// or https:// address, got {url!r}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def stream_url(server: str) -> str:
    parts = urlsplit(server)
    scheme = "wss" if parts.scheme == "https" else "ws"
    return urlunsplit((scheme, parts.netloc, parts.path + STREAM_PATH, "", ""))


def platform_info() -> dict:
    info = {"os": platform.system(), "kernel": platform.release(), "arch": platform.machine(),
            "python": platform.python_version()}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                info["distro"] = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return info


def join(server: str, token: str, *, name: Optional[str] = None, timeout: float = 15.0) -> dict:
    """Trade a join token for this machine's credential."""
    body = json.dumps({"join_token": token, "protocol": PROTOCOL_VERSION, "name": name,
                       "hostname": socket.gethostname(), "agent_version": __version__}).encode()
    req = urllib.request.Request(server + JOIN_PATH, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": f"forgeyard-agent/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail") or {}
        except ValueError:
            detail = {}
        message = detail.get("message") if isinstance(detail, dict) else None
        raise JoinError(f"The server refused to join (HTTP {e.code}): {message or e.reason}") from None
    except (urllib.error.URLError, OSError) as e:
        raise JoinError(f"Cannot reach {server}: {getattr(e, 'reason', e)}") from None


class Agent:
    def __init__(self, server: str, credential: str, inventory: Inventory, *, labels: Optional[dict] = None,
                 backoff: Optional[Backoff] = None, heartbeat: float = 10.0,
                 connector: Callable = connect):
        self.server = server
        self.credential = credential
        self.inventory = inventory
        self.labels = labels or {}
        self.backoff = backoff or Backoff()
        self.heartbeat = heartbeat
        self._connect = connector
        self._stop = asyncio.Event()
        self._fingerprint: Optional[list] = None
        self.name: Optional[str] = None
        self.sessions = 0  # connections that got a welcome (tests and logs)

    def stop(self) -> None:
        self._stop.set()

    def hello(self, inventory: dict) -> dict:
        return {"v": PROTOCOL_VERSION, "type": "hello", "agent_version": __version__,
                "hostname": socket.gethostname(), "labels": self.labels, "platform": platform_info(),
                "runtimes": [], "simulated": self.inventory.simulated,
                "inventory": {"accelerators": inventory["accelerators"], "collectors": inventory["collectors"]}}

    async def run(self) -> int:
        """Connect, and reconnect after every drop, until stopped or refused for good."""
        url = stream_url(self.server)
        while not self._stop.is_set():
            try:
                await self._session(url)
            except AgentStop as e:
                log.error("%s", e)
                return e.exit_code
            except InvalidStatus as e:
                status = e.response.status_code
                if status == 401:
                    log.error("The server refused this machine's credential (revoked or removed). "
                              "Join again with a new token.")
                    return EXIT_REFUSED
                if status == 421:
                    log.error("The server does not answer to the host name in --server. Add it to the server's "
                              "ALLOWED_HOSTS (or use its IP address).")
                    return EXIT_INCOMPATIBLE
                if status == 403:
                    log.error("The server refused the connection (HTTP 403).")
                    return EXIT_REFUSED
                log.warning("Connection refused with HTTP %s; retrying.", status)
            except (OSError, asyncio.TimeoutError, ConnectionClosed, InvalidHandshake, InvalidURI) as e:
                log.warning("Connection to %s lost or refused: %s", self.server, e or type(e).__name__)
            if self._stop.is_set():
                break
            delay = self.backoff.next()
            log.info("Reconnecting in %.1f s.", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass
        return EXIT_OK

    async def _session(self, url: str) -> None:
        async with self._connect(url, additional_headers={"Authorization": f"Bearer {self.credential}"},
                                 user_agent_header=f"forgeyard-agent/{__version__}", open_timeout=10,
                                 ping_interval=10, ping_timeout=20, close_timeout=5, max_size=2**20) as ws:
            inventory = await asyncio.to_thread(self.inventory.full)
            self._fingerprint = fingerprint(inventory)
            await ws.send(json.dumps(self.hello(inventory)))
            welcome = json.loads(await asyncio.wait_for(ws.recv(), WELCOME_TIMEOUT))
            if welcome.get("type") != "welcome":
                raise ConnectionError(f"expected welcome, got {welcome.get('type')}: {welcome.get('message')}")
            self.backoff.reset()
            self.sessions += 1
            self.name = welcome.get("name")
            interval = float(welcome.get("heartbeat_interval") or self.heartbeat)
            log.info("Connected to %s as %r (%d accelerators; heartbeat every %.0f s).", self.server, self.name,
                     len(inventory["accelerators"]), interval)
            tasks = {asyncio.create_task(self._send_loop(ws, interval)), asyncio.create_task(self._receive_loop(ws)),
                     asyncio.create_task(self._stop.wait())}
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
            if self._stop.is_set():
                await ws.close(1000, "agent stopping")
                return
            for t in done:
                if not t.cancelled() and t.exception() is not None:
                    exc = t.exception()
                    if not isinstance(exc, ConnectionClosed):
                        raise exc
        self._check_close(ws.close_code, ws.close_reason)

    @staticmethod
    def _check_close(code: Optional[int], reason: Optional[str]) -> None:
        if code in FATAL_CLOSE_CODES:
            text = {CLOSE_REVOKED: "This machine was revoked or removed on the server. Join again with a new token.",
                    CLOSE_PROTOCOL: "The server speaks a different agent protocol. Use the agent from the same "
                                    "Forgeyard version as the server.",
                    CLOSE_REPLACED: "Another agent connected with this machine's credential (a copied state "
                                    "directory?). Stopping this one."}[code]
            raise AgentStop(text, EXIT_REFUSED if code == CLOSE_REVOKED else EXIT_INCOMPATIBLE)
        raise ConnectionError(f"closed by the server ({code} {reason or ''})".strip())

    async def _receive_loop(self, ws) -> None:
        async for message in ws:
            try:
                data = json.loads(message)
            except ValueError:
                continue
            if data.get("type") == "error":
                log.warning("Server: %s (%s)", data.get("message"), data.get("code"))

    async def _send_loop(self, ws, interval: float) -> None:
        seq = 0
        while True:
            await asyncio.sleep(interval)
            seq += 1
            metrics = await asyncio.to_thread(self.inventory.metrics)
            await ws.send(json.dumps({"v": PROTOCOL_VERSION, "type": "heartbeat", "seq": seq, "metrics": metrics}))
            if seq % INVENTORY_EVERY == 0:
                inventory = await asyncio.to_thread(self.inventory.full)
                fp = fingerprint(inventory)
                if fp != self._fingerprint:
                    self._fingerprint = fp
                    log.info("Hardware changed; sending the full inventory.")
                    await ws.send(json.dumps({"v": PROTOCOL_VERSION, "type": "inventory", "inventory": {
                        "accelerators": inventory["accelerators"], "collectors": inventory["collectors"]}}))
