"""How this agent runs, so its machine's page can show the exact command that updates it (1.4.2, #121).

Sent once per connection in `hello.install` (older servers ignore it):

    container   the agent runs in a container: what `docker run` / `podman run` it was started with, read
                from its own container through the same engine CLI the container runtime uses (`inspect`):
                name, image, network, restart policy, GPUs, devices, group ids, mounts, the environment it
                was given (the image's own variables left out), extra arguments and a few other options
    install.sh  installed by the server's install.sh: the systemd unit, the program's directory, agent.toml
    manual      run from a checkout or a Python environment of one's own
    unknown     none of the above could be told, with why

Never sent: the join token, the machine credential, or any value that looks like a secret. An environment
variable whose name suggests one (TOKEN, SECRET, PASSWORD, KEY, CREDENTIAL, AUTH, …), or whose value looks
like one (a Forgeyard token, a Hugging Face or GitHub token, a URL with a password, a PEM block), or contains
the credential, is reported by name only, as `hidden: "secret"`; the server then renders `-e NAME="$NAME"` and
says to export it first. An argument after the image is treated the same way (`--join-token X` loses X).

Everything here is best effort: a failure becomes `{"method": "unknown", "reason": …}`, never an exception.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("forgeyard.agent")

JOIN_TOKEN_ENV = "FORGEYARD_JOIN_TOKEN"
# The agent image's title label (Dockerfile.agent): a container without it, whose entrypoint does not run
# `forgeyard` either, is someone's own container the agent happens to run in.
AGENT_IMAGE_TITLE = "Forgeyard agent"
STATE_IN_CONTAINER = "/var/lib/forgeyard-agent"
# install.sh's defaults (forgeyard/install/install.sh).
INSTALL_HOME = "/opt/forgeyard"
UNIT_DIR = Path("/etc/systemd/system")
UNIT_MARKER = "/install.sh"  # its first line: "# Installed by <server>/install.sh. Running install.sh again rewrites it."

# What the report may hold: enough for any real agent container, small next to MAX_MESSAGE_BYTES.
MAX_ITEMS = 64
MAX_VALUE = 1024
MAX_REPORT = 24 * 1024

# A name that suggests a secret. Conservative on purpose: a variable left out by mistake costs an export,
# a secret sent by mistake cannot be taken back.
SECRET_NAME = re.compile(r"TOKEN|SECRET|PASSW|PASSPHRASE|CREDENTIAL|PRIVATE|COOKIE|SESSION|AUTH|KEY|CERT_PEM",
                         re.IGNORECASE)
# A value that looks like a secret whatever its name: Forgeyard's join token, machine credential and API key,
# Hugging Face, GitHub, GitLab, OpenAI-style, Slack and AWS keys, a URL with a password, a PEM block.
SECRET_VALUE = re.compile(r"^(fyj_|fym_|fyk_|hf_|ghp_|gho_|ghs_|github_pat_|glpat-|sk-|xox[abprs]-|AKIA)"
                          r"|://[^/@\s]*:[^/@\s]*@|-----BEGIN ")
# Arguments after the image that are followed by a secret (`--join-token X`, `--join-token=X`).
SECRET_FLAG = re.compile(r"^--?[A-Za-z0-9-]*(token|secret|password|passwd|key|credential)[A-Za-z0-9-]*$",
                         re.IGNORECASE)

Runner = Callable[[list[str]], Any]


def _cut(value: Any, limit: int = MAX_VALUE) -> str:
    return str(value)[:limit]


def secret_value(value: str, credential: Optional[str] = None) -> bool:
    """Whether a value must never leave the machine."""
    if credential and credential in value:
        return True
    return bool(SECRET_VALUE.search(value))


def secret_name(name: str) -> bool:
    return bool(SECRET_NAME.search(name))


# ------------------------------------------------------------------ which container am I
_DOCKER_ID = re.compile(r"/containers/([0-9a-f]{64})/(?:hostname|hosts|resolv\.conf)\b")
_PODMAN_ID = re.compile(r"/overlay-containers/([0-9a-f]{64})/userdata/")
_CGROUP_ID = re.compile(r"(?:docker[-/]|libpod-|crio-|containerd[-/])([0-9a-f]{64})")


def own_container_id(mountinfo: str, cgroup: str = "") -> Optional[str]:
    """The id of the container this process runs in, from /proc/self/mountinfo (the engine bind-mounts the
    container's own hostname, hosts and resolv.conf, which works with `--network host` too, where the host
    name is the host's) or, failing that, /proc/self/cgroup (cgroup v1 and the systemd driver)."""
    for pattern in (_DOCKER_ID, _PODMAN_ID):
        m = pattern.search(mountinfo)
        if m:
            return m.group(1)
    m = _CGROUP_ID.search(cgroup)
    return m.group(1) if m else None


def in_container(root: Path = Path("/"), mountinfo: str = "") -> bool:
    return ((root / ".dockerenv").exists() or (root / "run" / ".containerenv").exists()
            or own_container_id(mountinfo) is not None)


# ------------------------------------------------------------------ reading an inspect
def _env_pairs(items: Any) -> list[tuple[str, Optional[str]]]:
    out = []
    for item in items or []:
        if not isinstance(item, str) or not item:
            continue
        name, eq, value = item.partition("=")
        out.append((name, value if eq else None))
    return out


def environment(container_env: Any, image_env: Any, credential: Optional[str] = None) -> list[dict]:
    """The variables the container was given: its environment less what the image sets the same way.
    Secrets by name or value are named only."""
    image = set(_env_pairs(image_env))
    out: list[dict] = []
    for name, value in _env_pairs(container_env):
        if (name, value) in image or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,127}", name):
            continue
        if name == JOIN_TOKEN_ENV or secret_name(name) or (value is not None and secret_value(value, credential)):
            out.append({"name": name, "hidden": "secret"})
        elif value is not None and len(value) > MAX_VALUE:
            out.append({"name": name, "hidden": "too long"})
        else:
            out.append({"name": name, "value": value})
    return out[:MAX_ITEMS]


