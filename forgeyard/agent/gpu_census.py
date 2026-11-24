"""1.5.4: how many GPUs the host has, from sysfs, next to how many the agent sees (NVML / amd-smi).

An agent container sees only the device nodes it was started with, so a GPU installed later stays invisible to it
until the container is created again (the machine page's update command passes every NVIDIA device of the host). The
PCI bus is not namespaced: `/sys/bus/pci/devices` lists every function of the host from inside a container too, with
or without the GPUs' device nodes. Sent in `hello.runtime_info["gpu_census"]`; servers before 1.5.4 drop it, and the
server raises a Setup health finding when the host has GPUs the agent cannot see.

What counts as a GPU, per PCI function (`vendor`, `class`):

    NVIDIA 0x10de   0x0300xx (VGA) or 0x0302xx (3D controller: every data-centre card). Not its HDMI audio (0x0403),
                    USB-C or UCSI functions (0x0c..), nor an NVSwitch (0x0680).
    AMD    0x1002   0x0300xx, 0x0302xx, 0x0380xx (display controller: Instinct MI100-MI250) or 0x1200xx (processing
                    accelerator: MI300). An APU's graphics is one 0x0300 function, like a dGPU; its audio (0x0403),
                    crypto (0x1080) and other functions do not count. The CPU's own functions are vendor 0x1022.

SR-IOV virtual functions (a `physfn` link) are not GPUs of the host: their physical function is counted. Nothing is
reported where sysfs cannot be read (not Linux), or for a simulated agent unless FORGEYARD_AGENT_SYSFS names a stand-in
tree (development and tests).
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable, Optional

log = logging.getLogger("forgeyard.agent")

SYSFS = Path("/sys/bus/pci/devices")
SYSFS_ENV = "FORGEYARD_AGENT_SYSFS"
VENDOR_IDS = {"0x10de": "nvidia", "0x1002": "amd"}
GPU_CLASSES = {"nvidia": ("0x0300", "0x0302"), "amd": ("0x0300", "0x0302", "0x0380", "0x1200")}
MAX_IDS = 64
_BDF = re.compile(r"^([0-9a-fA-F]{1,8}):([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])$")


def bus_id(value: Any) -> Optional[str]:
    """A PCI address as sysfs writes it ("0000:17:00.0"): NVML's "00000000:17:00.0" and upper case become that."""
    m = _BDF.match(value.strip()) if isinstance(value, str) else None
    if m is None:
        return None
    return f"{int(m.group(1), 16):04x}:{m.group(2).lower()}:{m.group(3).lower()}.{m.group(4)}"


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip().lower()
    except OSError:
        return ""


def host_gpus(root: Path = SYSFS) -> Optional[dict[str, list[str]]]:
    """{vendor: [bus ids]} of the host's GPUs, or None when sysfs cannot be listed."""
    try:
        entries = sorted(root.iterdir())
    except OSError:
        return None
    out: dict[str, list[str]] = {}
    for d in entries:
        vendor = VENDOR_IDS.get(_read(d / "vendor"))
        if vendor is None or not _read(d / "class").startswith(GPU_CLASSES[vendor]):
            continue
        if (d / "physfn").exists():
            continue  # an SR-IOV virtual function: its physical function is the GPU
        bdf = bus_id(d.name)
        if bdf is not None:
            out.setdefault(vendor, []).append(bdf)
    return out


# Variables that limit which GPUs of a vendor this agent may use: set to a subset, the agent was deliberately given
# only some of the host's GPUs, and seeing fewer is no problem.
LIMIT_VARS = {"nvidia": ("NVIDIA_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES"),
              "amd": ("ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "GPU_DEVICE_ORDINAL")}


def limits(environ: dict) -> dict[str, str]:
    """{vendor: "NAME=value"} for each vendor whose GPUs this agent's environment limits. NVIDIA_VISIBLE_DEVICES=all
    (the CUDA images' default) does not limit; any value of the others does."""
    out: dict[str, str] = {}
    for vendor, names in LIMIT_VARS.items():
        for name in names:
            value = environ.get(name)
            if value is None or (name == "NVIDIA_VISIBLE_DEVICES" and value.strip().lower() in ("all", "")):
                continue
            out[vendor] = f"{name}={value.strip()[:64]}"
            break
    return out


def census(accelerators: Iterable[dict], root: Path = SYSFS, limited: Optional[dict[str, str]] = None) -> Optional[dict]:
    """The host's GPUs next to the ones the agent sees, per vendor:

        {"host_gpus": 9, "visible_gpus": 8, "missing": ["0000:c1:00.0"],
         "vendors": {"nvidia": {"host": 9, "visible": 8, "missing": ["0000:c1:00.0"]}}}

    `missing`: the host's bus ids no seen GPU has. `limited_by` (only when set): the variable that limits this agent
    to some of the vendor's GPUs (limits()). When a seen GPU does not say its bus id, only the counts compare
    (`missing` holds what cannot be matched only if the counts differ)."""
    host = host_gpus(root)
    if host is None:
        return None
    seen: dict[str, list[Optional[str]]] = {}
    for a in accelerators:
        vendor = a.get("vendor") if isinstance(a, dict) else None
        if vendor in GPU_CLASSES:
            seen.setdefault(vendor, []).append(bus_id((a.get("detail") or {}).get("pci_bus_id")))
    vendors: dict[str, dict] = {}
    for vendor in sorted(set(host) | set(seen)):
        on_host, visible = host.get(vendor, []), seen.get(vendor, [])
        if all(visible):
            missing = [b for b in on_host if b not in set(visible)]
        else:
            missing = [] if len(visible) >= len(on_host) else on_host[len(visible):]
        vendors[vendor] = {"host": len(on_host), "visible": len(visible), "missing": missing[:MAX_IDS]}
        if (limited or {}).get(vendor):
            vendors[vendor]["limited_by"] = limited[vendor]
    return {"host_gpus": sum(v["host"] for v in vendors.values()),
            "visible_gpus": sum(v["visible"] for v in vendors.values()),
            "missing": [b for v in vendors.values() for b in v["missing"]][:MAX_IDS],
            "vendors": vendors}


def for_agent(accelerators: Iterable[dict], *, simulated: bool, environ: Optional[dict] = None) -> Optional[dict]:
    """census() for this agent, never raising: the real sysfs, or for a simulated agent only a stand-in one."""
    env = os.environ if environ is None else environ
    stand_in = env.get(SYSFS_ENV)
    if simulated and not stand_in:
        return None  # the fixture's GPUs are not this host's
    try:
        return census(accelerators, Path(stand_in) if simulated and stand_in else SYSFS, limits(env))
    except Exception:  # never keep an agent from connecting over this
        log.debug("Could not count the host's GPUs", exc_info=True)
        return None
