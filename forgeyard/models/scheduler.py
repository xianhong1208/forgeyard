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
  then the machine name;
- (1.3.1) before all of that, a replica that is restarted goes back to the machine it ran on, when
  that machine still takes it.

When nothing fits, every machine says why, and by how much memory it falls short. When exclusive
cards are what is in the way and the memory would be enough, it says that it fits if both share VRAM
(1.3.1), and the preview of an exclusive deployment that needs half a card or less says so too.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard.config import get_settings
from forgeyard.db import Accelerator, Node, Replica, utcnow
from forgeyard.machines.protocol import FEATURE_ENGINE_ZIP, FEATURE_MODELS, FEATURE_REPLICA_GUARD, FEATURE_WORKLOADS
from forgeyard.models import render, sources
from forgeyard.models.catalog import Context, context_for, deployment_of, model_block
from forgeyard.models.estimate import EstimateError, gib
from forgeyard.models.recipes import Variant, variant_matches, variant_params_problem
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
    # 1.3.1: exclusive cards are in the way, but the memory is there: {gpus, holders, both}.
    share_fits: Optional[dict] = None

    def as_dict(self) -> dict:
        out = {"machine_id": self.machine_id, "name": self.name, "status": self.status, "fits": self.fits,
               "reason": self.reason, "short_by": self.short_by, "cards": self.cards}
        if self.share_fits is not None:
            out["share_fits"] = self.share_fits
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
    if variant.engine is not None and variant.engine.url.lower().endswith(".zip") \
            and FEATURE_ENGINE_ZIP not in (node.features or []):
        return "its agent cannot unpack .zip engines (update the agent)"
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


def _share_fits(ctx: Context, avail: list[tuple[int, Accelerator]], cards: dict[str, list[Hold]]
                ) -> Optional[tuple[dict, str]]:
    """Exclusive cards are in the way: would the memory be enough if the replica and the ones holding
    the cards shared VRAM? Then which cards, who holds them, and the sentence that says so."""
    need = ctx.amount
    roomy = sorted(((free, a) for free, a in avail if free >= need), key=lambda t: (t[0], t[1].index))
    if len(roomy) < ctx.gpus:
        return None
    chosen = roomy[:ctx.gpus]
    holds = [h for _, a in chosen for h in cards.get(a.key, [])]
    if not holds:
        return None
    holders = list(dict.fromkeys(h.workload for h in holds))
    exclusive = list(dict.fromkeys(h.workload for h in holds if h.share != "vram"))
    where = ", ".join(f"#{a.index} has {gib(max(0, free))} available" for free, a in chosen)
    if ctx.share == "exclusive" and exclusive:
        text = f"fits if both share VRAM: {where}, held by {', '.join(holders)}"
    elif ctx.share == "exclusive":
        text = f"fits if this deployment shares VRAM: {where}, shared by {', '.join(holders)}"
    else:
        text = f"fits if {', '.join(exclusive)} shares VRAM too: {where}"
    return ({"gpus": [a.index for _, a in chosen], "holders": holders, "exclusive": exclusive,
             "both": ctx.share == "exclusive" and bool(exclusive)}, text)


def evaluate_node(db: DbSession, node: Node, ctx: Context, *, exclude: Optional[int] = None,
                  extra: Optional[dict[str, list[Hold]]] = None, release: Collection[int] = (),
                  credit: Optional[dict[str, int]] = None) -> Verdict:
    """Can this machine take one replica, and where. `release`: replicas that restart (an edit) and so
    hold nothing; `credit`: memory per card that a replica restarting right now still uses there."""
    v = Verdict(node.id, node.name, node.status, False)
    if node.revoked_at is not None or node.status == "revoked":
        v.reason = "revoked"
        return v
    feats = node.features or []
    if FEATURE_WORKLOADS not in feats or FEATURE_MODELS not in feats:
        v.reason = "its agent does not serve models (update it and start it with --model-cache)"
        return v
    if ctx.recipe.builtin and not ctx.recipe.api_key_env and FEATURE_REPLICA_GUARD not in feats:
        # 1.3: an engine with no API key of its own (stable-diffusion.cpp) is closed only by the agent's
        # key guard; without one, its port would answer anyone who can reach it.
        v.reason = f"{ctx.recipe.engine} has no API key of its own and the agent has no key guard (update the agent)"
        return v
    used = placement.usage(db, node.id, exclude, node=node, release=release)
    back = dict(used.credit)
    for k, b in (credit or {}).items():
        back[k] = back.get(k, 0) + b
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
            rt = variant_params_problem(variant, ctx.params) or _runtime_problem(node, variant)
            if rt:
                problems.append(f"{variant.name}: {rt}")
                continue
            fitting, avail = [], []
            for a in compatible:
                holds = cards.get(a.key, [])
                free = placement.available(a, holds, back.get(a.key, 0))
                avail.append((free, a))
                if placement.card_problem(a, holds, ctx.share, need, back.get(a.key, 0)) is None:
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
                text = (f"needs {ctx.gpus} GPU{'s' if ctx.gpus > 1 else ''} it can have; in use: {', '.join(busy) or 'none'}"
                        + ("" if ctx.share == "vram" else " (this deployment does not share cards)"))
                sharing = _share_fits(ctx, avail, cards)
                if sharing is not None:
                    v.share_fits = sharing[0]
                    text += f"; {sharing[1]}"
                shortfalls.append((need, text))
        else:
            cpu = next((a for a in node.accelerators if a.vendor == "cpu"), None)
            if cpu is None:
                continue
            why = variant_matches(variant, _hw(cpu))
            if why:
                problems.append(f"{variant.name}: {why}")
                continue
            matched_hw = True
            rt = variant_params_problem(variant, ctx.params) or _runtime_problem(node, variant)
            if rt:
                problems.append(f"{variant.name}: {rt}")
                continue
            cores = float(ctx.params.get("threads") or 0)
            threads = float((cpu.detail or {}).get("threads") or 0)
            if cores and threads and used.cpu + cores > threads:
                problems.append(f"{variant.name}: needs {cores:g} CPU threads, {max(0.0, threads - used.cpu):g} free "
                                f"of {threads:g}")
                continue
            free = placement.available(cpu, ram_holds, back.get(cpu.key, 0))
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
        # Memory is not what is missing when sharing would fit (1.3.1): no "short" figure then.
        v.short_by, v.reason = (None if v.share_fits is not None and "fits if" in text else short), text
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


