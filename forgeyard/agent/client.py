"""Joining a server and keeping the connection: hello, heartbeats, inventory, reconnects."""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import socket
import ssl
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidStatus, InvalidURI

from forgeyard import __version__
from forgeyard.agent import trust
from forgeyard.agent.backoff import Backoff
from forgeyard.agent.inventory import Inventory, fingerprint
from forgeyard.agent.relay import RelayClient
from forgeyard.machines.protocol import (
    CLOSE_PROTOCOL,
    CLOSE_REPLACED,
    CLOSE_REVOKED,
    FATAL_CLOSE_CODES,
    FEATURE_CACHE_ADMIN,
    FEATURE_ENGINE_ZIP,
    FEATURE_IMAGE_IDS,
    FEATURE_IMAGE_LOAD,
    FEATURE_MODELS,
    FEATURE_PREFETCH,
    FEATURE_RELAY,
    FEATURE_RELAY_IMAGES,
    FEATURE_RELAY_SOURCE,
    FEATURE_REPLICA_GUARD,
    FEATURE_REPLICA_KEYS,
    FEATURE_WORKLOAD_GUARD,
    FEATURE_WORKLOADS,
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


def join(server: str, token: str, *, name: Optional[str] = None, timeout: float = 15.0,
         ssl_context: Optional[ssl.SSLContext] = None) -> dict:
    """Trade a join token for this machine's credential. Over https the server is verified with
    `ssl_context` (the pinned CA, see agent/trust.py), else with the system trust store."""
    body = json.dumps({"join_token": token, "protocol": PROTOCOL_VERSION, "name": name,
                       "hostname": socket.gethostname(), "agent_version": __version__}).encode()
    req = urllib.request.Request(server + JOIN_PATH, data=body, method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json",
                                          "User-Agent": f"forgeyard-agent/{__version__}"})
    if ssl_context is None and server.startswith("https://"):
        ssl_context = trust.context(None)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode()).get("detail") or {}
        except ValueError:
            detail = {}
        message = detail.get("message") if isinstance(detail, dict) else None
        raise JoinError(f"The server refused to join (HTTP {e.code}): {message or e.reason}") from None
    except (urllib.error.URLError, OSError) as e:
        raise JoinError(trust.explain(e, server)) from None


