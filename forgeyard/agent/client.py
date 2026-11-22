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
from forgeyard.agent import replica_tls, trust
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
    FEATURE_GUARD_TLS,
    FEATURE_IMAGE_IDS,
    FEATURE_IMAGE_LOAD,
    FEATURE_IMAGE_REMOVE,
    FEATURE_IMAGE_SAVE,
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
    SERVER_MESSAGES,
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
                 connector: Callable = connect, supervisor=None, ca_pem: Optional[str] = None,
                 install: Optional[dict] = None):
        self.server = server
        # 1.4.2 (#121): how this agent runs (agent/how_it_runs.py), for its update command; sent in hello.
        self.install = install
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
        # 1.4.5 (#163): the binding name and CA the server gave in its welcome, and a wake-up for the certificate loop.
        self._tls_want: Optional[dict] = None
        self._tls_wake = asyncio.Event()
        # 1.4.1 ("relay_source"): sends a cached model file to the server for another machine.
        self._saving = False  # 1.4.1 ("image_save"): an image being sent for another machine
        self.sender = None
        if models is not None and getattr(models, "root", None) is not None:
            from forgeyard.agent.share import Sender

            self.sender = Sender(models, self.relay_client)

    def stop(self) -> None:
        self._stop.set()

    def hello(self, inventory: dict, local: Optional[tuple] = None) -> dict:
        """The first message. `local`: this connection's own (address, port), which (1.4.5, #171) the server
        compares with the peer it sees to tell whether this agent runs on its own host."""
        msg = {"v": PROTOCOL_VERSION, "type": "hello", "agent_version": __version__,
               "hostname": socket.gethostname(), "labels": self.labels, "platform": platform_info(),
               "runtimes": [], "simulated": self.inventory.simulated,
               "inventory": {"accelerators": inventory["accelerators"], "collectors": inventory["collectors"]}}
        if self.install:
            msg["install"] = self.install
        if self.supervisor is not None:
            msg["features"] = [FEATURE_WORKLOADS, FEATURE_REPLICA_KEYS, FEATURE_REPLICA_GUARD, FEATURE_IMAGE_IDS,
                               FEATURE_ENGINE_ZIP, FEATURE_WORKLOAD_GUARD]
            msg["runtimes"] = self.supervisor.available()
            msg["runtime_info"] = self.supervisor.runtime_info()
            tls = self._tls()
            if tls is not None:
                # 1.4.5 (#163): the guards serve TLS to a server that lists it too, and this is what they serve now.
                msg["features"].append(FEATURE_GUARD_TLS)
                if tls.report():
                    msg["runtime_info"]["tls"] = tls.report()
            # 1.4.5 (#171): the address replicas are published on (what the gateway dials), and this connection's
            # own end. Servers before 1.4.5 drop both.
            address = getattr(self.supervisor, "address", None)
            if address:
                msg["runtime_info"]["address"] = address
            if local and len(local) >= 2:
                msg["runtime_info"]["connection"] = {"address": local[0], "port": local[1]}
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
            if getattr(self.supervisor, "can_save_images", lambda: False)():
                msg["features"].append(FEATURE_IMAGE_SAVE)
            if getattr(self.supervisor, "can_remove_images", lambda: False)():
                msg["features"].append(FEATURE_IMAGE_REMOVE)
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
            local = getattr(ws, "local_address", None)
            if self.supervisor is not None:
                # Before the hello, which says where replicas are published (1.4.5, #171).
                self.supervisor.set_address(local[0] if local else None)
            await ws.send(json.dumps(self.hello(inventory, local)))
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
                self.supervisor.new_connection(self.server_features)
                tasks.add(asyncio.create_task(self._status_loop(ws)))
                if self._tls() is not None and FEATURE_GUARD_TLS in self.server_features:
                    want = welcome.get("replica_tls") if isinstance(welcome.get("replica_tls"), dict) else None
                    if want is None:
                        log.error("The server reaches replicas over TLS but sent no certificate details: the key "
                                  "guards refuse every connection until it does. See the server's log.")
                    else:
                        self._tls_want = want
                        tasks.add(asyncio.create_task(self._tls_loop(ws)))
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
        ignored: set[str] = set()  # message types this agent does not know, already warned about
        async for message in ws:
            try:
                data = json.loads(message)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            kind = data.get("type")
            if isinstance(kind, str) and kind not in SERVER_MESSAGES:
                # 1.4.5 (#170): a newer server's message this agent does not know: ignored, with one warning.
                if kind not in ignored and len(ignored) < 16:
                    ignored.add(kind)
                    log.warning("The server sent a message this agent does not know (%r); ignored. A newer "
                                "server? Update the agent.", kind[:40])
                continue
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
            elif kind in ("image_list", "image_save") and self.workloads and FEATURE_IMAGE_SAVE in self.server_features:
                task = asyncio.create_task(self._answer_image_list(ws, data) if kind == "image_list"
                                           else self._answer_image_save(ws, data))
                self._answers.add(task)
                task.add_done_callback(self._answers.discard)
            elif kind == "image_remove" and self.workloads and FEATURE_IMAGE_REMOVE in self.server_features:
                task = asyncio.create_task(self._answer_image_remove(ws, data))
                self._answers.add(task)
                task.add_done_callback(self._answers.discard)
            elif kind == "certificate" and self.workloads and self._tls() is not None:
                self._certificate(data)
            elif kind == "relay_send" and self.workloads and FEATURE_RELAY_SOURCE in self.server_features:
                task = asyncio.create_task(self._answer_relay_send(ws, data))
                self._answers.add(task)
                task.add_done_callback(self._answers.discard)

    # ------------------------------------------------------------------ 1.4.5 (#163): the guards' certificate
    def _tls(self):
        """The guards' TLS (agent/replica_tls.py), or None: no workloads, or no cryptography package."""
        tls = getattr(self.supervisor, "tls", None) if self.supervisor is not None else None
        return tls if tls is not None and tls.usable else None

    async def _tls_loop(self, ws) -> None:
        """Ask for a certificate when there is none for this machine and CA, or when it gets old: now, then every
        hour. A request not answered within a minute is sent again; after a refusal, five minutes later."""
        tls = self._tls()
        while True:
            want = self._tls_want or {}
            reason = tls.wants(want.get("name"), want.get("ca"))
            now = asyncio.get_running_loop().time()
            waiting = tls.requested_at and now - tls.requested_at < replica_tls.ANSWER_WAIT and tls._pending
            resting = tls.refused_at and now - tls.refused_at < replica_tls.RETRY_AFTER
            if reason and not waiting and not resting:
                request_id, csr = await asyncio.to_thread(tls.request)
                tls.requested_at = now
                log.info("Asking the server for the key guards' certificate (%s).", reason)
                await ws.send(json.dumps({"v": PROTOCOL_VERSION, "type": "cert_request", "request_id": request_id,
                                          "csr": csr}))
            self._tls_wake.clear()
            delay = replica_tls.CHECK_EVERY if not reason else replica_tls.ANSWER_WAIT / 4
            try:
                await asyncio.wait_for(self._tls_wake.wait(), delay)
            except asyncio.TimeoutError:
                pass

    def _certificate(self, data: dict) -> None:
        tls = self._tls()
        want = self._tls_want or {}
        if not tls.answers(data.get("request_id")):
            log.info("Ignored a certificate answer to an earlier request.")
            return
        if data.get("ok") is True:
            try:
                info = tls.install(data.get("request_id"), data.get("chain"), name=want.get("name"), ca=want.get("ca"))
            except (replica_tls.CertificateRefused, ValueError, OSError, ssl.SSLError) as e:
                log.error("The server's certificate for the key guards cannot be used: %s.", e)
                tls.refused(data.get("request_id"), asyncio.get_running_loop().time())
            else:
                log.info("The key guards serve TLS with a certificate for %s, valid until %s.", info["name"],
                         info["not_after"][:10])
        else:
            log.error("The server did not sign the key guards' certificate (%s): %s", data.get("code"),
                      data.get("reason"))
            tls.refused(data.get("request_id"), asyncio.get_running_loop().time())
        self._tls_wake.set()

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

    def _image_engine(self):
        rt = self.supervisor.runtimes.get("container") if self.supervisor is not None else None
        return rt if rt is not None and getattr(self.supervisor, "can_save_images", lambda: False)() else None

    async def _answer_image_list(self, ws, data: dict) -> None:
        """1.4.1 ("image_save"): the images in the container engine, for an admin who copies one elsewhere."""
        rt = self._image_engine()
        try:
            if rt is None:
                answer = {"ok": False, "reason": "this agent has no container engine"}
            else:
                answer = {"ok": True, "images": await asyncio.to_thread(rt.list_images)}
                if FEATURE_IMAGE_REMOVE in self.server_features:
                    # 1.4.2: which image the agent runs from (never removed), and the space around the images.
                    answer["agent_image"] = await asyncio.to_thread(rt.agent_image)
                    answer.update(await asyncio.to_thread(rt.disk))
        except Exception as e:  # never take the connection down
            answer = {"ok": False, "reason": (str(e) or type(e).__name__)[:400]}
        await self._send_answer(ws, "image_listed", data, answer)

    async def _answer_image_remove(self, ws, data: dict) -> None:
        """1.4.2 ("image_remove"): remove one of the engine's images by its id, as an admin asked."""
        try:
            answer = await self.supervisor.remove_image(str(data.get("image") or ""))
        except Exception as e:  # never take the connection down
            log.exception("Removing an image failed")
            answer = {"ok": False, "code": "failed", "reason": (str(e) or type(e).__name__)[:400]}
        await self._send_answer(ws, "image_removed", data, answer)

    async def _answer_image_save(self, ws, data: dict) -> None:
        """1.4.1 ("image_save"): `docker save` one of the engine's own images (by its id) to this agent's own server,
        which passes it on to another machine. One at a time."""
        import threading

        from forgeyard.agent import relay as relay_client
        from forgeyard.agent.workloads.container import SaveFailed
        from forgeyard.models import fetch

        rt = self._image_engine()
        path = str(data.get("path") or "")
        cancel = threading.Event()
        sent = {"bytes": 0}
        if rt is None or not relay_client.IMAGE_SEND_PATH_RE.match(path):
            answer = {"ok": False, "reason": "this agent cannot save images, or the path is wrong"}
        elif self._saving:
            answer = {"ok": False, "reason": "this machine is already sending an image"}
        else:
            self._saving = True
            chunks = None
            try:
                entry, chunks = await asyncio.to_thread(rt.save_stream, str(data.get("image") or ""), cancel)

                def counted():
                    for chunk in chunks:
                        sent["bytes"] += len(chunk)
                        yield chunk

                await asyncio.to_thread(self.relay_client.post, path, counted(), None)
                answer = {"ok": True, "bytes": sent["bytes"], "image": entry}
                log.info("Sent image %s (%s) to the server for another machine: %.1f MiB.", entry["id"][:19],
                         ", ".join(entry["tags"]) or "no tag", sent["bytes"] / 1024**2)
            except asyncio.CancelledError:
                cancel.set()
                raise
            except (SaveFailed, relay_client.RelayRefused, fetch.FetchError, OSError) as e:
                log.warning("Sending an image to the server stopped: %s", e)
                answer = {"ok": False, "reason": (str(e) or type(e).__name__)[:400], "bytes": sent["bytes"]}
            except Exception as e:  # never take the connection down
                log.exception("Sending an image failed")
                answer = {"ok": False, "reason": (str(e) or type(e).__name__)[:400], "bytes": sent["bytes"]}
            finally:
                if chunks is not None:
                    await asyncio.to_thread(chunks.close)  # the engine's save is stopped if it still runs
                self._saving = False
        await self._send_answer(ws, "image_saved", data, answer)

    async def _send_answer(self, ws, kind: str, data: dict, answer: dict) -> None:
        try:
            await ws.send(json.dumps({"v": PROTOCOL_VERSION, "type": kind, "request_id": data.get("request_id"),
                                      **answer}))
        except ConnectionClosed:
            pass  # the server went away; it no longer waits for the answer

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
