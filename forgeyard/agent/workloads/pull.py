"""Pulling an engine image before `run`, with progress, a no-progress limit and a cancel.

The agent pulls through the engine's API on its Unix socket when it can: `POST /images/create?
fromImage=<name>&tag=<tag or digest>` (Docker Engine API; Podman serves the same compatible path)
streams one JSON object per event:

    {"status": "Pulling fs layer", "id": "4f4fb700ef54", "progressDetail": {}}
    {"status": "Downloading", "id": "4f4fb700ef54", "progressDetail": {"current": 1048576, "total": 31457280}}
    {"status": "Download complete" | "Verifying Checksum" | "Extracting" | "Pull complete", "id": ...}
    {"status": "Already exists", "id": ...}
    {"error": "...", "errorDetail": {"message": "..."}}

`Layers` sums them: every layer counts once, by its id; a layer's bytes are its download's `current`
until it is downloaded, then its `total`; a layer that "Already exists" is done and adds no bytes. A
layer's size is known only once its download starts (the engine downloads a few at a time), so the
total is final ("sized") only when every layer that is still to come has one.

When the API cannot be reached (no socket, a remote DOCKER_HOST, another context) the agent runs
`docker pull` / `podman pull` instead and reads its lines: without a terminal they carry no byte
counts, only each layer's steps, so the progress is "3 of 7 layers".

Either way the pull fails when nothing moves for `stall` seconds (FORGEYARD_PULL_STALL_MINUTES), not
after a fixed time: a slow pull that keeps receiving bytes runs as long as it needs. While the layers
are unpacked (everything downloaded) the limit is three times as long: that is local disk work, and
some engines report nothing while they do it.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import socket
import subprocess
import threading
import time
from typing import Callable, Optional
from urllib.parse import urlencode

from forgeyard.agent.watchdog import CANCELLED, STALLED, Watchdog

Report = Callable[[dict], None]

DONE_DOWNLOADING = ("Download complete", "Verifying Checksum", "Extracting", "Pull complete")
UNPACK_FACTOR = 3
REPORT_EVERY = 0.5
LINE_MAX = 64 * 1024
# What an engine says when the registry wants credentials it was not given: the CLI may have them.
AUTH_HINTS = ("unauthorized", "authentication required", "access denied", "denied:", "no basic auth credentials",
              "requested access to the resource is denied")


class PullError(Exception):
    """The pull failed; the message says where (which layer, how far) and the engine's error."""


class PullStalled(PullError):
    """Nothing moved for the limit: the registry is reachable but stopped sending (not an offline machine)."""


class PullCancelled(Exception):
    """The pull was cancelled: nobody wants the replica any more."""


class Unreachable(Exception):
    """The engine's API is not there (no socket, refused): the CLI is tried instead."""


class NeedsCredentials(Exception):
    """The registry refused an anonymous API pull: the CLI, with the user's credentials, is tried."""


def pull_ref(image: str) -> tuple[str, str]:
    """(fromImage, tag) as the Docker CLI sends them: 'repo:tag@sha256:…' and 'repo@sha256:…' pull the
    digest, 'repo:tag' the tag, a bare 'repo' its latest tag (an empty tag would pull every tag)."""
    ref, at, digest = image.partition("@")
    last = ref.rsplit("/", 1)[-1]
    name, tag = (ref.rsplit(":", 1) if ":" in last else (ref, ""))
    if at and digest:
        return name, digest
    return name, tag or "latest"


def gib(n: int) -> str:
    return f"{n / 1024**3:.2f}"


