"""The server relays model files and engine archives to a machine that cannot reach their source, storing
nothing (1.4, issue #35, D1/D3; protocol feature "relay").

The agent calls it outbound, the way it reaches the server for everything else: an HTTPS GET with its
machine credential as a Bearer token, through the CA it pins, so it works through the same tunnel or NAT
that carries its WebSocket (amd-strix's `ssh -R`).

    GET /agent/v1/relay/{replica|download}/{id}/model/{file path}   a Hugging Face model's file
    GET /agent/v1/relay/{replica|download}/{id}/model               a URL model's file
    GET /agent/v1/relay/{replica|download}/{id}/engine              the engine archive of a process variant

**Not an open proxy.** A request is served only when the machine's *current* desired state names it: a
replica placed on that machine and wanted running (`id` = the replica's uid), or a download to that machine
still queued or under way (`id` = the download's uid), whose model block lists that file, or whose engine
is that archive. The source URL is built here from that block (the catalog entry or the recipe): nothing in
the request but the ids and the file path is used, and the file path only to pick one of the block's files.
The machine's `fetch_via` must allow it ("auto" or "server"). Cancelling the download, removing or moving
the replica, revoking the machine or setting it to "direct" closes a relay under way within RECHECK seconds.

**The source fetch** goes through the server's own model-source rules (forgeyard.models.fetch: https and
public addresses only, unless MODEL_ALLOW_PRIVATE_HOSTS; every redirect checked; URL models only from the
allowed hosts) with the server's Hugging Face token, or the deployment's own, for gated repositories: for a
relayed download that token never goes to the agent. `Range` is passed through (`bytes=N-` or `bytes=N-M`),
so the agent's resume from its `.partial` file works as it does directly. The body is streamed in chunks of
at most CHUNK bytes, one at a time (memory per relay is bounded, and a slow agent slows the read from the
source), and kept nowhere. The agent checks every file as it does directly (SHA-256, git blob id, the
pinned archive SHA-256): bytes changed on the way are refused there.

**Limits**: MAX_PER_MACHINE relays at a time per machine and MAX_TOTAL in all; more get 429 with
Retry-After, which the agent waits out. Each relay is logged when it starts and when it ends (bytes, time,
rate, why it ended).
"""

from __future__ import annotations

import asyncio
import http.client
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import quote, urlsplit

import anyio
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard.config import get_settings
from forgeyard.db import ModelDownload, Node, Replica, Workload, new_session
from forgeyard.machines import store
from forgeyard.machines.protocol import FEATURE_RELAY, RELAY_PATH
from forgeyard.models import fetch, reach

router = APIRouter(tags=["agent"])
log = logging.getLogger("forgeyard.relay")

