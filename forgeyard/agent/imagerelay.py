"""An engine image through the server (1.4, issue #35 PR 4, D4; protocol feature "relay_images").

A desired replica or download whose image is pinned and recorded may carry `image_relay: {path, mode}`. When
the image is not on this machine and the machine must use the server (mode "server", or "auto" after the
pull failed to reach the registry), the agent:

1. GETs `<path>/manifest` from its own server: its platform's manifest, whose bytes must hash to one of the
   image's recorded digests (`image_ids.ids`), and whose config must be a recorded one too;
2. GETs the config and each layer from `<path>/blobs/<digest>` into a staging directory in the model cache
   (`.partial/image-<manifest digest>/`), through the model cache's own download code: resume from what is
   on disk, retries, a 429 waited out, cancel, progress. Each blob must hash to its digest (else it is
   deleted and the download fails), and the config's content id (layers + run configuration) must be a
   recorded one and its architecture this machine's;
3. streams an archive to `docker load` / `podman load` (write_archive): an OCI image layout (`oci-layout`,
   `index.json`, `blobs/sha256/…`, the manifest with its registry bytes, so its digest is kept) plus the
   classic `manifest.json` that names the same blobs. That is the layout `docker save` writes since Docker
   25: Docker's classic store reads `manifest.json` (and checks each layer against its diff id as it
   unpacks it, compressed or not), its containerd store and Podman read the OCI layout;
4. tags what was loaded with the recipe's `repo:tag`, and runs the #10 check (container.choose_image) on it:
   only an image whose ids match the recorded ones is ever run.

The staging directory is deleted once the image is loaded. A cancelled or failed transfer leaves it for the
next try to resume (the machine page's "Remove partial downloads" clears it).
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import platform as _platform
import shutil
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Optional

from forgeyard.workloads import images
from forgeyard.workloads.registry import INDEX_TYPES, MANIFEST_TYPES, parse_manifest, split_image, tag_ref

log = logging.getLogger("forgeyard.agent")

CONFIG_MAX = 4 * 1024 * 1024
MANIFEST_MAX = 4 * 1024 * 1024
OCI_INDEX = "application/vnd.oci.image.index.v1+json"
ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "armv7l": "arm",
        "s390x": "s390x", "ppc64le": "ppc64le", "riscv64": "riscv64"}
PATH_SUFFIX = "/image"


class ImageRelayError(Exception):
    """The image could not come through the server; the message says why (safe to show)."""


def machine_arch() -> Optional[str]:
    return ARCH.get(_platform.machine().lower())


@dataclass
class ImageRelay:
    """What the agent needs to fetch one image through its server."""
    path: str                      # RELAY_PATH/<replica|download>/<id>/image
    mode: str                      # "server" | "auto"
    client: object                 # agent.relay.RelayClient
    cache: object                  # agent.models.ModelCache (staging, locks, the download code)

    def host(self, image: str) -> str:
        """The registry a direct pull goes to (what "auto" remembers as unreachable)."""
        from forgeyard.models.reach import registry

        return registry(image) or ""

    def first(self, image: str) -> bool:
        """Through the server at once, without trying the registry first."""
        return self.mode == "server" or self.client.unreachable(self.host(image))


@dataclass
class Staged:
    image: str
    manifest: bytes
    manifest_digest: str
    media_type: str
    config_digest: str
    layers: list[str]              # layer digests in order (may repeat)
    dir: Path
    files: dict[str, Path] = field(default_factory=dict)   # digest -> file

    def blob(self, digest: str) -> Path:
        return self.files[digest]


def full_name(image: str) -> str:
    """The fully qualified name:tag (what containerd and Podman record): 'vllm/x:1' -> 'docker.io/vllm/x:1'."""
    registry, repo, tag, _ = split_image(image)
    host = "docker.io" if registry == "registry-1.docker.io" else registry
    return f"{host}/{repo}:{tag}"


class _Progress:
    """The pull-style snapshot (pull.Layers' shape) of a relayed image, reported at most every 0.5 s."""

    def __init__(self, report: Optional[Callable[[dict], None]], total: int, layers: int):
        self.report, self.total, self.layers = report, total, layers
        self.done_before = 0      # bytes of the blobs already finished
        self.layers_done = 0
        self.layer = 0            # which layer is coming (1-based; 0: the config)
        self.unpacking = False
        self.last = 0.0

    def snap(self, done: int) -> dict:
        out = {"done": min(done, self.total), "total": self.total, "layers": self.layers,
               "layers_done": self.layers_done, "via": "server", "layer": self.layer}
        if self.unpacking:
            out["unpacking"] = True
        return out

    def send(self, done: int, force: bool = False) -> None:
        if self.report is None:
            return
        now = time.monotonic()
        if force or now - self.last >= 0.5:
            self.last = now
            self.report(self.snap(done))


def _check_manifest(data: bytes, check: dict) -> tuple[str, dict]:
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    if digest not in set(check.get("ids") or []):
        raise ImageRelayError(f"the manifest the server sent ({digest[:19]}…) is not one of the image's recorded "
                              "digests: refused")
    try:
        parsed = parse_manifest(data)
    except ValueError as e:
        raise ImageRelayError(f"the manifest the server sent is not usable: {e}") from None
    if parsed["config"]["digest"] not in set(check.get("ids") or []):
        raise ImageRelayError("the manifest names a config that is not the recorded one: refused")
    return digest, parsed


def fetch(relay: ImageRelay, image: str, check: Optional[dict], *,
          progress: Optional[Callable[[dict], None]] = None, cancel: Optional[threading.Event] = None) -> Staged:
    """Every blob of the image's manifest for this machine, through the server, checked, into the staging
    directory. Raises ImageRelayError, agent.models.CacheError (a download that failed) or Cancelled."""
    from forgeyard.agent.models import CacheError, _check

    cancel = cancel or threading.Event()
    if not check or not check.get("ids") or not check.get("content"):
        raise ImageRelayError("there are no recorded ids to check an image from the server against")
    cache = relay.cache
    if getattr(cache, "error", None):
        raise ImageRelayError(f"the model cache (where it is staged) cannot be used: {cache.error}")
    _check(cancel)
    data, media = _get_small(relay, f"{relay.path}/manifest", MANIFEST_MAX, "manifest", cancel)
    if media in INDEX_TYPES:
        raise ImageRelayError("the server sent an index, not this machine's manifest")
    mdigest, parsed = _check_manifest(data, check)
    staging = cache.root / ".partial" / f"image-{mdigest[7:31]}"
    layers = [d["digest"] for d in parsed["layers"]]
    sizes = {parsed["config"]["digest"]: parsed["config"]["size"], **{d["digest"]: d["size"] for d in parsed["layers"]}}
    total = sum(sizes.values())
    with cache._locked(f"image:{mdigest}", cancel), cache._shared("partial", cancel):
        cache._check_space(max(0, total - _on_disk(staging)))
        staging.mkdir(mode=0o700, parents=True, exist_ok=True)
        staged = Staged(image, data, mdigest, parsed["media_type"], parsed["config"]["digest"], layers, staging)
        prog = _Progress(progress, total, len(layers))
        order = [parsed["config"]["digest"]] + list(dict.fromkeys(layers))
        for i, digest in enumerate(order):
            _check(cancel)
            prog.layer = i  # 0: the config, then layer 1..n
            dest = staging / digest[7:]   # only ever a checked blob (moved there once its hash matched)
            if not dest.exists():
                part = staging / (digest[7:] + ".part")
                base = prog.done_before

                def step(phase, done, _total, _label, base=base):
                    prog.send(done)

                cache._fetch(f"https://{relay.host(image)}/v2/blobs/{digest}", part, sizes[digest], headers={},
                             progress=step, phase="image", base=base, total=total,
                             label=f"layer {i} of {len(order) - 1}" if i else "the image config", cancel=cancel,
                             relay={"path": f"{relay.path}/blobs/{digest}", "mode": "server"})
                got = "sha256:" + cache._sha256(part, cancel)
                if got != digest:
                    part.unlink(missing_ok=True)
                    raise CacheError(f"{'layer ' + str(i) if i else 'the image config'} from the server: SHA-256 "
                                     f"mismatch (got {got[7:19]}…, expected {digest[7:19]}…)")
                part.replace(dest)
            staged.files[digest] = dest
            prog.done_before += sizes[digest]
            if i:
                prog.layers_done += 1
            prog.send(prog.done_before, force=True)
        _check_config(staged, check)
    return staged


def _on_disk(staging: Path) -> int:
    try:
        return sum(p.stat().st_size for p in staging.iterdir() if p.is_file())
    except OSError:
        return 0


def _check_config(staged: Staged, check: dict) -> None:
    raw = staged.blob(staged.config_digest).read_bytes()[:CONFIG_MAX + 1]
    if len(raw) > CONFIG_MAX:
        raise ImageRelayError("the image config is too large")
    try:
        config = json.loads(raw)
    except ValueError:
        raise ImageRelayError("the image config is not JSON") from None
    content = images.content_of_config(config if isinstance(config, dict) else {})
    if content is None or content not in set(check.get("content") or []):
        raise ImageRelayError("the image config's layers and settings are not the recorded ones: refused")
    diff_ids = (config.get("rootfs") or {}).get("diff_ids") or []
    if len(diff_ids) != len(staged.layers):
        raise ImageRelayError(f"the config lists {len(diff_ids)} layers, the manifest {len(staged.layers)}")
    arch = machine_arch()
    if arch is not None and config.get("architecture") and config.get("architecture") != arch:
        raise ImageRelayError(f"the server sent the image for {config.get('architecture')}; this machine is {arch}")


def _get_small(relay: ImageRelay, path: str, limit: int, what: str, cancel: threading.Event) -> tuple[bytes, str]:
    """A small answer (the manifest) from the server, with the relay client's refusals and a few retries."""
    from forgeyard.agent import relay as relay_client
    from forgeyard.agent.models import Cancelled, _check
    from forgeyard.models import fetch

    attempt = 0
    while True:
        _check(cancel)
        try:
            with relay.client.open(path) as resp:
                if resp.status >= 400:
                    raise fetch.FetchError(f"HTTP {resp.status}", resp.status)
                data = resp.read(limit + 1)
                media = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            if len(data) > limit:
                raise ImageRelayError(f"the {what} from the server is larger than {limit} bytes")
            return data, media
        except relay_client.RelayRefused as e:
            raise ImageRelayError(f"through the server: {e}") from None
        except relay_client.RelayBusy as e:
            if cancel.wait(e.wait):
                raise Cancelled("cancelled") from None
        except (fetch.FetchError, OSError) as e:
            attempt += 1
            if attempt > 3:
                raise ImageRelayError(f"the {what} through the server failed: {e}") from None
            if cancel.wait(min(10, 2 ** attempt)):
                raise Cancelled("cancelled") from None


# ------------------------------------------------------------------ the archive
class _Reader(io.RawIOBase):
    """A blob file read for the archive, ending at once on a cancel."""

    def __init__(self, path: Path, cancel: threading.Event):
        self._fh = path.open("rb")
        self._cancel = cancel

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        if self._cancel.is_set():
            raise OSError("cancelled")
        return self._fh.readinto(b)

    def close(self) -> None:
        self._fh.close()
        super().close()


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), 0o644, 0
    tar.addfile(info, io.BytesIO(data))


