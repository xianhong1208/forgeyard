"""An admin uploads an image file to a machine from the console (1.4, issue #35 PR 4, D4; agent feature
"image_load").

The file is what `docker save` (or `podman save`) writes, optionally gzip-compressed. It goes from the browser
through the server to the agent, which loads it into its container engine; the server keeps nothing but a
small buffer (QUEUE_CHUNKS pieces), so a slow agent slows the browser's upload instead of filling memory:

    browser  POST /api/machines/{id}/images        (the file as the body, Content-Length required)
    server   -> agent over its WebSocket: image_load {request_id, path, size, name}
    agent    GET <server>/agent/v1/image-upload/<ticket>  (its machine credential, the CA it pins)
    server   pipes the browser's body into that answer as it arrives
    agent    streams it into `docker load` / `podman load`, inspects what was loaded
    agent    -> server: image_loaded {request_id, ok, reason, images: [{id, tags, content, os, architecture}]}
    server   answers the browser: what was loaded, and which pinned recipe image each one is (by its recorded
             ids, forgeyard/recipes/images.lock.json), or which tag-only recipe image it is named as

The ticket is random, for one machine and one GET; it is gone when the upload ends. One upload at a time per
machine, MAX_TOTAL in all. The size limit is IMAGE_UPLOAD_MAX_GB. Audited as `node.image_uploaded`.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session as DbSession

from forgeyard.db import Node, new_session
from forgeyard.machines import store
from forgeyard.machines.hub import hub
from forgeyard.machines.protocol import FEATURE_IMAGE_LOAD, IMAGE_UPLOAD_PATH, PROTOCOL_VERSION
from forgeyard.workloads import images

router = APIRouter(tags=["agent"])
log = logging.getLogger("forgeyard.upload")

UNIT = 1024**3            # IMAGE_UPLOAD_MAX_GB counts these
QUEUE_CHUNKS = 64         # what the server holds at most: this many pieces (uvicorn's are 64 KiB at most)
MAX_TOTAL = 2
CONNECT_TIMEOUT = 30.0    # the agent must start fetching within this long
STALL = 120.0             # the agent must take the next piece within this long
ANSWER_TIMEOUT = 3600.0   # loading a large image takes minutes
_ABORT = object()


class UploadFailed(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Upload:
    ticket: str
    node_id: int
    node_name: str
    size: int
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_CHUNKS))
    claimed: bool = False
    started: asyncio.Event = field(default_factory=asyncio.Event)
    gone: Optional[str] = None     # why the agent's side ended early
    sent: int = 0


_uploads: dict[str, Upload] = {}


def busy(node_id: int) -> bool:
    return any(u.node_id == node_id for u in _uploads.values())


def reset() -> None:
    """Forget every upload (tests)."""
    _uploads.clear()


def recipe_matches(db: DbSession, item: dict) -> list[dict]:
    """Which recipe images a loaded image is: by its recorded ids (a pinned image: "checked"), else by a tag
    that a tag-only recipe image names ("by tag", not checked)."""
    from forgeyard.models.catalog import all_recipes

    lock = images.load_lock()
    content, image_id = item.get("content"), item.get("id")
    tags = set(item.get("tags") or [])
    out = []
    for name, (recipe, _) in sorted(all_recipes(db).items()):
        for v in recipe.variants:
            if v.runtime != "container" or not v.image:
                continue
            entry = lock.get(v.image) if "@" in v.image else None
            if isinstance(entry, dict):
                check = images.entry_check(entry)
                if (content and content in check["content"]) or (image_id and image_id in check["ids"]):
                    out.append({"recipe": name, "variant": v.name, "image": v.image, "how": "checked"})
            elif v.image in tags:
                out.append({"recipe": name, "variant": v.name, "image": v.image, "how": "tag"})
    return out[:32]


def precheck(node: Node) -> None:
    """Whether this machine can take an upload now (before a byte is read). Raises UploadFailed."""
    conn = hub.get(node.id)
    if conn is None:
        raise UploadFailed(409, "machine_offline", f"{node.name}'s agent is not connected. Try again once it is "
                                                   "online.")
    if FEATURE_IMAGE_LOAD not in conn.features:
        raise UploadFailed(409, "agent_cannot_load", f"{node.name}'s agent cannot load images: it has no container "
                                                     "engine, or it is too old (update the agent).")
    if busy(node.id):
        raise UploadFailed(409, "upload_busy", f"An image is already being uploaded to {node.name}. Wait for it to "
                                               "finish.")
    if len(_uploads) >= MAX_TOTAL:
        raise UploadFailed(429, "upload_busy", f"The server takes {MAX_TOTAL} image uploads at a time. Try again "
                                               "shortly.")


async def receive(request: Request, node: Node, size: int, name: str) -> dict:
    """Pipe the request's body to the machine's agent, and return its answer. Raises UploadFailed."""
    precheck(node)
    conn = hub.get(node.id)
    assert conn is not None
    up = Upload(secrets.token_urlsafe(24), node.id, node.name, size)
    _uploads[up.ticket] = up
    fut = conn.request({"v": PROTOCOL_VERSION, "type": "image_load", "path": f"{IMAGE_UPLOAD_PATH}/{up.ticket}",
                        "size": size, "name": name[:200]})
    started = time.monotonic()
    log.info("Upload of an image file (%s, %.1f MiB) to %s started.", name[:80], size / 1024**2, node.name)
    try:
        try:
            await asyncio.wait_for(up.started.wait(), CONNECT_TIMEOUT)
        except asyncio.TimeoutError:
            raise UploadFailed(504, "agent_no_fetch", f"{node.name}'s agent did not start fetching the file within "
                                                      f"{CONNECT_TIMEOUT:g} s.") from None
        got = 0
        try:
            async for chunk in request.stream():
                if not chunk:
                    continue
                got += len(chunk)
                if got > size:
                    raise UploadFailed(400, "upload_too_long", "The file is longer than its Content-Length.")
                await _put(up, chunk)
        except UploadFailed:
            raise
        except Exception as e:  # the browser went away (ClientDisconnect), or the connection broke
            raise UploadFailed(400, "upload_broken", f"The upload broke off after {got} of {size} bytes "
                                                     f"({type(e).__name__}).") from None
        if got != size:
            raise UploadFailed(400, "upload_short", f"The upload ended after {got} of {size} bytes.")
        await _put(up, None)
        try:
            answer = await asyncio.wait_for(asyncio.wrap_future(fut), ANSWER_TIMEOUT)
        except asyncio.TimeoutError:
            raise UploadFailed(504, "agent_no_answer", f"{node.name}'s agent did not say what it loaded in time. "
                                                       "Look at the machine again later.") from None
        except Exception:
            raise UploadFailed(409, "agent_no_answer", f"{node.name}'s agent went away before it said what it "
                                                       "loaded.") from None
    except UploadFailed as e:
        if up.queue.full():
            try:
                up.queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        up.queue.put_nowait(_ABORT)
        log.warning("Upload of an image file to %s failed: %s", node.name, e.message)
        raise
    finally:
        conn.forget(fut)
        _uploads.pop(up.ticket, None)
    took = max(time.monotonic() - started, 1e-6)
    log.info("Upload of an image file to %s: %.1f MiB in %.1f s, %s.", node.name, size / 1024**2, took,
             "loaded" if answer.get("ok") else f"not loaded: {answer.get('reason')}")
    return answer


async def _put(up: Upload, chunk) -> None:
    """One piece into the buffer, waiting while it is full: as long as the agent takes pieces."""
    waited = 0.0
    while True:
        if up.gone:
            raise UploadFailed(409, "agent_stopped", f"{up.node_name}'s agent stopped reading the file: {up.gone}.")
        try:
            await asyncio.wait_for(up.queue.put(chunk), 1.0)
            return
        except asyncio.TimeoutError:
            waited += 1.0
            if waited >= STALL:
                raise UploadFailed(504, "agent_stalled", f"{up.node_name}'s agent took nothing for {STALL:.0f} s.") \
                    from None


# ------------------------------------------------------------------ the agent's side
def _node_of(credential: Optional[str]) -> Optional[tuple[int, str]]:
    db = new_session()
    try:
        node = store.authenticate(db, credential)
        if node is None or node.credential_hash is None:
            return None
        return node.id, node.name
    finally:
        db.close()


@router.get(IMAGE_UPLOAD_PATH + "/{ticket}")
async def upload_stream(ticket: str, request: Request):
    """The bytes of an upload for this machine, as they arrive from the browser."""
    if request.headers.get("origin"):
        return JSONResponse({"detail": {"code": "browser_refused", "message": "This endpoint is for Forgeyard "
                                        "agents, not browsers."}}, status_code=403)
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    found = await run_in_threadpool(_node_of, value.strip() if scheme.lower() == "bearer" else None)
    if found is None:
        return JSONResponse({"detail": {"code": "credential_refused", "message": "This machine credential is "
                                        "unknown or was revoked."}}, status_code=401)
    up = _uploads.get(ticket)
    if up is None or up.node_id != found[0]:
        return JSONResponse({"detail": {"code": "upload_not_found", "message": "No upload to this machine with "
                                        "that ticket is under way."}}, status_code=404)
    if up.claimed:
        return JSONResponse({"detail": {"code": "upload_claimed", "message": "That upload is already being "
                                        "fetched."}}, status_code=409)
    up.claimed = True
    up.started.set()

    async def body():
        finished = False
        try:
            while True:
                chunk = await up.queue.get()
                if chunk is None:
                    finished = True
                    return
                if chunk is _ABORT:
                    raise ConnectionAbortedError("the upload was abandoned")  # the agent sees a short body
                yield chunk
                up.sent += len(chunk)
        finally:
            if not finished and up.gone is None:
                up.gone = "the connection to the agent ended"

    return StreamingResponse(body(), media_type="application/octet-stream",
                             headers={"Content-Length": str(up.size), "Cache-Control": "no-store"})
