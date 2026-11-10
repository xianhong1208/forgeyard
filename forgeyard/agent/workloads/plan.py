"""How a replica is started: its environment and its command line, as pure functions.

Everything that decides what a workload can see lives here, so it can be tested without
starting anything:

- **The environment starts empty.** Never `os.environ`: the agent's own variables, tokens and
  credentials stay with the agent. A process gets PATH, HOME, LANG, USER/LOGNAME, what the spec
  declares, its GPU variables, its ports and FORGEYARD_* identity. A container gets only what
  the spec declares plus its GPU variables and identity (its image supplies the rest).
- **GPUs:** only the assigned cards, by UUID where the tools accept one.
"""

from __future__ import annotations

import re
from typing import Optional

BASIC_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
WRAPPER_NAME = "forgeyard-replica"
_PORT_REF = re.compile(r"\$\{(PORT(?:_[A-Z0-9_]+)?)\}")


def port_var(name: str) -> str:
    return "PORT_" + re.sub(r"[^A-Z0-9]", "_", name.upper())


def _nvidia(gpus: list[dict]) -> list[dict]:
    return [g for g in gpus if g.get("vendor") == "nvidia"]


def _amd(gpus: list[dict]) -> list[dict]:
    return [g for g in gpus if g.get("vendor") == "amd"]


def nvidia_ids(gpus: list[dict]) -> list[str]:
    """UUIDs ("GPU-…"), else indices. A UUID names one physical card whatever the enumeration
    order: CUDA numbers cards fastest-first by default, NVML and nvidia-smi by PCI bus, so an
    index can point at a different card in CUDA than the one Forgeyard assigned."""
    return [g.get("uuid") or str(g["index"]) for g in _nvidia(gpus)]


def amd_ids(gpus: list[dict]) -> list[str]:
    """ROCr UUIDs ("GPU-<KFD unique_id>"), else indices."""
    out = []
    for g in _amd(gpus):
        rocr = (g.get("detail") or {}).get("rocr_uuid") or g.get("rocr_uuid")
        out.append(rocr or str(g["index"]))
    return out


def gpu_env(gpus: list[dict], runtime: str) -> dict[str, str]:
    """GPU visibility variables. With no GPUs every vendor's list is empty, so CUDA and ROCm
    libraries see no device at all.

    NVIDIA: CUDA_VISIBLE_DEVICES and NVIDIA_VISIBLE_DEVICES hold the assigned UUIDs, with
    CUDA_DEVICE_ORDER=PCI_BUS_ID. AMD: ROCR_VISIBLE_DEVICES (the lowest layer, honoured by HIP,
    OpenCL and everything on ROCr) holds the ROCr UUIDs; HIP then numbers the visible cards 0..n-1,
    so HIP_VISIBLE_DEVICES lists exactly those. Inside a container the engine already hides the
    other cards and renumbers from 0, so there only NVIDIA_VISIBLE_DEVICES is set (the NVIDIA
    runtime reads it) and CUDA_/ROCR_ variables are left to the image when cards are assigned.
    """
    nv, amd = nvidia_ids(gpus), amd_ids(gpus)
    env: dict[str, str] = {}
    if runtime == "container":
        # A CUDA base image declares NVIDIA_VISIBLE_DEVICES=all; on a host whose default Docker
        # runtime is nvidia that alone would expose every card. Always overridden.
        env["NVIDIA_VISIBLE_DEVICES"] = ",".join(nv) if nv else "void"
        if not nv:
            env["CUDA_VISIBLE_DEVICES"] = ""
        if not amd:
            env["ROCR_VISIBLE_DEVICES"] = ""
            env["HIP_VISIBLE_DEVICES"] = ""
        return env
    env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
    env["CUDA_VISIBLE_DEVICES"] = ",".join(nv)
    env["NVIDIA_VISIBLE_DEVICES"] = ",".join(nv) if nv else "void"
    env["ROCR_VISIBLE_DEVICES"] = ",".join(amd)
    env["HIP_VISIBLE_DEVICES"] = ",".join(str(i) for i in range(len(amd)))
    return env


def port_env(ports: list[dict], runtime: str) -> dict[str, str]:
    """PORT (the first) and PORT_<NAME>: the host port for a process, which must listen on it.
    A container keeps its own port; the engine maps the host port to it."""
    env: dict[str, str] = {}
    for i, p in enumerate(ports):
        value = str(p["host_port"] if runtime == "process" else p.get("port") or p["host_port"])
        env[port_var(p["name"])] = value
        if i == 0:
            env["PORT"] = value
    return env


def identity_env(replica_id: str, workload: str, ordinal: int) -> dict[str, str]:
    return {"FORGEYARD_REPLICA": replica_id, "FORGEYARD_WORKLOAD": workload, "FORGEYARD_REPLICA_INDEX": str(ordinal)}


def process_env(spec_env: dict[str, str], gpus: list[dict], ports: list[dict], *, home: str, user: str,
                replica_id: str, workload: str, ordinal: int) -> dict[str, str]:
    env = {"PATH": BASIC_PATH, "HOME": home, "LANG": "C.UTF-8", "USER": user, "LOGNAME": user}
    env.update(spec_env)                       # the spec cannot declare the reserved names below
    env.update(gpu_env(gpus, "process"))
    env.update(port_env(ports, "process"))
    env.update(identity_env(replica_id, workload, ordinal))
    return env


