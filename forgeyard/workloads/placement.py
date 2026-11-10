"""Placement: the machine the person picked, or the first online machine that has enough.

M2 workloads keep their simple rules:

- the machine can run the workload at all (agent with workloads, the runtime,
  NVIDIA-in-containers support);
- the CPU and memory that the replicas already there declared are added up against the
  machine's threads and RAM;
- GPUs come **only from the machine's allowlist** (all cards when no allowlist is set), and are
  **exclusive** by default: one card, one replica.

M3 adds VRAM (forgeyard.models.scheduler builds on this for model deployments):

- a workload may declare the memory it needs on each card (`gpus.vram`) and `share: vram`: such
  workloads share a card, each **reserving** its amount;
- what is available on a card is what its agent last reported **free** (the cards may be shared
  with work outside Forgeyard), minus the reservations of Forgeyard replicas that are not yet
  using their memory (pending, starting, or ready so recently that the agent's numbers predate
  it), minus a small headroom. Never the card's total.

Placement holds a process-wide lock, so two requests (or the background sweep) never give the
same card, or the same memory, to two replicas.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from datetime import timedelta

from forgeyard.config import get_settings
from forgeyard.db import Accelerator, Node, Replica, Workload, as_utc, utcnow
from forgeyard.machines.protocol import FEATURE_WORKLOADS
from forgeyard.workloads.spec import WorkloadSpec

_lock = threading.Lock()

GIB = 1024**3
VENDOR_LABEL = {"nvidia": "NVIDIA", "amd": "AMD"}


def is_gpu(a: Accelerator) -> bool:
    return a.vendor != "cpu"


def is_allowed(node: Node, a: Accelerator) -> bool:
    """May Forgeyard assign this card? Never the CPU row; with an allowlist, only cards on it."""
    if not is_gpu(a):
        return False
    return node.gpu_allowlist is None or a.key in node.gpu_allowlist


def _size(n: float) -> str:
    return f"{n / GIB:.1f} GiB".replace(".0 GiB", " GiB")


def _cards(indices) -> str:
    return ", ".join(f"#{i}" for i in indices) or "none"


def holding(db: DbSession, node_id: int) -> list[Replica]:
    """Replicas that hold resources on this machine: placed, not finished, not confirmed stopped."""
    return list(db.scalars(select(Replica).where(Replica.node_id == node_id, Replica.final.is_(False),
                                                 Replica.state != "stopped")).all())


# The agent's free-memory numbers must be at least this much newer than the moment a replica
# became ready before its reservation stops counting (the load itself is then in the numbers).
SETTLE = timedelta(seconds=2)


@dataclass
class Hold:
    """One replica's claim on a card (or, with key "cpu", on the machine's RAM)."""
    workload: str
    share: str               # exclusive | vram
    vram: Optional[int]      # bytes reserved; None: an M2 exclusive workload that declared none
    pending: bool            # the reservation is not yet visible in the reported free memory


@dataclass
class Usage:
    cpu: float = 0.0
    memory: int = 0
    gpu_keys: dict[str, str] = field(default_factory=dict)  # key -> workload name
    cards: dict[str, list[Hold]] = field(default_factory=dict)
    ram: list[Hold] = field(default_factory=list)


def replica_spec(r: Replica) -> dict:
    """What the replica runs: its own rendered spec (model deployments) or its workload's."""
    return r.spec or r.workload.spec or {}


def _pending(r: Replica, reported_at) -> bool:
    if r.ready_at is None or r.state not in ("healthy", "running", "unhealthy"):
        return True
    reported = as_utc(reported_at)
    return reported is None or reported < as_utc(r.ready_at) + SETTLE


def usage(db: DbSession, node_id: int, exclude: Optional[int] = None, node: Optional[Node] = None) -> Usage:
    u = Usage()
    node = node or db.get(Node, node_id)
    seen = {a.key: a.updated_at for a in node.accelerators} if node is not None else {}
    cpu_key = next((a.key for a in node.accelerators if a.vendor == "cpu"), None) if node is not None else None
    for r in holding(db, node_id):
        if r.id == exclude:
            continue
        spec = replica_spec(r)
        res = spec.get("resources") or {}
        u.cpu += float(res.get("cpu") or 0)
        u.memory += int(res.get("memory") or 0)
        gpus = res.get("gpus") or {}
        share = gpus.get("share") or "exclusive"
        for g in r.gpus or []:
            u.gpu_keys[g["key"]] = r.workload.name
            vram = r.vram if r.vram is not None else gpus.get("vram")
            u.cards.setdefault(g["key"], []).append(
                Hold(r.workload.name, share, vram, _pending(r, seen.get(g["key"]))))
        if not r.gpus and r.vram:
            u.ram.append(Hold(r.workload.name, "vram", r.vram, _pending(r, seen.get(cpu_key))))
    return u


