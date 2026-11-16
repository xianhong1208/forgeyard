"""What a machine could not fetch, and what a replica would have to fetch there (1.4, #56).

A replica of a model deployment needs up to three things on its machine before it starts: the model
(in the agent's model cache), and the engine, either a pinned archive (process variants) or an image
(container variants). Placement prefers a machine that already has them, and avoids one that could
not reach where they come from:

- **Failed fetches are remembered** per machine and source host (huggingface.co, github.com, ghcr.io,
  …) for `TTL`, when a replica's download there ended with a *network* error: the name did not
  resolve, the connection was refused or timed out, the network was unreachable. HTTP errors (a
  gated model, a missing file), checksum mismatches and a full disk are not network errors: another
  machine would get the same answer.
- The source of a model is its Hugging Face endpoint or its URL's host; of an engine archive, its
  URL's host; of an image, its registry (docker.io when the reference names none).
- Failures are read from the replica's reason as the agent words it (`<file>: download failed: <why>`,
  or an image that `could not be pulled`), so agents of every version take part.
- An image counts as on a machine once a replica started from it there, or a "Download to…" pulled it
  (since the server started); an engine archive once the agent lists it in its model cache (1.4 agents)
  or a replica started from it.

All of it is kept in memory: after a server restart placement starts again from what the agents
report. Nothing here is a column.
"""

from __future__ import annotations

import posixpath
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlsplit

from forgeyard.db import Node, Replica, as_utc, utcnow

TTL = timedelta(hours=1)

# What a download or pull says when it could not reach the source at all. Matched in lower case.
NETWORK_ERRORS = (
    "cannot resolve", "temporary failure in name resolution", "name or service not known", "no such host",
    "server misbehaving", "nodename nor servname", "connection refused", "timed out", "i/o timeout",
    "tls handshake timeout", "context deadline exceeded", "network is unreachable", "no route to host",
    "host is unreachable", "connection reset",
)

PHASE_LABEL = {"model": "the model", "engine": "the engine", "image": "the engine image"}


@dataclass(frozen=True)
class Failure:
    host: str
    phase: str      # model | engine | image
    detail: str     # the agent's words
    at: datetime


_lock = threading.Lock()
_failures: dict[tuple[int, str], Failure] = {}
_seen: dict[int, set[str]] = {}       # machine id -> "engine:<sha256[:16]>" / "image:<reference>"
_notes: dict[int, str] = {}           # replica id -> why it was placed where it is
MAX_ENTRIES = 10_000


def reset() -> None:
    """Forget everything (tests)."""
    with _lock:
        _failures.clear()
        _seen.clear()
        _notes.clear()


# ------------------------------------------------------------------ sources
def _host(url: Optional[str]) -> Optional[str]:
    try:
        return (urlsplit(url or "").hostname or "").lower() or None
    except ValueError:
        return None


def model_host(model: Optional[dict]) -> Optional[str]:
    """Where a model block (catalog.model_block) is downloaded from; None for a local directory."""
    if not model:
        return None
    if model.get("kind") == "hf":
        return _host(model.get("endpoint") or "https://huggingface.co")
    if model.get("kind") == "url":
        return _host(model.get("url"))
    return None


def engine_host(engine: Optional[dict]) -> Optional[str]:
    return _host((engine or {}).get("url"))


def registry(image: Optional[str]) -> Optional[str]:
    """The registry of an image reference: ghcr.io/x/y:tag -> ghcr.io; python:3.13 -> docker.io."""
    if not image:
        return None
    first, slash, _ = image.partition("/")
    if slash and ("." in first or ":" in first or first == "localhost"):
        return first.split(":", 1)[0].lower()
    return "docker.io"


def network_error(text: Optional[str]) -> bool:
    t = (text or "").lower()
    return any(p in t for p in NETWORK_ERRORS)


def classify(r: Replica, reason: Optional[str], now: Optional[datetime] = None) -> Optional[Failure]:
    """The fetch that failed for a network reason, from the replica's reason; None for anything else."""
    text = reason or ""
    if not network_error(text):
        return None
    now = now or utcnow()
    label, sep, rest = text.partition(": download failed: ")
    if sep:
        engine = r.engine or {}
        if engine.get("url") and label == posixpath.basename(str(engine["url"])):
            host, phase = engine_host(engine), "engine"
        else:
            host, phase = model_host(r.model), "model"
        detail = rest.rsplit("; restarting in ", 1)[0]
    elif "could not be pulled" in text or "could not pull" in text:
        image = (((r.spec or {}).get("runtime") or {}).get("image"))
        host, phase, detail = registry(image), "image", text.rsplit("; restarting in ", 1)[0]
    else:
        return None
    return Failure(host, phase, detail[:300], now) if host else None


