"""1.5.4: a machine's GPUs changed: cards added or removed since its agent last reported them.

The server keeps the GPUs it last trusted per machine in `nodes.runtime_info["gpu_set"]` ({key: summary}) and the
changes it saw in `runtime_info["gpu_changes"]` (newest last, the last MAX_CHANGES). No migration: both live in the
JSON column the agent's hello already fills, and survive a reconnect like the image listing does.

- A card is told by its key (its UUID, or for an AMD card its PCI address), never by its index: a swapped card is one
  removed and one added, and renumbered cards are no change.
- Only a vendor whose collector worked counts. When NVML or amd-smi fails (or the agent container lost its device
  access) the cards it did not list are not "removed": the last known ones stay, and nothing is recorded.
- The first report of a machine (a new machine, or one that joined before 1.5.4) only sets what is known.
- A key carried over to a new name (store.carry_over_keys) is no change.

Each change is also an audit entry, `node.gpus_changed` (actor "agent"): what was added and removed, model and
memory, and the deployments whose replicas were placed on a removed card. Setup health then says what to do while the
change is recent (advisor.machine_gpu_changes): allow a new card on a machine with a GPU access list, or know that it
is already in use; restart the deployments a removed card held.

Nothing here changes placement: a new card on a machine with a GPU access list is not allowed until an admin adds
it (the list fails closed), and a replica placed on a card that is gone keeps its place until it is restarted.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard import audit
from forgeyard.db import Deployment, Node, Replica, utcnow
from forgeyard.machines.messages import InventoryIn

log = logging.getLogger("forgeyard.machines")

SET_KEY = "gpu_set"
CHANGES_KEY = "gpu_changes"
KEPT_KEYS = (SET_KEY, CHANGES_KEY)
MAX_CHANGES = 20
GPU_VENDORS = ("nvidia", "amd")
VENDOR_LABEL = {"nvidia": "NVIDIA", "amd": "AMD"}


def summary(item) -> dict:
    """What a change says about one card: enough to recognise it on the machine and in the audit log."""
    detail = item.detail or {}
    out = {"key": item.key, "vendor": item.vendor, "index": item.index, "model": item.model or "Unknown",
           "vram_total": item.vram_total}
    if isinstance(detail.get("pci_bus_id"), str):
        out["pci_bus_id"] = detail["pci_bus_id"][:32]
    return out


def _trusted(inventory: InventoryIn, vendor: str) -> bool:
    c = inventory.collectors.get(vendor)
    return c is not None and c.ok is True


def placed_on(db: DbSession, node: Node, keys: set[str]) -> dict[str, list[str]]:
    """{card key: names of the deployments with a replica placed on it} for this machine's live replicas."""
    out: dict[str, list[str]] = {}
    replicas = db.scalars(select(Replica).where(Replica.node_id == node.id, Replica.final.is_(False))).all()
    workload_ids = {r.workload_id for r in replicas}
    names = dict(db.execute(select(Deployment.workload_id, Deployment.name)
                            .where(Deployment.workload_id.in_(workload_ids))).all()) if workload_ids else {}
    for r in replicas:
        for g in r.gpus or []:
            key = g.get("key") if isinstance(g, dict) else None
            if key in keys:
                name = names.get(r.workload_id)
                if name and name not in out.setdefault(key, []):
                    out[key].append(name)
    return out


