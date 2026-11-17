"""The server relays model files and engine archives to a machine that cannot reach their source, storing
nothing (1.4, issue #35, D1/D3; protocol feature "relay").

The agent calls it outbound, the way it reaches the server for everything else: an HTTPS GET with its
machine credential as a Bearer token, through the CA it pins, so it works through the same tunnel or NAT
that carries its WebSocket (amd-strix's `ssh -R`).

    GET /agent/v1/relay/{replica|download}/{id}/model/{file path}   a Hugging Face model's file
    GET /agent/v1/relay/{replica|download}/{id}/model               a URL model's file
    GET /agent/v1/relay/{replica|download}/{id}/engine              the engine archive of a process variant
    GET /agent/v1/relay/{replica|download}/{id}/image/manifest      a pinned engine image's manifest for the
                                                                    machine's platform (1.4 PR 4, D4)
    GET /agent/v1/relay/{replica|download}/{id}/image/blobs/{digest} its config or one of its layers

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

**Engine images** (#35 PR 4, D4; agent feature "relay_images"): only an image pinned by digest whose ids
Forgeyard recorded (forgeyard/recipes/images.lock.json) comes through the server. The lock is the source of
truth: the manifest served is the one recorded for the machine's platform (its `platform.arch`), fetched by
that digest and checked against it; a blob is served only when that manifest names it (its config or a
layer), by digest, from the registry (forgeyard.workloads.registry: an anonymous pull token, the same egress
rules). A whole blob is hashed on its way through, and its last piece is held back until the hash matches,
so a registry or CDN that sends other bytes never gets a complete answer to the agent; the agent checks
every blob's digest again, the layers' diff ids are checked by `docker load`, and the loaded image by its
recorded ids. A tag-only image (a custom recipe's, Forgeyard's own vision images, which carry this
release's tag) is refused with a word to upload it to the machine instead (forgeyard/machines/upload.py).

**Limits**: MAX_PER_MACHINE relays at a time per machine and MAX_TOTAL in all; more get 429 with
Retry-After, which the agent waits out. Each relay is logged when it starts and when it ends (bytes, time,
rate, why it ended).
"""

from __future__ import annotations

import asyncio
import hashlib
import http.client
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional
from urllib.parse import quote, urlsplit

import anyio
from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard.config import get_settings
from forgeyard.db import ModelDownload, Node, Replica, Workload, new_session
from forgeyard.machines import peers, store
from forgeyard.machines.protocol import FEATURE_RELAY, FEATURE_RELAY_IMAGES, RELAY_PATH
from forgeyard.models import fetch, reach
from forgeyard.workloads import images, registry as oci

router = APIRouter(tags=["agent"])
log = logging.getLogger("forgeyard.relay")

CHUNK = 1024 * 1024
RECHECK = 2.0            # seconds between checks that the relay is still wanted
MAX_PER_MACHINE = 2
MAX_TOTAL = 4
RETRY_AFTER = 5
KINDS = ("replica", "download")
RELAYED_PHASES = ("model", "engine")   # always; "image" too for agents that list "relay_images" (#35 PR 4)
_RANGE = re.compile(r"^bytes=(\d{1,19})-(\d{1,19})?$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
# What `platform.machine()` says (the agent's inventory, `nodes.platform.arch`) -> the OCI architecture.
ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "arm",
        "s390x": "s390x", "ppc64le": "ppc64le", "riscv64": "riscv64"}


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


def can_relay_images(node: Node) -> bool:
    return can_relay(node) and FEATURE_RELAY_IMAGES in (node.features or [])


def relayable_image(image: Optional[str]) -> bool:
    """An image the server can relay: pinned by digest, with its ids recorded (images.lock.json)."""
    return bool(image) and "@" in str(image) and isinstance(images.load_lock().get(str(image)), dict)


def relays(node: Node, phase: str, image: Optional[str] = None) -> bool:
    """Placement (#71): what this machine fetches in this phase can come through the server, so a network
    failure to its source does not make the machine a bad place for it. An image only when it is pinned and
    recorded, and the machine's agent relays images."""
    if phase == "image":
        return can_relay_images(node) and relayable_image(image)
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
    # 1.4 PR 4: a pinned engine image, beside the spec (never in it: an agent validates it strictly).
    image = item_image(item)
    if image and can_relay_images(node) and relayable_image(image):
        m = mode(node, reach.registry(image))
        if m is not None:
            out["image_relay"] = {"path": f"{RELAY_PATH}/{kind}/{uid}/image", "mode": m}
    return out