def arguments(cmd: Any, image_cmd: Any, credential: Optional[str] = None) -> tuple[list[str], list[str]]:
    """The arguments after the image name when they differ from the image's own: (arguments, the flags
    whose values were left out)."""
    if not isinstance(cmd, list) or cmd == (image_cmd or []) or not cmd:
        return [], []
    args: list[str] = []
    hidden: list[str] = []
    skip = False
    for raw in cmd[:MAX_ITEMS]:
        a = _cut(raw)
        if skip:
            skip = False
            continue
        flag, eq, value = a.partition("=")
        if SECRET_FLAG.match(flag):
            hidden.append(flag)
            skip = not eq  # the value is the next argument
            continue
        if secret_value(a, credential):
            hidden.append("(an argument)")
            continue
        args.append(a)
    return args, hidden


def _bind(spec: str) -> Optional[dict]:
    parts = spec.split(":")
    if len(parts) < 2 or not parts[0] or not parts[1]:
        return None
    kind = "bind" if parts[0].startswith("/") else "volume"
    out = {"type": kind, "source": _cut(parts[0]), "target": _cut(parts[1])}
    if len(parts) > 2 and parts[2]:
        out["options"] = _cut(":".join(parts[2:]), 64)
    return out


def mounts(host_config: dict, runtime_mounts: Any) -> list[dict]:
    """`-v` mounts as given (HostConfig.Binds), then what else is mounted (`--mount`, and the anonymous
    volume of the image's VOLUME, which must be named on the new container to keep the machine's
    credential)."""
    out: list[dict] = []
    for spec in host_config.get("Binds") or []:
        if isinstance(spec, str):
            b = _bind(spec)
            if b:
                out.append(b)
    seen = {m["target"] for m in out}
    for m in runtime_mounts or []:
        if not isinstance(m, dict):
            continue
        target = m.get("Destination") or m.get("Target")
        if not target or target in seen:
            continue
        kind = m.get("Type")
        ro = m.get("RW") is False or m.get("ReadOnly") is True
        if kind == "volume" and m.get("Name"):
            out.append({"type": "volume", "source": _cut(m["Name"]), "target": _cut(target), **({"options": "ro"} if ro else {})})
        elif kind == "bind" and m.get("Source"):
            out.append({"type": "bind", "source": _cut(m["Source"]), "target": _cut(target), **({"options": "ro"} if ro else {})})
        elif kind == "tmpfs":
            out.append({"type": "tmpfs", "target": _cut(target)})
        seen.add(target)
    return out[:MAX_ITEMS]