def track(db: DbSession, node: Node, inventory: InventoryIn, renamed: dict[str, str],
          now: Optional[datetime] = None) -> Optional[dict]:
    """Compare this inventory's GPUs with the last trusted set; record a change (runtime info, audit, log) when cards
    were added or removed. Returns the change, or None."""
    info = dict(node.runtime_info or {})
    before = info.get(SET_KEY)
    reported = {a.key: summary(a) for a in inventory.accelerators if a.vendor in GPU_VENDORS}
    if not isinstance(before, dict):
        info[SET_KEY] = reported
        node.runtime_info = info
        return None
    before = {renamed.get(k, k): v for k, v in before.items() if isinstance(v, dict)}
    after: dict[str, dict] = {}
    for vendor in GPU_VENDORS:
        if _trusted(inventory, vendor):
            after.update({k: v for k, v in reported.items() if v["vendor"] == vendor})
        else:
            # Nothing to go by: keep what was known, and add what it reported anyway.
            after.update({k: v for k, v in before.items() if v.get("vendor") == vendor})
            after.update({k: v for k, v in reported.items() if v["vendor"] == vendor})
    added = [after[k] for k in after if k not in before]
    removed = [before[k] for k in before if k not in after]
    info[SET_KEY] = after
    if not added and not removed:
        node.runtime_info = info
        return None
    held = placed_on(db, node, {g["key"] for g in removed}) if removed else {}
    removed = [dict(g, deployments=held[g["key"]]) if held.get(g["key"]) else g for g in removed]
    change = {"at": (now or utcnow()).isoformat(), "added": added, "removed": removed,
              "gpus": len(after)}
    info[CHANGES_KEY] = ([c for c in info.get(CHANGES_KEY) or [] if isinstance(c, dict)] + [change])[-MAX_CHANGES:]
    node.runtime_info = info
    audit.record(db, "node.gpus_changed", actor_name="agent", target=node.name, machine_id=node.id,
                 added=added, removed=removed, gpus=change["gpus"])
    log.warning("%s: GPUs changed: %s", node.name, describe(change))
    return change


def label(g: dict) -> str:
    """"GPU #8 (NVIDIA A100 80GB PCIe, 80 GiB)"."""
    size = g.get("vram_total")
    memory = f", {round(size / 1024**3)} GiB" if isinstance(size, int) and size > 0 else ""
    return f"GPU #{g.get('index')} ({g.get('model') or 'Unknown'}{memory})"


def describe(change: dict) -> str:
    parts = []
    if change.get("added"):
        parts.append("added " + ", ".join(label(g) for g in change["added"]))
    if change.get("removed"):
        parts.append("removed " + ", ".join(label(g) for g in change["removed"]))
    return "; ".join(parts)


def changes_of(runtime_info: dict) -> list[dict]:
    value = (runtime_info or {}).get(CHANGES_KEY)
    return [c for c in value if isinstance(c, dict)] if isinstance(value, list) else []


def changes(node: Node) -> list[dict]:
    return changes_of(node.runtime_info or {})


# ------------------------------------------------------------------ the host's GPUs next to the agent's (gpu_census)
def annotate_census(runtime_info: dict) -> None:
    """An agent container started with `--gpus "device=0,2"` or `--gpus 2` (as its install report says) is limited to
    some NVIDIA GPUs on purpose, which its own environment does not show: mark the census so (`limited_by`)."""
    census = runtime_info.get("gpu_census")
    nvidia = (census or {}).get("vendors", {}).get("nvidia") if isinstance(census, dict) else None
    install = runtime_info.get("install")
    container = install.get("container") if isinstance(install, dict) else None
    gpus = container.get("gpus") if isinstance(container, dict) else None
    if not isinstance(nvidia, dict) or nvidia.get("limited_by") or not isinstance(gpus, str) or gpus in ("", "all"):
        return
    nvidia["limited_by"] = f"--gpus {gpus[:64]}"


def census_state(v: dict) -> Optional[str]:
    """For one vendor's census: None (the agent sees every GPU of the host), "unused" (it sees none: an agent run
    without that vendor's GPUs on purpose, say a CPU-only agent on a GPU host), "limited" (it was given some on purpose)
    or "hidden" (it sees some but not all, and nothing limits it: usually a GPU added after the container was made)."""
    host, visible = v.get("host", 0), v.get("visible", 0)
    if not isinstance(host, int) or not isinstance(visible, int) or host <= visible:
        return None
    if visible == 0:
        return "unused"
    return "limited" if v.get("limited_by") else "hidden"