def item_image(item: dict) -> Optional[str]:
    """The engine image of a desired download (`image`) or replica (its container spec's `runtime.image`)."""
    if isinstance(item.get("image"), str):
        return item["image"]
    runtime = ((item.get("spec") or {}).get("runtime") or {}) if isinstance(item.get("spec"), dict) else {}
    image = runtime.get("image") if isinstance(runtime, dict) else None
    return image if isinstance(image, str) and image else None


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
    digest: Optional[str] = None   # an image blob: what its bytes must hash to ("sha256:…")
    size: Optional[int] = None     # and how many there are (the manifest says)
    # An image blob: opens it from the registry with its pull token (renewed once on a 401), given the Range.
    opener: Optional[Callable[[dict], fetch.Response]] = None
    # 1.4.1 (#104): a model file that may come from another machine's cache, and the machines that have it.
    copy: Optional["peers.FileCopy"] = None


# ------------------------------------------------------------------ engine images (#35 PR 4)
@dataclass
class ImageTarget:
    image: str                     # as the recipe pins it: repo:tag@sha256:<index or manifest>
    registry: str
    repo: str
    platform: str                  # the lock's key: "linux/amd64"
    manifest: str                  # the recorded manifest digest of that platform
    config: str                    # the recorded config digest


_registry: Optional[oci.Registry] = None
_manifests: "dict[str, tuple[bytes, str, dict]]" = {}   # manifest digest -> (bytes, media type, parsed)
_manifests_lock = threading.Lock()
MANIFESTS_KEPT = 64


def registry_client() -> oci.Registry:
    global _registry
    if _registry is None:
        _registry = oci.Registry(lambda: fetch.Policy(allow_private=get_settings().model_allow_private_hosts))
    return _registry


def platform_of(node: Node, entry: dict) -> Optional[str]:
    """The lock's platform key for this machine's architecture (linux/amd64, linux/arm64/v8), or None."""
    arch = ARCH.get(str((node.platform or {}).get("arch") or "").lower())
    if arch is None:
        return None
    keys = sorted((entry.get("platforms") or {}).keys())
    for key in keys:
        if key == f"linux/{arch}":
            return key
    for key in keys:
        if key.startswith(f"linux/{arch}/"):
            return key
    return None


def image_target(node: Node, image: Optional[str]) -> ImageTarget:
    """What the server relays for this machine's engine image, from the recorded ids only; else Refused."""
    if not image:
        raise Refused(404, "relay_not_wanted", "There is no engine image to fetch for that.")
    entry = images.load_lock().get(image) if "@" in image else None
    if not isinstance(entry, dict):
        raise Refused(409, "relay_image_not_pinned",
                      f"{oci.tag_ref(image)} is not an image Forgeyard pinned by digest, so it cannot come through "
                      "the server: upload the image file to the machine instead (its page: Upload an image), or "
                      "docker load it there.")
    platform = platform_of(node, entry)
    if platform is None:
        have = ", ".join(sorted((entry.get("platforms") or {}).keys())) or "none"
        arch = (node.platform or {}).get("arch") or "unknown"
        raise Refused(409, "relay_no_platform", f"{oci.tag_ref(image)} has no build for {node.name}'s architecture "
                                                f"({arch}); it has: {have}.")
    p = entry["platforms"][platform]
    registry, repo, _, _ = oci.split_image(image)
    return ImageTarget(image, registry, repo, platform, p["manifest"], p["config"])


def manifest_of(target: ImageTarget) -> tuple[bytes, str, dict]:
    """The recorded manifest of the target's platform: (bytes, media type, parsed), fetched by its digest (the
    bytes hash to it) and kept in memory. A manifest whose config is not the recorded one is refused."""
    with _manifests_lock:
        held = _manifests.get(target.manifest)
    if held is not None:
        return held
    try:
        data, media = registry_client().manifest(target.registry, target.repo, target.manifest)
    except fetch.BlockedAddress as e:
        raise Refused(403, "source_blocked", f"the server may not fetch from {target.registry}: {e}") from None
    except fetch.FetchError as e:
        raise Refused(502, "source_failed", f"the server could not get the image's manifest: {e}") from None
    try:
        parsed = oci.parse_manifest(data)
    except ValueError as e:
        raise Refused(502, "source_failed", f"{target.registry}: the recorded manifest is not one image's: {e}") \
            from None
    if parsed["config"]["digest"] != target.config:
        raise Refused(502, "source_failed", f"{target.registry}: the manifest names another config than the "
                                            "recorded one")
    with _manifests_lock:
        if len(_manifests) >= MANIFESTS_KEPT:
            _manifests.clear()
        _manifests[target.manifest] = (data, media, parsed)
    return data, media, parsed


