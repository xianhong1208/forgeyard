"""The agent's model cache (`--model-cache DIR`): models and process engines, downloaded once per
machine and shared by every replica there.

- **Keyed by source and revision**, laid out like the Hugging Face cache (see
  forgeyard.models.sources), so engines that read one find it as is.
- **Downloads** use the standard library only, through the same SSRF guard as the server (https,
  public addresses only unless `--allow-private-model-hosts`). They resume (HTTP Range), retry,
  check the free disk space first, and verify every file: SHA-256 for large (LFS) files, the git
  blob id for small ones, the published SHA-256 for engines. Nothing unverified is ever used.
- **Progress** is reported per replica: the bytes of that model really on disk (finished files and
  partial ones) of the total, never above it. A download is cancelled as soon as nobody wants it any
  more: the cancel ends a read that is waiting on the network, a retry's pause, a checksum and a wait
  for another replica's download of the same model, and releases the model's lock.
- **Read-only**: finished files are mode 0444 and directories 0755; containers get the model's
  directory mounted read-only, and nothing else of the host.
- A Hugging Face token arrives with the desired state, is used for these requests only and is
  never written to disk or logged.
- **Listing and removing** (1.4, "cache_admin"): `info()` reports each model with its size (a
  revision, and each GGUF file of it; a single-file model; an engine archive) and the partial
  downloads. `remove()` deletes one, like `hf cache rm`: the revision's links, its marker and its refs,
  and only the blobs no other revision links to; `prune_partial()` the partial downloads. Neither
  runs while a download of the same files is in progress (a download holds a shared lock that a
  removal takes exclusively, without waiting), nor while a replica here uses them (asked of the
  supervisor while that lock is held). The key from the server is only looked up among what this
  agent found on its own disk: no path is ever made from it.
"""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import json
import logging
import os
import posixpath
import re
import shutil
import stat
import tarfile
import threading
import zipfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from forgeyard.agent.watchdog import Watchdog
from forgeyard.models import cache_keys, fetch, sources

log = logging.getLogger("forgeyard.agent")

CHUNK = 1024 * 1024
RETRIES = 5
LOCK_POLL = 0.2
DISK_MARGIN = 1024**3
MANIFEST_DIR = ".forgeyard"
# engines/<name>-<sha256[:16]>, as ensure_engine unpacks them (not its .tmp-… directories).
ENGINE_DIR_RE = re.compile(r"^[A-Za-z0-9._-]+-([0-9a-f]{16})$")

Progress = Callable[[str, int, int, Optional[str]], None]   # phase, done, total, file


class CacheError(Exception):
    """The model or engine cannot be made available; the message is shown on the replica."""


class CacheRefused(CacheError):
    """A removal that was not done: `code` is "not_found", "busy" or "in_use" (with the users)."""

    def __init__(self, code: str, message: str, users=()):
        super().__init__(message)
        self.code = code
        self.users = list(users)


# What the cache listing may take of a status message (the server accepts 512 KiB in all): the
# biggest models are kept, the rest summarised as "truncated".
DETAIL_BUDGET = 192 * 1024
MAX_ENTRIES = 256
MAX_PARTS = 64
URL_DIR_RE = re.compile(r"^[0-9a-f]{24}$")


def tree_bytes(path: Path) -> int:
    """Bytes of the files under a directory (links counted as links, never followed)."""
    total = 0
    for top, dirs, files in os.walk(path):
        for name in files + [d for d in dirs if os.path.islink(os.path.join(top, d))]:
            try:
                total += os.lstat(os.path.join(top, name)).st_size
            except OSError:
                pass
    return total


def _inside(path: Path, root: Path) -> bool:
    """`path` (not followed if it is a link) is below `root`, through no linked directory."""
    parent = os.path.realpath(path.parent)
    top = os.path.realpath(root)
    return (parent == top or parent.startswith(top + os.sep)) and path.name not in ("", ".", "..")


def _fit(detail: dict) -> dict:
    """The listing, within DETAIL_BUDGET once serialised: the biggest models first, at most
    MAX_ENTRIES of them and MAX_PARTS files each; `truncated` when something was left out."""
    models = sorted(detail["models"], key=lambda e: (-e["bytes"], e["key"]))
    cut = len(models) > MAX_ENTRIES
    models = models[:MAX_ENTRIES]
    for e in models:
        if len(e.get("parts") or []) > MAX_PARTS:
            e["parts"] = sorted(e["parts"], key=lambda p: (-p["bytes"], p["file"]))[:MAX_PARTS]
            cut = True
    detail = {**detail, "models": models, "engines_detail": detail["engines_detail"][:MAX_ENTRIES]}
    while models and len(json.dumps(detail)) > DETAIL_BUDGET:
        models.pop()
        cut = True
    if cut:
        detail["truncated"] = True
    return detail


MAX_UNZIPPED = 16 * 1024**3