def write_archive(staged: Staged, out: BinaryIO, cancel: Optional[threading.Event] = None) -> None:
    """The staged image as one tar stream `docker load` and `podman load` accept (see the module docstring).
    Each blob once, under blobs/sha256/<hex>; the layers as the registry serves them (compressed)."""
    cancel = cancel or threading.Event()
    name = full_name(staged.image)
    tag = split_image(staged.image)[2]
    index = {"schemaVersion": 2, "mediaType": OCI_INDEX,
             "manifests": [{"mediaType": staged.media_type if staged.media_type in MANIFEST_TYPES else MANIFEST_TYPES[1],
                            "digest": staged.manifest_digest, "size": len(staged.manifest),
                            "annotations": {"io.containerd.image.name": name,
                                            "org.opencontainers.image.ref.name": tag}}]}
    classic = [{"Config": f"blobs/sha256/{staged.config_digest[7:]}", "RepoTags": [tag_ref(staged.image)],
                "Layers": [f"blobs/sha256/{d[7:]}" for d in staged.layers]}]
    with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as tar:
        _add_bytes(tar, "oci-layout", json.dumps({"imageLayoutVersion": "1.0.0"}).encode())
        dirs = tarfile.TarInfo("blobs")
        dirs.type, dirs.mode, dirs.mtime = tarfile.DIRTYPE, 0o755, 0
        tar.addfile(dirs)
        sub = tarfile.TarInfo("blobs/sha256")
        sub.type, sub.mode, sub.mtime = tarfile.DIRTYPE, 0o755, 0
        tar.addfile(sub)
        _add_bytes(tar, f"blobs/sha256/{staged.manifest_digest[7:]}", staged.manifest)
        for digest in dict.fromkeys([staged.config_digest, *staged.layers]):
            path = staged.blob(digest)
            info = tarfile.TarInfo(f"blobs/sha256/{digest[7:]}")
            info.size, info.mode, info.mtime = path.stat().st_size, 0o644, 0
            with _Reader(path, cancel) as fh:
                tar.addfile(info, io.BufferedReader(fh, 1024 * 1024))
        _add_bytes(tar, "index.json", json.dumps(index, separators=(",", ":")).encode())
        _add_bytes(tar, "manifest.json", json.dumps(classic, separators=(",", ":")).encode())


def clean(staged: Staged) -> None:
    shutil.rmtree(staged.dir, ignore_errors=True)