def headroom() -> int:
    return max(0, get_settings().vram_headroom_mib) * 1024**2


def available(a: Accelerator, holds: list[Hold]) -> int:
    """Memory Forgeyard may still promise on this card (or the RAM of the CPU row): what the agent
    reported free, minus reservations not yet in that number, minus the headroom."""
    pending = sum(h.vram or 0 for h in holds if h.pending)
    return int(a.vram_free or 0) - pending - headroom()


def card_problem(a: Accelerator, holds: list[Hold], share: str, vram: Optional[int]) -> Optional[str]:
    """Why this card cannot take a replica wanting `vram` bytes with this sharing, or None."""
    if holds and (share == "exclusive" or any(h.share != "vram" for h in holds)):
        return f"GPU #{a.index} is in use by {holds[0].workload}"
    if vram:
        free = available(a, holds)
        if free < vram:
            return (f"GPU #{a.index} has {_size(max(0, free))} available, needs {_size(vram)} "
                    f"({_size(vram - free)} short)")
    return None


def runtime_problem(node: Node, spec: WorkloadSpec) -> Optional[str]:
    """Why this machine can never run this workload as it is, or None."""
    if node.revoked_at is not None or node.status == "revoked":
        return "revoked"
    if FEATURE_WORKLOADS not in (node.features or []):
        return "its agent does not run workloads yet (update it to this Forgeyard version)"
    info = (node.runtime_info or {}).get(spec.runtime_type) or {}
    if not info.get("ok"):
        why = info.get("reason") or "not available"
        return f"no {spec.runtime_type} runtime ({why})"
    gpus = spec.resources.gpus
    if spec.runtime_type == "container" and gpus.wanted and gpus.vendor == "nvidia" and not info.get("nvidia"):
        return "its container engine cannot pass NVIDIA GPUs into containers (no NVIDIA runtime or CDI)"
    return None


def choose_gpus(node: Node, spec: WorkloadSpec, taken: dict[str, str],
                container_nvidia: bool = True, holds: Optional[dict[str, list[Hold]]] = None
                ) -> tuple[Optional[list[Accelerator]], Optional[str]]:
    """The cards for one replica, or the reason there are none. Only allowed, free cards (or, for
    workloads sharing VRAM, cards shared only with such workloads and with enough memory left)."""
    want = spec.resources.gpus
    if want.wanted == 0:
        return [], None
    if holds is None:
        holds = {k: [Hold(w, "exclusive", None, False)] for k, w in taken.items()}
    vendor = want.vendor
    cards = [a for a in node.accelerators if is_gpu(a) and (vendor is None or a.vendor == vendor)]
    allowed = [a for a in cards if is_allowed(node, a)]
    allowed_text = _cards(sorted(a.index for a in node.accelerators if is_allowed(node, a)))
    if want.indices is not None:
        chosen = []
        for idx in want.indices:
            matches = [a for a in cards if a.index == idx]
            if len({a.vendor for a in matches}) > 1:
                return None, f"GPU #{idx} exists for more than one vendor here: choose the vendor"
            if not matches:
                return None, f"has no GPU #{idx}"
            card = matches[0]
            if not is_allowed(node, card):
                return None, f"GPU #{idx} is not allowed for Forgeyard here (allowed: {allowed_text})"
            problem = card_problem(card, holds.get(card.key, []), want.share, want.vram)
            if problem:
                return None, problem
            chosen.append(card)
        return chosen, None
    free = [a for a in allowed if card_problem(a, holds.get(a.key, []), want.share, want.vram) is None]
    if want.share == "vram":  # binpack: the fullest card that still fits first
        free.sort(key=lambda a: available(a, holds.get(a.key, [])))
    if spec.runtime_type == "container" and not container_nvidia:
        free = [a for a in free if a.vendor != "nvidia"]
    by_vendor: dict[str, list[Accelerator]] = {}
    for a in (free if want.share == "vram" else sorted(free, key=lambda a: (a.vendor, a.index))):
        by_vendor.setdefault(a.vendor, []).append(a)
    for group in by_vendor.values():
        if len(group) >= want.count:
            return group[: want.count], None
    label = f"{VENDOR_LABEL[vendor]} " if vendor else ""
    best = max((len(g) for g in by_vendor.values()), default=0)
    if not allowed:
        return None, f"no {label}GPU is allowed for Forgeyard here"
    if want.vram and len(allowed) == 1:
        return None, card_problem(allowed[0], holds.get(allowed[0].key, []), want.share, want.vram)
    return None, f"needs {want.count} free {label}GPU{'s' if want.count > 1 else ''}, {best} free of the allowed ({allowed_text})"