class Layers:
    """The progress of one pull, summed over its layers (each counted once)."""

    def __init__(self) -> None:
        self.layers: dict[str, dict] = {}
        self.last_layer: Optional[str] = None
        self.bytes_seen = False  # the API's byte counts (the CLI has none)
        self.version = 0  # how many times it moved
        self._lock = threading.Lock()  # the reader feeds it, the watchdog's tick reads it

    def feed(self, msg: dict) -> bool:
        """One event; True when the pull moved (a new layer, a new step, more bytes)."""
        with self._lock:
            moved = self._feed(msg)
            self.version += moved
            return moved

    def _feed(self, msg: dict) -> bool:
        lid = msg.get("id")
        status = str(msg.get("status") or "")
        if not isinstance(lid, str) or not lid or status.startswith(("Pulling from", "Digest:", "Status:")):
            return bool(status)
        layer = self.layers.setdefault(lid, {"status": "", "current": 0, "total": 0, "done": False,
                                             "exists": False, "complete": False})
        before = dict(layer)
        self.last_layer = lid
        detail = msg.get("progressDetail") if isinstance(msg.get("progressDetail"), dict) else {}
        current, total = _int(detail.get("current")), _int(detail.get("total"))
        if status == "Already exists":
            layer.update(done=True, exists=True, complete=True)
        elif status == "Downloading":
            self.bytes_seen = self.bytes_seen or bool(total)
            if total:
                layer["total"] = max(layer["total"], total)
            layer["current"] = min(current, layer["total"]) if layer["total"] else current
        elif status in DONE_DOWNLOADING:
            layer["done"] = True
            if status == "Pull complete":
                layer["complete"] = True
            elif status == "Extracting" and current != layer.get("extracted"):
                layer["extracted"] = current
        elif status.startswith("Retrying"):
            layer["current"] = 0  # the engine starts that layer again
        layer["status"] = status
        return layer != before

    def snapshot(self) -> dict:
        with self._lock:
            ls = [dict(x) for x in self.layers.values()]
            seen = self.bytes_seen
        return self._summary(ls, seen)

    @staticmethod
    def _summary(ls: list[dict], bytes_seen: bool) -> dict:
        total = sum(x["total"] for x in ls)
        done = sum(x["total"] if x["done"] else x["current"] for x in ls)
        downloaded = bool(ls) and all(x["done"] for x in ls)
        sized = bytes_seen and bool(ls) and all(x["exists"] or x["total"] for x in ls)
        return {"done": min(done, total) if sized else done, "total": total if sized else 0,
                "layers": len(ls), "layers_done": sum(1 for x in ls if x["complete"]),
                "unpacking": downloaded and not all(x["complete"] for x in ls)}

    def where(self) -> str:
        """For an error: the layer it was at, and how far the pull got."""
        s = self.snapshot()
        parts = []
        if self.last_layer:
            parts.append(f"at layer {self.last_layer[:12]}")
        if s["layers"]:
            parts.append(f"{s['layers_done']} of {s['layers']} layers done")
        if s["total"]:
            parts.append(f"{gib(s['done'])} of {gib(s['total'])} GiB")
        elif s["done"]:
            parts.append(f"{gib(s['done'])} GiB")
        return ", ".join(parts)


