"""The container runtime: Docker or Podman, driven through their CLI.

Every container carries labels (`forgeyard.agent`, `forgeyard.replica`, `forgeyard.generation`,
`forgeyard.workload`, `forgeyard.ports`), so a restarted agent finds and adopts what it started
instead of starting it twice, and removes what nobody wants any more. Containers run with the
engine's defaults plus `--security-opt no-new-privileges`, never `--privileged`, with only the
assigned GPUs, and their host ports bound to one address.

An image pinned as `repo:tag@sha256:…` is found by its digest only. Before `run` the agent decides
what to run (choose_image), so a machine that cannot reach the registry can use a copy loaded with
`docker load`, which keeps the tag but often not the digest:

    the pinned digest is local                      run it as pinned
    the tag is local and matches the recorded ids   run that image by its local Id (checked, no pull)
    the tag is local and does not match             never run it; pull the pinned digest
    neither                                         pull the pinned digest

An image named by tag only is pulled when it is not local (what `run` would do).

The pull comes before `run` and is the agent's own (pull.py): through the engine's API on its socket
when it can, with progress per layer ("pulling the image: 0.80 of 1.90 GiB (42%)"), else through the
CLI. It fails when it stops making progress, not after a fixed time, and ends at once when the replica
is cancelled. A failed pull says why the image is missing and how to load it (pull_failed_reason).

Through the server (1.4, #35 PR 4, "relay_images"): a pinned image with an `image_relay` is fetched from the
agent's own server instead (agent/imagerelay.py) when the machine is set so, or after the pull could not
reach the registry ("auto"), then streamed into `docker load` / `podman load`, tagged, and run only when
choose_image recognises it by its recorded ids. `load()` is also what an image file uploaded by an admin
goes through ("image_load").
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
from pathlib import Path
from typing import BinaryIO, Callable, Mapping, Optional

from forgeyard.agent.workloads import plan
from forgeyard.agent.workloads import pull as pulling
from forgeyard.agent.workloads.records import Record, RecordStore
from forgeyard.agent.workloads.runtime import Observation, StartError
from forgeyard.workloads import images
from forgeyard.workloads.spec import ContainerRuntime as ContainerSpec, WorkloadSpec

log = logging.getLogger("forgeyard.agent")
# A replica's reason is kept to this many characters (the supervisor cuts there): build reasons to fit.
REASON_MAX = 300

ENGINES = ("docker", "podman")
CDI_DIRS = ("/etc/cdi", "/var/run/cdi")
# `run` of an image that is local (the agent pulled it first): creating and starting the container.
RUN_TIMEOUT = 600.0
# A pull fails when nothing moved for this long (FORGEYARD_PULL_STALL_MINUTES).
PULL_STALL = 300.0
DOCKER_SOCKET = "/var/run/docker.sock"
PODMAN_SOCKETS = ("/run/podman/podman.sock",)
# `docker load` / `podman load` printing what it loaded: "Loaded image: repo:tag", "Loaded image ID: sha256:…",
# Podman's older "Loaded image(s): a,b".
LOADED_RE = re.compile(r"^\s*Loaded image(?:\(s\))?(?: ID)?:\s*(.+?)\s*$")

Runner = Callable[..., subprocess.CompletedProcess]


# What the docker CLI prints when its credential helper (credsStore / credHelpers in config.json)
# fails. A helper that needs a desktop session, a keyring or a GPG agent can fail under the agent
# even though it works in an interactive shell, and the CLI then gives up before pulling even a
# public image, which needs no credentials at all.
CREDENTIAL_HELPER_FAILED = "error getting credentials"

# What choose_image found locally for a pinned image.
PINNED = "pinned"        # the pinned digest itself
LOADED = "loaded"        # the tag, matching the recorded ids
MISMATCH = "mismatch"    # the tag, not matching them
UNCHECKED = "unchecked"  # the tag, with no recorded ids to check it against
ABSENT = "absent"        # neither


def tag_of(image: str) -> Optional[str]:
    """'repo:tag@sha256:…' -> 'repo:tag'; None for an image that is not pinned by digest."""
    ref, at, digest = image.partition("@")
    return ref if at and digest and ref else None


def digest_ref(image: str) -> str:
    """'repo:tag@sha256:…' -> 'repo@sha256:…', the form both engines keep in RepoDigests."""
    ref, _, digest = image.partition("@")
    name = ref.rsplit(":", 1)[0] if ":" in ref.rsplit("/", 1)[-1] else ref
    return f"{name}@{digest}"


def choose_image(image: str, check: Optional[dict], inspect: Callable[[str], Optional[dict]]) -> tuple[str, str]:
    """(what to pass to `run`, what was found). Only an image whose ids match is ever run in place
    of the pinned reference; everything else goes to the registry by the pinned digest."""
    tag = tag_of(image)
    if tag is None:
        return image, PINNED  # not pinned by digest: the engine resolves it as it always did
    if inspect(digest_ref(image)) is not None:
        return image, PINNED
    local = inspect(tag)
    if local is None:
        return image, ABSENT
    if not check:
        return image, UNCHECKED
    how = images.matches(local, check)
    local_id = local.get("Id")
    if how is None or not isinstance(local_id, str) or not local_id:
        log.warning("A different image is loaded under %s (its ids do not match the recorded ones): not running it; "
                    "pulling %s instead.", tag, image)
        return image, MISMATCH
    log.info("Running %s as the loaded image %s (it matches the recorded ids by %s).", image, local_id[:19], how)
    return local_id, LOADED


def pull_failed_reason(engine: str, image: str, found: str, detail: str) -> str:
    """Why a pinned image could not be had, and what to do on a machine that cannot reach the registry."""
    tag = tag_of(image) or image
    if found == MISMATCH:
        why = (f"a different image is loaded under {tag} (its ids do not match the recorded ones), so it is not run, "
               f"and the pinned image could not be pulled: {engine} load the image the recipe names")
    elif found == UNCHECKED:
        why = (f"{tag} is loaded but Forgeyard has no recorded ids for this image, so it cannot check it, and the "
               "pinned image could not be pulled: name the image by tag only in a custom recipe")
    else:
        why = (f"the image is not on this machine and could not be pulled; on an offline machine, {engine} load "
               "the image (Forgeyard checks it by its recorded ids)")
    return f"{why}. {pulling.brief(detail, max(80, REASON_MAX - len(why) - 2))}"


def _unix_path(host: str) -> Optional[str]:
    return host[len("unix://"):] if host.startswith("unix://") and len(host) > len("unix://") else None


def engine_socket(kind: str, raw: Mapping, environ: Mapping[str, str] = os.environ) -> Optional[str]:
    """The engine API's Unix socket the agent pulls through, or None (then it pulls with the CLI).

    Docker: DOCKER_HOST when it is unix://, none when it points elsewhere (tcp://, ssh://) or another
    context is in use, else /var/run/docker.sock. Podman: CONTAINER_HOST likewise, else the socket
    `podman info` reports (it exists only while podman.socket is enabled)."""
    if kind == "docker":
        host = environ.get("DOCKER_HOST")
        if host:
            return _unix_path(host)
        if environ.get("DOCKER_CONTEXT") not in (None, "", "default"):
            return None
        conf = Path(environ.get("DOCKER_CONFIG") or Path(environ.get("HOME", "/root")) / ".docker") / "config.json"
        try:
            context = json.loads(conf.read_text()).get("currentContext")
        except (OSError, ValueError, AttributeError):
            context = None
        if context not in (None, "", "default"):
            return None
        return DOCKER_SOCKET if os.path.exists(DOCKER_SOCKET) else None
    host = environ.get("CONTAINER_HOST")
    if host:
        return _unix_path(host)
    remote = ((raw.get("host") or {}).get("remoteSocket") or {}) if isinstance(raw.get("host"), dict) else {}
    found = remote.get("path") if isinstance(remote, dict) else None
    if isinstance(found, str) and found:
        path = _unix_path(found) or found
        if os.path.exists(path):
            return path
    runtime = environ.get("XDG_RUNTIME_DIR")
    for path in ([f"{runtime}/podman/podman.sock"] if runtime else []) + list(PODMAN_SOCKETS):
        if os.path.exists(path):
            return path
    return None


def anonymous_config(src_dir: Optional[str], dest: Path) -> Path:
    """A copy of the CLI's config.json without its credential helpers, for an anonymous pull.

    Everything else is kept (`auths` with stored tokens, `currentContext`, proxies), and the
    contexts directory is linked, so the pull talks to the same daemon. Returns the config dir."""
    src = Path(src_dir) if src_dir else Path(os.environ.get("HOME", "/root")) / ".docker"
    data: dict = {}
    try:
        data = json.loads((src / "config.json").read_text())
    except (OSError, ValueError):
        data = {}
    data.pop("credsStore", None)
    data.pop("credHelpers", None)
    dest.mkdir(parents=True, exist_ok=True)
    os.chmod(dest, 0o700)
    fd = os.open(dest / "config.json", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    contexts = src / "contexts"
    if contexts.is_dir() and not (dest / "contexts").exists():
        (dest / "contexts").symlink_to(contexts)
    return dest


def _run(argv: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess:
    # The engine CLI needs nothing from the agent's environment but where to find things.
    env = {"PATH": os.environ.get("PATH", plan.BASIC_PATH), "HOME": os.environ.get("HOME", "/root")}
    for key in ("DOCKER_HOST", "DOCKER_CONFIG", "CONTAINER_HOST", "XDG_RUNTIME_DIR"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env, stdin=subprocess.DEVNULL)


def nvidia_cdi_present(dirs=CDI_DIRS) -> bool:
    for d in dirs:
        p = Path(d)
        if p.is_dir() and any(f.name.startswith("nvidia") for f in p.iterdir()):
            return True
    return False


def detect(engine: str = "auto", run: Runner = _run, which=shutil.which, cdi=nvidia_cdi_present) -> dict:
    """Which engine works here, and whether it can pass NVIDIA GPUs into containers."""
    if engine == "none":
        return {"ok": False, "reason": "disabled (container engine: none)"}
    candidates = ENGINES if engine == "auto" else (engine,)
    tried = []
    for name in candidates:
        path = which(name) if "/" not in name else (name if os.access(name, os.X_OK) else None)
        if not path:
            tried.append(f"{name} not found")
            continue
        try:
            r = run([path, "info", "--format", "{{json .}}"], timeout=20)
        except (OSError, subprocess.TimeoutExpired) as e:
            tried.append(f"{name}: {e}")
            continue
        if r.returncode != 0:
            tried.append(f"{name} info failed: {(r.stderr or r.stdout).strip()[:160]}")
            continue
        try:
            info = json.loads(r.stdout or "{}")
        except ValueError:
            info = {}
        kind = "podman" if "podman" in Path(path).name or "host" in info and "version" in info else "docker"
        out = {"ok": True, "engine": kind, "path": path, "socket": engine_socket(kind, info)}
        if kind == "docker":
            out["version"] = info.get("ServerVersion")
            runtimes = sorted((info.get("Runtimes") or {}).keys())
            out["runtimes"] = runtimes
            out["default_runtime"] = info.get("DefaultRuntime")
            if "nvidia" in runtimes or which("nvidia-container-runtime-hook"):
                out["nvidia"], out["gpu_mode"] = True, "gpus"
            elif cdi():
                out["nvidia"], out["gpu_mode"] = True, "cdi"
            else:
                out["nvidia"], out["gpu_mode"] = False, None
        else:
            out["version"] = (info.get("version") or {}).get("Version")
            out["nvidia"] = cdi()
            out["gpu_mode"] = "cdi" if out["nvidia"] else None
        return out
    return {"ok": False, "reason": "; ".join(tried) or "no container engine"}


def render_devices(gpus: list[dict]) -> list[str]:
    """/dev/dri/renderD<minor> for each assigned AMD card: from the KFD topology, else from sysfs."""
    out = []
    for g in gpus:
        if g.get("vendor") != "amd":
            continue
        minor = g.get("render_minor")
        if minor:
            out.append(f"/dev/dri/renderD{minor}")
            continue
        bdf = g.get("pci_bus_id")
        found = sorted(Path(f"/sys/bus/pci/devices/{bdf}/drm").glob("renderD*")) if bdf else []
        if not found:
            raise StartError(f"cannot find the render node of AMD GPU #{g.get('index')}")
        out.append(f"/dev/dri/{found[0].name}")
    return out


class ContainerRuntime:
    name = "container"

    def __init__(self, store: RecordStore, *, agent_id: str, engine: str = "auto", log_max_bytes: int = 10 * 1024**2,
                 run: Runner = _run, which=shutil.which, cdi=nvidia_cdi_present, info: Optional[dict] = None,
                 pull_stall: float = PULL_STALL):
        self.store = store
        self.agent_id = agent_id
        self.log_max_bytes = log_max_bytes
        self.pull_stall = pull_stall
        self._run = run
        self._info = info if info is not None else detect(engine, run=run, which=which, cdi=cdi)
        self.engine = self._info.get("path") or "docker"
        self.socket: Optional[str] = self._info.get("socket")
        self._chosen: dict[str, tuple[str, str]] = {}  # replica id -> what pull() decided, for start()

    def info(self) -> dict:
        out = {k: v for k, v in self._info.items() if k not in ("path", "socket")}
        if self._info.get("ok"):
            out["pulls"] = "engine API" if self.socket else "CLI"
        return out

    def _cli(self, *args: str, timeout: float = 30.0) -> subprocess.CompletedProcess:
        return self._run([self.engine, *args], timeout=timeout)

    def labels(self, rec: Record) -> dict[str, str]:
        return {"forgeyard.agent": self.agent_id, "forgeyard.replica": rec.id,
                "forgeyard.generation": str(rec.generation), "forgeyard.workload": rec.workload,
                "forgeyard.ports": json.dumps(rec.ports, separators=(",", ":"))}

    def start(self, rec: Record, address: str, secret: Optional[dict[str, str]] = None) -> None:
        if not self._info.get("ok"):
            raise StartError(self._info.get("reason") or "no container engine")
        spec = WorkloadSpec.model_validate(rec.spec)
        rt = spec.runtime
        assert isinstance(rt, ContainerSpec)
        if any(g.get("vendor") == "nvidia" for g in rec.gpus) and not self._info.get("nvidia"):
            raise StartError("this container engine cannot pass NVIDIA GPUs (no NVIDIA runtime or CDI)")
        name = plan.container_name(rec.workload, rec.id)
        self._cli("rm", "-f", name)  # a leftover of this replica that was never recorded
        log_dir = self.store.log_dir(rec.id)
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(log_dir, 0o700)
        env_file = log_dir / "env"
        extra, mounts = None, []
        if rec.model is not None:
            if not rec.paths:
                raise StartError("the model is not in the cache")
            extra = plan.model_values(rec.paths, container=True, model=rec.model, bind=address)
            from forgeyard.models import sources

            host = rec.paths["mount"]
            if "," in host or not os.path.isdir(host):
                raise StartError(f"cannot mount the model directory {host}")
            mounts = [(host, sources.container_paths(rec.model)["mount"])]
        env = plan.container_env(spec.env, rec.gpus, rec.ports, replica_id=rec.id, workload=rec.workload,
                                 ordinal=rec.ordinal, extra=extra, secret=secret)
        fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(plan.env_file_text(env))
        amd = [g for g in rec.gpus if g.get("vendor") == "amd"]
        devices = render_devices(rec.gpus) if amd else []
        kfd = ["/dev/kfd"] if plan.needs_kfd(rec.gpus) else []
        gids = sorted({os.stat(d).st_gid for d in [*kfd, *devices] if os.path.exists(d)}) if amd else []
        image, found = self._chosen.pop(rec.id, None) or choose_image(rt.image, rec.image_ids, self._image)
        argv = plan.container_run_argv(
            self.engine, name=name, image=image, command=rt.command,
            args=plan.substitute(rt.args, {}, extra) if extra else rt.args, env_file=str(env_file),
            labels=self.labels(rec), ports=rec.ports, bind=address, cpu=spec.resources.cpu,
            memory=spec.resources.memory, gpus=rec.gpus, gpu_mode=self._info.get("gpu_mode"),
            render_devices=devices, render_gids=gids, log_max_bytes=self.log_max_bytes, mounts=mounts)
        try:
            r = self._run(argv, timeout=RUN_TIMEOUT)
            if r.returncode != 0 and CREDENTIAL_HELPER_FAILED in (r.stderr or ""):
                # `run` pulled after all (the image went away since pull()) and the CLI's credential
                # helper is broken under the agent: pull it anonymously, then start it again.
                code, out = self._cli_pull(rt.image, anonymous=True)
                if code == 0:
                    self._cli("rm", "-f", name)
                    r = self._run(argv, timeout=RUN_TIMEOUT)
                else:
                    r = subprocess.CompletedProcess(argv, code, "", out)
        except subprocess.TimeoutExpired:
            raise StartError(f"the container did not start within {RUN_TIMEOUT / 60:.0f} minutes") from None
        except pulling.PullError as e:
            raise StartError(str(e)[:300]) from None
        finally:
            env_file.unlink(missing_ok=True)
        if r.returncode != 0:
            engine = Path(self.engine).name
            detail = f"{engine} run failed: {(r.stderr or r.stdout).strip()[-300:]}"
            if found not in (PINNED, LOADED) and self._image(digest_ref(rt.image)) is None:
                # Still not here after `run`: the pull itself failed (no network, no DNS, refused).
                raise StartError(pull_failed_reason(engine, rt.image, found, (r.stderr or r.stdout).strip()[-120:]))
            raise StartError(detail)
        rec.container = name
        rec.pid = None

    def _image(self, ref: str) -> Optional[dict]:
        """`image inspect` of a local image (Docker and Podman print a JSON list), or None."""
        try:
            r = self._cli("image", "inspect", ref)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if r.returncode != 0:
            return None
        try:
            items = json.loads(r.stdout or "[]")
        except ValueError:
            return None
        return items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else None

    # ------------------------------------------------------------------ pulling, before run
    def pull(self, rec: Record, progress: Optional[pulling.Report] = None,
             cancel: Optional[threading.Event] = None, relay=None) -> None:
        """Make the replica's image local, if it is not: pulled by the pinned reference, with progress.
        Raises StartError (the pull failed: why, which layer, how to load it on an offline machine) or
        pull.PullCancelled. An image chosen by its recorded ids (#10) is never pulled."""
        if not self._info.get("ok"):
            return  # start() says why
        spec = WorkloadSpec.model_validate(rec.spec)
        rt = spec.runtime
        assert isinstance(rt, ContainerSpec)
        try:
            self._chosen[rec.id] = self.ensure_image(rt.image, rec.image_ids, progress, cancel,
                                                     who=f"replica {rec.id[:12]} of {rec.workload}", relay=relay)
        except (StartError, pulling.PullCancelled):
            self._chosen.pop(rec.id, None)
            raise

    def ensure_image(self, image: str, check: Optional[dict], progress: Optional[pulling.Report] = None,
                     cancel: Optional[threading.Event] = None, *, who: str = "a download",
                     relay=None) -> tuple[str, str]:
        """The image on this machine, pulled when it is not local; (what to run, what was found). Also what
        "Download to…" (1.4) fetches an engine image with, starting nothing. `relay` (1.4, an
        agent.imagerelay.ImageRelay): a pinned image may come through the server instead, at once ("server",
        or a registry already found unreachable) or after a pull that could not reach the registry. Raises
        StartError or pull.PullCancelled."""
        if not self._info.get("ok"):
            raise StartError(f"no container engine: {self._info.get('reason') or 'not available'}")
        target, found = choose_image(image, check, self._image)
        pinned = tag_of(image) is not None
        if found == LOADED or (found == PINNED and (pinned or self._image(image) is not None)):
            return target, found  # local: the pinned digest, a loaded copy that matches, or a tag-only image
        engine = Path(self.engine).name
        relay = relay if pinned and check else None
        if relay is not None and relay.first(image):
            return self._relay_image(image, check, relay, progress, cancel, who)
        log.info("Pulling %s for %s.", image, who)
        try:
            self._pull(image, progress, cancel)
        except pulling.PullStalled as e:
            raise StartError(str(e)[:300]) from None  # it was reaching the registry: no offline advice
        except pulling.PullError as e:
            from forgeyard.models import fetch

            if relay is not None and fetch.network_error(str(e)):
                # "auto": the registry cannot be reached from here; the server fetches the image instead (and
                # the next images from this registry go there at once).
                relay.client.mark_unreachable(relay.host(image))
                log.warning("Could not pull %s for %s (%s); fetching it through the server.", image, who,
                            pulling.brief(str(e), 200))
                return self._relay_image(image, check, relay, progress, cancel, who)
            if pinned:
                raise StartError(pull_failed_reason(engine, image, found, str(e))) from None
            if fetch.network_error(str(e)):
                advice = (". It is not pinned by digest, so it cannot come through the server: upload the image file "
                          "to this machine (its page: Upload an image)")
                start = f"{engine} could not pull {image}: "
                detail = pulling.brief(str(e), max(60, REASON_MAX - len(start) - len(advice)))
                raise StartError(f"{start}{detail}{advice}"[-REASON_MAX:]) from None
            raise StartError(pulling.brief(f"{engine} could not pull {image}: {e}", REASON_MAX)) from None
        if pinned and self._image(digest_ref(image)) is None:
            raise StartError(f"the pull of {image} finished, but {engine} does not show the pinned digest")
        log.info("Pulled %s.", image)
        return (image, PINNED) if pinned else (target, found)

    # ------------------------------------------------------------------ through the server (1.4)
    def _relay_image(self, image: str, check: dict, relay, progress: Optional[pulling.Report],
                     cancel: Optional[threading.Event], who: str) -> tuple[str, str]:
        """Fetch the image through the server, load it, tag it and check it (agent/imagerelay.py)."""
        from forgeyard.agent import imagerelay
        from forgeyard.agent.models import CacheError, Cancelled
        from forgeyard.workloads.registry import tag_ref

        cancel = cancel or threading.Event()
        engine = Path(self.engine).name
        log.info("Fetching %s for %s through the server.", image, who)
        try:
            staged = imagerelay.fetch(relay, image, check, progress=progress, cancel=cancel)
        except Cancelled:
            raise pulling.PullCancelled() from None
        except (imagerelay.ImageRelayError, CacheError) as e:
            raise StartError(f"the image through the server: {e}"[:300]) from None
        total = sum(p.stat().st_size for p in {staged.blob(d) for d in [staged.config_digest, *staged.layers]})
        if progress is not None:
            n = len(staged.layers)
            progress({"done": total, "total": total, "layers": n, "layers_done": n, "unpacking": True, "via": "server"})
        refs = self.load(lambda out: imagerelay.write_archive(staged, out, cancel), cancel)
        want = tag_ref(image)
        item = None
        for ref in dict.fromkeys([want, *refs, staged.config_digest]):
            found_item = self._image(ref)
            if found_item is not None and images.matches(found_item, check):
                item = found_item
                break
        if item is None:
            raise StartError(f"{engine} loaded the image from the server, but what it shows does not match the "
                             "recorded ids: not running it")
        if not {want, imagerelay.full_name(image)} & set(item.get("RepoTags") or []):  # Podman names it in full
            r = self._cli("tag", str(item.get("Id")), want)
            if r.returncode != 0:
                raise StartError(f"{engine} tag {want} failed: {(r.stderr or r.stdout).strip()[-200:]}")
        target, found = choose_image(image, check, self._image)
        if found not in (LOADED, PINNED):
            raise StartError(f"the image loaded from the server is not recognised under {want}: not running it")
        imagerelay.clean(staged)
        log.info("Loaded %s through the server for %s (%s).", image, who, found)
        return target, found

    def load(self, write: Callable[[BinaryIO], None], cancel: Optional[threading.Event] = None) -> list[str]:
        """`docker load` / `podman load` of the archive `write` streams into it; what it says it loaded (tags,
        or ids). A cancel ends it (and its process group). Raises StartError or pull.PullCancelled."""
        if not self._info.get("ok"):
            raise StartError(f"no container engine: {self._info.get('reason') or 'not available'}")
        cancel = cancel or threading.Event()
        engine = Path(self.engine).name
        try:
            proc = subprocess.Popen([self.engine, "load"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, env=self._engine_env(), start_new_session=True)
        except OSError as e:
            raise StartError(f"{engine} load could not start: {e}") from None
        lines: list[str] = []

        def read() -> None:
            assert proc.stdout is not None
            for raw in iter(proc.stdout.readline, b""):
                lines.append(raw.decode("utf-8", "replace").rstrip())
                del lines[:-200]

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

        def watch() -> None:
            while proc.poll() is None:
                if cancel.wait(0.5):
                    end()
                    return

        reader = threading.Thread(target=read, daemon=True)
        watcher = threading.Thread(target=watch, daemon=True)
        reader.start()
        watcher.start()
        problem: Optional[BaseException] = None
        killed = False
        try:
            assert proc.stdin is not None
            write(proc.stdin)
        except Exception as e:  # a broken pipe (the engine stopped reading), a cancel, a short or broken upload
            problem = e
            if proc.poll() is None:
                killed = True
                end()  # before its input ends: never load half an archive (a cut at a file's end is a valid tar)
        finally:
            try:
                proc.stdin.close()  # type: ignore[union-attr]
            except OSError:
                pass
        code = proc.wait()
        reader.join(timeout=10)
        if cancel.is_set():
            raise pulling.PullCancelled()
        tail = "\n".join(x for x in lines if x)[-300:]
        if problem is not None and killed:
            raise StartError(f"{engine} load: the archive broke off, nothing was loaded: {problem}"[:300])
        if code != 0:
            raise StartError(f"{engine} load failed: {tail or f'exit code {code}'}"[:400])
        if problem is not None:
            raise StartError(f"{engine} load: the archive broke off: {problem}"[:300])
        refs = []
        for line in lines:
            m = LOADED_RE.match(line)
            if m:
                refs += [r.strip() for r in m.group(1).split(",") if r.strip()]
        return list(dict.fromkeys(refs))

    def load_upload(self, opened, size: Optional[int], cancel: Optional[threading.Event] = None) -> list[dict]:
        """An uploaded image file (1.4, "image_load"): `opened` is the server's open answer (agent.relay), copied
        into `docker load` as it arrives; each image loaded, as the console shows it."""
        def write(out: BinaryIO) -> None:
            got = 0
            with opened as resp:
                while True:
                    chunk = resp.read1(1024 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    got += len(chunk)
            if size and got != size:
                raise OSError(f"the upload ended after {got} of {size} bytes")

        refs = self.load(write, cancel)
        found: dict[str, dict] = {}
        for ref in refs:
            item = self._image(ref)
            if item is None or not isinstance(item.get("Id"), str):
                continue
            entry = found.setdefault(item["Id"], {
                # Podman shows a bare hex id: as a digest, so the server can compare it with the recorded ids.
                "id": images.normalize(item["Id"]) or item["Id"],
                "tags": [t for t in (item.get("RepoTags") or []) if isinstance(t, str)][:16],
                "content": images.content_of_inspect(item), "os": str(item.get("Os") or "")[:32],
                "architecture": str(item.get("Architecture") or "")[:32]})
            if ref not in entry["tags"] and not ref.startswith("sha256:") and len(entry["tags"]) < 16:
                entry["tags"].append(ref)
        return list(found.values())[:32]

    def _pull(self, image: str, progress: Optional[pulling.Report], cancel: Optional[threading.Event]) -> None:
        """Through the engine API when it answers; else (or when the registry wants credentials the
        API pull did not carry) through the CLI, which has the user's."""
        if self.socket:
            try:
                pulling.api_pull(self.socket, image, report=progress, cancel=cancel, stall=self.pull_stall)
                return
            except pulling.Unreachable as e:
                log.info("The engine API is not reachable (%s); pulling %s with the CLI.", e, image)
            except pulling.NeedsCredentials as e:
                log.info("The registry wants credentials for %s (%s); pulling with the CLI.", image, e)
        code, out = self._cli_pull(image, progress=progress, cancel=cancel)
        if code != 0 and CREDENTIAL_HELPER_FAILED in out:
            # The image is not here and the CLI's credential helper is broken under the agent: pull
            # it anonymously, with the helpers left out (enough for every public image).
            code, out = self._cli_pull(image, progress=progress, cancel=cancel, anonymous=True)
        if code != 0:
            raise pulling.PullError(out.strip()[-200:] or f"{Path(self.engine).name} pull exited with {code}")

    @staticmethod
    def _engine_env() -> dict:
        env = {"PATH": os.environ.get("PATH", plan.BASIC_PATH), "HOME": os.environ.get("HOME", "/root")}
        for key in ("DOCKER_HOST", "DOCKER_CONFIG", "DOCKER_CONTEXT", "CONTAINER_HOST", "XDG_RUNTIME_DIR"):
            if os.environ.get(key):
                env[key] = os.environ[key]
        return env

    def _cli_pull(self, image: str, *, progress: Optional[pulling.Report] = None,
                  cancel: Optional[threading.Event] = None, anonymous: bool = False) -> tuple[int, str]:
        """`docker pull` (with a config without credential helpers when anonymous, see
        anonymous_config); (exit code, the end of its output)."""
        env = self._engine_env()
        argv = [self.engine, "pull", image]
        if anonymous:
            log.warning("%s's credential helper failed; pulling %s without it", Path(self.engine).name, image)
            conf = anonymous_config(os.environ.get("DOCKER_CONFIG"), self.store.dir.parent / "docker-anonymous")
            argv = ["env", f"DOCKER_CONFIG={conf}", *argv]
        return pulling.cli_pull(argv, env, report=progress, cancel=cancel, stall=self.pull_stall)

    def _inspect(self, name: str) -> Optional[dict]:
        r = self._cli("inspect", "--format", "{{json .State}}", name)
        if r.returncode != 0:
            return None
        try:
            return json.loads(r.stdout)
        except ValueError:
            return None

    def observe(self, rec: Record) -> Observation:
        if not rec.container:
            return Observation(alive=False, missing=True)
        state = self._inspect(rec.container)
        if state is None:
            return Observation(alive=False, missing=True)
        status = (state.get("Status") or "").lower()
        if status in ("running", "restarting", "paused", "created"):
            rec.pid = state.get("Pid") or None
            return Observation(alive=True)
        return Observation(alive=False, exit_code=state.get("ExitCode"))

    def stop(self, rec: Record, grace: float) -> None:
        if rec.container:
            try:
                self._cli("stop", "-t", str(int(grace)), rec.container, timeout=grace + 30)
            except subprocess.TimeoutExpired:
                self._cli("kill", rec.container)

    def cleanup(self, rec: Record) -> None:
        pass

    def remove(self, rec: Record) -> None:
        if rec.container:
            self._cli("rm", "-f", rec.container)
        shutil.rmtree(self.store.log_dir(rec.id), ignore_errors=True)

    def remove_name(self, name: str) -> None:
        self._cli("rm", "-f", name)

    def tail(self, rec: Record, lines: int) -> tuple[str, bool]:
        if not rec.container:
            return "", False
        r = self._run([self.engine, "logs", "--tail", str(lines + 1), rec.container], timeout=15)
        text = (r.stdout or "") + (r.stderr or "")
        parts = text.splitlines()
        return "\n".join(parts[-lines:]), len(parts) > lines

    def trim(self, rec: Record, max_bytes: int) -> None:
        pass  # the engine's log driver keeps the size (--log-opt max-size)

    def owned(self) -> dict[str, dict]:
        """Containers this agent started: {replica id: {name, generation, ports}}."""
        if not self._info.get("ok"):
            return {}
        r = self._cli("ps", "-a", "-q", "--filter", f"label=forgeyard.agent={self.agent_id}")
        ids = [x for x in (r.stdout or "").split() if x]
        if r.returncode != 0 or not ids:
            return {}
        r = self._cli("inspect", *ids)
        try:
            items = json.loads(r.stdout or "[]")
        except ValueError:
            return {}
        out = {}
        for item in items:
            labels = (item.get("Config") or {}).get("Labels") or {}
            if labels.get("forgeyard.agent") != self.agent_id or not labels.get("forgeyard.replica"):
                continue
            try:
                ports = json.loads(labels.get("forgeyard.ports") or "[]")
                generation = int(labels.get("forgeyard.generation") or 0)
            except ValueError:
                ports, generation = [], 0
            out[labels["forgeyard.replica"]] = {"name": (item.get("Name") or "").lstrip("/"),
                                                "generation": generation, "ports": ports}
        return out
