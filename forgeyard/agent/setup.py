"""`forgeyard agent init`: a config file from what this machine has.

Detected: the GPUs (the same collectors as the agent), a container engine and whether it can pass
NVIDIA GPUs, systemd (for process limits), whether the agent runs in a container, and an account
for process workloads. What was given as a flag or an environment variable is kept as it is.
"""

from __future__ import annotations

import os
import pwd
import socket
from pathlib import Path
from typing import Optional

from forgeyard import __version__
from forgeyard.agent import config, state

WORKLOAD_USER = "forgeyard-run"
ROOT_MODEL_CACHE = "/srv/forgeyard-models"


def in_container() -> bool:
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


def _user_exists(name: str) -> bool:
    try:
        pwd.getpwnam(name)
        return True
    except KeyError:
        return False


def _gpus(simulate: Optional[str]) -> tuple[list[dict], dict]:
    from forgeyard.agent.inventory import Inventory, SimulatedInventory

    inv = SimulatedInventory(simulate) if simulate else Inventory()
    try:
        data = inv.full()
    finally:
        inv.close()
    return [a for a in data["accelerators"] if a["vendor"] != "cpu"], data["collectors"]


def _gpu_lines(gpus: list[dict], collectors: dict) -> list[str]:
    if not gpus:
        why = "; ".join(f"{name}: {c.get('reason')}" for name, c in collectors.items()
                        if name in ("nvidia", "amd") and c.get("reason"))
        return [f"GPUs: none found{f' ({why})' if why else ''}. This machine runs CPU work."]
    counts: dict[str, int] = {}
    for a in gpus:
        counts[a["model"]] = counts.get(a["model"], 0) + 1
    driver = next((a.get("driver") for a in gpus if a.get("driver")), None)
    return [f"GPUs: {', '.join(f'{n} x {m}' for m, n in counts.items())}"
            + (f" (driver {driver})" if driver else "")
            + ". Choose which ones Forgeyard may use on the Machines page."]


def detect(args, res: config.Resolved) -> tuple[dict, dict, list[str]]:
    """The values to write, a note per setting saying why, and the header lines."""
    from forgeyard.agent.workloads import container as engines
    from forgeyard.agent.workloads.process import systemd_running

    values = dict(res.values)
    notes: dict[str, str] = {}
    given = {k for k, src in res.sources.items() if src != "default"}
    root = os.geteuid() == 0
    boxed = in_container()

    gpus, collectors = _gpus(getattr(args, "simulate_inventory", None))
    header = [f"Forgeyard agent settings, written by `forgeyard agent init` ({__version__}).",
              "Every setting can also be given as a flag or a FORGEYARD_* environment variable, which win",
              "over this file. `forgeyard agent config` shows what is in effect; the guide's Reference lists",
              "every setting.", ""] + _gpu_lines(gpus, collectors)

    if "name" not in given:
        values["name"] = socket.gethostname()
    if "state_dir" not in given:
        values["state_dir"] = str(state.default_state_dir())

    if "workloads.container_engine" not in given:
        found = engines.detect("auto")
        if found.get("ok"):
            values["workloads.container_engine"] = found["engine"]
            gpu = (f"; NVIDIA GPUs via {found.get('gpu_mode')}" if found.get("nvidia") else
                   "; it cannot pass NVIDIA GPUs (no NVIDIA Container Toolkit or CDI)"
                   if any(a["vendor"] == "nvidia" for a in gpus) else "")
            notes["workloads.container_engine"] = f"Found: {found['engine']} {found.get('version') or ''}".rstrip() + gpu + "."
        else:
            values["workloads.container_engine"] = "none"
            notes["workloads.container_engine"] = (f"No container engine found ({found.get('reason')}): only process "
                                                   "workloads. Install Docker or Podman, then set auto.")

    if "workloads.limits" not in given:
        if boxed:
            values["workloads.limits"] = "rlimit"
            notes["workloads.limits"] = "Running in a container: there is no systemd here."
        elif root and systemd_running():
            values["workloads.limits"] = "auto"
            notes["workloads.limits"] = "systemd found: process workloads get a scope with CPU and memory caps."
        else:
            values["workloads.limits"] = "rlimit"
            notes["workloads.limits"] = ("No systemd here: rlimits only." if root else
                                         "The agent is not root: rlimits only (a systemd scope needs root).")

    if "workloads.user" not in given:
        if root:
            if _user_exists(WORKLOAD_USER):
                values["workloads.user"] = WORKLOAD_USER
            else:
                values["workloads.user"] = "nobody"
                notes["workloads.user"] = (f"A dedicated account is better: useradd --system --no-create-home "
                                           f"--shell /usr/sbin/nologin {WORKLOAD_USER}, then set it here.")
        else:
            notes["workloads.user"] = "The agent is not root: process workloads run as its own user."

    if "models.cache" not in given:
        values["models.cache"] = ROOT_MODEL_CACHE if root else str(Path.home() / ".cache" / "forgeyard-models")
        notes["models.cache"] = "Put it on a disk with room: models take from 1 to over 100 GB each."
    return values, {k: v for k, v in notes.items() if v}, header
