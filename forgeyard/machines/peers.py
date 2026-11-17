"""A model file from another machine's cache, through the server (1.4.1, issue #104; protocol feature
"relay_source").

A machine that gets its files through the server (its `fetch_via` is "server", or "auto" after the source
could not be reached) asks the server's relay for a model file as in 1.4.0 (forgeyard/machines/relay.py): its
request does not change. Before the server fetches the file from the internet, it asks a machine that already
has it in its cache to send it:

    target  GET /agent/v1/relay/{replica|download}/<id>/model/<file>   (Range: bytes=N-)   unchanged
    server  picks a source among the machines whose reported cache has the model (CANDIDATES below)
    server  -> source over its WebSocket: relay_send {request_id, path, item, offset, length}
    source  looks the file up among what it found on its own disk, and
            POST <its server>/agent/v1/relay-send/<ticket>  (its machine credential, Content-Length = length)
    server  pipes that body into the target's answer through a small queue (QUEUE_CHUNKS pieces): a slow
            target slows the source, and nothing is stored
    source  -> server: relay_sent {request_id, ok, code, reason, bytes}

**Nothing the target or the source sends picks what is sent.** The file is one the target's desired state
names (relay.resolve, as in 1.4.0), and the server describes it to the source by the digest it already holds:
the SHA-256 or git blob id of a Hugging Face file, the SHA-256 recorded for a URL model (D8; a URL model with
no recorded SHA-256 never comes from a machine). The source agent turns that description into a path only by
looking it up among the models it found on its own disk (forgeyard/agent/share.py). `Content-Length` and
`Content-Range` are the server's, from the file's size in the model block. The ticket is random, for one
source and one POST, and is gone when the pipe ends.

**Candidates**, per request: machines other than the target, connected, whose agent lists "relay_source",
whose reported cache (`nodes.model_cache`) holds the model's key (the revision, or for a URL model its key with
the recorded SHA-256), and that are not marked for this file. Ranked: a machine that lists the very GGUF file
first (a revision may hold only some of its files), then the one with fewer sends under way, then the most
recent heartbeat. (The machine on the server's own host cannot be told apart: a tunnel's connections come from
127.0.0.1 too.) The first one is asked; one that answers "not_found" or "busy", or does not start sending within
SOURCE_TIMEOUT (it is then marked slow for that file for SLOW_FOR), is skipped for the next, and then the
internet is used as in 1.4.0. All of that happens before the target gets its first byte.

**Checks.** The target checks every file as it always does. The server also hashes a whole-file transfer and
holds its last piece back until the hash matches (as for image blobs): a source whose bytes do not match is not
asked for that file again for BAD_FOR, the audit log names it (`node.relay_copy_refused`), and the target's
next request for the file (its resume) is answered whole from elsewhere, so it starts the file over. A source
that breaks off mid-file ends the target's answer short; the target resumes with Range, and that request picks
a source again.

**Limits**: the relay's own (2 per target, 4 in all), and MAX_PER_SOURCE sends at a time per source.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard.db import Node, new_session
from forgeyard.machines import store
from forgeyard.machines.hub import hub
from forgeyard.machines.protocol import FEATURE_RELAY_SOURCE, PROTOCOL_VERSION, RELAY_SEND_PATH
from forgeyard.models import sources

router = APIRouter(tags=["agent"])
log = logging.getLogger("forgeyard.relay")

SOURCE_TIMEOUT = 10.0     # a source must send its first bytes within this long, or the next one is asked
STALL = 60.0              # a source that sends nothing for this long has broken off
MAX_PER_SOURCE = 2
QUEUE_CHUNKS = 64         # what the server holds per pipe at most (uvicorn's pieces are 64 KiB at most)
BAD_FOR = 3600.0          # a source whose bytes did not match is not asked for that file for this long
SLOW_FOR = 600.0          # nor one that did not start in time
_HEX = re.compile(r"^[0-9a-f]+$")
_TICKET = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
_END = object()
_BROKEN = object()


# ------------------------------------------------------------------ which file, and which machines have it
@dataclass(frozen=True)
class Candidate:
    node_id: int
    name: str
    exact: bool          # its cache lists the very file (a GGUF file's own key), not only the revision
    seen: float          # its last heartbeat (epoch seconds)


@dataclass
class FileCopy:
    """One model file that may come from a machine: what the source is told, and what its bytes must hash to."""
    key: str             # what marks are kept by: "hub/models--a--b@<rev>/<file>" or "url/<id>"
    item: dict           # sent to the source in relay_send
    size: int
    algo: str            # "sha256" or "oid" (the git blob id)
    expect: str          # lower-case hex
    label: str
    candidates: list[Candidate] = field(default_factory=list)


def _hex(value, *lengths: int) -> Optional[str]:
    v = str(value or "").lower()
    return v if len(v) in lengths and _HEX.match(v) else None


def file_copy(db: DbSession, target: Node, model: dict, path: Optional[str]) -> Optional[FileCopy]:
    """The machine copy a relayed model file may come from, with the machines that could send it; None when
    it cannot come from a machine (no digest to check it by, no size, a local model)."""
    try:
        if model.get("kind") == "hf" and path is not None:
            f = next((f for f in model.get("files") or [] if isinstance(f, dict) and str(f.get("path")) == path), None)
            if f is None:
                return None
            size = int(f.get("size") or 0)
            sha, oid = _hex(f.get("sha256"), 64), _hex(f.get("oid"), 40)
            rev = _hex(model.get("revision"), 40)
            if size <= 0 or not rev or not (sha or oid):
                return None
            base = f"{sources.hf_dir(str(model.get('repo') or ''))}@{rev}"
            # The agent names a blob by its SHA-256, else its git blob id (agent/models.py).
            item = {"kind": "hf", "repo": str(model["repo"]), "revision": rev, "file": path, "blob": sha or oid,
                    "size": size}
            copy = FileCopy(f"{base}/{path}", item, size, "sha256" if sha else "oid", sha or oid,
                            f"{model['repo']}@{rev[:12]}/{path}")
            copy.candidates = _candidates(db, target, copy, base, f"{base}/{path}", None)
            return copy
        if model.get("kind") == "url" and path is None and model.get("url"):
            files = model.get("files") or [{}]
            size = int(files[0].get("size") or 0)
            sha = _hex(files[0].get("sha256"), 64)
            if size <= 0 or not sha:
                return None  # no SHA-256 recorded yet (D8): nothing to check a machine's copy against
            key = sources.url_dir(str(model["url"]))
            copy = FileCopy(key, {"kind": "url", "dir": key.removeprefix("url/"), "sha256": sha, "size": size}, size,
                            "sha256", sha, str(model["url"])[:200])
            copy.candidates = _candidates(db, target, copy, key, key, sha)
            return copy
    except (sources.SourceError, TypeError, ValueError, KeyError):
        return None
    return None


def _candidates(db: DbSession, target: Node, copy: FileCopy, key: str, exact: str,
                url_sha: Optional[str]) -> list[Candidate]:
    out = []
    for n in db.scalars(select(Node).where(Node.id != target.id, Node.revoked_at.is_(None),
                                           Node.credential_hash.is_not(None))).all():
        conn = hub.get(n.id)
        if conn is None or FEATURE_RELAY_SOURCE not in conn.features:
            continue
        mc = n.model_cache or {}
        if not isinstance(mc, dict) or mc.get("ok") is False:
            continue
        cached = set(mc.get("cached") or [])
        if key not in cached:
            continue
        if url_sha is not None:
            listed = next((m for m in mc.get("models") or [] if isinstance(m, dict) and m.get("key") == key), None)
            if not listed or listed.get("sha256") != url_sha:
                continue  # not the bytes the server recorded (D8), or not said
        if marked(n.id, copy.key):
            continue
        seen = n.last_heartbeat_at.timestamp() if n.last_heartbeat_at is not None else 0.0
        out.append(Candidate(n.id, n.name, exact in cached, seen))
    return out


def ranked(candidates: list[Candidate]) -> list[Candidate]:
    return sorted(candidates, key=lambda c: (not c.exact, sending(c.node_id), -c.seen, c.node_id))


# ------------------------------------------------------------------ what the server remembers (in memory)
_lock = threading.Lock()
_marks: dict[tuple[int, str], tuple[float, str]] = {}      # (source, file key) -> (until, why)
_restart: dict[tuple[int, str, str, str], float] = {}      # (target, kind, id, path) -> when its copy was refused
_served: dict[tuple[str, str], tuple[int, str]] = {}       # (kind, id) -> the source machine of its last file
_pipes: dict[str, "Pipe"] = {}                             # ticket -> pipe (the event loop's only)
_sending: dict[int, int] = {}                              # source -> sends under way


def mark(node_id: int, key: str, seconds: float, why: str) -> None:
    with _lock:
        if len(_marks) > 4096:
            _marks.clear()
        _marks[(node_id, key)] = (time.monotonic() + seconds, why)


def marked(node_id: int, key: str) -> Optional[str]:
    with _lock:
        held = _marks.get((node_id, key))
        if held is None:
            return None
        if held[0] <= time.monotonic():
            _marks.pop((node_id, key), None)
            return None
        return held[1]


def restart_due(target_id: int, kind: str, uid: str, path: Optional[str]) -> bool:
    """A copy of this file to this machine was refused: its partial file holds those bytes, so the next request
    is answered whole (the agent then starts the file over). Once."""
    with _lock:
        at = _restart.pop((target_id, kind, uid, path or ""), None)
    return at is not None and time.monotonic() - at < BAD_FOR


def note_source(kind: str, uid: str, source: Optional[tuple[int, str]]) -> None:
    """Which machine sends the files of this replica or download now (None: the internet)."""
    with _lock:
        if source is None:
            _served.pop((kind, uid), None)
            return
        if len(_served) > 4096:
            _served.clear()
        _served[(kind, uid)] = source


def source_of(kind: str, uid: str) -> Optional[tuple[int, str]]:
    with _lock:
        return _served.get((kind, uid))


def label(download: Optional[dict], kind: str, uid: str) -> Optional[dict]:
    """A replica's or download's progress, with the machine its model file comes from when it comes through
    the server from one (`source`); the agent cannot say it (it only knows its server)."""
    if not isinstance(download, dict) or download.get("via") != "server" or download.get("phase", "model") != "model":
        return download
    src = source_of(kind, uid)
    return {**download, "source": src[1][:64]} if src else download


def sending(node_id: int) -> int:
    return _sending.get(node_id, 0)


def reset() -> None:
    """Forget every mark, pipe and source (tests)."""
    with _lock:
        _marks.clear()
        _restart.clear()
        _served.clear()
    _pipes.clear()
    _sending.clear()


# ------------------------------------------------------------------ the pipe
@dataclass
class Pipe:
    ticket: str
    source_id: int
    source_name: str
    length: int
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=QUEUE_CHUNKS))
    first: asyncio.Event = field(default_factory=asyncio.Event)   # bytes arrived, or the source's side ended
    claimed: bool = False
    aborted: bool = False          # the target's side is gone: the source's POST is ended
    ended: Optional[str] = None    # how the source's side ended: "finished", else why not
    received: int = 0
    read: int = 0
    closed: bool = False


class PipeSource:
    """The target's side of a pipe, as the relay reads it (forgeyard/machines/relay.py)."""

    def __init__(self, pipe: Pipe, copy: FileCopy, offset: int, conn, fut):
        self.pipe, self.copy, self.offset = pipe, copy, offset
        self._conn, self._fut = conn, fut
        self.source = (pipe.source_id, pipe.source_name)
        ranged = offset > 0 or pipe.length != copy.size
        self.status = 206 if ranged else 200
        self.headers = {"Content-Length": str(pipe.length), "Accept-Ranges": "bytes"}
        if ranged:
            self.headers["Content-Range"] = f"bytes {offset}-{offset + pipe.length - 1}/{copy.size}"

    async def read(self) -> bytes:
        try:
            chunk = await asyncio.wait_for(self.pipe.queue.get(), STALL)
        except asyncio.TimeoutError:
            raise TimeoutError(f"{self.pipe.source_name} sent nothing for {STALL:.0f} s") from None
        if chunk is _END:
            return b""
        if chunk is _BROKEN:
            raise ConnectionError(f"{self.pipe.source_name}: {self.pipe.ended or 'its side ended'}")
        self.pipe.read += len(chunk)
        return chunk

    def remaining(self) -> int:
        return max(0, self.pipe.length - self.pipe.read)

    def abort(self) -> None:
        """The target's side is gone or no longer wanted: the source's POST is ended, a read waiting wakes."""
        _abort(self.pipe)

    async def close(self) -> None:
        _abort(self.pipe)
        _close(self.pipe, self._conn, self._fut)