def gpus(device_requests: Any) -> tuple[Optional[str], list[str]]:
    """(`--gpus` value, CDI device names) from HostConfig.DeviceRequests."""
    value: Optional[str] = None
    cdi: list[str] = []
    for r in device_requests or []:
        if not isinstance(r, dict):
            continue
        ids = [str(i) for i in (r.get("DeviceIDs") or []) if i is not None]
        if r.get("Driver") == "cdi":
            cdi += ids
            continue
        caps = [c for group in (r.get("Capabilities") or []) for c in (group or [])]
        if caps and "gpu" not in caps:
            continue
        if ids:
            value = "device=" + ",".join(ids)
        elif r.get("Count") == -1:
            value = "all"
        elif isinstance(r.get("Count"), int) and r["Count"] > 0:
            value = str(r["Count"])
    return value, cdi[:MAX_ITEMS]


def devices(host_config: dict, cdi: list[str]) -> list[dict]:
    out = []
    for d in host_config.get("Devices") or []:
        if isinstance(d, dict) and d.get("PathOnHost"):
            item = {"host": _cut(d["PathOnHost"], 256)}
            if d.get("PathInContainer") and d["PathInContainer"] != d["PathOnHost"]:
                item["container"] = _cut(d["PathInContainer"], 256)
            if d.get("CgroupPermissions") and d["CgroupPermissions"] != "rwm":
                item["permissions"] = _cut(d["CgroupPermissions"], 8)
            out.append(item)
    out += [{"host": _cut(name, 256)} for name in cdi]
    return out[:MAX_ITEMS]


_NVIDIA_NODE = re.compile(r"^nvidia(ctl|-uvm|-uvm-tools|-modeset|[0-9]{1,3})$")
_NVIDIA_OTHER = re.compile(r"^nvidia[A-Za-z0-9_-]{1,32}$")
_NVIDIA_ORDER = {"ctl": 0, "-uvm": 1, "-uvm-tools": 2, "-modeset": 3}


def nvidia_devices(dev: Path = Path("/dev"), is_device: Callable[[Path], bool] = Path.is_char_device) -> list[str]:
    """1.4.5 (#202): the NVIDIA device nodes this container has (what `--gpus` gave it), in the order a `docker run`
    lists them: the control nodes, then /dev/nvidia0, 1, … The update command passes them with --device. 1.5.4: any
    other /dev/nvidia… character device too (after those, by name), so a node a newer driver or toolkit adds, which
    the update command's shell loop would not find, is still passed by name."""
    try:
        names = [p.name for p in dev.iterdir() if _NVIDIA_OTHER.match(p.name) and is_device(p)]
    except OSError:
        return []

    def order(name: str) -> tuple[int, int, str]:
        m = _NVIDIA_NODE.match(name)
        if m is None:
            return (2, 0, name)
        rest = m.group(1)
        return (0, _NVIDIA_ORDER[rest], "") if rest in _NVIDIA_ORDER else (1, int(rest), "")
    return [str(dev / n) for n in sorted(names, key=order)][:MAX_ITEMS]


def _restart(policy: Any) -> Optional[str]:
    if not isinstance(policy, dict) or policy.get("Name") in (None, "", "no"):
        return None
    name = str(policy["Name"])
    retries = policy.get("MaximumRetryCount")
    return f"{name}:{retries}" if name == "on-failure" and isinstance(retries, int) and retries > 0 else name


def _ports(bindings: Any) -> list[str]:
    out = []
    for port, hosts in (bindings or {}).items() if isinstance(bindings, dict) else []:
        for h in hosts or [{}]:
            ip = (h or {}).get("HostIp") or ""
            hp = (h or {}).get("HostPort") or ""
            spec = port.removesuffix("/tcp")
            out.append(":".join(x for x in (ip, hp, spec) if x) if hp else spec)
    return out[:MAX_ITEMS]