CHUNK = 1024 * 1024
RECHECK = 2.0            # seconds between checks that the relay is still wanted
MAX_PER_MACHINE = 2
MAX_TOTAL = 4
RETRY_AFTER = 5
KINDS = ("replica", "download")
RELAYED_PHASES = ("model", "engine")   # images come with a later change (#35 PR 4)
_RANGE = re.compile(r"^bytes=(\d{1,19})-(\d{1,19})?$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ------------------------------------------------------------------ which machines relay, and how
def can_relay(node: Node) -> bool:
    """The machine may get bytes through the server: its agent speaks "relay" and its setting allows it."""
    return FEATURE_RELAY in (node.features or []) and (node.fetch_via or "auto") != "direct"


def mode(node: Node, host: Optional[str]) -> Optional[str]:
    """How this machine fetches from `host`: None (directly only), "server", or "auto" (directly first,
    through the server after a network failure). "auto" with a network failure to that host remembered in
    the last hour (forgeyard.models.reach) is "server": the direct try would fail again."""
    if not host or not can_relay(node):
        return None
    if (node.fetch_via or "auto") == "server" or reach.failed(node.id, host) is not None:
        return "server"
    return "auto"


def relays(node: Node, phase: str) -> bool:
    """Placement (#71): what this machine fetches in this phase can come through the server, so a network
    failure to its source does not make the machine a bad place for it."""
    return phase in RELAYED_PHASES and can_relay(node)


def attach(item: dict, node: Node, kind: str, uid: str) -> dict:
    """A desired replica or download (`item`, with `model` / `engine` blocks), with `relay` added to each block
    this machine may fetch through the server. Through the server only ("server"), a Hugging Face block loses
    its `token`: the server adds its own when it fetches. Machines without the feature get the item as is."""
    if not can_relay(node):
        return item
    out = dict(item)
    model = item.get("model")
    if isinstance(model, dict) and model.get("kind") in ("hf", "url"):
        m = mode(node, reach.model_host(model))
        if m is not None:
            model = {k: v for k, v in model.items() if not (m == "server" and k == "token")}
            out["model"] = {**model, "relay": {"path": f"{RELAY_PATH}/{kind}/{uid}/model", "mode": m}}
    engine = item.get("engine")
    if isinstance(engine, dict) and engine.get("url"):
        m = mode(node, reach.engine_host(engine))
        if m is not None:
            out["engine"] = {**engine, "relay": {"path": f"{RELAY_PATH}/{kind}/{uid}/engine", "mode": m}}
    return out


# ------------------------------------------------------------------ what a request may fetch
class Refused(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Source:
    url: str
    policy: fetch.Policy
    label: str                     # what the log calls it: "acme/x@1a2b3c4d5e6f/model.gguf"
    headers: dict = field(default_factory=dict)   # the token, never logged


def _wanted_replica(db: DbSession, node: Node, uid: str) -> Optional[Replica]:
    return db.scalar(select(Replica).join(Workload).where(
        Replica.uid == uid, Replica.node_id == node.id, Replica.final.is_(False), Workload.desired_state == "running"))


def _wanted_download(db: DbSession, node: Node, uid: str) -> Optional[ModelDownload]:
    from forgeyard.models.prefetch import ACTIVE

    return db.scalar(select(ModelDownload).where(ModelDownload.uid == uid, ModelDownload.node_id == node.id,
                                                 ModelDownload.state.in_(ACTIVE)))


def _blocks(db: DbSession, node: Node, kind: str, uid: str) -> tuple[Optional[dict], Optional[dict], Optional[str]]:
    """(model block, engine block, the Hugging Face token for this model) of the replica or download, if this
    machine's desired state names it; else Refused."""
    from forgeyard.models import hf_token

    if kind == "replica":
        r = _wanted_replica(db, node, uid)
        if r is None:
            raise Refused(404, "relay_not_wanted", "Nothing this machine should run has that id (any more).")
        model, engine = r.model, r.engine
        token = None
        if model and model.get("kind") == "hf":
            from forgeyard.models.catalog import deployment_of

            dep = deployment_of(db, r.workload_id)
            token = hf_token.choose(dep.hf_token if dep is not None else None, hf_token.get(db))
        return model, engine, token
    row = _wanted_download(db, node, uid)
    if row is None or row.model is None:
        raise Refused(404, "relay_not_wanted", "No download to this machine with that id is under way (any more).")
    from forgeyard.models import prefetch
    from forgeyard.models.catalog import get_recipe, model_block

    model = model_block(row.model)
    engine = prefetch.engine_of(node, get_recipe(db, row.recipe))["engine"] if row.recipe else None
    return model, engine, (hf_token.get(db) if model.get("kind") == "hf" else None)


def resolve(db: DbSession, node: Node, kind: str, uid: str, what: str, path: Optional[str]) -> Source:
    """Where the bytes of this request come from, or Refused. Only blocks from the machine's desired state
    are read; `path` only picks one of the model block's files."""
    from forgeyard.models.catalog import hf_policy, url_policy

    if kind not in KINDS or not _ID.match(uid or ""):
        raise Refused(404, "relay_not_wanted", "Unknown relay path.")
    if not can_relay(node):
        raise Refused(403, "relay_off", f"{node.name} does not download through the server (its setting is "
                                        "'Direct only', or its agent does not relay).")
    model, engine, token = _blocks(db, node, kind, uid)
    if what == "engine":
        if not engine or not engine.get("url"):
            raise Refused(404, "relay_not_wanted", "There is no engine archive to fetch for that.")
        return Source(str(engine["url"]), fetch.Policy(allow_private=get_settings().model_allow_private_hosts),
                      f"engine {engine.get('name') or ''} ({str(engine.get('sha256') or '')[:12]})".strip())
    if not model:
        raise Refused(404, "relay_not_wanted", "There is no model to fetch for that.")
    if model.get("kind") == "hf":
        files = {str(f.get("path")): f for f in model.get("files") or [] if isinstance(f, dict)}
        if path is None or path not in files:
            raise Refused(404, "relay_not_wanted", "That file is not one of the model's files.")
        endpoint = str(model.get("endpoint") or get_settings().hf_endpoint).rstrip("/")
        url = f"{endpoint}/{model['repo']}/resolve/{model['revision']}/{quote(path)}"
        return Source(url, hf_policy(), f"{model['repo']}@{str(model['revision'])[:12]}/{path}",
                      {"Authorization": f"Bearer {token}"} if token else {})
    if model.get("kind") == "url" and path is None and model.get("url"):
        return Source(str(model["url"]), url_policy(db), str(model["url"])[:200])
    raise Refused(404, "relay_not_wanted", "That model cannot be fetched through the server.")


# ------------------------------------------------------------------ limits
class _Slots:
    def __init__(self):
        self._lock = threading.Lock()
        self.by_node: dict[int, int] = {}

    def take(self, node_id: int) -> bool:
        with self._lock:
            if self.by_node.get(node_id, 0) >= MAX_PER_MACHINE or sum(self.by_node.values()) >= MAX_TOTAL:
                return False
            self.by_node[node_id] = self.by_node.get(node_id, 0) + 1
            return True

    def give(self, node_id: int) -> None:
        with self._lock:
            n = self.by_node.get(node_id, 0) - 1
            if n > 0:
                self.by_node[node_id] = n
            else:
                self.by_node.pop(node_id, None)

    def reset(self) -> None:
        with self._lock:
            self.by_node.clear()


slots = _Slots()


def reset() -> None:
    """Forget the relays under way (tests)."""
    slots.reset()


# ------------------------------------------------------------------ the endpoint
def _refuse(status: int, code: str, message: str, headers: Optional[dict] = None) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status, headers=headers)


def _bearer(request: Request) -> Optional[str]:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    return value.strip() if scheme.lower() == "bearer" and value.strip() else None


def _start(credential: Optional[str], kind: str, uid: str, what: str, path: Optional[str]):
    """In a thread: (node id, node name, Source) or Refused."""
    db = new_session()
    try:
        node = store.authenticate(db, credential)
        if node is None or node.credential_hash is None:
            raise Refused(401, "credential_refused", "This machine credential is unknown or was revoked.")
        return node.id, node.name, resolve(db, node, kind, uid, what, path)
    finally:
        db.close()


def _still_wanted(node_id: int, kind: str, uid: str, what: str, path: Optional[str]) -> Optional[str]:
    """In a thread: None while the relay is still wanted, else why not."""
    db = new_session()
    try:
        node = db.get(Node, node_id)
        if node is None or node.revoked_at is not None or node.credential_hash is None:
            return "the machine was revoked"
        try:
            resolve(db, node, kind, uid, what, path)
        except Refused as e:
            return e.message
        return None
    finally:
        db.close()


def _upstream_error(resp: fetch.Response, host: str) -> JSONResponse:
    if resp.status in (401, 403):
        return _refuse(403, "source_refused",
                       f"{host} refused access (HTTP {resp.status}); a gated model needs a Hugging Face token "
                       "(the server's, under Models -> Model sources, or the deployment's own)")
    if resp.status == 416:
        return _refuse(416, "range_not_satisfiable", f"{host}: range not satisfiable",
                       {k: v for k, v in (("Content-Range", resp.headers.get("Content-Range")),) if v})
    return _refuse(502, "source_failed", f"the server's download from {host} failed: HTTP {resp.status}")


async def _relay(request: Request, kind: str, uid: str, what: str, path: Optional[str]):
    if request.headers.get("origin"):
        return _refuse(403, "browser_refused", "This endpoint is for Forgeyard agents, not browsers.")
    rng = request.headers.get("range")
    if rng is not None and not _RANGE.match(rng.strip()):
        return _refuse(416, "bad_range", "Only one range of bytes, bytes=N- or bytes=N-M, can be asked for.")
    try:
        node_id, name, src = await run_in_threadpool(_start, _bearer(request), kind, uid, what, path)
    except Refused as e:
        return _refuse(e.status, e.code, e.message)
    if not slots.take(node_id):
        return _refuse(429, "relay_busy", f"{name} already has {MAX_PER_MACHINE} downloads through the server, or "
                                          f"the server relays {MAX_TOTAL} at a time: try again shortly.",
                       {"Retry-After": str(RETRY_AFTER)})
    host = (urlsplit(src.url).hostname or "?").lower()
    headers = dict(src.headers)
    if rng is not None:
        headers["Range"] = rng.strip()
    try:
        resp = await run_in_threadpool(fetch.open_url, src.url, src.policy, headers=headers)
    except fetch.FetchError as e:
        slots.give(node_id)
        log.warning("Relay of %s to %s: could not open it at %s: %s", src.label, name, host, e)
        status, code = (403, "source_blocked") if isinstance(e, fetch.BlockedAddress) else (502, "source_unreachable")
        return _refuse(status, code, f"the server could not reach {host}: {e}")
    if resp.status >= 400:
        answer = _upstream_error(resp, host)
        await run_in_threadpool(resp.close)
        slots.give(node_id)
        log.warning("Relay of %s to %s: %s answered HTTP %s.", src.label, name, host, resp.status)
        return answer
    out = {"Content-Type": "application/octet-stream", "Cache-Control": "no-store"}
    for k in ("Content-Length", "Content-Range", "Accept-Ranges"):
        if resp.headers.get(k):
            out[k] = resp.headers[k]
    log.info("Relaying %s to %s%s.", src.label, name, f" ({rng.strip()})" if rng else "")
    state = {"sent": 0, "why": "finished", "started": time.monotonic(), "done": False}

    async def finish() -> None:
        """Once, however the response ends (even if it never started): the source closed, the slot given back,
        the log line written."""
        if state["done"]:
            return
        state["done"] = True
        resp.abort()
        await anyio.to_thread.run_sync(resp.close, abandon_on_cancel=True)
        slots.give(node_id)
        took = max(time.monotonic() - state["started"], 1e-6)
        mib = state["sent"] / 1024**2
        log.info("Relayed %s to %s: %.1f MiB in %.1f s (%.1f MiB/s), %s.", src.label, name, mib, took, mib / took,
                 state["why"])

    async def body():
        stop = asyncio.Event()

        async def watch():
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), RECHECK)
                    return
                except asyncio.TimeoutError:
                    pass
                gone = await run_in_threadpool(_still_wanted, node_id, kind, uid, what, path)
                if gone:
                    state["why"] = f"no longer wanted: {gone}"
                    resp.abort()  # a read waiting on the source ends at once
                    return

        watcher = asyncio.create_task(watch())
        try:
            while True:
                chunk = await anyio.to_thread.run_sync(resp.read1, CHUNK, abandon_on_cancel=True)
                if state["why"] != "finished":
                    break
                if not chunk:
                    if resp.remaining():
                        state["why"] = f"the source ended {resp.remaining()} bytes early"
                    break
                yield chunk
                state["sent"] += len(chunk)
        except (OSError, http.client.HTTPException) as e:
            if state["why"] == "finished":
                state["why"] = f"the source broke off ({e or type(e).__name__})"
        except BaseException:
            if state["why"] == "finished":
                state["why"] = "the machine closed the connection"
            raise
        finally:
            stop.set()
            watcher.cancel()

    return _RelayResponse(body(), finish, status_code=206 if resp.status == 206 else 200, headers=out)


class _RelayResponse(StreamingResponse):
    """A streamed answer whose clean-up runs however it ends: also when it never started (the agent gone
    before the first byte), where a generator's own `finally` would never run."""

    def __init__(self, content, on_end, **kw):
        super().__init__(content, **kw)
        self._on_end = on_end

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            with anyio.CancelScope(shield=True):
                await self._on_end()


@router.get(RELAY_PATH + "/{kind}/{uid}/model/{path:path}")
async def relay_model_file(kind: str, uid: str, path: str, request: Request):
    return await _relay(request, kind, uid, "model", path)


@router.get(RELAY_PATH + "/{kind}/{uid}/model")
async def relay_model(kind: str, uid: str, request: Request):
    return await _relay(request, kind, uid, "model", None)


@router.get(RELAY_PATH + "/{kind}/{uid}/engine")
async def relay_engine(kind: str, uid: str, request: Request):
    return await _relay(request, kind, uid, "engine", None)