def _abort(pipe: Pipe) -> None:
    if pipe.aborted:
        return
    pipe.aborted = True
    while not pipe.queue.empty():
        pipe.queue.get_nowait()
    pipe.queue.put_nowait(_BROKEN)
    pipe.ended = pipe.ended or "the machine that asked for it stopped reading"


def _close(pipe: Pipe, conn, fut) -> None:
    if pipe.closed:
        return
    pipe.closed = True
    _pipes.pop(pipe.ticket, None)
    n = _sending.get(pipe.source_id, 0) - 1
    if n > 0:
        _sending[pipe.source_id] = n
    else:
        _sending.pop(pipe.source_id, None)
    if conn is not None and fut is not None:
        conn.forget(fut)


async def open_copy(target_name: str, copy: FileCopy, offset: int, length: int) -> Optional[PipeSource]:
    """Ask the candidates in turn to send bytes offset..offset+length-1 of the file; the first that starts
    sending is returned, None when none did (the internet then, as in 1.4.0)."""
    order = ranked(copy.candidates)
    for i, cand in enumerate(order):
        if sending(cand.node_id) >= MAX_PER_SOURCE:
            continue
        conn = hub.get(cand.node_id)
        if conn is None or FEATURE_RELAY_SOURCE not in conn.features:
            continue
        pipe = Pipe(secrets.token_urlsafe(24), cand.node_id, cand.name, length)
        _pipes[pipe.ticket] = pipe
        _sending[cand.node_id] = sending(cand.node_id) + 1
        fut = conn.request({"v": PROTOCOL_VERSION, "type": "relay_send", "path": f"{RELAY_SEND_PATH}/{pipe.ticket}",
                            "item": copy.item, "offset": offset, "length": length})
        answer = asyncio.wrap_future(fut)
        first = asyncio.ensure_future(pipe.first.wait())
        try:
            await asyncio.wait({first, answer}, timeout=SOURCE_TIMEOUT, return_when=asyncio.FIRST_COMPLETED)
        finally:
            first.cancel()
        if pipe.first.is_set() and (pipe.received or pipe.ended == "finished"):
            part = "" if offset == 0 and length == copy.size else f" (bytes {offset}-{offset + length - 1})"
            log.info("%s%s for %s comes from %s.", copy.label, part, target_name, cand.name)
            return PipeSource(pipe, copy, offset, conn, fut)
        if answer.done() and not answer.cancelled() and answer.exception() is None:
            said = answer.result()
            why = f"{said.get('code') or 'failed'}: {said.get('reason') or ''}".strip(": ")
        elif answer.done():
            why = "its agent went away"
        elif pipe.first.is_set():
            why = pipe.ended or "it sent nothing"
        else:
            why = f"it did not start sending within {SOURCE_TIMEOUT:g} s"
            mark(cand.node_id, copy.key, SLOW_FOR, why)
        if not answer.done():
            answer.cancel()
        _abort(pipe)
        _close(pipe, conn, fut)
        log.info("%s could not send %s for %s (%s); trying %s.", cand.name, copy.label, target_name, why,
                 "the next machine" if i + 1 < len(order) else "the internet")
    return None