# ------------------------------------------------------------------ memory
def record(node_id: int, f: Failure) -> None:
    with _lock:
        if len(_failures) > MAX_ENTRIES:
            _failures.clear()
        _failures[(node_id, f.host)] = f


def failed(node_id: int, host: Optional[str], now: Optional[datetime] = None) -> Optional[Failure]:
    """The machine's last network failure reaching `host`, while it is remembered."""
    if not host:
        return None
    with _lock:
        f = _failures.get((node_id, host))
        if f is not None and as_utc(f.at) + TTL <= as_utc(now or utcnow()):
            _failures.pop((node_id, host), None)
            return None
        return f


def failures(node_id: int, now: Optional[datetime] = None) -> list[Failure]:
    with _lock:
        hosts = [h for (n, h) in _failures if n == node_id]
    return [f for f in (failed(node_id, h, now) for h in hosts) if f is not None]


def seen(node_id: int, r: Replica) -> None:
    """A replica started on this machine: its engine archive or image is there."""
    things = set()
    if r.engine and r.engine.get("sha256"):
        things.add("engine:" + str(r.engine["sha256"])[:16])
    image = ((r.spec or {}).get("runtime") or {}).get("image")
    if image:
        things.add(f"image:{image}")
    if not things:
        return
    with _lock:
        if len(_seen) > MAX_ENTRIES:
            _seen.clear()
        _seen.setdefault(node_id, set()).update(things)


def seen_image(node_id: int, image: str) -> None:
    """A download ahead of a deployment (1.4, "Download to…") pulled this image onto the machine."""
    with _lock:
        if len(_seen) > MAX_ENTRIES:
            _seen.clear()
        _seen.setdefault(node_id, set()).add(f"image:{image}")


def has_engine(node: Node, engine: dict) -> bool:
    sha = str(engine.get("sha256") or "")[:16]
    if sha and sha in ((node.model_cache or {}).get("engines") or []):
        return True
    with _lock:
        return f"engine:{sha}" in _seen.get(node.id, set())


def has_image(node: Node, image: str) -> bool:
    with _lock:
        return f"image:{image}" in _seen.get(node.id, set())


def note(replica_id: int, text: Optional[str]) -> None:
    """What the deployment page says about where this replica was placed (None: nothing)."""
    with _lock:
        if text:
            if len(_notes) > MAX_ENTRIES:
                _notes.clear()
            _notes[replica_id] = text[:300]
        else:
            _notes.pop(replica_id, None)


def note_of(replica_id: int) -> Optional[str]:
    with _lock:
        return _notes.get(replica_id)


# ------------------------------------------------------------------ what a placement needs
def needs(node: Node, *, cached: bool, model: Optional[dict], engine: Optional[dict], image: Optional[str]
          ) -> list[tuple[str, Optional[str]]]:
    """What a replica would have to fetch on this machine: [(phase, source host)]."""
    out: list[tuple[str, Optional[str]]] = []
    if model is not None and not cached and model.get("kind") != "local":
        out.append(("model", model_host(model)))
    if engine is not None and not has_engine(node, engine):
        out.append(("engine", engine_host(engine)))
    if image and not has_image(node, image):
        out.append(("image", registry(image)))
    return out


def blocked(node: Node, wanted: list[tuple[str, Optional[str]]], now: Optional[datetime] = None) -> Optional[Failure]:
    """The remembered failure that makes this machine a bad place for those fetches, if any."""
    for _, host in wanted:
        f = failed(node.id, host, now)
        if f is not None:
            return f
    return None


def ago(f: Failure, now: Optional[datetime] = None) -> str:
    minutes = int((as_utc(now or utcnow()) - as_utc(f.at)).total_seconds() // 60)
    return "just now" if minutes < 1 else f"{minutes} min ago"


_WS = re.compile(r"\s+")


def could_not_reach(machine: str, f: Failure) -> str:
    """"cpu-01 could not reach huggingface.co" (the phase in brackets when it is not the model)."""
    what = "" if f.phase == "model" else f" (for {PHASE_LABEL.get(f.phase, f.phase)})"
    return _WS.sub(" ", f"{machine} could not reach {f.host}{what}")
