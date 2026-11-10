"""The scheduler for model deployments (design section 5): filter, then score.

**Filter**, per machine and per recipe variant (in the recipe's order):
- the agent runs workloads and has a model cache; the machine is not revoked;
- a variant for the hardware: vendor, NVIDIA compute capability or AMD gfx target / CPU
  architecture, driver version;
- the variant's runtime works there (and passes NVIDIA GPUs into containers when it needs to);
- enough allowed cards, each with the memory one replica reserves available: the card's free
  memory as its agent reported it, minus Forgeyard reservations not yet in that number, minus a
  headroom. Cards are exclusive unless the deployment shares VRAM (`share: vram`), and a shared
  card is only ever shared with other sharing replicas;
- CPU replicas: the free RAM, the same way.

**Score**, among the machines that pass:
- model services **binpack**: the card (or machine) left with the least memory after placing
  wins, so big cards stay whole for big models; one-off work (spread) the opposite;
- on a tie (the same whole GiB left), a machine that already has the model in its cache wins,
  then the machine name.

When nothing fits, every machine says why, and by how much memory it falls short.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard.config import get_settings
from forgeyard.db import Accelerator, Node, Replica, utcnow
from forgeyard.machines.protocol import FEATURE_MODELS, FEATURE_WORKLOADS
from forgeyard.models import render, sources
from forgeyard.models.catalog import Context, context_for, deployment_of, model_block
from forgeyard.models.estimate import EstimateError, gib
from forgeyard.models.recipes import Variant, variant_matches
from forgeyard.workloads import placement
from forgeyard.workloads.placement import Hold

GIB = 1024**3


def gpu_context() -> int:
    return max(0, get_settings().gpu_context_mib) * 1024**2


@dataclass
class Option:
    node: Node
    variant: Variant
    cards: list[Accelerator]
    leftover: int          # memory left on the tightest chosen card (or RAM) after placing
    cached: bool

    def describe(self) -> str:
        where = f"GPU {', '.join(f'#{a.index}' for a in self.cards)}" if self.cards else "CPU"
        return f"{self.node.name} ({where}, {self.variant.name})"


@dataclass
class Verdict:
    machine_id: int
    name: str
    status: str
    fits: bool
    reason: str = ""
    short_by: Optional[int] = None
    option: Optional[Option] = None
    cards: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        out = {"machine_id": self.machine_id, "name": self.name, "status": self.status, "fits": self.fits,
               "reason": self.reason, "short_by": self.short_by, "cards": self.cards}
        if self.option is not None:
            out["variant"] = self.option.variant.name
            out["runtime"] = self.option.variant.runtime
            out["image"] = self.option.variant.image
            out["gpus"] = [a.index for a in self.option.cards]
            out["cached"] = self.option.cached
            out["leftover"] = self.option.leftover
        return out


def _hw(a: Accelerator) -> dict:
    return {"vendor": a.vendor, "arch": a.arch, "driver": a.driver}


def _cached(node: Node, ctx: Context) -> bool:
    try:
        key = sources.cache_key(model_block(ctx.model))
    except (sources.SourceError, KeyError):
        return False
    return key in ((node.model_cache or {}).get("cached") or [])


def _runtime_problem(node: Node, variant: Variant) -> Optional[str]:
    info = (node.runtime_info or {}).get(variant.runtime) or {}
    if not info.get("ok"):
        return f"no {variant.runtime} runtime ({info.get('reason') or 'not available'})"
    if variant.runtime == "container" and variant.when.vendor == "nvidia" and not info.get("nvidia"):
        return "its container engine cannot pass NVIDIA GPUs (no NVIDIA runtime or CDI)"
    return None


def _merge(cards: dict[str, list[Hold]], extra: dict[str, list[Hold]]) -> dict[str, list[Hold]]:
    out = {k: list(v) for k, v in cards.items()}
    for k, v in extra.items():
        out.setdefault(k, []).extend(v)
    return out


def evaluate_node(db: DbSession, node: Node, ctx: Context, *, exclude: Optional[int] = None,
                  extra: Optional[dict[str, list[Hold]]] = None) -> Verdict:
    v = Verdict(node.id, node.name, node.status, False)
    if node.revoked_at is not None or node.status == "revoked":
        v.reason = "revoked"
        return v
    feats = node.features or []
    if FEATURE_WORKLOADS not in feats or FEATURE_MODELS not in feats:
        v.reason = "its agent does not serve models (update it and start it with --model-cache)"
        return v
    used = placement.usage(db, node.id, exclude, node=node)
    cards = _merge(used.cards, extra or {})
    ram_holds = list(used.ram) + list((extra or {}).get("__ram__", []))
    need = ctx.amount
    want_gpu = ctx.device == "gpu"
    problems: list[str] = []
    shortfalls: list[tuple[int, str]] = []
    matched_hw = False
    for variant in ctx.recipe.variants:
        if variant.gpu != want_gpu:
            continue
        if want_gpu:
            compatible = []
            for a in node.accelerators:
                if not placement.is_gpu(a) or not placement.is_allowed(node, a):
                    continue
                why = variant_matches(variant, _hw(a))
                if why is None:
                    compatible.append(a)
                elif a.vendor == variant.when.vendor:
                    problems.append(f"{variant.name}: {why}")
            if not compatible:
                continue
            matched_hw = True
            rt = _runtime_problem(node, variant)
            if rt:
                problems.append(f"{variant.name}: {rt}")
                continue
            fitting, avail = [], []
            for a in compatible:
                holds = cards.get(a.key, [])
                free = placement.available(a, holds)
                avail.append((free, a))
                if placement.card_problem(a, holds, ctx.share, need) is None:
                    fitting.append((free, a))
            v.cards = [{"index": a.index, "available": max(0, free), "total": a.vram_total,
                        "shared_with": [h.workload for h in cards.get(a.key, [])]} for free, a in avail]
            if len(fitting) >= ctx.gpus:
                fitting.sort(key=lambda t: (t[0], t[1].index))  # binpack: the fullest that fits
                chosen = [a for _, a in fitting[:ctx.gpus]]
                left = min(free for free, a in fitting[:ctx.gpus]) - need
                v.fits, v.option = True, Option(node, variant, chosen, left, _cached(node, ctx))
                break
            # Not enough: by how much (memory), or why not at all (cards in use, exclusive).
            usable = sorted((free for free, a in avail if not cards.get(a.key)
                             or (ctx.share == "vram" and all(h.share == "vram" for h in cards.get(a.key, [])))),
                            reverse=True)
            if len(usable) >= ctx.gpus:
                short = need - usable[ctx.gpus - 1]
                which = ", ".join(f"#{a.index} {gib(max(0, f))}" for f, a in sorted(avail, key=lambda t: -t[0])[:3])
                shortfalls.append((short, f"needs {gib(need)} per GPU; available: {which} ({gib(short)} short)"))
            else:
                busy = [f"#{a.index} ({cards[a.key][0].workload})" for f, a in avail if cards.get(a.key)]
                shortfalls.append((need, f"needs {ctx.gpus} GPU{'s' if ctx.gpus > 1 else ''} it can have; "
                                         f"in use: {', '.join(busy) or 'none'}" +
                                         ("" if ctx.share == "vram" else " (this deployment does not share cards)")))
        else:
            cpu = next((a for a in node.accelerators if a.vendor == "cpu"), None)
            if cpu is None:
                continue
            why = variant_matches(variant, _hw(cpu))
            if why:
                problems.append(f"{variant.name}: {why}")
                continue
            matched_hw = True
            rt = _runtime_problem(node, variant)
            if rt:
                problems.append(f"{variant.name}: {rt}")
                continue
            cores = float(ctx.params.get("threads") or 0)
            threads = float((cpu.detail or {}).get("threads") or 0)
            if cores and threads and used.cpu + cores > threads:
                problems.append(f"{variant.name}: needs {cores:g} CPU threads, {max(0.0, threads - used.cpu):g} free "
                                f"of {threads:g}")
                continue
            free = placement.available(cpu, ram_holds)
            v.cards = [{"index": None, "available": max(0, free), "total": cpu.vram_total, "shared_with":
                        [h.workload for h in ram_holds]}]
            if free >= need:
                v.fits, v.option = True, Option(node, variant, [], free - need, _cached(node, ctx))
                break
            shortfalls.append((need - free, f"needs {gib(need)} of RAM, {gib(max(0, free))} available "
                                             f"({gib(need - free)} short)"))
    if v.fits:
        if node.status != "online":
            v.reason = "offline"
        return v
    if shortfalls:
        short, text = min(shortfalls, key=lambda t: t[0])
        v.short_by, v.reason = short, text
    elif problems:
        v.reason = "; ".join(dict.fromkeys(problems))[:300]
    elif not matched_hw:
        kind = "GPU" if want_gpu else "CPU"
        v.reason = f"no allowed {kind} that {ctx.recipe.name} supports"
    return v


def candidates(db: DbSession, machine_id: Optional[int]) -> list[Node]:
    q = select(Node).options(selectinload(Node.accelerators)).order_by(Node.name, Node.id)
    if machine_id is not None:
        q = q.where(Node.id == machine_id)
    return list(db.scalars(q).all())


def choose(verdicts: list[Verdict], machine_id: Optional[int], strategy: str = "binpack") -> Optional[Option]:
    usable = [v.option for v in verdicts if v.fits and (v.status == "online" or v.machine_id == machine_id)]
    if not usable:
        return None
    sign = 1 if strategy == "binpack" else -1

    def key(o: Option):
        return (sign * (o.leftover // GIB), not o.cached, o.node.name, [a.index for a in o.cards])

    return min(usable, key=key)


def evaluate(db: DbSession, ctx: Context, *, exclude: Optional[int] = None,
             extra: Optional[dict[int, dict[str, list[Hold]]]] = None) -> list[Verdict]:
    return [evaluate_node(db, n, ctx, exclude=exclude, extra=(extra or {}).get(n.id))
            for n in candidates(db, ctx.machine_id)]


def preview(db: DbSession, ctx: Context) -> dict:
    """Where the deployment's replicas would go now, one after the other, without placing them."""
    extra: dict[int, dict[str, list[Hold]]] = {}
    placed = []
    first: Optional[list[Verdict]] = None
    for i in range(ctx.replicas):
        verdicts = evaluate(db, ctx, extra=extra)
        if first is None:
            first = verdicts
        opt = choose(verdicts, ctx.machine_id)
        if opt is None:
            return {"fits": False, "placed": placed, "failed_replica": i,
                    "machines": [v.as_dict() for v in verdicts], "summary": summary(verdicts, ctx)}
        holds = extra.setdefault(opt.node.id, {})
        hold = Hold(f"replica {i}", ctx.share, ctx.amount, True)
        for a in opt.cards:
            holds.setdefault(a.key, []).append(hold)
        if not opt.cards:
            holds.setdefault("__ram__", []).append(hold)
        entry = {"replica": i, "machine": opt.node.name, "machine_id": opt.node.id, "variant": opt.variant.name,
                 "runtime": opt.variant.runtime, "image": opt.variant.image, "gpus": [a.index for a in opt.cards],
                 "cached": opt.cached, "leftover": opt.leftover, "online": opt.node.status == "online"}
        if opt.cards and ctx.recipe.engine == "vllm":
            entry["gpu_memory_utilization"] = render.values(ctx, opt.variant, opt.cards, gpu_context())["gpu_util"]
        placed.append(entry)
    return {"fits": True, "placed": placed, "machines": [v.as_dict() for v in first or []]}