def container_env(spec_env: dict[str, str], gpus: list[dict], ports: list[dict], *, replica_id: str,
                  workload: str, ordinal: int) -> dict[str, str]:
    env = dict(spec_env)
    env.update(gpu_env(gpus, "container"))
    env.update(port_env(ports, "container"))
    env.update(identity_env(replica_id, workload, ordinal))
    return env


def substitute(args: list[str], env: dict[str, str]) -> list[str]:
    """${PORT} and ${PORT_<NAME>} in arguments become the assigned ports; nothing else is expanded."""
    return [_PORT_REF.sub(lambda m: env.get(m.group(1), m.group(0)), a) for a in args]


def ulimits(memory: Optional[int], gpus: list[dict]) -> list[str]:
    """Limits for the fallback without a systemd scope. No core dumps. A memory cap is an
    address-space limit (RLIMIT_AS), which GPU runtimes break (CUDA and ROCm reserve huge virtual
    ranges), so it is only applied to workloads without GPUs. CPU cannot be capped this way."""
    out = ["-c 0"]
    if memory and not gpus:
        out.append(f"-v {max(1, memory // 1024)}")
    return out


def process_argv(exe: str, args: list[str], exit_file: str, limits: list[str]) -> list[str]:
    """`sh` runs the program and writes its exit code to a file. The agent waits for its own
    children, but after an agent restart the processes it adopts are no longer its children;
    the file is how it still learns how they ended."""
    script = "f=$1; shift; "
    script += "".join(f"ulimit {lim} || exit 125; " for lim in limits)
    script += '"$@"; c=$?; printf %s "$c" > "$f" 2>/dev/null; exit $c'
    return ["/bin/sh", "-c", script, WRAPPER_NAME, exit_file, exe, *args]


def systemd_argv(unit: str, *, cpu: Optional[float], memory: Optional[int], uid: int, gid: int,
                 groups: list[int], inner: list[str]) -> list[str]:
    """A transient systemd scope (cgroup) with CPU and memory limits, then drop to the workload
    user with setpriv. Used when the agent runs as root on a systemd host."""
    argv = ["systemd-run", "--scope", "--quiet", "--collect", f"--unit={unit}"]
    if memory:
        argv += ["-p", f"MemoryMax={memory}", "-p", "MemorySwapMax=0"]
    if cpu:
        argv += ["-p", f"CPUQuota={int(round(cpu * 100))}%"]
    argv += ["--", "setpriv", f"--reuid={uid}", f"--regid={gid}"]
    argv += [f"--groups={','.join(str(g) for g in groups)}"] if groups else ["--clear-groups"]
    return argv + ["--", *inner]


def container_name(workload: str, replica_id: str) -> str:
    return f"fy-{workload}-{replica_id[:12]}"


def container_run_argv(engine: str, *, name: str, image: str, command: Optional[list[str]], args: list[str],
                       env_file: str, labels: dict[str, str], ports: list[dict], bind: str,
                       cpu: Optional[float], memory: Optional[int], gpus: list[dict], gpu_mode: Optional[str],
                       render_devices: list[str], render_gids: list[int], log_max_bytes: int) -> list[str]:
    """`docker run` / `podman run`. No --privileged, ever; no new privileges; only the assigned
    cards; host ports bound to one address (the agent's own on the network to the server)."""
    argv = [engine, "run", "-d", "--name", name, "--security-opt", "no-new-privileges", "--restart", "no",
            "--env-file", env_file]
    for key, value in sorted(labels.items()):
        argv += ["--label", f"{key}={value}"]
    size = f"{max(1, log_max_bytes // 2 // 1024**2)}m"
    if engine.endswith("podman"):
        argv += ["--log-driver", "k8s-file", "--log-opt", f"max-size={size}"]
    else:
        argv += ["--log-driver", "json-file", "--log-opt", f"max-size={size}", "--log-opt", "max-file=2"]
    for p in ports:
        argv += ["-p", f"{bind}:{p['host_port']}:{p['port']}/tcp"]
    if cpu:
        argv += ["--cpus", f"{cpu:g}"]
    if memory:
        argv += ["--memory", str(memory)]
    nv = nvidia_ids(gpus)
    if nv:
        if gpu_mode == "cdi":
            for ident in nv:
                argv += ["--device", f"nvidia.com/gpu={ident}"]
        else:
            # The quotes are part of the value: docker splits --gpus on commas otherwise.
            argv += ["--gpus", f'"device={",".join(nv)}"']
    if _amd(gpus):
        argv += ["--device", "/dev/kfd"]
        for dev in render_devices:
            argv += ["--device", dev]
        for gid in render_gids:
            argv += ["--group-add", str(gid)]
    if command:
        argv += [f"--entrypoint={command[0]}"]
    argv.append(image)
    if command:
        argv += command[1:]
    return argv + list(args)


def env_file_text(env: dict[str, str]) -> str:
    # The spec refuses newlines in values, so one line per variable is exact.
    return "".join(f"{k}={v}\n" for k, v in env.items())
