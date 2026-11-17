"""The agent sends a model file from its cache to its server, for another machine (1.4.1, issue #104; protocol
feature "relay_source").

The server asks `relay_send {request_id, path, item, offset, length}` when a machine that gets its files through
the server needs a file this agent has. The agent:

1. looks the file up **only among the models it found on its own disk** (the rule `ModelCache.remove()` keeps):
   a Hugging Face file must be listed by its revision's marker, its snapshot link must point at the blob the
   server names (its SHA-256 or git blob id), and that blob must be a regular file inside the cache with the
   size the server says; a URL model must have its marker, with the SHA-256 the server says. Nothing from the
   server is used as a path. Every file there was checked when it was downloaded, and only then moved in;
2. POSTs bytes offset..offset+length-1 of it to `<its own server><path>` (never a host the server names), with
   its machine credential and the CA it pins (agent/relay.py), holding the model's files lock shared, so the
   machine page's Remove waits for the send ("busy") instead of deleting the file under it;
3. hashes a whole-file send as it reads it and says "corrupt" when it does not match (the server's own hash
   has refused it then: its log and audit log name this machine). It never deletes anything by itself;
4. answers `relay_sent {request_id, ok, code, reason, bytes}`: code "not_found" (not here, or not as the server
   describes it), "busy" (MAX_SEND sends under way), "corrupt", or "failed".

Sends run in a thread and never block the connection, the replicas or their health checks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Optional

from forgeyard.agent import relay as relay_client
from forgeyard.agent.models import CacheError, ModelCache, _hex
from forgeyard.models import fetch, sources

log = logging.getLogger("forgeyard.agent")

MAX_SEND = 2
CHUNK = 1024 * 1024
SEND_PATH_RE = re.compile(r"^/agent/v1/relay-send/[A-Za-z0-9_-]{16,64}$")


class SendRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _regular_inside(path: Path, root: Path, size: int) -> bool:
    """A regular file (not a link), below the cache root through no linked directory, of `size` bytes."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    real = os.path.realpath(path)
    top = os.path.realpath(root)
    return stat.S_ISREG(st.st_mode) and real == os.path.abspath(path) and real.startswith(top + os.sep) \
        and st.st_size == size


def find(cache: ModelCache, item: dict) -> tuple[Path, int, str, str, str]:
    """(path, size, algo, expected hex, files lock) of the file `item` describes, found on this disk; else
    SendRefused("not_found")."""
    if not isinstance(item, dict):
        raise SendRefused("not_found", "not a file description")
    try:
        size = int(item.get("size"))
    except (TypeError, ValueError):
        size = 0
    if size <= 0:
        raise SendRefused("not_found", "no size")
    if item.get("kind") == "hf":
        try:
            base_rel = sources.hf_dir(str(item.get("repo") or ""))
            file = sources.safe_relpath(str(item.get("file") or ""))
        except sources.SourceError:
            raise SendRefused("not_found", "not a model file") from None
        rev, blob = _hex(item.get("revision"), 40), _hex(item.get("blob"), 64) or _hex(item.get("blob"), 40)
        if not rev or not blob:
            raise SendRefused("not_found", "not a model file")
        entry = next((e for e in cache._entries() if e["key"] == f"{base_rel}@{rev}"), None)
        if entry is None:
            raise SendRefused("not_found", "that revision is not in this machine's cache")
        try:
            listed = {str(f.get("path")) for f in json.loads(entry["_marker"].read_text()).get("files") or []
                      if isinstance(f, dict)}
        except (OSError, ValueError):
            listed = set()
        if file not in listed:
            raise SendRefused("not_found", f"{file} of that revision is not in this machine's cache")
        base: Path = entry["_base"]
        path = base / "blobs" / blob
        link = base / "snapshots" / rev / file
        if not link.is_symlink() or os.path.realpath(link) != os.path.realpath(path) \
                or not _regular_inside(path, cache.root, size):
            raise SendRefused("not_found", f"{file} here is not the file asked for")
        return path, size, "sha256" if len(blob) == 64 else "oid", blob, "repo:" + base_rel
    if item.get("kind") == "url":
        d, sha = str(item.get("dir") or ""), _hex(item.get("sha256"), 64)
        if not sha:
            raise SendRefused("not_found", "not a model file")
        entry = next((e for e in cache._entries() if e["key"] == f"url/{d}"), None)
        if entry is None or entry.get("sha256") != sha or not entry.get("file") or "/" in entry["file"]:
            raise SendRefused("not_found", "that model is not in this machine's cache, or not with that checksum")
        path = entry["_dir"] / entry["file"]
        if not _regular_inside(path, cache.root, size):
            raise SendRefused("not_found", "the file here is not the file asked for")
        return path, size, "sha256", sha, f"url:url/{d}"
    raise SendRefused("not_found", "unknown kind of model file")