def summary(verdicts: list[Verdict], ctx: Context) -> str:
    what = f"{gib(ctx.amount)} per GPU" if ctx.device == "gpu" else f"{gib(ctx.amount)} of RAM"
    if not verdicts:
        return "No machine has joined." if ctx.machine_id is None else "The chosen machine no longer exists."
    lines = [f"{v.name}{'' if v.status == 'online' else ' (' + v.status + ')'}: "
             f"{v.reason or ('fits' if v.fits else 'cannot run it')}" for v in verdicts]
    return f"Nothing can take a replica that needs {what}. " + "; ".join(lines)


def place_replica(db: DbSession, r: Replica) -> Optional[int]:
    """Place one pending replica of a model deployment. Returns the machine id, or None (the
    reason is on the replica). Called with placement's lock held; does not commit."""
    from forgeyard.workloads.state import event

    dep = deployment_of(db, r.workload_id)
    ctx = context_for(db, dep) if dep is not None else None
    if ctx is None:
        reason = "Cannot be placed: its deployment or recipe is gone"
        if reason != r.reason:
            r.reason, r.updated_at = reason, utcnow()
        return None
    verdicts = evaluate(db, ctx, exclude=r.id)
    opt = choose(verdicts, ctx.machine_id)
    if opt is None:
        reason = ("Cannot be placed: " + summary(verdicts, ctx))[:400]
        if reason != r.reason:
            r.reason, r.updated_at = reason, utcnow()
            event(db, r.workload, "unplaced", reason, r.ordinal)
        return None
    try:
        spec = render.render(ctx, opt.variant, opt.cards, r.workload.name, gpu_context=gpu_context())
    except EstimateError as e:
        r.reason = f"Cannot be placed: {e}"[:400]
        return None
    r.node_id = opt.node.id
    r.gpus = [placement.gpu_ref(a) for a in opt.cards]
    r.vram = ctx.amount
    r.spec = spec
    r.model = model_block(ctx.model)
    r.engine = render.engine_block(ctx, opt.variant)
    r.state = "pending"
    r.download = None
    r.reason = None if opt.node.status == "online" else f"waiting for {opt.node.name} to come online"
    r.updated_at = utcnow()
    where = f" with GPU {', '.join(f'#{a.index}' for a in opt.cards)}" if opt.cards else " on the CPU"
    util = ""
    if opt.cards and ctx.recipe.engine == "vllm":
        util = f", gpu_memory_utilization {render.values(ctx, opt.variant, opt.cards, gpu_context())['gpu_util']}"
    event(db, r.workload, "placed", f"Replica {r.ordinal} placed on {opt.node.name}{where} ({opt.variant.name}; "
                                    f"reserves {gib(ctx.amount)}{util}"
                                    f"{'; model already cached there' if opt.cached else ''}).", r.ordinal)
    db.flush()
    return opt.node.id