class Agent:
    def __init__(self, server: str, credential: str, inventory: Inventory, *, labels: Optional[dict] = None,
                 backoff: Optional[Backoff] = None, heartbeat: float = 10.0,
                 connector: Callable = connect, supervisor=None, ca_pem: Optional[str] = None):
        self.server = server
        # wss:// is verified against the pinned CA only (agent/trust.py); None: the system store.
        self.ssl_context = trust.context(ca_pem) if server.startswith("https://") else None
        self._https_hint = False
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
        # Runs workloads (M2). None: an inventory-only agent, as in M1.
        self.supervisor = supervisor
        # 1.4 ("relay"): model files, engine archives and images through this server, and image files an admin
        # uploads ("image_load"), with this credential and the same TLS trust as the connection.
        self.relay_client = RelayClient(server, self.ssl_context, credential)
        models = getattr(supervisor, "models", None) if supervisor is not None else None
        if models is not None:
            models.relay_client = self.relay_client
        self.server_features: set[str] = set()
        self._answers: set[asyncio.Task] = set()
        # 1.4.1 ("relay_source"): sends a cached model file to the server for another machine.
        self.sender = None
        if models is not None and getattr(models, "root", None) is not None:
            from forgeyard.agent.share import Sender

            self.sender = Sender(models, self.relay_client)

    def stop(self) -> None:
        self._stop.set()

    def hello(self, inventory: dict) -> dict:
        msg = {"v": PROTOCOL_VERSION, "type": "hello", "agent_version": __version__,
               "hostname": socket.gethostname(), "labels": self.labels, "platform": platform_info(),
               "runtimes": [], "simulated": self.inventory.simulated,
               "inventory": {"accelerators": inventory["accelerators"], "collectors": inventory["collectors"]}}
        if self.supervisor is not None:
            msg["features"] = [FEATURE_WORKLOADS, FEATURE_REPLICA_KEYS, FEATURE_REPLICA_GUARD, FEATURE_IMAGE_IDS,
                               FEATURE_ENGINE_ZIP, FEATURE_WORKLOAD_GUARD]
            msg["runtimes"] = self.supervisor.available()
            msg["runtime_info"] = self.supervisor.runtime_info()
            cache = self.supervisor.model_cache_info()
            if cache is not None:
                msg["features"] += [FEATURE_MODELS, FEATURE_CACHE_ADMIN, FEATURE_PREFETCH]
                if getattr(self.supervisor.models, "relay_client", None) is not None:
                    msg["features"].append(FEATURE_RELAY)
                    if self.sender is not None:
                        msg["features"].append(FEATURE_RELAY_SOURCE)
                    if getattr(self.supervisor, "can_relay_images", lambda: False)():
                        msg["features"].append(FEATURE_RELAY_IMAGES)
                msg["model_cache"] = cache
            if getattr(self.supervisor, "can_load_images", lambda: False)():
                msg["features"].append(FEATURE_IMAGE_LOAD)
        return msg

    @property
    def workloads(self) -> bool:
        return self.supervisor is not None and FEATURE_WORKLOADS in self.server_features

    async def run(self) -> int:
        """Connect, and reconnect after every drop, until stopped or refused for good. Workloads
        are supervised the whole time, connected or not."""
        supervising = asyncio.create_task(self.supervisor.run()) if self.supervisor is not None else None
        try:
            return await self._run()
        finally:
            if supervising is not None:
                supervising.cancel()
                await asyncio.gather(supervising, return_exceptions=True)
                await self.supervisor.close()

    async def _run(self) -> int:
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
            except ssl.SSLCertVerificationError as e:
                # Retried (the server may come back with the right certificate), but said loudly.
                log.error("%s", trust.explain(e, self.server))
            except (OSError, asyncio.TimeoutError, ConnectionClosed, InvalidHandshake, InvalidURI) as e:
                log.warning("Connection to %s lost or refused: %s", self.server, e or type(e).__name__)
                if (self.server.startswith("http://") and not self._https_hint
                        and isinstance(e, (InvalidHandshake, ConnectionClosed, EOFError))):
                    self._https_hint = True  # once per run: it would repeat on every retry
                    log.warning("If the server now uses HTTPS (Forgeyard 1.1 and later do by default), run once: "
                                "forgeyard agent trust --server https://%s --ca-fingerprint <fingerprint> (both are "
                                "on the server's Machines page). The machine keeps its credential.",
                                self.server.removeprefix("http://"))
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
        extra = {"ssl": self.ssl_context} if self.ssl_context is not None else {}
        async with self._connect(url, additional_headers={"Authorization": f"Bearer {self.credential}"}, **extra,
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
            self.server_features = set(welcome.get("features") or [])
            interval = float(welcome.get("heartbeat_interval") or self.heartbeat)
            log.info("Connected to %s as %r (%d accelerators; heartbeat every %.0f s).", self.server, self.name,
                     len(inventory["accelerators"]), interval)
            tasks = {asyncio.create_task(self._send_loop(ws, interval)), asyncio.create_task(self._receive_loop(ws)),
                     asyncio.create_task(self._stop.wait())}
            if self.workloads:
                local = getattr(ws, "local_address", None)
                self.supervisor.set_address(local[0] if local else None)
                self.supervisor.new_connection(self.server_features)
                tasks.add(asyncio.create_task(self._status_loop(ws)))
            elif self.supervisor is not None:
                log.warning("The server does not run workloads (an older Forgeyard); only reporting hardware.")
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
            kind = data.get("type")
            if kind == "error":
                log.warning("Server: %s (%s)", data.get("message"), data.get("code"))
            elif kind == "desired" and self.workloads:
                self.supervisor.set_desired(data)
            elif kind == "logs_request" and self.workloads:
                task = asyncio.create_task(self._answer_logs(ws, data))
                self._answers.add(task)
                task.add_done_callback(self._answers.discard)
            elif kind in ("cache_remove", "cache_prune") and self.workloads:
                task = asyncio.create_task(self._answer_cache(ws, data))
                self._answers.add(task)
                task.add_done_callback(self._answers.discard)
            elif kind == "image_load" and self.workloads:
                task = asyncio.create_task(self._answer_image_load(ws, data))
                self._answers.add(task)
                task.add_done_callback(self._answers.discard)
            elif kind == "relay_send" and self.workloads and FEATURE_RELAY_SOURCE in self.server_features:
                task = asyncio.create_task(self._answer_relay_send(ws, data))
                self._answers.add(task)
                task.add_done_callback(self._answers.discard)

    async def _answer_logs(self, ws, data: dict) -> None:
        replica = str(data.get("replica") or "")
        try:
            answer = await self.supervisor.logs(replica, int(data.get("lines") or 200))
        except Exception as e:  # a log request must never take the connection down
            answer = {"ok": False, "reason": f"could not read the output: {e}"}
        await ws.send(json.dumps({"v": PROTOCOL_VERSION, "type": "logs", "request_id": data.get("request_id"),
                                  "replica": replica, **answer}))

    async def _answer_cache(self, ws, data: dict) -> None:
        """1.4 (cache_admin): remove a model, or the partial downloads, from the model cache."""
        partial = data.get("type") == "cache_prune"
        try:
            answer = await self.supervisor.cache_remove(None if partial else str(data.get("key") or ""),
                                                        partial=partial)
        except Exception as e:  # never take the connection down
            log.exception("Model cache removal failed")
            answer = {"ok": False, "code": "failed", "reason": f"could not remove it: {e}"[:400]}
        await ws.send(json.dumps({"v": PROTOCOL_VERSION, "type": "cache_removed", "request_id": data.get("request_id"),
                                  **answer}))

    async def _answer_image_load(self, ws, data: dict) -> None:
        """1.4 ("image_load"): fetch an uploaded image file from the server and load it into the container engine.
        Only from this agent's own server, by a path of the upload's form."""
        import threading

        from forgeyard.agent import relay as relay_client

        path = str(data.get("path") or "")
        size = data.get("size") if isinstance(data.get("size"), int) and data.get("size") > 0 else None
        answer: dict
        if not relay_client.UPLOAD_PATH_RE.match(path) or not getattr(self.supervisor, "can_load_images", lambda: False)():
            answer = {"ok": False, "reason": "this agent cannot load images (no container engine), or the path is wrong"}
        else:
            cancel = threading.Event()
            try:
                opened = await asyncio.to_thread(self.relay_client.open, path)
                images = await self.supervisor.load_upload(opened, size, cancel)
                answer = {"ok": True, "images": images}
                if not images:
                    answer = {"ok": False, "reason": "the engine loaded nothing from that file"}
            except asyncio.CancelledError:
                cancel.set()
                raise
            except Exception as e:  # never take the connection down
                log.warning("Loading an uploaded image failed: %s", e)
                answer = {"ok": False, "reason": (str(e) or type(e).__name__)[:400]}
        await ws.send(json.dumps({"v": PROTOCOL_VERSION, "type": "image_loaded", "request_id": data.get("request_id"),
                                  **answer}))

    async def _answer_relay_send(self, ws, data: dict) -> None:
        """1.4.1 ("relay_source"): send a cached model file to this agent's own server, for another machine."""
        import threading

        cancel = threading.Event()
        try:
            if self.sender is None:
                answer = {"ok": False, "code": "not_found", "reason": "this agent has no model cache"}
            else:
                answer = await asyncio.to_thread(self.sender.send, data, cancel)
        except asyncio.CancelledError:
            cancel.set()
            raise
        except Exception as e:  # never take the connection down
            log.warning("Sending a cached file failed: %s", e)
            answer = {"ok": False, "code": "failed", "reason": (str(e) or type(e).__name__)[:400]}
        try:
            await ws.send(json.dumps({"v": PROTOCOL_VERSION, "type": "relay_sent", "request_id": data.get("request_id"),
                                      **answer}))
        except ConnectionClosed:
            pass  # the server went away; it no longer waits for the answer

    async def _status_loop(self, ws) -> None:
        sup = self.supervisor
        sup.report_event.set()  # whatever changed while the connection was down
        while True:
            await sup.report_event.wait()
            sup.report_event.clear()
            msg = sup.take_report()
            if msg is not None:
                await ws.send(json.dumps(msg))

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