def _image_source(db: DbSession, node: Node, kind: str, uid: str, digest: str, *, check_only: bool) -> Source:
    if not can_relay_images(node):
        raise Refused(403, "relay_off", f"{node.name}'s agent does not fetch engine images through the server "
                                        "(update the agent).")
    target = image_target(node, _blocks(db, node, kind, uid).image)
    _, _, parsed = manifest_of(target)
    blobs = {parsed["config"]["digest"]: parsed["config"]["size"]}
    blobs.update({d["digest"]: d["size"] for d in parsed["layers"]})
    if digest not in blobs:
        raise Refused(404, "relay_not_in_image", "That digest is not the config or a layer of the image.")
    reg = registry_client()
    what = "config" if digest == target.config else "layer"
    return Source(reg.blob_url(target.registry, target.repo, digest), reg.policy(),
                  f"{oci.tag_ref(target.image)} ({target.platform}) {what} {digest[7:19]}",
                  digest=digest, size=blobs[digest],
                  opener=lambda h: reg.open_blob(target.registry, target.repo, digest, headers=h))


def _wanted_replica(db: DbSession, node: Node, uid: str) -> Optional[Replica]:
    return db.scalar(select(Replica).join(Workload).where(
        Replica.uid == uid, Replica.node_id == node.id, Replica.final.is_(False), Workload.desired_state == "running"))


def _wanted_download(db: DbSession, node: Node, uid: str) -> Optional[ModelDownload]:
    from forgeyard.models.prefetch import ACTIVE

    return db.scalar(select(ModelDownload).where(ModelDownload.uid == uid, ModelDownload.node_id == node.id,
                                                 ModelDownload.state.in_(ACTIVE)))


@dataclass
class Wanted:
    """What one desired replica or download of this machine fetches."""
    model: Optional[dict]
    engine: Optional[dict]
    image: Optional[str]


def _blocks(db: DbSession, node: Node, kind: str, uid: str) -> Wanted:
    """The model block, engine block and engine image of the replica or download, if this machine's desired
    state names it; else Refused."""
    if kind == "replica":
        r = _wanted_replica(db, node, uid)
        if r is None:
            raise Refused(404, "relay_not_wanted", "Nothing this machine should run has that id (any more).")
        spec = r.spec or r.workload.spec or {}
        runtime = spec.get("runtime") or {}
        image = runtime.get("image") if runtime.get("type") == "container" else None
        return Wanted(r.model, r.engine, image)
    row = _wanted_download(db, node, uid)
    if row is None or row.model is None:
        raise Refused(404, "relay_not_wanted", "No download to this machine with that id is under way (any more).")
    from forgeyard.models import prefetch
    from forgeyard.models.catalog import get_recipe, model_block

    eng = prefetch.engine_of(node, get_recipe(db, row.recipe)) if row.recipe else {}
    return Wanted(model_block(row.model), eng.get("engine"), eng.get("image"))


def _token(db: DbSession, kind: str, uid: str, node: Node) -> Optional[str]:
    """The Hugging Face token for a relayed model file: the deployment's own, else the server's."""
    from forgeyard.models import hf_token

    if kind == "replica":
        from forgeyard.models.catalog import deployment_of

        r = _wanted_replica(db, node, uid)
        dep = deployment_of(db, r.workload_id) if r is not None else None
        return hf_token.choose(dep.hf_token if dep is not None else None, hf_token.get(db))
    return hf_token.get(db)


def resolve(db: DbSession, node: Node, kind: str, uid: str, what: str, path: Optional[str], *,
            check_only: bool = False) -> Source:
    """Where the bytes of this request come from, or Refused. Only blocks from the machine's desired state
    are read; `path` only picks one of the model block's files, or (an image) one of the digests its manifest
    names. `check_only`: only whether it is still wanted (no token is fetched)."""
    from forgeyard.models.catalog import hf_policy, url_policy

    if kind not in KINDS or not _ID.match(uid or ""):
        raise Refused(404, "relay_not_wanted", "Unknown relay path.")
    if not can_relay(node):
        raise Refused(403, "relay_off", f"{node.name} does not download through the server (its setting is "
                                        "'Direct only', or its agent does not relay).")
    if what == "image":
        return _image_source(db, node, kind, uid, path or "", check_only=check_only)
    wanted = _blocks(db, node, kind, uid)
    model, engine = wanted.model, wanted.engine
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
        token = None if check_only else _token(db, kind, uid, node)
        return Source(url, hf_policy(), f"{model['repo']}@{str(model['revision'])[:12]}/{path}",
                      {"Authorization": f"Bearer {token}"} if token else {},
                      copy=None if check_only else peers.file_copy(db, node, model, path))
    if model.get("kind") == "url" and path is None and model.get("url"):
        return Source(str(model["url"]), url_policy(db), str(model["url"])[:200],
                      copy=None if check_only else peers.file_copy(db, node, model, None))
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
    """Forget the relays under way, the manifests and the registry tokens, and (1.4.1) the machine copies' marks
    (tests)."""
    global _registry
    slots.reset()
    peers.reset()
    with _manifests_lock:
        _manifests.clear()
    _registry = None


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
            resolve(db, node, kind, uid, what, path, check_only=True)
        except Refused as e:
            # A registry that fails to answer for a moment (the manifest fetched again) does not end a relay.
            return None if e.status >= 500 else e.message
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