def choose(verdicts: list[Verdict], machine_id: Optional[int], strategy: str = "binpack",
           siblings: Optional[set[int]] = None, prefer: Optional[int] = None) -> Optional[Option]:
    """The best option: first the machine a restarted replica ran on (`prefer`), when it still takes
    it; then a machine without another replica of the same deployment (`siblings`: their machines),
    so replicas spread over machines and the gateway can balance and fail over between them; then
    binpack; then a machine with the model cached; then by name."""
    usable = [v.option for v in verdicts if v.fits and (v.status == "online" or v.machine_id == machine_id)]
    if not usable:
        return None
    sign = 1 if strategy == "binpack" else -1
    taken = siblings or set()

    def key(o: Option):
        return (prefer is not None and o.node.id != prefer, o.node.id in taken, sign * (o.leftover // GIB),
                not o.cached, o.node.name, [a.index for a in o.cards])

    return min(usable, key=key)


def evaluate(db: DbSession, ctx: Context, *, exclude: Optional[int] = None,
             extra: Optional[dict[int, dict[str, list[Hold]]]] = None, release: Collection[int] = (),
             credit: Optional[tuple[int, dict[str, int]]] = None) -> list[Verdict]:
    return [evaluate_node(db, n, ctx, exclude=exclude, extra=(extra or {}).get(n.id), release=release,
                          credit=credit[1] if credit is not None and credit[0] == n.id else None)
            for n in candidates(db, ctx.machine_id)]


# An exclusive deployment that needs at most this share of the card it gets is told that sharing VRAM
# would leave the rest of the card for other models (1.3.1).
SHARE_HINT_FRACTION = 0.5


def _an(text: str) -> str:
    return "an" if re.match(r"8|1[18](\D|$)", text) else "a"   # an 80 GiB card, an 11 GiB card


def share_hint(ctx: Context, card: Optional[Accelerator], machine: Optional[str]) -> Optional[dict]:
    """For an exclusive GPU deployment that needs half the card (or less): it could share it."""
    if ctx.device != "gpu" or ctx.share != "exclusive" or card is None or not card.vram_total:
        return None
    if ctx.amount > card.vram_total * SHARE_HINT_FRACTION:
        return None
    total = gib(card.vram_total)
    return {"need": ctx.amount, "card_total": card.vram_total, "card_model": card.model, "machine": machine,
            "gpu": card.index,
            "text": f"This model needs {gib(ctx.amount)}{' per GPU' if ctx.gpus > 1 else ''} of {_an(total)} {total} "
                    "card; with Share VRAM the rest can hold other models. They will share compute."}


def _largest_card(db: DbSession, verdicts: list[Verdict]) -> tuple[Optional[Accelerator], Optional[str]]:
    best: tuple[Optional[Accelerator], Optional[str]] = (None, None)
    for v in verdicts:
        for c in v.cards:
            if c.get("index") is None or not c.get("total"):
                continue
            if best[0] is None or c["total"] > (best[0].vram_total or 0):
                a = db.scalar(select(Accelerator).where(Accelerator.node_id == v.machine_id,
                                                        Accelerator.index == c["index"], Accelerator.vendor != "cpu"))
                if a is not None:
                    best = (a, v.name)
    return best


def preview(db: DbSession, ctx: Context, *, release: Collection[int] = (),
            prefer: Optional[dict[int, int]] = None) -> dict:
    """Where the deployment's replicas would go now, one after the other, without placing them.
    `release` and `prefer` (an edit, 1.3.1): the deployment's own replicas restart, so they hold
    nothing, and replica i goes back to machine prefer[i] when that machine still takes it."""
    extra: dict[int, dict[str, list[Hold]]] = {}
    placed = []
    first: Optional[list[Verdict]] = None
    hint_card: tuple[Optional[Accelerator], Optional[str]] = (None, None)
    for i in range(ctx.replicas):
        verdicts = evaluate(db, ctx, extra=extra, release=release)
        if first is None:
            first = verdicts
        opt = choose(verdicts, ctx.machine_id, siblings={p["machine_id"] for p in placed},
                     prefer=(prefer or {}).get(i))
        if opt is None:
            if hint_card[0] is None:
                hint_card = _largest_card(db, first or verdicts)
            return {"fits": False, "placed": placed, "failed_replica": i,
                    "machines": [v.as_dict() for v in verdicts], "summary": summary(verdicts, ctx),
                    "share_hint": share_hint(ctx, *hint_card)}
        if i == 0 and opt.cards:
            hint_card = (opt.cards[0], opt.node.name)
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
        entry["previous"] = (prefer or {}).get(i) == opt.node.id
        placed.append(entry)
    return {"fits": True, "placed": placed, "machines": [v.as_dict() for v in first or []],
            "share_hint": share_hint(ctx, *hint_card)}


def summary(verdicts: list[Verdict], ctx: Context) -> str:
    what = f"{gib(ctx.amount)} per GPU" if ctx.device == "gpu" else f"{gib(ctx.amount)} of RAM"
    if not verdicts:
        return "No machine has joined." if ctx.machine_id is None else "The chosen machine no longer exists."
    lines = [f"{v.name}{'' if v.status == 'online' else ' (' + v.status + ')'}: "
             f"{v.reason or ('fits' if v.fits else 'cannot run it')}" for v in verdicts]
    return f"Nothing can take a replica that needs {what}. " + "; ".join(lines)


def place_replica(db: DbSession, r: Replica, credit: Optional[tuple[int, dict[str, int]]] = None) -> Optional[int]:
    """Place one pending replica of a model deployment. Returns the machine id, or None (the
    reason is on the replica). Called with placement's lock held; does not commit. `credit`: the
    replica is restarting right now, and this is its machine and the memory it still uses there."""
    from forgeyard.workloads.state import event

    dep = deployment_of(db, r.workload_id)
    ctx = context_for(db, dep) if dep is not None else None
    if ctx is None:
        reason = "Cannot be placed: its deployment or recipe is gone"
        if reason != r.reason:
            r.reason, r.updated_at = reason, utcnow()
        return None
    verdicts = evaluate(db, ctx, exclude=r.id, credit=credit)
    siblings = {o.node_id for o in r.workload.replicas if o.id != r.id and o.node_id is not None and not o.final}
    before = placement.previous(r.id)
    opt = choose(verdicts, ctx.machine_id, siblings=siblings, prefer=before)
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
    r.gpus = [placement.gpu_ref(a, kfd=opt.variant.amd_kfd) for a in opt.cards]
    r.vram = ctx.amount
    r.spec = spec
    r.model = model_block(ctx.model)
    r.engine = render.engine_block(ctx, opt.variant)
    # A fresh internal key for every placement (M4): only the gateway, which adds it, can use the
    # engine. An agent with the key guard requires it on every path, whatever the engine; elsewhere
    # only an engine that reads a key (the recipe's api_key_env) gets one.
    guard = FEATURE_REPLICA_GUARD in (opt.node.features or [])
    r.secret = secrets.token_urlsafe(32) if (ctx.recipe.api_key_env or guard) else None
    r.state = "pending"
    r.download = None
    r.reason = None if opt.node.status == "online" else f"waiting for {opt.node.name} to come online"
    r.updated_at = utcnow()
    placement.forget(r.id)
    where = f" with GPU {', '.join(f'#{a.index}' for a in opt.cards)}" if opt.cards else " on the CPU"
    util = ""
    if opt.cards and ctx.recipe.engine == "vllm":
        util = f", gpu_memory_utilization {render.values(ctx, opt.variant, opt.cards, gpu_context())['gpu_util']}"
    again = " again, where it ran before," if opt.node.id == before else ""
    event(db, r.workload, "placed", f"Replica {r.ordinal} placed{again} on {opt.node.name}{where} ({opt.variant.name}; "
                                    f"reserves {gib(ctx.amount)}{util}"
                                    f"{'; model already cached there' if opt.cached else ''}).", r.ordinal)
    db.flush()
    return opt.node.id