def unzip(archive: Path, dest: Path) -> None:
    """Unpack a .zip engine release (1.3: stable-diffusion.cpp publishes only .zip) as safely as the
    tar.gz path does: relative paths inside `dest` only, no links (a zip has none that Python makes),
    regular files and directories only, the executable bit kept from the archive's Unix mode, and at
    most MAX_UNZIPPED bytes in all."""
    total = 0
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            name = info.filename
            try:
                rel = sources.safe_relpath(name.rstrip("/"))
            except sources.SourceError:
                raise CacheError(f"the engine archive has an unsafe path {name!r}") from None
            mode = (info.external_attr >> 16) & 0o177777
            if mode and stat.S_ISLNK(mode):
                raise CacheError(f"the engine archive has a link {name!r}")
            target = dest / rel
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            total += info.file_size
            if total > MAX_UNZIPPED:
                raise CacheError("the engine archive unpacks to more than 16 GiB")
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, CHUNK)
            os.chmod(target, 0o755 if mode & 0o111 else 0o644)


class Cancelled(CacheError):
    pass


def _hex(value, n: int) -> Optional[str]:
    v = str(value or "").lower()
    return v if len(v) == n and all(c in "0123456789abcdef" for c in v) else None


def validate_model(model: dict, local_dirs: list[str]) -> dict:
    """The model block from the server, checked: never trust a path the server sends."""
    kind = model.get("kind")
    out: dict = {"kind": kind, "file": None}
    if kind == "hf":
        repo = str(model.get("repo") or "")
        sources.hf_dir(repo)
        rev = _hex(model.get("revision"), 40)
        if not rev:
            raise CacheError("the model is not pinned to a commit")
        files = []
        for f in model.get("files") or []:
            files.append({"path": sources.safe_relpath(str(f.get("path"))), "size": max(0, int(f.get("size") or 0)),
                          "sha256": _hex(f.get("sha256"), 64), "oid": _hex(f.get("oid"), 40)})
        if not files:
            raise CacheError("the model has no files to download")
        ref = str(model.get("ref") or "")
        out.update(repo=repo, revision=rev, files=files, endpoint=str(model.get("endpoint") or "https://huggingface.co"),
                   ref=ref if sources.REF_RE.match(ref) and ".." not in ref and not _hex(ref, 40) else None)
        if model.get("file"):
            out["file"] = sources.safe_relpath(str(model["file"]))
    elif kind == "url":
        url = str(model.get("url") or "")
        src = sources.parse_source(url)
        if src.kind != "url":
            raise CacheError("bad model URL")
        files = model.get("files") or [{}]
        # The SHA-256 the server recorded after the first complete download of this URL (1.4, D8), if any.
        out.update(url=url, size=max(0, int(files[0].get("size") or 0)), sha256=_hex(files[0].get("sha256"), 64))
    elif kind == "local":
        path = posixpath.normpath(str(model.get("path") or ""))
        if not sources.local_allowed(path, local_dirs):
            raise CacheError(f"{path} is not under a directory this agent allows for local models "
                             "(--model-local-dir)")
        out.update(path=path)
        if model.get("file"):
            out["file"] = sources.safe_relpath(str(model["file"]))
    else:
        raise CacheError(f"unknown model source {kind!r}")
    return out


def _check(cancel: Optional[threading.Event]) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled("cancelled")