class _Internet:
    """The bytes from their source on the internet (or a registry), read in a thread."""

    def __init__(self, resp: fetch.Response):
        self._resp = resp
        self.status = resp.status
        self.headers = resp.headers

    async def read(self) -> bytes:
        return await anyio.to_thread.run_sync(self._resp.read1, CHUNK, abandon_on_cancel=True)

    def remaining(self) -> Optional[int]:
        return self._resp.remaining()

    def abort(self) -> None:
        self._resp.abort()

    async def close(self) -> None:
        await anyio.to_thread.run_sync(self._resp.close, abandon_on_cancel=True)


def _span(rng: Optional[str], size: int) -> Optional[tuple[int, int]]:
    """(offset, length) of the bytes a Range asks for in a file of `size` bytes; None when it asks for none."""
    if rng is None:
        return 0, size
    m = _RANGE.match(rng)
    if m is None:
        return None
    first = int(m.group(1))
    last = min(int(m.group(2)), size - 1) if m.group(2) is not None else size - 1
    if first >= size or last < first:
        return None
    return first, last - first + 1


def _hasher(algo: str, size: int):
    """What a whole file is checked with: SHA-256, or the git blob id (SHA-1 of a "blob <size>" header and the
    bytes)."""
    if algo == "oid":
        return hashlib.sha1(f"blob {size}\0".encode())
    return hashlib.sha256()


