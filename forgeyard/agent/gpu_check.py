"""1.4.5 (#202): can a *new* process still open this machine's GPUs?

An agent in a container started with `docker run --gpus all` gets its NVIDIA devices from the NVIDIA runtime hook,
which sets up the container's device cgroup rules behind Docker's and systemd's back. When the host runs `systemctl
daemon-reload` (package installs, unattended upgrades, any unit change), systemd re-applies the container's cgroup from
what it knows, and those rules are gone: handles that are already open keep working (the agent's own NVML inventory,
engine containers that were running), but every new open() inside the old container fails, so a new process workload
gets `cuInit: CUDA_ERROR_NO_DEVICE` or `Failed to initialize NVML: Unknown Error`. New containers get fresh rules and
work. The agent's command now passes the device nodes with --device as well (install/update.py, install/commands.py),
which Docker records, so they survive a reload; this check finds the machines where that is not (yet) the case.

At start and every CHECK_EVERY seconds the agent runs a tiny probe *as the workload account, the way a process
workload starts* (agent/workloads/process.py `probe`): `/bin/sh` opens each device node read-write and closes it again,
which is the first thing CUDA, NVML and ROCm do and is where the device cgroup says no (EPERM). Nothing else is done
with the device: no context, no memory, nothing on cards that are busy with other work.

    NVIDIA  /dev/nvidiactl, /dev/nvidia-uvm (when it exists) and every /dev/nvidiaN
    AMD     /dev/kfd and every /dev/dri/renderD*

The result goes into hello's `runtime_info.gpu_check` and, when it changes, a `gpu_check` message to a server that
lists the feature "gpu_check" (older servers never get it):

    {"ok": bool, "at": ISO time, "user": the account it ran as,
     "devices": [{"path", "vendor", "ok", "error"?}], "reason"?: why it failed, in one line}

None (nothing reported) when there is nothing to check: no GPUs, process workloads off (then they are never placed
here anyway), or a simulated inventory, whose cards do not exist; for development the simulation can name device
paths to probe (SIMULATION_DEVICES_ENV, honoured only together with FORGEYARD_AGENT_SIMULATION=1).
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

log = logging.getLogger("forgeyard.agent")

CHECK_EVERY = 300.0
PROBE_TIMEOUT = 30.0
MAX_DEVICES = 64
SIMULATION_DEVICES_ENV = "FORGEYARD_AGENT_SIMULATION_GPU_DEVICES"

_NVIDIA_CARD = re.compile(r"^nvidia(\d{1,3})$")
_RENDER = re.compile(r"^renderD\d{1,4}$")

# Opens each path read-write ("<>") on a spare descriptor of a throwaway command and says how that went, one line per
# path in order. A path that does not exist is not opened (`<>` would create a file). `true` and not `:`: a
# redirection error on a special built-in may end a POSIX shell.
PROBE_SCRIPT = ('for p do if [ ! -e "$p" ]; then echo "fail No such file or directory"; '
                'elif e=$( { true 3<>"$p"; } 2>&1 ); then echo ok; '
                'else printf "fail %s\\n" "$(printf %s "$e" | tr "\\n" " ")"; fi; done')


def _number(name: str, pattern: re.Pattern) -> int:
    m = pattern.match(name)
    return int(re.sub(r"\D", "", name)) if m else -1


def device_nodes(vendors: set[str], dev: Path = Path("/dev")) -> list[tuple[str, str]]:
    """(vendor, path) of every device node a new GPU process of these vendors opens, as this machine has them."""
    out: list[tuple[str, str]] = []
    if "nvidia" in vendors:
        out.append(("nvidia", str(dev / "nvidiactl")))  # always: no NVIDIA process starts without it
        if (dev / "nvidia-uvm").exists():
            out.append(("nvidia", str(dev / "nvidia-uvm")))
        try:
            cards = sorted((p.name for p in dev.iterdir() if _NVIDIA_CARD.match(p.name)),
                           key=lambda n: _number(n, _NVIDIA_CARD))
        except OSError:
            cards = []
        out += [("nvidia", str(dev / n)) for n in cards]
    if "amd" in vendors:
        out.append(("amd", str(dev / "kfd")))
        try:
            renders = sorted((p.name for p in (dev / "dri").iterdir() if _RENDER.match(p.name)),
                             key=lambda n: _number(n, _RENDER))
        except OSError:
            renders = []
        out += [("amd", str(dev / "dri" / n)) for n in renders]
    return out[:MAX_DEVICES]


def simulated_nodes(env: dict, vendors: set[str]) -> Optional[list[tuple[str, str]]]:
    """For a simulated inventory: the paths SIMULATION_DEVICES_ENV names (space separated, `vendor=path` or a path,
    which counts as the first vendor of the inventory), or None: nothing to check."""
    from forgeyard.agent.inventory import SIMULATION_ENV

    raw = env.get(SIMULATION_DEVICES_ENV, "")
    if env.get(SIMULATION_ENV) != "1" or not raw.strip() or not vendors:
        return None
    default = sorted(vendors)[0]
    out = []
    for item in raw.split()[:MAX_DEVICES]:
        vendor, _, path = item.rpartition("=")
        out.append((vendor if vendor in ("nvidia", "amd") else default, path))
    return out


def parse(stdout: str, nodes: list[tuple[str, str]], *, returncode: int = 0, stderr: str = "") -> list[dict]:
    """The probe's lines as one result per device node. A line that is missing (the probe ended early) counts as a
    failure with whatever the probe said."""
    lines = (stdout or "").splitlines()
    out = []
    for i, (vendor, path) in enumerate(nodes):
        line = lines[i].strip() if i < len(lines) else ""
        if line == "ok":
            out.append({"path": path, "vendor": vendor, "ok": True})
            continue
        if line.startswith("fail"):
            error = line[4:].strip()
        else:
            error = (stderr or "").strip().replace("\n", " ") or f"the probe ended (exit {returncode})"
        out.append({"path": path, "vendor": vendor, "ok": False, "error": _why(error, path)})
    return out


def _why(error: str, path: str) -> str:
    """The shell's message without its prefix ("sh: 1: cannot open /dev/nvidia0: Operation not permitted"), short."""
    text = error.rsplit(f"{path}:", 1)[-1].strip() if f"{path}:" in error else error.strip()
    return (text or "could not open it")[:160]


