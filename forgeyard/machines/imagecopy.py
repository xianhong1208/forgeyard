"""An admin copies an engine image from one machine to another, through the server (1.4.1, issue #104, Q4;
agent feature "image_save" on the source, "image_load" on the target).

    console  GET  /api/machines/{source}/images               the images in the source's engine (image_list)
    console  POST /api/machines/{target}/images/copy {source_id, image}
    server   -> source over its WebSocket: image_save {request_id, path, image}
    source   checks `image` is the id of one of its own images, then
             `docker save` / `podman save` (by its own tags) | POST <its server>/agent/v1/image-send/<ticket>, chunked
    server   pipes that body into the target's image upload (forgeyard/machines/upload.py: image_load, unchanged)
    target   `docker load`, reports what it loaded (image_loaded)
    source   -> server: image_saved {request_id, ok, reason, bytes, image}
    server   answers the console: what was loaded, which recipe image each one is (by its recorded ids, or by a
             tag a tag-only recipe image names), and whether it is the image the source has (its content id)

**An explicit act, never automatic**: a tag-only image has no recorded ids, so copying it trusts the source
machine with what the target will run under that tag; only an admin does that, and it is audited
(`node.image_copied`: from, to, the image, the ids loaded). The server stores nothing but QUEUE_CHUNKS pieces:
a slow target slows the source's save. A `docker save` stream has no size ahead and no Range, so a copy that
breaks off starts again from the beginning; the target's answer is chunked and never ends cleanly when the source
broke off, so its `docker load` loads nothing then. Limits: the upload's (one at a time per target machine, 2 in
all, IMAGE_UPLOAD_MAX_GB), and one copy at a time per source machine.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from forgeyard.db import Node, new_session
from forgeyard.machines import store, upload
from forgeyard.machines.hub import hub
from forgeyard.machines.protocol import FEATURE_IMAGE_SAVE, IMAGE_SEND_PATH, PROTOCOL_VERSION
from forgeyard.workloads import images

router = APIRouter(tags=["agent"])
log = logging.getLogger("forgeyard.upload")

LIST_TIMEOUT = 30.0       # the source must list its images within this long
START_TIMEOUT = 120.0     # and start sending a save within this long (a large image takes a while to start)
STALL = 120.0             # a source that sends nothing for this long has broken off
SAID_GRACE = 10.0         # how long the copy waits for the source to say how its save ended (or why it stopped)
QUEUE_CHUNKS = 64
_TICKET = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_END = object()


class CopyFailed(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


@dataclass
class Send:
    ticket: str
    source_id: int
    source_name: str
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_CHUNKS))
    started: asyncio.Event = field(default_factory=asyncio.Event)   # the first bytes arrived, or it ended
    claimed: bool = False
    aborted: bool = False          # the target's side is gone: the source's POST is ended
    ended: Optional[str] = None    # "finished", else why the source's side ended
    broke: bool = False            # the source's POST broke off (a chunked body that never ended)
    received: int = 0


_sends: dict[str, Send] = {}


def reset() -> None:
    _sends.clear()


def sending(node_id: int) -> bool:
    return any(s.source_id == node_id for s in _sends.values())


def _source_conn(node: Node):
    conn = hub.get(node.id)
    if conn is None:
        raise CopyFailed(409, "source_offline", f"{node.name}'s agent is not connected.")
    if FEATURE_IMAGE_SAVE not in conn.features:
        raise CopyFailed(409, "source_cannot_save", f"{node.name}'s agent cannot send images: it has no container "
                                                    "engine, or it is older than 1.4.1 (update the agent).")
    return conn


async def list_images(node: Node) -> list[dict]:
    """The images in this machine's container engine, as its agent lists them. Raises CopyFailed."""
    return list((await list_answer(node)).get("images") or [])


async def list_answer(node: Node) -> dict:
    """The agent's whole answer to image_list: its images, and (1.4.2, "image_remove") the image it runs from and the
    space around them. Raises CopyFailed."""
    conn = _source_conn(node)
    fut = conn.request({"v": PROTOCOL_VERSION, "type": "image_list"})
    try:
        answer = await asyncio.wait_for(asyncio.wrap_future(fut), LIST_TIMEOUT)
    except asyncio.TimeoutError:
        raise CopyFailed(504, "agent_no_answer", f"{node.name}'s agent did not list its images in time.") from None
    except Exception:
        raise CopyFailed(409, "agent_no_answer", f"{node.name}'s agent went away before it listed its images.") \
            from None
    finally:
        conn.forget(fut)
    if not answer.get("ok"):
        raise CopyFailed(409, "image_list_failed", f"{node.name}: {answer.get('reason') or 'could not list images'}")
    return answer