async def _relay(request: Request, kind: str, uid: str, what: str, path: Optional[str]):
    if request.headers.get("origin"):
        return _refuse(403, "browser_refused", "This endpoint is for Forgeyard agents, not browsers.")
    rng = request.headers.get("range")
    if rng is not None and not _RANGE.match(rng.strip()):
        return _refuse(416, "bad_range", "Only one range of bytes, bytes=N- or bytes=N-M, can be asked for.")
    rng = rng.strip() if rng is not None else None
    try:
        node_id, name, src = await run_in_threadpool(_start, _bearer(request), kind, uid, what, path)
    except Refused as e:
        return _refuse(e.status, e.code, e.message)
    if not slots.take(node_id):
        return _refuse(429, "relay_busy", f"{name} already has {MAX_PER_MACHINE} downloads through the server, or "
                                          f"the server relays {MAX_TOTAL} at a time: try again shortly.",
                       {"Retry-After": str(RETRY_AFTER)})
    # 1.4.1 (#104): a model file comes from another machine that has it, when one can send it; else from the
    # internet as before. A copy refused for this machine's file before: its partial file holds those bytes, so
    # this answer is the whole file (the agent then starts it over).
    copy = src.copy
    if copy is not None and rng is not None and peers.restart_due(node_id, kind, uid, path):
        log.info("Relay of %s to %s: sending the whole file again (a copy of it was refused).", src.label, name)
        rng = None
    up = None
    if copy is not None:
        span = _span(rng, copy.size)
        if span is not None and copy.candidates:
            up = await peers.open_copy(name, copy, *span)
        peers.note_source(kind, uid, up.source if up is not None else None)
    if up is not None:
        host = up.source[1]
        label = f"{src.label} from {host}"
        verify = (copy.algo, copy.expect, copy.size)
    else:
        host = (urlsplit(src.url).hostname or "?").lower()
        label = src.label
        verify = ("sha256", src.digest[7:], src.size) if src.digest else None
        headers = dict(src.headers)
        if rng is not None:
            headers["Range"] = rng
        try:
            if src.opener is not None:
                resp = await run_in_threadpool(src.opener, headers)
            else:
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
        up = _Internet(resp)
    out = {"Content-Type": "application/octet-stream", "Cache-Control": "no-store"}
    for k in ("Content-Length", "Content-Range", "Accept-Ranges"):
        if up.headers.get(k):
            out[k] = up.headers[k]
    log.info("Relaying %s to %s%s.", label, name, f" ({rng})" if rng else "")
    state = {"sent": 0, "why": "finished", "started": time.monotonic(), "done": False}
    # A whole file with a digest (an image blob; 1.4.1, a model file from a machine) is hashed on its way
    # through; its last piece is sent only once the hash matches.
    hasher = _hasher(verify[0], verify[2] or 0) if verify and rng is None and up.status == 200 else None
    expect = verify[1] if verify else None
    size = verify[2] if verify else None

    async def finish() -> None:
        """Once, however the response ends (even if it never started): the source closed, the slot given back,
        the log line written."""
        if state["done"]:
            return
        state["done"] = True
        up.abort()
        await up.close()
        slots.give(node_id)
        took = max(time.monotonic() - state["started"], 1e-6)
        mib = state["sent"] / 1024**2
        log.info("Relayed %s to %s: %.1f MiB in %.1f s (%.1f MiB/s), %s.", label, name, mib, took, mib / took,
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
                    up.abort()  # a read waiting on the source ends at once
                    return

        watcher = asyncio.create_task(watch())
        held = b""
        try:
            while True:
                chunk = await up.read()
                if state["why"] != "finished":
                    break
                if not chunk:
                    if up.remaining():
                        state["why"] = f"the source ended {up.remaining()} bytes early"
                    elif hasher is not None:
                        got = hasher.hexdigest()
                        if got != expect:
                            state["why"] = f"the source's bytes hash to {got[:12]}…, not {expect[:12]}…: not sent"
                            log.warning("Relay of %s to %s: the bytes from %s do not match their digest.", label,
                                        name, host)
                            if copy is not None and up.status == 200 and isinstance(up, peers.PipeSource):
                                await run_in_threadpool(peers.refused, node_id, name, kind, uid, path, copy, up.source,
                                                        got)
                        elif held:
                            yield held
                            state["sent"] += len(held)
                    break
                if hasher is not None:
                    hasher.update(chunk)
                    if size is not None and state["sent"] + len(held) + len(chunk) > size:
                        state["why"] = f"the source sent more than the {size} bytes the file has"
                        break
                    chunk, held = held, chunk
                    if not chunk:
                        continue
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

    return _RelayResponse(body(), finish, status_code=206 if up.status == 206 else 200, headers=out)


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


def _start_manifest(credential: Optional[str], kind: str, uid: str):
    """In a thread: (node name, target, manifest bytes, media type) or Refused."""
    db = new_session()
    try:
        node = store.authenticate(db, credential)
        if node is None or node.credential_hash is None:
            raise Refused(401, "credential_refused", "This machine credential is unknown or was revoked.")
        if kind not in KINDS or not _ID.match(uid or ""):
            raise Refused(404, "relay_not_wanted", "Unknown relay path.")
        if not can_relay(node) or not can_relay_images(node):
            raise Refused(403, "relay_off", f"{node.name} does not fetch engine images through the server (its "
                                            "setting is 'Direct only', or its agent does not relay images).")
        target = image_target(node, _blocks(db, node, kind, uid).image)
        data, media, _ = manifest_of(target)
        return node.name, target, data, media
    finally:
        db.close()


@router.get(RELAY_PATH + "/{kind}/{uid}/image/manifest")
async def relay_image_manifest(kind: str, uid: str, request: Request):
    """The recorded manifest of the machine's platform, as the registry serves it (the agent checks that its
    bytes hash to a recorded manifest digest)."""
    if request.headers.get("origin"):
        return _refuse(403, "browser_refused", "This endpoint is for Forgeyard agents, not browsers.")
    try:
        name, target, data, media = await run_in_threadpool(_start_manifest, _bearer(request), kind, uid)
    except Refused as e:
        return _refuse(e.status, e.code, e.message)
    log.info("Relayed the manifest of %s (%s) to %s.", oci.tag_ref(target.image), target.platform, name)
    return Response(data, media_type=media or oci.MANIFEST_TYPES[1],
                    headers={"Docker-Content-Digest": target.manifest, "Forgeyard-Platform": target.platform,
                             "Cache-Control": "no-store"})


@router.get(RELAY_PATH + "/{kind}/{uid}/image/blobs/{digest}")
async def relay_image_blob(kind: str, uid: str, digest: str, request: Request):
    """The image's config or one of its layers, by digest (only those its manifest names)."""
    return await _relay(request, kind, uid, "image", digest)