def refused(target_id: int, target_name: str, kind: str, uid: str, path: Optional[str], copy: FileCopy,
            source: tuple[int, str], got: str) -> None:
    """In a thread: a source's whole copy did not hash to the file's digest. It is not asked for the file for
    BAD_FOR; the target starts the file over on its next request; the audit log names the source."""
    from forgeyard import audit

    mark(source[0], copy.key, BAD_FOR, "its copy did not match its checksum")
    with _lock:
        if len(_restart) > 4096:
            _restart.clear()
        _restart[(target_id, kind, uid, path or "")] = time.monotonic()
    log.warning("%s's copy of %s does not match its checksum (%s %s…, expected %s…): not sent to %s, and not "
                "asked for again for an hour. Remove it from %s's model cache and download it again.", source[1],
                copy.label, copy.algo, got[:12], copy.expect[:12], target_name, source[1])
    db = new_session()
    try:
        audit.record(db, "node.relay_copy_refused", actor_name="forgeyard", target=source[1], machine_id=source[0],
                     file=copy.label, to=target_name, expected=copy.expect, got=got)
        if kind == "replica":
            from forgeyard.db import Replica
            from forgeyard.workloads.state import event

            r = db.scalar(select(Replica).where(Replica.uid == uid))
            if r is not None:
                event(db, r.workload, "state", f"{source[1]}'s copy of {copy.label} did not match its checksum: "
                      "it comes from elsewhere", r.ordinal)
        db.commit()
    finally:
        db.close()