def summary(devices: list[dict]) -> Optional[str]:
    failed = [d for d in devices if not d.get("ok")]
    if not failed:
        return None
    first = failed[0]
    more = f" and {len(failed) - 1} more" if len(failed) > 1 else ""
    return f"{first['path']}{more}: {first.get('error') or 'could not open it'}"[:300]


class GpuCheck:
    """Runs the probe through the process runtime and keeps the last result."""

    def __init__(self, runtime: Any, *, simulated: bool = False, env: Optional[dict] = None,
                 dev: Path = Path("/dev"), clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.runtime = runtime
        self.simulated = simulated
        self.env = dict(os.environ) if env is None else env
        self.dev = dev
        self.clock = clock
        self.last: Optional[dict] = None
        self.checked_at: Optional[float] = None  # monotonic, of the last run
        self._said: Optional[bool] = None

    def nodes(self, accelerators: list[dict]) -> list[tuple[str, str]]:
        vendors = {a.get("vendor") for a in accelerators or [] if a.get("vendor") in ("nvidia", "amd")}
        if not vendors:
            return []
        if self.simulated:
            return simulated_nodes(self.env, vendors) or []
        return device_nodes(vendors, self.dev)

    def run(self, accelerators: list[dict]) -> Optional[dict]:
        """Probe now; the result (also kept in `last`), or None when there is nothing to check here."""
        info = self.runtime.info() if self.runtime is not None else {}
        nodes = self.nodes(accelerators) if info.get("ok") else []
        if not nodes:
            self.last = None
            return None
        try:
            done = self.runtime.probe(["/bin/sh", "-c", PROBE_SCRIPT, "forgeyard-gpu-check", *[p for _, p in nodes]],
                                      vendors={v for v, _ in nodes}, timeout=PROBE_TIMEOUT)
            devices = parse(done.stdout, nodes, returncode=done.returncode, stderr=done.stderr)
        except subprocess.TimeoutExpired:
            devices = [{"path": p, "vendor": v, "ok": False, "error": f"no answer within {PROBE_TIMEOUT:g} s"}
                       for v, p in nodes]
        except OSError as e:
            devices = [{"path": p, "vendor": v, "ok": False, "error": f"the probe did not start: {e.strerror or e}"}
                       for v, p in nodes]
        result: dict = {"ok": all(d["ok"] for d in devices), "at": self.clock().isoformat(timespec="seconds"),
                        "user": info.get("user"), "devices": devices}
        why = summary(devices)
        if why:
            result["reason"] = why
        if result["ok"] != self._said:
            if result["ok"]:
                if self._said is not None:
                    log.info("New processes can open the GPUs again.")
            else:
                log.error("New processes cannot open this machine's GPUs (%s). In a container this happens after the "
                          "host ran `systemctl daemon-reload`: restart the agent container, and update it with the "
                          "command on the machine's page, which keeps the device access.", why)
            self._said = result["ok"]
        self.last = result
        return result


def changed(before: Optional[dict], after: Optional[dict]) -> bool:
    """Whether the server should hear about it: the verdict or the devices that fail changed (not only the time)."""
    def key(r: Optional[dict]):
        if r is None:
            return None
        return r.get("ok"), tuple((d.get("path"), d.get("ok"), d.get("error")) for d in r.get("devices") or [])
    return key(before) != key(after)