def _int(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def describe(s: dict) -> str:
    """'pulling the image: 0.80 of 1.90 GiB (42%)', and the forms for what is not known yet. Through the
    server (1.4): 'pulling the image via the server: 0.80 of 1.90 GiB (42%), layer 3 of 7', then 'pulling the
    image via the server: loading it'."""
    if s.get("via") == "server":
        if s.get("unpacking"):
            return f"pulling the image via the server: loading it, {s.get('layers', 0)} layers"
        total = int(s.get("total") or 0)
        done = min(int(s.get("done") or 0), total) if total else int(s.get("done") or 0)
        where = f", layer {s['layer']} of {s['layers']}" if s.get("layer") and s.get("layers") else ""
        if total:
            return f"pulling the image via the server: {gib(done)} of {gib(total)} GiB ({100 * done // total}%){where}"
        return f"pulling the image via the server{where}"
    if s.get("unpacking"):
        return f"pulling the image: unpacking, {s['layers_done']} of {s['layers']} layers"
    if s.get("total"):
        done = min(s["done"], s["total"])
        return f"pulling the image: {gib(done)} of {gib(s['total'])} GiB ({100 * done // s['total']}%)"
    if s.get("done"):
        return f"pulling the image: {gib(s['done'])} GiB, {s['layers_done']} of {s['layers']} layers"
    if s.get("layers"):
        return f"pulling the image: {s['layers_done']} of {s['layers']} layers"
    return "pulling the image"


class _Throttle:
    """Report at most every REPORT_EVERY seconds; a change held back is sent by the watchdog's next
    tick, so the last state always arrives, also when the stream then goes quiet."""

    def __init__(self, report: Optional[Report], layers: Layers):
        self.report, self.layers, self.last, self.sent = report, layers, 0.0, -1
        self._lock = threading.Lock()

    def __call__(self, force: bool = False) -> None:
        if self.report is None:
            return
        with self._lock:
            now = time.monotonic()
            if not force and (self.layers.version == self.sent or now - self.last < REPORT_EVERY):
                return
            self.last, self.sent = now, self.layers.version
            self.report(self.layers.snapshot())


def _limit(dog: Watchdog, layers: Layers, stall: float) -> None:
    dog.limit = stall * UNPACK_FACTOR if layers.snapshot()["unpacking"] else stall


def _stalled(stall: float, layers: Layers) -> PullStalled:
    minutes = stall / 60
    span = f"{minutes:g} minute{'s' if minutes != 1 else ''}" if minutes >= 1 else f"{stall:g} s"
    where = layers.where()
    return PullStalled(f"the image pull made no progress for {span}{f' ({where})' if where else ''}; a slow pull "
                     "that keeps moving is never stopped (FORGEYARD_PULL_STALL_MINUTES sets how long to wait)")


# ------------------------------------------------------------------ the engine's API
class _UnixHTTP(http.client.HTTPConnection):
    def __init__(self, path: str, timeout: float):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self) -> None:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(self._path)
        except OSError:
            s.close()
            raise
        self.sock = s


def _abort_socket(conn: http.client.HTTPConnection, held: dict) -> Callable[[], None]:
    def abort() -> None:
        from forgeyard.models.fetch import response_socket

        sock = response_socket(conn, held.get("resp"))
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    return abort


def _message(body: bytes) -> str:
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return body.decode("utf-8", "replace").strip()[:300]
    if isinstance(data, dict):
        return str(data.get("message") or data.get("error") or data)[:300]
    return str(data)[:300]


def _needs_credentials(text: str) -> bool:
    low = text.lower()
    return any(h in low for h in AUTH_HINTS)