def git_blob_id(path: Path, cancel: Optional[threading.Event] = None) -> str:
    h = hashlib.sha1(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            _check(cancel)
            h.update(chunk)
    return h.hexdigest()


class ModelCache:
    def __init__(self, root: Path, *, allow_private: bool = False, local_dirs: Optional[list[str]] = None,
                 resolver: Optional[fetch.Resolver] = None, disk_usage=shutil.disk_usage):
        self.root = Path(root).resolve()
        self.allow_private = allow_private
        self.local_dirs = [str(d) for d in (local_dirs or [])]
        self._resolver = resolver
        self._disk_usage = disk_usage
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()
        self.error: Optional[str] = None
        try:
            for d in (self.root, self.root / "hub", self.root / "url", self.root / "engines", self.root / ".partial",
                      self.root / ".locks"):
                d.mkdir(parents=True, exist_ok=True)
            os.chmod(self.root, 0o755)
            for d in (".partial", ".locks"):
                os.chmod(self.root / d, 0o700)
        except OSError as e:
            self.error = f"cannot use {self.root}: {e.strerror or e}"

    # ------------------------------------------------------------------ what the server sees
    def policy(self) -> fetch.Policy:
        p = fetch.Policy(allow_private=self.allow_private, timeout=60.0)
        if self._resolver is not None:
            p.resolver = self._resolver
        return p

    def cached_keys(self) -> list[str]:
        keys = []
        for manifest in sorted((self.root / "hub").glob(f"models--*/{MANIFEST_DIR}/*.json")):
            base = f"hub/{manifest.parent.parent.name}@{manifest.stem}"
            try:
                files = [f["path"] for f in json.loads(manifest.read_text()).get("files") or []]
            except (OSError, ValueError):
                continue
            # The commit, and each GGUF file of it (a deployment of one GGUF file needs only that).
            keys.append(base)
            keys += [f"{base}/{f}" for f in files if f.lower().endswith(".gguf")]
        for manifest in sorted((self.root / "url").glob(f"*/{MANIFEST_DIR}.json")):
            keys.append(f"url/{manifest.parent.name}")
        return keys

    def info(self) -> dict:
        if self.error:
            return {"ok": False, "reason": self.error, "cached": []}
        try:
            du = self._disk_usage(self.root)
            free, total = du.free, du.total
        except OSError:
            free = total = None
        return {"ok": True, "free_bytes": free, "total_bytes": total, "cached": self.cached_keys(),
                "engines": self.cached_engines(), **self.detail()}

    def detail(self) -> dict:
        """Per model, its size on disk (1.4): what the machine page lists, and what Remove offers.

        - `models`: a Hugging Face revision (`bytes` = its files, as `hf cache ls` counts them: a blob
          shared with another revision counts in both), with `parts` for its GGUF files when it has
          any (each removable alone; split files once, with all their parts); a single-file model.
        - `engines_detail`: the engine archives unpacked here.
        - `used_bytes`: what the cache really takes on disk (shared blobs once), `partial_bytes` and
          `partial_files`: what unfinished downloads left behind.
        """
        models = []
        for e in self._entries():
            out = {k: v for k, v in e.items() if not k.startswith("_")}
            if "parts" in out:
                out["parts"] = [{k: v for k, v in p.items() if k != "paths"} for p in out["parts"]]
            models.append(out)
        engines = [{"key": cache_keys.engine_key(sha16), "id": sha16, "name": name[:64], "bytes": tree_bytes(d)}
                   for d, sha16, name in self._engine_dirs()]
        partial = self._partials()
        partial_bytes = sum(n for _, n in partial)
        used = sum(tree_bytes(self.root / d) for d in ("hub", "url", "engines")) + sum(
            n for p, n in partial if p.parent == self.root / ".partial")
        return _fit({"models": models, "engines_detail": engines, "used_bytes": used, "partial_bytes": partial_bytes,
                     "partial_files": len(partial)})

    def _entries(self) -> list[dict]:
        """The models with a marker, found on this disk (the only list a removal key is looked up in).
        Keys starting with "_" are the agent's own paths and never leave it."""
        out = []
        for marker in sorted((self.root / "hub").glob(f"models--*/{MANIFEST_DIR}/*.json")):
            rev, base = marker.stem, marker.parent.parent
            if not sources.REVISION_RE.match(rev) or base.is_symlink():
                continue
            try:
                data = json.loads(marker.read_text())
            except (OSError, ValueError):
                continue
            snap = base / "snapshots" / rev
            sizes: dict[str, int] = {}
            missing = 0
            for f in data.get("files") or []:
                try:
                    path = sources.safe_relpath(str(f.get("path")))
                except (sources.SourceError, AttributeError):
                    continue
                try:
                    sizes[path] = (snap / path).stat().st_size
                except OSError:
                    sizes[path], missing = 0, missing + 1
            key = f"hub/{base.name}@{rev}"
            repo = data.get("repo") if isinstance(data.get("repo"), str) else None
            if not repo or _hf_dir_or_none(repo) != f"hub/{base.name}":
                repo = base.name.removeprefix("models--").replace("--", "/", 1)
            entry = {"key": key, "kind": "hf", "repo": repo[:200], "revision": rev, "bytes": sum(sizes.values()),
                     "files": len(sizes), "completed_at": _number(data.get("completed_at")),
                     "_base": base, "_marker": marker}
            if missing:
                entry["missing"] = missing
            parts = []
            for c in sources.gguf_choices([{"path": p, "size": n} for p, n in sizes.items()]):
                paths = sorted(p for p in sizes if cache_keys.same_choice(p, c["path"]))
                parts.append({"key": f"{key}/{c['path']}", "file": c["path"], "bytes": sum(sizes[p] for p in paths),
                              "paths": paths})
            if parts:
                entry["parts"] = parts
            out.append(entry)
        for marker in sorted((self.root / "url").glob(f"*/{MANIFEST_DIR}.json")):
            d = marker.parent
            if not URL_DIR_RE.match(d.name) or d.is_symlink():
                continue
            try:
                data = json.loads(marker.read_text())
            except (OSError, ValueError):
                continue
            files = [p for p in d.iterdir() if p.name != marker.name and (p.is_file() or p.is_symlink())]
            name = data.get("file") if isinstance(data.get("file"), str) else None
            entry = {"key": f"url/{d.name}", "kind": "url", "file": (name or "")[:200] or None,
                     "bytes": sum(p.lstat().st_size for p in files), "files": len(files),
                     "completed_at": _number(data.get("completed_at")), "_dir": d}
            if _hex(data.get("sha256"), 64):
                entry["sha256"] = data["sha256"]  # 1.4 (D8): computed after the download
            out.append(entry)
        return out

    def _engine_dirs(self) -> list[tuple[Path, str, str]]:
        """(directory, first 16 hex digits of the archive's SHA-256, name) of each unpacked engine."""
        try:
            dirs = sorted(p for p in (self.root / "engines").iterdir() if p.is_dir() and not p.is_symlink())
        except OSError:
            return []
        out = []
        for d in dirs:
            m = ENGINE_DIR_RE.match(d.name)
            if m:
                out.append((d, m.group(1), d.name[: -len(m.group(1)) - 1]))
        return out

    def _partials(self) -> list[tuple[Path, int]]:
        """What unfinished downloads left: files in .partial, and engine directories half unpacked."""
        out = []
        roots = [(self.root / ".partial", lambda p: True), (self.root / "engines", lambda p: p.name.startswith(".tmp-"))]
        for root, wanted in roots:
            try:
                items = sorted(root.iterdir())
            except OSError:
                continue
            for p in items:
                if not wanted(p):
                    continue
                try:
                    out.append((p, tree_bytes(p) if p.is_dir() and not p.is_symlink() else p.lstat().st_size))
                except OSError:
                    pass
        return out

    def cached_engines(self) -> list[str]:
        """The engine archives unpacked here, by the first 16 hex digits of their SHA-256 (1.4: placement
        prefers a machine that has the engine a replica needs)."""
        try:
            names = sorted(p.name for p in (self.root / "engines").iterdir() if p.is_dir())
        except OSError:
            return []
        return [m.group(1) for m in (ENGINE_DIR_RE.match(n) for n in names) if m][:256]

    # ------------------------------------------------------------------ locking
    @contextmanager
    def _locked(self, key: str, cancel: Optional[threading.Event] = None):
        """The model's lock, for this agent's threads and for another agent sharing this cache
        directory. Waiting for it ends when `cancel` is set (a replica nobody wants any more must not
        stay queued behind another one's download)."""
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        while not lock.acquire(timeout=LOCK_POLL):
            _check(cancel)
        try:
            path = self.root / ".locks" / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".lock")
            with open(path, "w") as fh:
                while True:
                    try:
                        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        _check(cancel)
                        time.sleep(LOCK_POLL)
                try:
                    yield
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            lock.release()

    def busy(self, key: str) -> bool:
        with self._guard:
            lock = self._locks.get(key)
        return bool(lock and lock.locked())

    def _rw_path(self, name: str) -> Path:
        return self.root / ".locks" / (hashlib.sha256(name.encode()).hexdigest()[:24] + ".rw")

    @contextmanager
    def _shared(self, name: str, cancel: Optional[threading.Event] = None):
        """Held by a download for as long as it runs (a flock, so it also holds against another agent
        sharing this directory, and between this agent's threads). A removal of the same files takes
        it exclusively; it never waits for one, so a removal only ever delays a download a moment."""
        with open(self._rw_path(name), "a") as fh:
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    _check(cancel)
                    time.sleep(LOCK_POLL)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    @contextmanager
    def _exclusive(self, name: str):
        """The removal's side of `_shared`: True when no download holds it, without waiting."""
        with open(self._rw_path(name), "a") as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            try:
                yield True
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    @staticmethod
    def _files_lock(model: dict) -> str:
        """The lock over one repository's (or one URL model's) files on disk: every revision of a
        repository shares its blobs, so a removal of one revision must not run beside a download of
        another."""
        if model["kind"] == "hf":
            return "repo:" + sources.hf_dir(model["repo"])
        return "url:" + sources.url_dir(model["url"])

    # ------------------------------------------------------------------ paths
    def paths(self, model: dict) -> dict:
        """Host paths of a validated model: {mount, path, file, hub}."""
        lay = sources.layout(model)
        if model["kind"] == "local":
            return {"mount": lay["mount"], "path": lay["path"], "file": lay["file"], "hub": None}
        r = str(self.root)
        return {"mount": f"{r}/{lay['mount']}", "path": f"{r}/{lay['path']}",
                "file": f"{r}/{lay['file']}" if lay["file"] else None, "hub": f"{r}/hub"}

    def is_cached(self, model: dict) -> bool:
        if model["kind"] == "local":
            return True
        if model["kind"] == "hf":
            m = self.root / sources.hf_dir(model["repo"]) / MANIFEST_DIR / f"{model['revision']}.json"
            try:
                have = {f["path"] for f in json.loads(m.read_text()).get("files") or []}
            except (OSError, ValueError):
                return False
            # Complete for this model only if every file it needs is there (another catalog entry
            # may have fetched a different GGUF file of the same commit).
            return all(f["path"] in have for f in model["files"])
        m = self.root / sources.url_dir(model["url"]) / f"{MANIFEST_DIR}.json"
        return m.is_file()

    # ------------------------------------------------------------------ downloading
    def _check_space(self, need: int) -> None:
        try:
            free = self._disk_usage(self.root).free
        except OSError:
            return
        if need + DISK_MARGIN > free:
            raise CacheError(f"not enough disk space in the model cache: needs {need / 1024**3:.1f} GiB "
                             f"(+1 GiB margin), {free / 1024**3:.1f} GiB free")

    def _fetch(self, url: str, dest: Path, size: int, *, headers: dict, progress: Progress, phase: str,
               base: int, total: int, label: str, cancel: threading.Event, sha256: Optional[str] = None) -> None:
        """Download url into dest (resuming a partial file), reporting base+done of total (never more
        than total). A cancel ends the transfer at once, even a read waiting on a stalled connection."""
        attempt = 0
        while True:
            _check(cancel)
            have = dest.stat().st_size if dest.exists() else 0
            if size and have > size:
                dest.unlink()
                have = 0
            if size and have == size:
                return
            h = {**headers}
            if have:
                h["Range"] = f"bytes={have}-"
            try:
                with fetch.open_url(url, self.policy(), headers=h) as resp, Watchdog(resp.abort, cancel):
                    if resp.status == 416 and size and have >= size:
                        return
                    if resp.status >= 400:
                        if resp.status in (401, 403):
                            raise CacheError(f"{label}: access refused (HTTP {resp.status}); a gated model needs a "
                                             "Hugging Face token (the server's, under Models -> Model sources, "
                                             "or the deployment's own)")
                        raise fetch.FetchError(f"HTTP {resp.status}", resp.status)
                    if have and resp.status != 206:
                        have = 0  # the server ignored Range: start over
                    if not total:
                        length = resp.headers.get("Content-Length")
                        total = have + int(length) if length and length.isdigit() else 0
                    mode = "ab" if have else "wb"
                    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | (os.O_APPEND if have else os.O_TRUNC), 0o600)
                    with os.fdopen(fd, mode) as out:
                        done = have
                        last = 0.0
                        while True:
                            _check(cancel)
                            chunk = resp.read1(CHUNK)
                            _check(cancel)  # an aborted read ends like the end of the body
                            if not chunk:
                                break
                            out.write(chunk)
                            done += len(chunk)
                            now = time.monotonic()
                            if now - last >= 0.5:
                                last = now
                                progress(phase, min(base + done, total) if total else base + done, total, label)
                if size and dest.stat().st_size != size:
                    raise fetch.FetchError(f"incomplete ({dest.stat().st_size} of {size} bytes)")
                return
            except (fetch.FetchError, OSError, http.client.HTTPException) as e:
                _check(cancel)  # the cancel itself broke the connection
                if isinstance(e, fetch.BlockedAddress):
                    raise CacheError(f"{label}: {e}") from None
                attempt += 1
                if attempt > RETRIES:
                    raise CacheError(f"{label}: download failed: {e}") from None
                log.warning("Download of %s failed (%s); retrying (%d of %d).", label, e, attempt, RETRIES)
                if cancel.wait(min(30, 2 ** attempt)):
                    raise Cancelled("cancelled")

    @staticmethod
    def _sha256(path: Path, cancel: Optional[threading.Event] = None) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK), b""):
                _check(cancel)  # a large file takes many seconds: a cancel must not wait for it
                h.update(chunk)
        return h.hexdigest()

    def _verify(self, part: Path, f: dict, label: str, cancel: Optional[threading.Event] = None) -> None:
        if f.get("sha256"):
            got = self._sha256(part, cancel)
            if got != f["sha256"]:
                part.unlink(missing_ok=True)
                raise CacheError(f"{label}: SHA-256 mismatch (got {got[:12]}…, expected {f['sha256'][:12]}…)")
        elif f.get("oid"):
            got = git_blob_id(part, cancel)
            if got != f["oid"]:
                part.unlink(missing_ok=True)
                raise CacheError(f"{label}: content does not match its git blob id")

    def ensure_model(self, model: dict, *, token: Optional[str] = None, progress: Optional[Progress] = None,
                     cancel: Optional[threading.Event] = None) -> dict:
        """Make the model available in the cache (downloading what is missing); its host paths."""
        progress = progress or (lambda *a: None)
        cancel = cancel or threading.Event()
        if self.error:
            raise CacheError(self.error)
        if model["kind"] == "local":
            p = Path(model["path"])
            if not p.is_dir():
                raise CacheError(f"{p} does not exist on this machine")
            if model.get("file") and not (p / model["file"]).is_file():
                raise CacheError(f"{p / model['file']} does not exist on this machine")
            return self.paths(model)
        key = sources.cache_key(model)
        with self._locked(key, cancel), self._shared("partial", cancel), self._shared(self._files_lock(model), cancel):
            if not self.is_cached(model):
                if model["kind"] == "hf":
                    self._download_hf(model, token, progress, cancel)
                else:
                    self._download_url(model, progress, cancel)
        return self.paths(model)

    def _download_hf(self, model: dict, token: Optional[str], progress: Progress, cancel: threading.Event) -> None:
        base = self.root / sources.hf_dir(model["repo"])
        blobs = base / "blobs"
        snap = base / "snapshots" / model["revision"]
        for d in (base, blobs, base / "snapshots", snap, base / "refs", base / MANIFEST_DIR):
            d.mkdir(parents=True, exist_ok=True)
            os.chmod(d, 0o755)
        files = model["files"]
        total = sum(f["size"] for f in files)
        missing = [f for f in files if not (blobs / (f["sha256"] or f["oid"] or "")).is_file()]
        partial = sum((self.root / ".partial" / f"{f['sha256'] or f['oid']}.part").stat().st_size
                      for f in missing if (self.root / ".partial" / f"{f['sha256'] or f['oid']}.part").exists())
        self._check_space(sum(f["size"] for f in missing) - partial)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        ep = model["endpoint"].rstrip("/")

        def on_disk(f: dict) -> int:
            """The bytes of one file that are on disk now: all of a finished file, the partial part of
            one being fetched (a part longer than the file is thrown away before it is resumed)."""
            blob_id = f["sha256"] or f["oid"] or ""
            if (blobs / blob_id).is_file():
                return f["size"]
            part = self.root / ".partial" / f"{blob_id}.part"
            have = part.stat().st_size if blob_id and part.is_file() else 0
            return have if have <= f["size"] else 0

        progress("model", min(sum(on_disk(f) for f in files), total), total, None)
        for i, f in enumerate(files):
            blob_id = f["sha256"] or f["oid"]
            if not blob_id:
                raise CacheError(f"{f['path']}: no checksum to verify it against")
            blob = blobs / blob_id
            if not blob.is_file():
                part = self.root / ".partial" / f"{blob_id}.part"
                url = f"{ep}/{model['repo']}/resolve/{model['revision']}/{quote(f['path'])}"
                # What the other files have on disk, plus what this one has (the fetch counts it from
                # its own partial file): each byte once, so a resumed download never passes 100%.
                others = sum(on_disk(g) for j, g in enumerate(files) if j != i)
                self._fetch(url, part, f["size"], headers=headers, progress=progress, phase="model", base=others,
                            total=total, label=f["path"], cancel=cancel)
                self._verify(part, f, f["path"], cancel)
                os.chmod(part, 0o444)
                os.replace(part, blob)
            progress("model", min(sum(on_disk(g) for g in files), total), total, f["path"])
            link = snap / f["path"]
            link.parent.mkdir(parents=True, exist_ok=True)
            target = os.path.relpath(blob, link.parent)
            if link.is_symlink() or link.exists():
                if os.readlink(link) == target if link.is_symlink() else False:
                    continue
                link.unlink()
            os.symlink(target, link)
        for d in sorted({p.parent for p in snap.rglob("*")} | {snap}):
            os.chmod(d, 0o755)
        if model.get("ref"):
            ref = base / "refs" / model["ref"]
            ref.parent.mkdir(parents=True, exist_ok=True)
            ref.write_text(model["revision"])  # no newline: huggingface_hub compares it as is
            os.chmod(ref, 0o644)
        manifest = base / MANIFEST_DIR / f"{model['revision']}.json"
        known = {}
        try:
            known = {f["path"]: f for f in json.loads(manifest.read_text()).get("files") or []}
        except (OSError, ValueError):
            pass
        known.update({f["path"]: {"path": f["path"], "size": f["size"]} for f in files})
        manifest.write_text(json.dumps({"repo": model["repo"], "revision": model["revision"],
                                        "files": sorted(known.values(), key=lambda f: f["path"]),
                                        "completed_at": time.time()}))
        os.chmod(manifest, 0o644)
        log.info("Cached %s@%s (%d files, %.2f GiB).", model["repo"], model["revision"][:12], len(files),
                 total / 1024**3)

    def _download_url(self, model: dict, progress: Progress, cancel: threading.Event) -> None:
        base = self.root / sources.url_dir(model["url"])
        base.mkdir(parents=True, exist_ok=True)
        os.chmod(base, 0o755)
        name = sources.url_filename(model["url"])
        part = self.root / ".partial" / f"{base.name}.part"
        size = model.get("size") or 0
        self._check_space(max(0, size - (part.stat().st_size if part.exists() else 0)))
        self._fetch(model["url"], part, size, headers={}, progress=progress, phase="model", base=0, total=size,
                    label=name, cancel=cancel)
        # No checksum is published for a file at a URL (1.4, D8): its SHA-256 is computed here and reported
        # (model_cache.models[].sha256); the server records the first one, and sends it with every later
        # download of the model, which must then match.
        got = self._sha256(part, cancel)
        want = model.get("sha256")
        if want and got != want:
            part.unlink(missing_ok=True)
            raise CacheError(f"{name}: SHA-256 mismatch (got {got[:12]}…, expected {want[:12]}…, recorded after "
                             "its first download): the file at this URL changed")
        os.chmod(part, 0o444)
        os.replace(part, base / name)
        manifest = base / f"{MANIFEST_DIR}.json"
        manifest.write_text(json.dumps({"url": model["url"], "file": name, "sha256": got, "completed_at": time.time()}))
        os.chmod(manifest, 0o644)

    # ------------------------------------------------------------------ engines (process variants)
    def ensure_engine(self, engine: dict, *, progress: Optional[Progress] = None,
                      cancel: Optional[threading.Event] = None) -> Path:
        """Download, verify and unpack a pinned engine archive; the absolute path of its program."""
        progress = progress or (lambda *a: None)
        cancel = cancel or threading.Event()
        sha = _hex(engine.get("sha256"), 64)
        url = str(engine.get("url") or "")
        binary = str(engine.get("binary") or "")
        name = "".join(c for c in str(engine.get("name") or "engine") if c.isalnum() or c in "._-")[:40] or "engine"
        if not sha or not (url.startswith("https://") or (self.allow_private and url.startswith("http://"))):
            raise CacheError("the engine must be an https:// archive pinned by its SHA-256")
        try:
            binary = sources.safe_relpath(binary)
        except sources.SourceError:
            raise CacheError("bad engine binary path") from None
        if self.error:
            raise CacheError(self.error)
        final = self.root / "engines" / f"{name}-{sha[:16]}"
        with (self._locked(f"engine:{sha}", cancel), self._shared("partial", cancel),
              self._shared(f"engine-dir:{sha[:16]}", cancel)):
            if not (final / binary).is_file():
                part = self.root / ".partial" / f"engine-{sha}.part"
                self._check_space(512 * 1024**2)
                self._fetch(url, part, 0, headers={}, progress=progress, phase="engine", base=0, total=0,
                            label=posixpath.basename(url), cancel=cancel)
                got = self._sha256(part, cancel)
                if got != sha:
                    part.unlink(missing_ok=True)
                    raise CacheError(f"engine archive SHA-256 mismatch (got {got[:12]}…)")
                tmp = self.root / "engines" / f".tmp-{sha[:16]}-{os.getpid()}"
                shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir()
                try:
                    if zipfile.is_zipfile(part):
                        unzip(part, tmp)
                    else:
                        with tarfile.open(part) as tar:
                            tar.extractall(tmp, filter="data")  # no absolute paths, no escaping links
                except (tarfile.TarError, zipfile.BadZipFile, CacheError, OSError) as e:
                    shutil.rmtree(tmp, ignore_errors=True)
                    raise CacheError(f"cannot unpack the engine: {e}") from None
                for p in [tmp, *tmp.rglob("*")]:
                    if p.is_symlink():
                        continue
                    if p.is_dir():
                        os.chmod(p, 0o755)
                    else:
                        os.chmod(p, 0o555 if os.access(p, os.X_OK) else 0o444)
                shutil.rmtree(final, ignore_errors=True)
                os.replace(tmp, final)
                part.unlink(missing_ok=True)
                log.info("Engine %s unpacked in %s.", name, final)
        exe = final / binary
        if not exe.is_file() or not os.access(exe, os.X_OK):
            raise CacheError(f"the engine archive has no program {binary}")
        return exe

    # ------------------------------------------------------------------ removing (1.4, cache_admin)
    def remove(self, key: str, in_use: Callable[[dict], list[str]] = lambda target: []) -> int:
        """Remove one model or engine from the cache; the bytes freed on disk. `in_use(target)` names
        the replicas here that need what the target would delete (asked while the lock is held, so no
        download can start using it in between); any name refuses the removal."""
        if self.error:
            raise CacheError(self.error)
        target = cache_keys.parse(key)
        if target is None:
            raise CacheRefused("not_found", "That is not a model in this machine's cache.")
        if target["kind"] == "engine":
            lock = f"engine-dir:{target['id']}"
        else:
            lock = ("repo:" if target["kind"] == "hf" else "url:") + target["dir"]
        with self._exclusive(lock) as free:
            if not free:
                raise CacheRefused("busy", "A download of this model is in progress on this machine. Try again "
                                           "once it has finished or been cancelled.")
            if target["kind"] == "engine":
                dirs = [d for d, sha16, _ in self._engine_dirs() if sha16 == target["id"]]
                if not dirs:
                    raise CacheRefused("not_found", "That engine is not in this machine's cache (any more).")
                self._refuse_if_used(in_use, target)
                freed = 0
                for d in dirs:
                    freed += tree_bytes(d)
                    shutil.rmtree(d)
            elif target["kind"] == "url":
                entry = next((e for e in self._entries() if e["key"] == key), None)
                if entry is None:
                    raise CacheRefused("not_found", "That model is not in this machine's cache (any more).")
                self._refuse_if_used(in_use, target)
                freed = tree_bytes(entry["_dir"])
                shutil.rmtree(entry["_dir"])
            else:
                base_key = f"{target['dir']}@{target['revision']}"
                entry = next((e for e in self._entries() if e["key"] == base_key), None)
                part = None
                if entry is not None and target.get("file"):
                    part = next((p for p in entry.get("parts") or [] if p["key"] == key), None)
                if entry is None or (target.get("file") and part is None):
                    raise CacheRefused("not_found", "That model is not in this machine's cache (any more).")
                # One GGUF file of a revision with others; the last one takes the revision with it (what
                # is left, a projector or a README, serves nothing on its own).
                paths = set(part["paths"]) if part is not None and len(entry["parts"]) > 1 else None
                self._refuse_if_used(in_use, {"kind": "hf", "dir": target["dir"], "revision": target["revision"],
                                              "paths": paths})
                freed = self._remove_hf(entry, paths)
        log.info("Removed %s from the model cache (%.2f GiB freed).", key, freed / 1024**3)
        return freed

    @staticmethod
    def _refuse_if_used(in_use: Callable[[dict], list[str]], target: dict) -> None:
        users = sorted(set(in_use(target)))
        if users:
            raise CacheRefused("in_use", f"In use by {', '.join(users)} on this machine. Stop it first.", users)

    def _remove_hf(self, entry: dict, paths: Optional[set[str]]) -> int:
        """Like `hf cache rm`: the revision's links (all, or `paths`), its marker and refs when it goes
        whole, and each blob they pointed at that no link of another revision (or of the files kept)
        still points at."""
        base: Path = entry["_base"]
        rev = entry["revision"]
        snap = base / "snapshots" / rev
        blobs = os.path.realpath(base / "blobs")
        if paths is None:
            links = [p for p in snap.rglob("*") if p.is_symlink() or p.is_file()] if snap.is_dir() and not snap.is_symlink() else []
        else:
            links = [snap / p for p in sorted(paths)]
        candidates: set[str] = set()
        freed = 0
        for link in links:
            if not _inside(link, snap):
                continue
            if link.is_symlink():
                real = os.path.realpath(link)
                if os.path.dirname(real) == blobs:
                    candidates.add(real)
                link.unlink()
            elif link.is_file():
                freed += link.lstat().st_size
                link.unlink()
        marker: Path = entry["_marker"]
        if paths is None:
            if snap.is_dir() and not snap.is_symlink():
                shutil.rmtree(snap)
            marker.unlink(missing_ok=True)
            refs = base / "refs"
            for ref in sorted(refs.rglob("*")) if refs.is_dir() else []:
                try:
                    if ref.is_file() and not ref.is_symlink() and ref.read_text().strip() == rev:
                        ref.unlink()
                except OSError:
                    pass
        else:
            try:
                data = json.loads(marker.read_text())
            except (OSError, ValueError):
                data = {}
            data["files"] = [f for f in data.get("files") or [] if isinstance(f, dict) and f.get("path") not in paths]
            tmp = marker.with_name(f".{marker.name}.{os.getpid()}.tmp")
            tmp.write_text(json.dumps(data))
            os.chmod(tmp, 0o644)
            os.replace(tmp, marker)
            for d in sorted({link.parent for link in links}, key=lambda p: len(p.parts), reverse=True):
                while d != snap and _inside(d, snap):
                    try:
                        d.rmdir()
                    except OSError:
                        break
                    d = d.parent
        still = {os.path.realpath(p) for p in (base / "snapshots").rglob("*") if p.is_symlink()} \
            if (base / "snapshots").is_dir() else set()
        for blob in sorted(candidates - still):
            try:
                freed += os.lstat(blob).st_size
                os.unlink(blob)
            except FileNotFoundError:
                pass
        self._drop_if_empty(base)
        return freed

    @staticmethod
    def _drop_if_empty(base: Path) -> None:
        """A repository with nothing left in it goes too (only empty directories are removed)."""
        for top, dirs, files in os.walk(base, topdown=False):
            if top == str(base):
                continue
            if not files:
                try:
                    os.rmdir(top)
                except OSError:
                    pass
        try:
            base.rmdir()
        except OSError:
            pass

    def prune_partial(self) -> int:
        """Remove what unfinished downloads left behind (they would resume from it); the bytes freed.
        Refused while any download runs here: one of them may be writing to it."""
        if self.error:
            raise CacheError(self.error)
        with self._exclusive("partial") as free:
            if not free:
                raise CacheRefused("busy", "A download is in progress on this machine. Remove the partial "
                                           "downloads once it has finished or been cancelled.")
            freed = 0
            for p, n in self._partials():
                try:
                    if p.is_dir() and not p.is_symlink():
                        shutil.rmtree(p)
                    else:
                        p.unlink()
                    freed += n
                except FileNotFoundError:
                    pass
        log.info("Removed the partial downloads from the model cache (%.2f GiB freed).", freed / 1024**3)
        return freed


def _hf_dir_or_none(repo: str) -> Optional[str]:
    try:
        return sources.hf_dir(repo)
    except sources.SourceError:
        return None


def _number(value) -> Optional[float]:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n == n and 0 <= n < 1e11 else None