def _strings(value: Any) -> list[str]:
    return [_cut(v, 256) for v in (value or []) if isinstance(v, (str, int))][:MAX_ITEMS]


def is_agent_container(inspect: dict) -> bool:
    config = inspect.get("Config") or {}
    if (config.get("Labels") or {}).get("org.opencontainers.image.title") == AGENT_IMAGE_TITLE:
        return True
    words = [str(w) for w in (config.get("Entrypoint") or []) + (config.get("Cmd") or [])]
    return any(Path(w).name == "forgeyard" for w in words) or "forgeyard" in " ".join(words[:4])


def describe_container(inspect: dict, image: Optional[dict] = None, *, credential: Optional[str] = None,
                       state_dir: str = STATE_IN_CONTAINER, nvidia_nodes: Optional[list[str]] = None) -> dict:
    """What `docker run` this container was started with, from `docker inspect <it>` and `docker image
    inspect <its image>` (to leave out the image's own environment and arguments)."""
    config = inspect.get("Config") or {}
    host = inspect.get("HostConfig") or {}
    image_config = (image or {}).get("Config") or {}
    out: dict = {"method": "container", "known": True}
    if not is_agent_container(inspect):
        out.update(known=False, foreign=True, image=_cut(config.get("Image") or "", 256),
                   reason="the agent runs inside a container that is not the Forgeyard agent image")
        return out
    gpu, cdi = gpus(host.get("DeviceRequests"))
    args, hidden_args = arguments(config.get("Cmd"), image_config.get("Cmd"), credential)
    network = host.get("NetworkMode")
    c = {
        "name": _cut(str(inspect.get("Name") or "").lstrip("/"), 128) or None,
        "image": _cut(config.get("Image") or "", 256),
        "image_id": _cut(inspect.get("Image") or "", 80) or None,
        "network": None if network in (None, "", "default", "bridge") else _cut(network, 128),
        "restart": _restart(host.get("RestartPolicy")),
        "gpus": gpu,
        "runtime": None if host.get("Runtime") in (None, "", "runc", "crun") else _cut(host["Runtime"], 64),
        "devices": devices(host, cdi),
        "group_add": _strings(host.get("GroupAdd")),
        "mounts": mounts(host, inspect.get("Mounts")),
        "env": environment(config.get("Env"), image_config.get("Env"), credential),
        "args": args,
        "hidden_args": hidden_args,
        "privileged": bool(host.get("Privileged")),
        "cap_add": _strings(host.get("CapAdd")),
        "security_opt": _strings(host.get("SecurityOpt")),
        "pid": "host" if host.get("PidMode") == "host" else None,
        "ipc": "host" if host.get("IpcMode") == "host" else None,
        "user": _cut(config["User"], 64) if config.get("User") and config.get("User") != image_config.get("User") else None,
        "ports": _ports(host.get("PortBindings")) if _needs_ports(network) else [],
        "add_hosts": _strings(host.get("ExtraHosts")),
    }
    if nvidia_nodes is not None:
        c["nvidia_devices"] = nvidia_nodes[:MAX_ITEMS]
    c["state_kept"] = any(m.get("type") in ("volume", "bind") and _under(state_dir, m.get("target") or "")
                          for m in c["mounts"])
    out["container"] = c
    return out


def _under(path: str, mount: str) -> bool:
    """Whether `path` is the mount point `mount` or inside it."""
    mount = mount.rstrip("/") or "/"
    return path == mount or path.startswith(mount if mount == "/" else mount + "/")


def _needs_ports(network: Any) -> bool:
    return network not in ("host",) and not str(network or "").startswith("container:")


# ------------------------------------------------------------------ outside a container
_SERVICE = re.compile(r"/([A-Za-z0-9@_.\\-]+\.service)\s*$", re.MULTILINE)


def service_of(cgroup: str) -> Optional[str]:
    """The systemd service this process runs in (/proc/self/cgroup), if any."""
    m = _SERVICE.search(cgroup)
    return m.group(1) if m else None


