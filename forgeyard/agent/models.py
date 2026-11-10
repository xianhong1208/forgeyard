"""The agent's model cache (`--model-cache DIR`): models and process engines, downloaded once per
machine and shared by every replica there.

- **Keyed by source and revision**, laid out like the Hugging Face cache (see
  forgeyard.models.sources), so engines that read one find it as is.
- **Downloads** use the standard library only, through the same SSRF guard as the server (https,
  public addresses only unless `--allow-private-model-hosts`). They resume (HTTP Range), retry,
  check the free disk space first, and verify every file: SHA-256 for large (LFS) files, the git
  blob id for small ones, the published SHA-256 for engines. Nothing unverified is ever used.
- **Progress** is reported per replica (bytes done of the total), and a download is cancelled as
  soon as nobody wants it any more.
- **Read-only**: finished files are mode 0444 and directories 0755; containers get the model's
  directory mounted read-only, and nothing else of the host.
- A Hugging Face token arrives with the desired state, is used for these requests only and is
  never written to disk or logged.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import posixpath
import shutil
import tarfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote

from forgeyard.models import fetch, sources

log = logging.getLogger("forgeyard.agent")

CHUNK = 1024 * 1024
RETRIES = 5
DISK_MARGIN = 1024**3
MANIFEST_DIR = ".forgeyard"

Progress = Callable[[str, int, int, Optional[str]], None]   # phase, done, total, file


class CacheError(Exception):
    """The model or engine cannot be made available; the message is shown on the replica."""


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
        out.update(url=url, size=max(0, int(files[0].get("size") or 0)))
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


def git_blob_id(path: Path) -> str:
    h = hashlib.sha1(f"blob {path.stat().st_size}\0".encode())
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
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
        return {"ok": True, "free_bytes": free, "total_bytes": total, "cached": self.cached_keys()}

    # ------------------------------------------------------------------ locking
    @contextmanager
    def _locked(self, key: str):
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            path = self.root / ".locks" / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".lock")
            with open(path, "w") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX)  # another agent sharing this cache directory
                try:
                    yield
                finally:
                    fcntl.flock(fh, fcntl.LOCK_UN)

    def busy(self, key: str) -> bool:
        with self._guard:
            lock = self._locks.get(key)
        return bool(lock and lock.locked())

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
        """Download url into dest (resuming a partial file), reporting base+done of total."""
        attempt = 0
        while True:
            if cancel.is_set():
                raise Cancelled("cancelled")
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
                with fetch.open_url(url, self.policy(), headers=h) as resp:
                    if resp.status == 416 and size and have >= size:
                        return
                    if resp.status >= 400:
                        if resp.status in (401, 403):
                            raise CacheError(f"{label}: access refused (HTTP {resp.status}); a gated model needs a "
                                             "Hugging Face token on the deployment")
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
                            if cancel.is_set():
                                raise Cancelled("cancelled")
                            chunk = resp.read1(CHUNK)
                            if not chunk:
                                break
                            out.write(chunk)
                            done += len(chunk)
                            now = time.monotonic()
                            if now - last >= 0.5:
                                last = now
                                progress(phase, base + done, total, label)
                if size and dest.stat().st_size != size:
                    raise fetch.FetchError(f"incomplete ({dest.stat().st_size} of {size} bytes)")
                return
            except (fetch.FetchError, OSError) as e:
                if isinstance(e, fetch.BlockedAddress):
                    raise CacheError(f"{label}: {e}") from None
                attempt += 1
                if attempt > RETRIES:
                    raise CacheError(f"{label}: download failed: {e}") from None
                log.warning("Download of %s failed (%s); retrying (%d of %d).", label, e, attempt, RETRIES)
                time.sleep(min(30, 2 ** attempt))

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(CHUNK), b""):
                h.update(chunk)
        return h.hexdigest()

    def _verify(self, part: Path, f: dict, label: str) -> None:
        if f.get("sha256"):
            got = self._sha256(part)
            if got != f["sha256"]:
                part.unlink(missing_ok=True)
                raise CacheError(f"{label}: SHA-256 mismatch (got {got[:12]}…, expected {f['sha256'][:12]}…)")
        elif f.get("oid"):
            got = git_blob_id(part)
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
        with self._locked(key):
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
        done = total - sum(f["size"] for f in missing)
        progress("model", done, total, None)
        for f in files:
            blob_id = f["sha256"] or f["oid"]
            if not blob_id:
                raise CacheError(f"{f['path']}: no checksum to verify it against")
            blob = blobs / blob_id
            if not blob.is_file():
                part = self.root / ".partial" / f"{blob_id}.part"
                url = f"{ep}/{model['repo']}/resolve/{model['revision']}/{quote(f['path'])}"
                self._fetch(url, part, f["size"], headers=headers, progress=progress, phase="model", base=done,
                            total=total, label=f["path"], cancel=cancel)
                self._verify(part, f, f["path"])
                os.chmod(part, 0o444)
                os.replace(part, blob)
            done += f["size"]
            progress("model", done, total, f["path"])
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
        os.chmod(part, 0o444)
        os.replace(part, base / name)
        manifest = base / f"{MANIFEST_DIR}.json"
        manifest.write_text(json.dumps({"url": model["url"], "file": name, "completed_at": time.time()}))
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
        with self._locked(f"engine:{sha}"):
            if not (final / binary).is_file():
                part = self.root / ".partial" / f"engine-{sha}.part"
                self._check_space(512 * 1024**2)
                self._fetch(url, part, 0, headers={}, progress=progress, phase="engine", base=0, total=0,
                            label=posixpath.basename(url), cancel=cancel)
                got = self._sha256(part)
                if got != sha:
                    part.unlink(missing_ok=True)
                    raise CacheError(f"engine archive SHA-256 mismatch (got {got[:12]}…)")
                tmp = self.root / "engines" / f".tmp-{sha[:16]}-{os.getpid()}"
                shutil.rmtree(tmp, ignore_errors=True)
                tmp.mkdir()
                try:
                    with tarfile.open(part) as tar:
                        tar.extractall(tmp, filter="data")  # no absolute paths, no escaping links
                except (tarfile.TarError, OSError) as e:
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
