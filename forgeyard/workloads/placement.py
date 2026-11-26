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

1.4.2 (#125): a machine's host ports are a resource too. Its agent reports the range it publishes services
and model replicas on (`runtime_info["ports"]`, its --port-range); a replica takes one port of it per port it
publishes, two when the agent's key guard fronts it (the guard's published port and the engine's internal one).
The ports the replicas already there hold are counted, and a machine without enough free ports is skipped like
one without memory ("no free port: 3 of 3 in use"). A machine whose agent does not report its range (older
than 1.4.2) is not counted, as before. Ports something outside Forgeyard holds are not known here: the agent
skips them when it picks one, and says so if none is left.

1.4.5 (#166): memory is never promised twice.
- A model replica on a GPU that also keeps part of itself in the machine's RAM (llama.cpp's layers left on the CPU:
  its deployment's estimate says how much, `ram`) reserves that RAM too, like a CPU replica.
- On an APU whose GPU memory is the machine's RAM (a unified-memory card that reports its GTT pool, `pool: gtt`:
  the GPU takes it from system RAM, which the CPU row's numbers count as well), a reservation on the card is also
  one on the RAM, and the other way round: what is not yet in the agent's numbers counts against both, and the card
  never offers more than the RAM has free. A card that reports its BIOS carve-out (`pool: vram`) keeps memory the
  operating system never sees (it is not in MemTotal), so the two stay apart.

1.3.1: a replica that is restarted (started again, or restarted by an edit of its deployment) goes
back to the machine it ran on when that machine still takes it, instead of the best fit elsewhere
(`prefer`). And an edit restarts the replicas of a running workload one at a time (`roll`): the next
old one restarts once every replica on the new settings is ready.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from datetime import timedelta

from forgeyard.config import get_settings
from forgeyard.db import Accelerator, Node, Replica, Workload, as_utc, utcnow
from forgeyard.machines import gpu_access
from forgeyard.machines.protocol import FEATURE_WORKLOADS
from forgeyard.workloads.spec import WorkloadSpec

_lock = threading.Lock()

GIB = 1024**3
VENDOR_LABEL = {"nvidia": "NVIDIA", "amd": "AMD"}


def is_gpu(a: Accelerator) -> bool:
    return a.vendor != "cpu"


def shares_ram(a: Accelerator) -> bool:
    """Is this card's memory the machine's RAM (1.4.5, #166)? A unified-memory card (an APU) that reports its GTT
    pool, or that does not say which pool it reports (an agent older than 1.1.1: assume the worst). A carve-out
    (`pool: vram`) is set aside by the BIOS and is not in the RAM the CPU row reports."""
    return bool(a.unified_memory) and a.vendor != "cpu" and (a.detail or {}).get("pool") != "vram"


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
    # Memory of released replicas (see usage) that the agent's numbers still show in use, per card
    # key (the CPU row's key for RAM): it is free once they stop.
    credit: dict[str, int] = field(default_factory=dict)
    # Host ports of the machine's range the replicas hold (1.4.2): see ports_for.
    ports: int = 0


def replica_spec(r: Replica) -> dict:
    """What the replica runs: its own rendered spec (model deployments) or its workload's."""
    return r.spec or r.workload.spec or {}


def _pending(r: Replica, reported_at) -> bool:
    if r.ready_at is None or r.state not in ("healthy", "running", "unhealthy"):
        return True
    reported = as_utc(reported_at)
    return reported is None or reported < as_utc(r.ready_at) + SETTLE


def host_ram(db: DbSession, replicas: list[Replica]) -> dict[int, int]:
    """{workload id: bytes} of the RAM that GPU model replicas also keep (1.4.5, #166): their deployment's estimate
    (`ram` of a GPU estimate: llama.cpp's layers left on the CPU)."""
    from forgeyard.db import Deployment

    ids = {r.workload_id for r in replicas if r.gpus and r.workload.runtime == "model"}
    if not ids:
        return {}
    out = {}
    for wid, est in db.execute(select(Deployment.workload_id, Deployment.estimate).where(Deployment.workload_id.in_(ids))):
        est = est or {}
        if est.get("device") == "gpu" and int(est.get("ram") or 0) > 0:
            out[wid] = int(est["ram"])
    return out


def usage(db: DbSession, node_id: int, exclude: Optional[int] = None, node: Optional[Node] = None,
          release: Collection[int] = ()) -> Usage:
    """What the replicas on this machine hold. `release`: replicas about to restart (an edit of
    their deployment). They hold nothing, and what they use now is credited back on their cards."""
    u = Usage()
    node = node or db.get(Node, node_id)
    seen = {a.key: a.updated_at for a in node.accelerators} if node is not None else {}
    cpu_key = next((a.key for a in node.accelerators if a.vendor == "cpu"), None) if node is not None else None
    rows = holding(db, node_id)
    on_host = host_ram(db, rows)
    for r in rows:
        if r.id == exclude:
            continue
        host = on_host.get(r.workload_id, 0)
        if r.id in release:
            for key in [g["key"] for g in r.gpus or []] or ([cpu_key] if r.vram and cpu_key else []):
                if r.vram and not _pending(r, seen.get(key)):
                    u.credit[key] = u.credit.get(key, 0) + r.vram
            if host and cpu_key and not _pending(r, seen.get(cpu_key)):
                u.credit[cpu_key] = u.credit.get(cpu_key, 0) + host
            continue
        spec = replica_spec(r)
        u.ports += ports_for(spec, guarded=bool(r.secret))
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
        elif r.gpus and host:
            u.ram.append(Hold(r.workload.name, "vram", host, _pending(r, seen.get(cpu_key))))
    return u


def port_range(node: Optional[Node]) -> Optional[tuple[int, int]]:
    """The host port range the machine's agent publishes on (1.4.2), or None when it does not say (older agents)."""
    ports = ((node.runtime_info if node is not None else None) or {}).get("ports")
    if not isinstance(ports, dict):
        return None
    low, high = ports.get("low"), ports.get("high")
    if type(low) is not int or type(high) is not int or not 1 <= low <= high <= 65535:
        return None
    return low, high


def ports_for(spec: dict, guarded: bool) -> int:
    """Host ports one replica takes from its machine's range: one per published port, two when the agent's key
    guard fronts it (a model replica on an agent with the guard): the guard listens
    on the published port and the program on an internal one, both taken from the range (see the agent's
    supervisor). A replica is guarded exactly when it has an internal key (`Replica.secret`)."""
    count = len(spec.get("ports") or [])
    return count * 2 if guarded else count


def port_problem(node: Node, held: int, need: int) -> Optional[str]:
    """Why the machine has no room in its port range for a replica taking `need` ports, or None (also when its
    agent does not report the range: not counted, as before 1.4.2)."""
    span = port_range(node)
    if span is None or need <= 0:
        return None
    total = span[1] - span[0] + 1
    if held + need <= total:
        return None
    where = f"host ports {span[0]}-{span[1]}"
    if held >= total:
        return f"no free port: {total} of {total} in use ({where})"
    return f"not enough free ports: {held} of {total} in use, it needs {need} ({where})"


# How the agent says that no port of its range was free (forgeyard.agent.workloads.ports.allocate).
NO_FREE_PORT = "no free host port left in "


def out_of_ports(db: DbSession, r: Replica, node: Node) -> bool:
    """The agent reported the replica in backoff because no port of its range was free (1.4.2, #125). When this
    count agrees (the replica was placed before the server counted ports), take it off the machine: it is placed
    again on a machine with room, or waits unplaced with the reason, instead of restarting there in backoff
    (True: the caller's next placement pass does that). When the count says there is room, something outside
    Forgeyard holds the ports: the replica stays, and its agent tries again (False)."""
    from forgeyard.workloads.state import event, reset_replica

    if not (r.reason or "").startswith(NO_FREE_PORT):  # (a replica fetch_failed moved has no reason left)
        return False
    held = usage(db, node.id, exclude=r.id, node=node).ports
    problem = port_problem(node, held, ports_for(replica_spec(r), guarded=bool(r.secret)))
    if problem is None:
        return False
    event(db, r.workload, "unplaced", f"Replica {r.ordinal}: {node.name} has {problem}; placing it again.", r.ordinal)
    reset_replica(r, r.generation, replacing=True)
    db.flush()
    return True


def headroom() -> int:
    return max(0, get_settings().vram_headroom_mib) * 1024**2


def pending_bytes(holds: list[Hold]) -> int:
    return sum(h.vram or 0 for h in holds if h.pending)


def available(a: Accelerator, holds: list[Hold], credit: int = 0, *, also: int = 0, cap: Optional[int] = None) -> int:
    """Memory Forgeyard may still promise on this card (or the RAM of the CPU row): what the agent
    reported free, minus reservations not yet in that number, minus the headroom (plus `credit`:
    memory of replicas that are about to stop). 1.4.5 (#166), for memory one pool shares with another (an APU's
    card and the machine's RAM): `also`, what is reserved in the other pool and not yet in its numbers, and `cap`,
    the other pool's own free memory, which this one can never exceed."""
    free = int(a.vram_free or 0)
    if cap is not None:
        free = min(free, cap)
    return free + credit - pending_bytes(holds) - also - headroom()


def card_problem(a: Accelerator, holds: list[Hold], share: str, vram: Optional[int], credit: int = 0, *,
                 also: int = 0, cap: Optional[int] = None) -> Optional[str]:
    """Why this card cannot take a replica wanting `vram` bytes with this sharing, or None."""
    if holds and (share == "exclusive" or any(h.share != "vram" for h in holds)):
        return f"GPU #{a.index} is in use by {holds[0].workload}"
    if vram:
        free = available(a, holds, credit, also=also, cap=cap)
        if free < vram:
            shared = " (the machine's RAM: an APU)" if cap is not None else ""
            return (f"GPU #{a.index} has {_size(max(0, free))} available{shared}, needs {_size(vram)} "
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
                container_nvidia: bool = True, holds: Optional[dict[str, list[Hold]]] = None,
                skip_vendors: frozenset[str] = frozenset()
                ) -> tuple[Optional[list[Accelerator]], Optional[str]]:
    """The cards for one replica, or the reason there are none. Only allowed, free cards (or, for
    workloads sharing VRAM, cards shared only with such workloads and with enough memory left).
    `skip_vendors` (1.4.5, #202): vendors whose cards a new process here cannot open; not chosen."""
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
    free = [a for a in free if a.vendor not in skip_vendors]
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
    problem = port_problem(node, used.ports, ports_for({"ports": spec.ports}, guarded=False))
    if problem:
        return None, problem
    container_nvidia = bool(((node.runtime_info or {}).get("container") or {}).get("nvidia"))
    skip: frozenset[str] = frozenset()
    if spec.runtime_type == "process" and res.gpus.wanted:
        # 1.4.5 (#202): new processes there cannot open these cards (the agent's GPU access check); containers can.
        failing = gpu_access.failing_vendors(node)
        gpu_vendors = {a.vendor for a in node.accelerators if is_gpu(a)}
        if res.gpus.vendor in failing or (res.gpus.vendor is None and failing and gpu_vendors <= failing):
            return None, gpu_access.process_problem(node, failing if res.gpus.vendor is None else {res.gpus.vendor})
        skip = frozenset(failing)
    return choose_gpus(node, spec, used.gpu_keys, container_nvidia, used.cards, skip)


def gpu_ref(a: Accelerator, *, kfd: bool = True) -> dict:
    """What the agent needs to give exactly this card to a replica. `kfd` False (an AMD card for a
    Vulkan engine): the container gets the card's render node only, not /dev/kfd. Agents older than
    1.3 ignore the flag and pass /dev/kfd as well, which the engine then does not use."""
    detail = a.detail or {}
    ref = {"key": a.key, "vendor": a.vendor, "index": a.index, "uuid": a.uuid, "model": a.model,
           "pci_bus_id": detail.get("pci_bus_id")}
    if a.vendor == "amd":
        ref["rocr_uuid"] = detail.get("rocr_uuid")
        ref["render_minor"] = detail.get("render_minor")
        if not kfd:
            ref["kfd"] = False
    return ref


# ------------------------------------------------------------------ restarts (1.3.1)
# Where a replica that is being restarted ran: {replica id: machine id}. Placement tries that machine
# first and forgets the entry once the replica is placed. Kept in memory: after a server restart a
# replica still waiting is placed by the usual rules.
_previous: dict[int, int] = {}


def remember(r: Replica) -> None:
    """Note the replica's machine before it is reset for a restart."""
    if r.node_id is not None:
        if len(_previous) > 10_000:
            _previous.clear()
        _previous[r.id] = r.node_id


def previous(replica_id: int) -> Optional[int]:
    return _previous.get(replica_id)


def forget(replica_id: int) -> None:
    _previous.pop(replica_id, None)


def roll(db: DbSession) -> tuple[dict[int, tuple[int, dict[str, int]]], set[int]]:
    """Rolling restart: in every running workload with replicas older than its generation (its
    deployment was edited), restart the oldest-numbered old replica once every replica already on the
    new generation is ready, one at a time (replicas that ended for good were restarted by the edit
    itself). Returns, per restarted replica, its machine and the memory
    it still uses there (credited to its own placement in the same pass), and the machines to poke."""
    from forgeyard.workloads.state import event, is_ready, reset_replica

    credits: dict[int, tuple[int, dict[str, int]]] = {}
    changed: set[int] = set()
    # The ids first: PostgreSQL cannot compare the JSON columns a DISTINCT over whole rows would need.
    ids = db.scalars(select(Replica.workload_id).join(Workload, Replica.workload_id == Workload.id)
                     .where(Workload.desired_state == "running", Replica.generation < Workload.generation,
                            Replica.final.is_(False)).distinct()).all()
    rows = db.scalars(select(Workload).where(Workload.id.in_(ids)).options(selectinload(Workload.replicas))
                      .order_by(Workload.id)).all() if ids else []
    for w in rows:
        if any(not is_ready(r) for r in w.replicas if r.generation == w.generation):
            continue  # the replica restarted last is not ready yet (or failed): wait
        old = sorted((r for r in w.replicas if r.generation < w.generation and not r.final), key=lambda r: r.ordinal)
        if not old:
            continue
        r = old[0]
        node_id = r.node_id
        if node_id is not None:
            credits[r.id] = (node_id, usage(db, node_id, release={r.id}).credit)
            changed.add(node_id)
        remember(r)
        # A new id, so the agent sees a new replica: it stops the old one and starts this one with its
        # new internal key. With the same id and a new generation, an agent (1.3.0) takes the new key,
        # then drops it when it removes the old generation, and the new one waits for a key forever.
        r.uid = uuid.uuid4().hex
        reset_replica(r, w.generation, replacing=True)   # (after the new id: it is what is remembered)
        left = len(old) - 1
        event(db, w, "restarting", f"Replica {r.ordinal} restarts with the new settings"
                                   + (f"; {left} more after it, one at a time." if left else "."), r.ordinal)
    db.flush()
    return credits, changed


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
        credits, changed = roll(db)
        rows = db.scalars(
            select(Replica).join(Workload).where(Replica.node_id.is_(None), Replica.final.is_(False),
                                                 Workload.desired_state == "running")
            .order_by(Workload.id, Replica.ordinal)).all()
        for r in rows:
            if r.workload.runtime == "model":
                from forgeyard.models import scheduler

                placed = scheduler.place_replica(db, r, credit=credits.get(r.id))
                if placed is not None:
                    changed.add(placed)
                continue
            spec = WorkloadSpec.model_validate(r.workload.spec)
            reasons = []
            nodes = _candidates(db, spec)
            before = previous(r.id)
            nodes.sort(key=lambda n: n.id != before)  # where it ran last, if it still fits there
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
                forget(r.id)
                cards = f" with GPU {_cards(a.index for a in gpus)}" if gpus else ""
                again = " again, where it ran before," if node.id == before else ""
                event(db, r.workload, "placed", f"Replica {r.ordinal} placed{again} on {node.name}{cards}.", r.ordinal)
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