def describe_host(*, cgroup: str, prefix: str, config_path: Optional[str], server: Optional[str],
                  unit_dir: Path = UNIT_DIR, package_dir: Optional[Path] = None) -> dict:
    service = service_of(cgroup)
    unit = None
    marked = False
    if service:
        path = unit_dir / service
        try:
            head = path.read_text(encoding="utf-8", errors="replace")[:400]
            unit = str(path)
            marked = "Installed by " in head and UNIT_MARKER in head
        except OSError:
            pass
    installed = marked or Path(prefix) == Path(INSTALL_HOME) / "venv"
    out: dict = {"method": "install.sh" if installed else "manual", "known": True,
                 "service": service, "unit": unit, "program": _cut(prefix, 512),
                 "config": _cut(config_path, 512) if config_path else None,
                 "server": _cut(server, 256) if server else None}
    if not installed:
        root = (package_dir or Path(__file__).resolve().parents[2])
        out["checkout"] = str(root) if (root / ".git").exists() else None
    return out


# ------------------------------------------------------------------ putting it together
def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _inspect(engine: str, run: Runner, *args: str) -> Optional[dict]:
    r = run([engine, *args])
    if getattr(r, "returncode", 1) != 0:
        return None
    try:
        data = json.loads(r.stdout or "null")
    except ValueError:
        return None
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


def collect(*, engine_info: Optional[dict], engine: Optional[str], run: Optional[Runner], credential: Optional[str],
            server: Optional[str], config_path: Optional[str], state_dir: Optional[str] = None,
            root: Path = Path("/"), prefix: str = sys.prefix) -> dict:
    """How this agent runs, never raising. `engine` / `run`: the container runtime's CLI, as it calls it."""
    try:
        mountinfo = _read(root / "proc" / "self" / "mountinfo")
        cgroup = _read(root / "proc" / "self" / "cgroup")
        if not in_container(root, mountinfo):
            return limit(describe_host(cgroup=cgroup, prefix=prefix, config_path=config_path, server=server))
        cid = own_container_id(mountinfo, cgroup)
        if cid is None:
            return {"method": "container", "known": False, "reason": "could not tell which container the agent runs in"}
        if not engine_info or not engine_info.get("ok") or not engine or run is None:
            return {"method": "container", "known": False,
                    "reason": "no container engine to read the agent's own container with (mount the engine's socket)"}
        me = _inspect(engine, run, "inspect", "--type", "container", cid)
        if me is None:
            return {"method": "container", "known": False,
                    "reason": f"{Path(engine).name} inspect could not read the agent's own container"}
        image = _inspect(engine, run, "image", "inspect", str(me.get("Image") or "")) if me.get("Image") else None
        out = describe_container(me, image, credential=credential, state_dir=state_dir or STATE_IN_CONTAINER,
                                 nvidia_nodes=nvidia_devices(root / "dev"))
        out["engine"] = engine_info.get("engine") or Path(engine).name
        return limit(out)
    except Exception as e:  # never keep an agent from connecting over this
        log.debug("Could not tell how the agent runs", exc_info=True)
        return {"method": "unknown", "known": False, "reason": _cut(f"{type(e).__name__}: {e}", 200)}


def limit(report: dict) -> dict:
    """The report, or a short one saying it was too large."""
    if len(json.dumps(report, default=str)) <= MAX_REPORT:
        return report
    return {"method": report.get("method", "unknown"), "known": False, "reason": "the description was too large to send"}


def for_agent(supervisor, *, credential: Optional[str], server: Optional[str], config_path: Optional[str],
              state_dir: Optional[str]) -> dict:
    """collect() with the agent's own container runtime (None without workloads)."""
    rt = (getattr(supervisor, "runtimes", None) or {}).get("container") if supervisor is not None else None
    info = rt.info() if rt is not None else None
    engine = getattr(rt, "engine", None) if rt is not None else None
    run = (lambda argv: rt._run(argv, timeout=15.0)) if rt is not None else None
    in_container_state = state_dir if state_dir and os.path.isabs(state_dir) else None
    return collect(engine_info=info, engine=engine, run=run, credential=credential, server=server,
                   config_path=config_path, state_dir=in_container_state)