class Sender:
    """The agent's sends, at most MAX_SEND at a time."""

    def __init__(self, cache: ModelCache, client: relay_client.RelayClient):
        self.cache, self.client = cache, client
        self._lock = threading.Lock()
        self.running = 0

    def send(self, message: dict, cancel: Optional[threading.Event] = None) -> dict:
        """In a thread: the whole send; the answer's fields (ok, code, reason, bytes)."""
        cancel = cancel or threading.Event()
        path_on_server = str(message.get("path") or "")
        try:
            offset, length = int(message.get("offset") or 0), int(message.get("length") or 0)
        except (TypeError, ValueError):
            offset = length = -1
        if not SEND_PATH_RE.match(path_on_server) or offset < 0 or length <= 0:
            return {"ok": False, "code": "failed", "reason": "the request is not one this agent sends for"}
        with self._lock:
            if self.running >= MAX_SEND:
                return {"ok": False, "code": "busy", "reason": f"{MAX_SEND} sends are under way here"}
            self.running += 1
        try:
            return self._send(message.get("item"), path_on_server, offset, length, cancel)
        finally:
            with self._lock:
                self.running -= 1

    def _send(self, item, path_on_server: str, offset: int, length: int, cancel: threading.Event) -> dict:
        if self.cache.error:
            return {"ok": False, "code": "not_found", "reason": self.cache.error}
        try:
            path, size, algo, expect, lock = find(self.cache, item)
        except SendRefused as e:
            return {"ok": False, "code": e.code, "reason": str(e)}
        if offset + length > size:
            return {"ok": False, "code": "not_found", "reason": "those bytes are not in the file"}
        whole = offset == 0 and length == size
        hasher = (hashlib.sha1(f"blob {size}\0".encode()) if algo == "oid" else hashlib.sha256()) if whole else None
        started = time.monotonic()
        try:
            with self.cache._shared(lock, cancel), open(path, "rb") as fh:
                fh.seek(offset)

                def chunks():
                    left = length
                    while left > 0:
                        if cancel.is_set():
                            raise relay_client.SendStopped("cancelled")
                        chunk = fh.read(min(CHUNK, left))
                        if not chunk:
                            raise relay_client.SendStopped("the file ended early")
                        left -= len(chunk)
                        if hasher is not None:
                            hasher.update(chunk)
                        yield chunk

                self.client.post(path_on_server, chunks(), length)
        except (relay_client.SendStopped, relay_client.RelayRefused, fetch.FetchError, CacheError, OSError) as e:
            log.info("Sending %s to the server stopped: %s", path.name, e)
            return {"ok": False, "code": "failed", "reason": str(e)[:300] or type(e).__name__}
        took = max(time.monotonic() - started, 1e-6)
        if hasher is not None and hasher.hexdigest() != expect:
            log.warning("A cached file failed its checksum while it was sent to the server (%s): remove the model "
                        "from this machine's cache (its page on the server) and download it again.", path)
            return {"ok": False, "code": "corrupt", "reason": "this machine's copy does not match its checksum",
                    "bytes": length}
        log.info("Sent %s to the server for another machine: %.1f MiB in %.1f s (%.1f MiB/s).", path.name,
                 length / 1024**2, took, length / 1024**2 / took)
        return {"ok": True, "bytes": length}