def fit(db: DbSession, node: Node, spec: WorkloadSpec, exclude: Optional[int] = None
        ) -> tuple[Optional[list[Accelerator]], Optional[str]]:
    """The GPUs to give one replica of `spec` on `node` (possibly none), or why it does not fit."""
    problem = runtime_problem(node, spec)
    if problem:
        return None, problem
    used = usage(db, node.id, exclude, node=node)
    cpu_row = next((a for a in node.accelerators if a.vendor == "cpu"), None)
    res = spec.resources
    if res.cpu and cpu_row is not None:
        threads = float((cpu_row.detail or {}).get("threads") or 0)
        if threads and used.cpu + res.cpu > threads:
            return None, f"needs {res.cpu:g} CPU cores, {max(0.0, threads - used.cpu):g} free of {threads:g}"
    if res.memory and cpu_row is not None and cpu_row.vram_total:
        if used.memory + res.memory > cpu_row.vram_total:
            free = max(0, cpu_row.vram_total - used.memory)
            return None, f"needs {_size(res.memory)} memory, {_size(free)} free of {_size(cpu_row.vram_total)}"
    container_nvidia = bool(((node.runtime_info or {}).get("container") or {}).get("nvidia"))
    return choose_gpus(node, spec, used.gpu_keys, container_nvidia, used.cards)


def gpu_ref(a: Accelerator) -> dict:
    """What the agent needs to give exactly this card to a replica."""
    detail = a.detail or {}
    ref = {"key": a.key, "vendor": a.vendor, "index": a.index, "uuid": a.uuid, "model": a.model,
           "pci_bus_id": detail.get("pci_bus_id")}
    if a.vendor == "amd":
        ref["rocr_uuid"] = detail.get("rocr_uuid")
        ref["render_minor"] = detail.get("render_minor")
    return ref


def _candidates(db: DbSession, spec: WorkloadSpec) -> list[Node]:
    q = select(Node).options(selectinload(Node.accelerators))
    if spec.machine_id is not None:
        node = db.scalar(q.where(Node.id == spec.machine_id))
        return [node] if node is not None else []
    return list(db.scalars(q.where(Node.status == "online").order_by(Node.name, Node.id)).all())


def place_pending(db: DbSession) -> set[int]:
    """Place every replica that wants to run and has no machine. Returns the machines whose
    desired state changed (the caller pokes their agents). Commits."""
    from forgeyard.workloads.state import event  # circular at import time

    changed: set[int] = set()
    with _lock:
        rows = db.scalars(
            select(Replica).join(Workload).where(Replica.node_id.is_(None), Replica.final.is_(False),
                                                 Workload.desired_state == "running")
            .order_by(Workload.id, Replica.ordinal)).all()
        for r in rows:
            if r.workload.runtime == "model":
                from forgeyard.models import scheduler

                placed = scheduler.place_replica(db, r)
                if placed is not None:
                    changed.add(placed)
                continue
            spec = WorkloadSpec.model_validate(r.workload.spec)
            reasons = []
            nodes = _candidates(db, spec)
            for node in nodes:
                gpus, why = fit(db, node, spec)
                if gpus is None:
                    reasons.append(f"{node.name}: {why}")
                    continue
                r.node_id = node.id
                r.gpus = [gpu_ref(a) for a in gpus]
                r.state = "pending"
                r.reason = None if node.status == "online" else f"waiting for {node.name} to come online"
                r.updated_at = utcnow()
                cards = f" with GPU {_cards(a.index for a in gpus)}" if gpus else ""
                event(db, r.workload, "placed", f"Replica {r.ordinal} placed on {node.name}{cards}.", r.ordinal)
                changed.add(node.id)
                db.flush()
                break
            else:
                if spec.machine_id is not None and not nodes:
                    reasons = ["the chosen machine no longer exists"]
                elif not nodes:
                    reasons = ["no machine is online"]
                reason = ("Cannot be placed: " + "; ".join(reasons))[:400]
                if reason != r.reason:
                    r.reason = reason
                    r.updated_at = utcnow()
                    event(db, r.workload, "unplaced", reason, r.ordinal)
        db.commit()
    return changed


def place_and_poke(db: DbSession) -> set[int]:
    """place_pending, then tell the agents of the machines that got work."""
    from forgeyard.machines.hub import hub

    changed = place_pending(db)
    for node_id in changed:
        hub.poke(node_id)
    return changed