# ------------------------------------------------------------------ the source's side
def _node_of(credential: Optional[str]) -> Optional[int]:
    db = new_session()
    try:
        node = store.authenticate(db, credential)
        return node.id if node is not None and node.credential_hash is not None else None
    finally:
        db.close()


def _refuse(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"detail": {"code": code, "message": message}}, status_code=status)


async def _put(pipe: Pipe, chunk) -> bool:
    """One piece into the queue, waiting while it is full: False when the target's side is gone, or took
    nothing for STALL."""
    waited = 0.0
    while not pipe.aborted:
        try:
            await asyncio.wait_for(pipe.queue.put(chunk), 1.0)
            return not pipe.aborted
        except asyncio.TimeoutError:
            waited += 1.0
            if waited >= STALL:
                pipe.ended = f"the machine that asked for it took nothing for {STALL:.0f} s"
                return False
    return False


@router.post(RELAY_SEND_PATH + "/{ticket}")
async def relay_send(ticket: str, request: Request):
    """The bytes of a cached model file, from the machine the server asked; piped into the relay of the machine
    that needs them."""
    if request.headers.get("origin"):
        return _refuse(403, "browser_refused", "This endpoint is for Forgeyard agents, not browsers.")
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    node_id = await run_in_threadpool(_node_of, value.strip() if scheme.lower() == "bearer" else None)
    if node_id is None:
        return _refuse(401, "credential_refused", "This machine credential is unknown or was revoked.")
    pipe = _pipes.get(ticket) if _TICKET.match(ticket or "") else None
    if pipe is None or pipe.source_id != node_id:
        return _refuse(404, "send_not_found", "No send from this machine with that ticket is under way.")
    if pipe.claimed:
        return _refuse(409, "send_claimed", "That send is already under way.")
    length = request.headers.get("content-length", "")
    if not length.isdigit() or int(length) != pipe.length:
        pipe.claimed = True
        pipe.ended = f"it announced {length or 'no'} bytes, not {pipe.length}"
        pipe.first.set()
        return _refuse(400, "send_length", f"Send exactly {pipe.length} bytes.")
    pipe.claimed = True
    try:
        async for chunk in request.stream():
            if pipe.aborted:
                break
            if not chunk:
                continue
            if pipe.received + len(chunk) > pipe.length:
                pipe.ended = "it sent more bytes than it announced"
                break
            if not await _put(pipe, chunk):
                break
            pipe.received += len(chunk)
            pipe.first.set()
        else:
            if pipe.received == pipe.length and await _put(pipe, _END):
                pipe.ended = "finished"
    except Exception as e:  # the source went away (ClientDisconnect), or the connection broke
        pipe.ended = f"it broke off after {pipe.received} of {pipe.length} bytes ({type(e).__name__})"
    if pipe.ended != "finished":
        pipe.ended = pipe.ended or f"it ended after {pipe.received} of {pipe.length} bytes"
        # What arrived is still read first; then the target's answer ends short, and it resumes from there.
        if pipe.aborted or not await _put(pipe, _BROKEN):
            _abort(pipe)
    pipe.first.set()
    if pipe.ended == "finished":
        return JSONResponse({"ok": True, "bytes": pipe.received})
    return _refuse(409, "send_ended", f"The send ended: {pipe.ended}.")