def _broken(source: Node, target: Node, send: Send, said) -> CopyFailed:
    """The source stopped sending part way (#215). Whether the server noticed its POST break off before the source
    said why on its WebSocket, or after, or before it read a byte of it, the admin is told the same: the copy broke
    off, nothing was loaded, and what the source says (`docker save failed: …`) when it said it in time."""
    said = said if isinstance(said, dict) else {}
    sent = said.get("bytes") if isinstance(said.get("bytes"), int) and said.get("bytes") > 0 else send.received
    reason = str(said.get("reason") or "").strip().rstrip(".") if said.get("ok") is False else ""
    head = f"The copy broke off after {sent} bytes: {source.name} stopped sending, so nothing was loaded on {target.name}."
    if reason:
        why = f" {source.name} says: {reason}. Fix that there, then copy the image again."
    else:
        why = (f" {source.name} did not say why ({send.ended or 'its side ended'}). Copy the image again; if it breaks "
               f"off again, look at {source.name}'s agent log.")
    log.warning("Copy of an image from %s to %s: %s", source.name, target.name, head + why)
    return CopyFailed(400, "upload_broken", head + why)


async def _said(saved: asyncio.Future) -> Optional[dict]:
    """What the source answered about its save, waiting up to SAID_GRACE for it; None when it did not answer."""
    try:
        said = await asyncio.wait_for(asyncio.shield(saved), SAID_GRACE)
    except Exception:
        return None
    return said if isinstance(said, dict) else None


def _abort(send: Send) -> None:
    if send.aborted:
        return
    send.aborted = True
    while not send.queue.empty():
        send.queue.get_nowait()
    send.queue.put_nowait(ConnectionAbortedError("the copy was abandoned"))