def api_pull(socket_path: str, image: str, *, report: Optional[Report] = None,
             cancel: Optional[threading.Event] = None, stall: float = 300.0, connect_timeout: float = 10.0) -> None:
    """Pull through the engine API on a Unix socket. Raises Unreachable, NeedsCredentials, PullCancelled
    or PullError; returns once the engine says the pull is over."""
    cancel = cancel or threading.Event()
    name, tag = pull_ref(image)
    conn = _UnixHTTP(socket_path, connect_timeout)
    layers = Layers()
    throttle = _Throttle(report, layers)
    try:
        try:
            conn.connect()
        except OSError as e:
            raise Unreachable(f"{socket_path}: {e.strerror or e}") from None
        if cancel.is_set():
            raise PullCancelled()
        held: dict = {}
        with Watchdog(_abort_socket(conn, held), cancel, limit=stall, tick=throttle) as dog:
            try:
                conn.request("POST", "/images/create?" + urlencode({"fromImage": name, "tag": tag}),
                             headers={"Content-Length": "0"})
                conn.sock.settimeout(None)  # the watchdog decides when waiting is over
                resp = held["resp"] = conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                if dog.reason == CANCELLED:
                    raise PullCancelled() from None
                if dog.reason == STALLED:
                    raise _stalled(stall, layers) from None
                raise Unreachable(f"{socket_path}: {e}") from None
            if resp.status >= 400:
                text = _message(resp.read(8192))
                if resp.status in (401, 403, 404) or _needs_credentials(text):
                    raise NeedsCredentials(text)
                raise PullError(f"the pull failed (HTTP {resp.status}): {text}")
            throttle(force=True)
            buf = b""
            error: Optional[str] = None
            try:
                while True:
                    chunk = resp.read1(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        error = error or _feed(line, layers, dog)
                    if len(buf) > LINE_MAX:
                        buf = b""  # never one event: not a stream this code knows
                    _limit(dog, layers, stall)
                    throttle()
                error = error or _feed(buf, layers, dog)
            except (OSError, http.client.HTTPException, ValueError) as e:
                if dog.reason is None and not cancel.is_set():
                    where = layers.where()
                    raise PullError(f"the connection to the engine broke{f' ({where})' if where else ''}: {e}") from None
            if dog.reason == CANCELLED or cancel.is_set():
                raise PullCancelled()
            if dog.reason == STALLED:
                raise _stalled(stall, layers)
            if error:
                if _needs_credentials(error):
                    raise NeedsCredentials(error)
                where = layers.where()
                raise PullError(f"the pull failed{f' {where}' if where else ''}: {error}")
            throttle(force=True)
    finally:
        conn.close()


def _feed(line: bytes, layers: Layers, dog: Watchdog) -> Optional[str]:
    line = line.strip()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    if not isinstance(msg, dict):
        return None
    if msg.get("error") or msg.get("errorDetail"):
        detail = msg.get("errorDetail") if isinstance(msg.get("errorDetail"), dict) else {}
        return str(msg.get("error") or detail.get("message") or "error")[:300]
    if layers.feed(msg):
        dog.moved()
    return None


# ------------------------------------------------------------------ the CLI
def cli_layers(line: str, layers: Layers) -> bool:
    """One line of `docker pull` / `podman pull` without a terminal; True when the pull moved.

    docker: '<id>: Pulling fs layer', '<id>: Download complete', '<id>: Pull complete',
            '<id>: Already exists'
    podman: 'Copying blob sha256:<id>', 'Copying blob <id> done', 'Copying blob <id> skipped: already exists'
    """
    text = line.strip()
    if not text:
        return False
    if text.startswith("Copying blob "):
        rest = text.removeprefix("Copying blob ").split()
        if not rest:
            return True
        lid = rest[0].removeprefix("sha256:")[:64]
        tail = " ".join(rest[1:])
        status = ("Already exists" if "already exists" in tail else "Pull complete" if tail.startswith("done")
                  else "Pulling fs layer")
        return layers.feed({"id": lid, "status": status}) or True
    lid, sep, status = text.partition(": ")
    if sep and lid and " " not in lid and all(c in "0123456789abcdef" for c in lid.lower()):
        return layers.feed({"id": lid, "status": status.strip()}) or True
    return True  # any other line is the engine talking: it is alive


def cli_pull(argv: list[str], env: dict, *, report: Optional[Report] = None,
             cancel: Optional[threading.Event] = None, stall: float = 300.0) -> tuple[int, str]:
    """Run the CLI's pull; (exit code, the end of its output). Raises PullCancelled, or PullError on a
    stall. The process (and its group) is ended when it is cancelled or stalls."""
    cancel = cancel or threading.Event()
    layers = Layers()
    throttle = _Throttle(report, layers)
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                env=env, start_new_session=True)
    except OSError as e:
        return 127, str(e)

    def end() -> None:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                proc.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                continue

    tail: list[str] = []
    assert proc.stdout is not None
    with Watchdog(end, cancel, limit=stall, tick=throttle) as dog:
        throttle(force=True)
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            tail = (tail + [line])[-20:]
            if cli_layers(line, layers):
                dog.moved()
            _limit(dog, layers, stall)
            throttle()
        code = proc.wait()
    proc.stdout.close()
    if dog.reason == CANCELLED or cancel.is_set():
        raise PullCancelled()
    if dog.reason == STALLED:
        raise _stalled(stall, layers)
    throttle(force=True)
    return code, "\n".join(tail)[-600:]
