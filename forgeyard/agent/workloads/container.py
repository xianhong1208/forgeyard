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
    neither                                         pull the pinned digest (`run` does)

A failed pull then says why the image is missing and how to load it (pull_failed_reason).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from forgeyard.agent.workloads import plan
from forgeyard.agent.workloads.records import Record, RecordStore
from forgeyard.agent.workloads.runtime import Observation, StartError
from forgeyard.workloads import images
from forgeyard.workloads.spec import ContainerRuntime as ContainerSpec, WorkloadSpec

log = logging.getLogger("forgeyard.agent")

ENGINES = ("docker", "podman")
CDI_DIRS = ("/etc/cdi", "/var/run/cdi")
PULL_TIMEOUT = 1800.0

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
    return f"{why}. {detail}"[:300]


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
        out = {"ok": True, "engine": kind, "path": path}
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
                 run: Runner = _run, which=shutil.which, cdi=nvidia_cdi_present, info: Optional[dict] = None):
        self.store = store
        self.agent_id = agent_id
        self.log_max_bytes = log_max_bytes
        self._run = run
        self._info = info if info is not None else detect(engine, run=run, which=which, cdi=cdi)
        self.engine = self._info.get("path") or "docker"

    def info(self) -> dict:
        return {k: v for k, v in self._info.items() if k != "path"}

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
        image, found = choose_image(rt.image, rec.image_ids, self._image)
        argv = plan.container_run_argv(
            self.engine, name=name, image=image, command=rt.command,
            args=plan.substitute(rt.args, {}, extra) if extra else rt.args, env_file=str(env_file),
            labels=self.labels(rec), ports=rec.ports, bind=address, cpu=spec.resources.cpu,
            memory=spec.resources.memory, gpus=rec.gpus, gpu_mode=self._info.get("gpu_mode"),
            render_devices=devices, render_gids=gids, log_max_bytes=self.log_max_bytes, mounts=mounts)
        try:
            r = self._run(argv, timeout=PULL_TIMEOUT)
            if r.returncode != 0 and CREDENTIAL_HELPER_FAILED in (r.stderr or ""):
                # The image is not here and the CLI's credential helper is broken under the agent:
                # pull it anonymously with the helpers left out, then start it again (it is local now).
                pulled = self._pull_anonymously(rt.image)
                if pulled.returncode == 0:
                    self._cli("rm", "-f", name)
                    r = self._run(argv, timeout=PULL_TIMEOUT)
                else:
                    r = pulled
        except subprocess.TimeoutExpired:
            raise StartError("the container did not start within 30 minutes (image pull?)") from None
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

    def _pull_anonymously(self, image: str) -> subprocess.CompletedProcess:
        """`docker pull` with a config that has no credential helpers (see anonymous_config)."""
        log.warning("%s's credential helper failed; pulling %s without it",
                    Path(self.engine).name, image)
        conf = anonymous_config(os.environ.get("DOCKER_CONFIG"), self.store.dir.parent / "docker-anonymous")
        return self._run(["env", f"DOCKER_CONFIG={conf}", self.engine, "pull", image], timeout=PULL_TIMEOUT)

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