async def copy(source: Node, target: Node, image: str, limit: int) -> dict:
    """Copy one image of `source` to `target`; the target's answer (ok, reason, images, bytes) and what the source
    says it sent (`saved`). Raises CopyFailed or upload.UploadFailed."""
    wanted = images.normalize(image)
    if wanted is None:
        raise CopyFailed(422, "bad_image", "Name the image by its id (sha256:…), as the source machine lists it.")
    if source.id == target.id:
        raise CopyFailed(422, "same_machine", "The image is already on that machine.")
    conn = _source_conn(source)
    if sending(source.id):
        raise CopyFailed(409, "source_busy", f"{source.name} is already sending an image. Wait for it to finish.")
    upload.precheck(target)  # before the source saves a byte
    send = Send(secrets.token_urlsafe(24), source.id, source.name)
    _sends[send.ticket] = send
    fut = conn.request({"v": PROTOCOL_VERSION, "type": "image_save", "path": f"{IMAGE_SEND_PATH}/{send.ticket}",
                        "image": wanted})
    saved = asyncio.wrap_future(fut)
    began = time.monotonic()
    log.info("Copy of image %s from %s to %s started.", wanted[:19], source.name, target.name)
    try:
        first = asyncio.ensure_future(send.started.wait())
        try:
            await asyncio.wait({first, saved}, timeout=START_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
        finally:
            first.cancel()
        if not send.received:
            said = saved.result() if saved.done() and not saved.cancelled() and saved.exception() is None else None
            if said is None and send.broke:  # its POST broke off before this server read a byte: what does it say?
                said = await _said(saved)
                if said is None:
                    raise _broken(source, target, send, None)
            if isinstance(said, dict) and said.get("ok") is False and (said.get("bytes") or 0) > 0:
                # It sent part of the image and stopped, and said so before this server read a byte of its POST
                # (or after its POST broke off unread): the same as when the bytes did arrive (#215).
                raise _broken(source, target, send, said)
            if said is not None:
                raise CopyFailed(409, "image_save_failed", f"{source.name}: {said.get('reason') or 'it did not send'}")
            if saved.done():
                raise CopyFailed(409, "agent_no_answer", f"{source.name}'s agent went away before it sent the image.")
            raise CopyFailed(504, "source_no_send", send.ended or f"{source.name} did not start sending within "
                                                                  f"{START_TIMEOUT:g} s.")

        async def chunks():
            while True:
                try:
                    item = await asyncio.wait_for(send.queue.get(), STALL)
                except asyncio.TimeoutError:
                    raise ConnectionError(f"{source.name} sent nothing for {STALL:.0f} s") from None
                if item is _END:
                    return
                if isinstance(item, BaseException):
                    raise ConnectionError(f"{source.name}: {send.ended or item}")
                yield item

        name = f"{wanted[:19]} from {source.name}"
        try:
            answer = await upload.pipe(target, chunks(), None, name, limit=limit)
        except upload.UploadFailed:
            if not send.broke:
                raise
            raise _broken(source, target, send, await _said(saved)) from None  # and why the source stopped (#215)
        try:
            said = await asyncio.wait_for(asyncio.shield(saved), SAID_GRACE)
        except Exception:
            said = {}
    finally:
        _abort(send)
        _sends.pop(send.ticket, None)
        conn.forget(fut)
    took = max(time.monotonic() - began, 1e-6)
    log.info("Copy of image %s from %s to %s: %.1f MiB in %.1f s, %s.", wanted[:19], source.name, target.name,
             send.received / 1024**2, took, "loaded" if answer.get("ok") else f"not loaded: {answer.get('reason')}")
    return {**answer, "saved": said.get("image") if isinstance(said, dict) else None}


def same_image(saved: Optional[dict], loaded: dict) -> Optional[bool]:
    """Whether what the target loaded is the image the source sent: by content id (stable across Docker's two
    stores and Podman), else by id. None when the source said nothing to compare with."""
    if not isinstance(saved, dict):
        return None
    if saved.get("content") and loaded.get("content"):
        return saved["content"] == loaded["content"]
    a, b = images.normalize(saved.get("id")), images.normalize(loaded.get("id"))
    return (a == b) if a and b else None


# ------------------------------------------------------------------ the source's side
def _node_of(credential: Optional[str]) -> Optional[int]:
    db = new_session()
    try:
        node = store.authenticate(db, credential)
        return node.id if node is not None and node.credential_hash is not None else None
    finally:
        db.close()


# Every answer closes the connection (as forgeyard/machines/peers.py does): a source whose send was ended early
# stops writing at once instead of sending the rest of the image to be thrown away.
CLOSE = {"Connection": "close"}


def _refuse(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status, headers=CLOSE)


async def _put(send: Send, item) -> bool:
    waited = 0.0
    while not send.aborted:
        try:
            await asyncio.wait_for(send.queue.put(item), 1.0)
            return not send.aborted
        except asyncio.TimeoutError:
            waited += 1.0
            if waited >= STALL:
                send.ended = f"the machine it goes to took nothing for {STALL:.0f} s"
                return False
    return False


@router.post(IMAGE_SEND_PATH + "/{ticket}")
async def image_send(ticket: str, request: Request):
    """The image a source machine saves, piped into the copy to another machine as it arrives."""
    if request.headers.get("origin"):
        return _refuse(403, "browser_refused", "This endpoint is for Forgeyard agents, not browsers.")
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    node_id = await run_in_threadpool(_node_of, value.strip() if scheme.lower() == "bearer" else None)
    if node_id is None:
        return _refuse(401, "credential_refused", "This machine credential is unknown or was revoked.")
    send = _sends.get(ticket) if _TICKET.match(ticket or "") else None
    if send is None or send.source_id != node_id:
        return _refuse(404, "send_not_found", "No image send from this machine with that ticket is under way.")
    if send.claimed:
        return _refuse(409, "send_claimed", "That send is already under way.")
    send.claimed = True
    try:
        async for chunk in request.stream():
            if send.aborted:
                break
            if not chunk:
                continue
            if not await _put(send, chunk):
                break
            send.received += len(chunk)
            send.started.set()
        else:
            if await _put(send, _END):
                send.ended = "finished"
    except Exception as e:  # the source went away, or its save broke off (a chunked body that never ended)
        send.ended = f"it broke off after {send.received} bytes ({type(e).__name__})"
        send.broke = True
    if send.ended != "finished":
        send.ended = send.ended or f"it ended after {send.received} bytes"
        if send.aborted or not await _put(send, ConnectionError(send.ended)):
            _abort(send)
    send.started.set()
    if send.ended == "finished":
        return JSONResponse({"ok": True, "bytes": send.received}, headers=CLOSE)
    return _refuse(409, "send_ended", f"The send ended: {send.ended}.")
