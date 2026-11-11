"""Usage statistics: requests, errors, limit refusals, prompt and completion tokens and latency, per
UTC day, source (an API key, or the console's playground), key, user, model and target (1.2: "local",
or "pm:<id>" for a provider's model, so cloud traffic can be priced; plus cached and reasoning tokens
and the requests that spilled from the first tier).

Requests are counted in memory and written to `usage_daily` every few seconds (and when the
server stops, and before the Usage page reads), so a busy gateway does not write the database
once per request. Latency is kept as a histogram per row (`BUCKETS_MS`), so p50 and p95 can be read
for any selection of rows; they are interpolated within a bucket.

Tokens come from the engine's own `usage` (prompt_tokens, completion_tokens; embeddings and rerank
report only input tokens; providers' usage is read by forgeyard/gateway/quirks.py). Each request's
cost (forgeyard/gateway/pricing.py) is added to its row when it finishes: micro-USD, 0 for local
targets, and requests whose cost is unknown are counted as unpriced instead.

1.3 adds one counter per unit of the new model kinds (UNITS, migration 0012): media items sent in
chat (filled since multimodal chat), characters, images, kilopixels, media milliseconds, bytes in
and out, and busy milliseconds (filled by the PRs that add those kinds). Every one is 0 on rows
from before.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard.db import ApiKey, Provider, ProviderModel, UsageDaily, new_session
from forgeyard.gateway import pricing

log = logging.getLogger("forgeyard")

# Upper bounds (ms) of the latency buckets; one more bucket holds everything slower.
BUCKETS_MS = (50, 100, 200, 400, 800, 1600, 3200, 6400, 12800, 25600, 51200, 102400, 204800, 409600)


def bucket_of(ms: float) -> int:
    for i, bound in enumerate(BUCKETS_MS):
        if ms <= bound:
            return i
    return len(BUCKETS_MS)


def percentile(buckets: list[int], q: float) -> Optional[float]:
    """The q-quantile (0..1) in ms from bucket counts, interpolated within its bucket."""
    total = sum(buckets)
    if total == 0:
        return None
    rank = q * total
    seen = 0
    for i, n in enumerate(buckets):
        if n and seen + n >= rank:
            lo = BUCKETS_MS[i - 1] if i > 0 else 0
            hi = BUCKETS_MS[i] if i < len(BUCKETS_MS) else BUCKETS_MS[-1] * 2
            return round(lo + (hi - lo) * max(0.0, rank - seen) / n, 1)
        seen += n
    return float(BUCKETS_MS[-1])


@dataclass
class _Count:
    deployment_id: Optional[int] = None
    requests: int = 0
    errors: int = 0
    limited: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    spilled: int = 0
    cost_picos: int = 0
    unpriced: int = 0
    cost_reported: int = 0
    cost_estimated: int = 0
    media_items: int = 0
    characters: int = 0
    images: int = 0
    kilopixels: int = 0
    media_ms: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    busy_ms: int = 0
    latency_ms_sum: int = 0
    buckets: list[int] = field(default_factory=lambda: [0] * (len(BUCKETS_MS) + 1))

    def add_cost(self, picos: Optional[int], method: str) -> None:
        if picos is None or method == pricing.UNPRICED:
            self.unpriced += 1
            return
        self.cost_picos += max(0, int(picos))
        self.cost_reported += 1 if method == pricing.REPORTED else 0
        self.cost_estimated += 1 if method == pricing.ESTIMATED else 0

    def merge(self, c: "_Count") -> None:
        for name in COUNTS:
            setattr(self, name, getattr(self, name) + getattr(c, name))
        self.buckets = [a + b for a, b in zip(self.buckets, c.buckets)]
        self.deployment_id = self.deployment_id or c.deployment_id


# The units added in 1.3 (migration 0012): one column each in usage_daily, 0 when a request has none.
UNITS = ("media_items", "characters", "images", "kilopixels", "media_ms", "bytes_in", "bytes_out", "busy_ms")
COUNTS = ("requests", "errors", "limited", "prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens",
          "spilled", "cost_picos", "unpriced", "cost_reported", "cost_estimated", "latency_ms_sum") + UNITS


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


class Recorder:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._pending: dict[tuple, _Count] = {}
        # Per slot, what the written micro-dollars were rounded by (within half a micro-dollar either
        # way), taken into the next write so cheap requests add up exactly.
        self._carry: dict[tuple, int] = {}

    def reset(self) -> None:
        with self._lock:
            self._pending.clear()
            self._carry.clear()

    def record(self, *, source: str, key_id: int, user_id: int, username: str, model: str,
               deployment_id: Optional[int], status: int, latency_ms: float, prompt_tokens: int = 0,
               completion_tokens: int = 0, limited: bool = False, target: str = "local", cached_tokens: int = 0,
               reasoning_tokens: int = 0, spilled: bool = False, cost_picos: Optional[int] = 0,
               cost_method: str = pricing.LOCAL, units: Optional[dict] = None) -> None:
        """One finished request. cost_picos None (or cost_method "unpriced"): its cost is unknown.
        `units`: the request's counts of UNITS ({"media_items": 2}); unknown names are ignored."""
        slot = (day_of(self.clock()), source, int(key_id or 0), int(user_id or 0), username or "", (model or "")[:128],
                (target or "local")[:32])
        with self._lock:
            c = self._pending.get(slot)
            if c is None:
                c = self._pending[slot] = _Count()
            if deployment_id is not None:
                c.deployment_id = deployment_id
            c.requests += 1
            c.errors += 1 if status >= 400 and status != 499 else 0  # 499: the client went away
            c.limited += 1 if limited else 0
            c.prompt_tokens += max(0, int(prompt_tokens or 0))
            c.completion_tokens += max(0, int(completion_tokens or 0))
            c.cached_tokens += max(0, int(cached_tokens or 0))
            c.reasoning_tokens += max(0, int(reasoning_tokens or 0))
            c.spilled += 1 if spilled else 0
            c.add_cost(cost_picos, cost_method)
            for name, n in (units or {}).items():
                if name in UNITS:
                    setattr(c, name, getattr(c, name) + max(0, int(n or 0)))
            c.latency_ms_sum += int(latency_ms)
            c.buckets[bucket_of(latency_ms)] += 1

    def flush(self, db: Optional[DbSession] = None) -> int:
        """Write what was counted; returns the number of rows touched."""
        with self._flush_lock:
            with self._lock:
                pending, self._pending = self._pending, {}
            if not pending:
                return 0
            own = db is None
            db = db or new_session()
            carry: dict[tuple, int] = {}
            try:
                for slot, c in pending.items():
                    day, source, key_id, user_id, username, model, target = slot
                    picos = c.cost_picos + self._carry.get(slot, 0)
                    micros = pricing.micros_of(picos)
                    if picos != micros * pricing.PICOS_PER_MICRO:
                        carry[slot] = picos - micros * pricing.PICOS_PER_MICRO
                    row = db.scalar(select(UsageDaily).where(
                        UsageDaily.day == day, UsageDaily.source == source, UsageDaily.key_id == key_id,
                        UsageDaily.user_id == user_id, UsageDaily.model == model, UsageDaily.target == target))
                    if row is None:
                        row = UsageDaily(day=day, source=source, key_id=key_id, user_id=user_id, username=username,
                                         model=model, target=target, requests=0, errors=0, limited=0, prompt_tokens=0,
                                         completion_tokens=0, cached_tokens=0, reasoning_tokens=0, spilled=0,
                                         cost_micros=0, unpriced=0, cost_reported=0, cost_estimated=0,
                                         **{name: 0 for name in UNITS}, latency_ms_sum=0,
                                         latency_buckets=[0] * (len(BUCKETS_MS) + 1))
                        db.add(row)
                    if c.deployment_id is not None:
                        row.deployment_id = c.deployment_id
                    row.username = username or row.username
                    row.requests += c.requests
                    row.errors += c.errors
                    row.limited += c.limited
                    row.prompt_tokens += c.prompt_tokens
                    row.completion_tokens += c.completion_tokens
                    row.cached_tokens = (row.cached_tokens or 0) + c.cached_tokens
                    row.reasoning_tokens = (row.reasoning_tokens or 0) + c.reasoning_tokens
                    row.spilled = (row.spilled or 0) + c.spilled
                    row.cost_micros = (row.cost_micros or 0) + micros
                    row.unpriced = (row.unpriced or 0) + c.unpriced
                    row.cost_reported = (row.cost_reported or 0) + c.cost_reported
                    row.cost_estimated = (row.cost_estimated or 0) + c.cost_estimated
                    for name in UNITS:
                        setattr(row, name, (getattr(row, name) or 0) + getattr(c, name))
                    row.latency_ms_sum += c.latency_ms_sum
                    merged = list(row.latency_buckets or [])
                    merged += [0] * (len(c.buckets) - len(merged))
                    row.latency_buckets = [a + b for a, b in zip(merged, c.buckets)]
                db.commit()
                with self._lock:
                    for slot in pending:
                        self._carry.pop(slot, None)
                    self._carry.update(carry)
                    today = day_of(self.clock())
                    self._carry = {k: v for k, v in self._carry.items() if k[0] >= today}
            except Exception:
                db.rollback()
                with self._lock:  # keep the counts for the next try
                    for slot, c in pending.items():
                        self._pending.setdefault(slot, _Count()).merge(c)
                raise
            finally:
                if own:
                    db.close()
            return len(pending)

    async def run_forever(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(self.flush)
            except Exception:  # a locked database now and then must not stop the counting
                log.exception("Writing gateway usage failed; trying again")


recorder = Recorder()


# ------------------------------------------------------------------ reading
GROUPS = ("day", "model", "key", "user", "source", "target", "provider")


@dataclass
class TargetInfo:
    name: str                 # "Local replicas", "<provider>/<model>"
    provider_id: str          # "local", "p:<id>", "deleted"
    provider: str             # "Local replicas", the provider's name
    cloud: bool               # a provider outside your network


def summary(db: DbSession, *, days: int = 7, user_id: Optional[int] = None, deployment_id: Optional[int] = None,
            group_by: str = "model", today: Optional[str] = None) -> dict:
    """Totals and one row per group over the last `days` days (today included), with cost; the
    totals also split local (replicas and providers inside your network) from cloud."""
    end = datetime.fromisoformat(today).date() if today else datetime.now(timezone.utc).date()
    first = (end - timedelta(days=max(1, days) - 1)).isoformat()
    q = select(UsageDaily).where(UsageDaily.day >= first, UsageDaily.day <= end.isoformat())
    if user_id is not None:
        q = q.where(UsageDaily.user_id == user_id)
    if deployment_id is not None:
        q = q.where(UsageDaily.deployment_id == deployment_id)
    rows = db.scalars(q).all()
    keys = {k.id: k for k in db.scalars(select(ApiKey).where(ApiKey.id.in_({r.key_id for r in rows if r.key_id}))).all()}
    targets = target_info(db, {r.target or "local" for r in rows})

    def label(r: UsageDaily) -> tuple[str, str]:
        if group_by == "day":
            return r.day, r.day
        if group_by == "key":
            if not r.key_id:
                return "console", "Console playground"
            k = keys.get(r.key_id)
            return str(r.key_id), f"{k.name} ({k.prefix}…)" if k else f"deleted key #{r.key_id}"
        if group_by == "user":
            return str(r.user_id), r.username or f"user #{r.user_id}"
        if group_by == "source":
            return r.source, "API keys" if r.source == "api" else "Console playground"
        if group_by == "target":
            t = r.target or "local"
            return t, targets[t].name
        if group_by == "provider":
            t = targets[r.target or "local"]
            return t.provider_id, t.provider
        return r.model, r.model

    groups: dict[str, dict] = {}
    total = _empty("total", "Total")
    split = {"local": _empty("local", "Local"), "cloud": _empty("cloud", "Cloud")}
    for r in rows:
        gid, name = label(r)
        g = groups.setdefault(gid, _empty(gid, name))
        if group_by in ("target", "provider"):
            g["cloud"] = targets[r.target or "local"].cloud
        where = split["cloud" if targets[r.target or "local"].cloud else "local"]
        for target in (g, total, where):
            _add(target, r)
    out = [_finish(g) for g in groups.values()]
    if group_by == "day":
        out.sort(key=lambda g: g["id"])
    else:
        out.sort(key=lambda g: (-g["requests"], g["name"]))
    totals = _finish(total)
    totals["split"] = {k: _finish(v) for k, v in split.items()}
    return {"from": first, "to": end.isoformat(), "days": days, "group_by": group_by, "total": totals, "rows": out}


def _empty(gid: str, name: str) -> dict:
    return {"id": gid, "name": name, "requests": 0, "errors": 0, "limited": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0, "spilled": 0, "cost_micros": 0,
            "unpriced": 0, "cost_reported": 0, "cost_estimated": 0, **{name: 0 for name in UNITS}, "latency_ms_sum": 0,
            "buckets": [0] * (len(BUCKETS_MS) + 1)}


def _add(g: dict, r: UsageDaily) -> None:
    g["requests"] += r.requests
    g["errors"] += r.errors
    g["limited"] += r.limited
    g["prompt_tokens"] += r.prompt_tokens
    g["completion_tokens"] += r.completion_tokens
    for name in ("cached_tokens", "reasoning_tokens", "spilled", "cost_micros", "unpriced", "cost_reported",
                 "cost_estimated") + UNITS:
        g[name] += getattr(r, name) or 0
    g["latency_ms_sum"] += r.latency_ms_sum
    b = list(r.latency_buckets or [])
    b += [0] * (len(g["buckets"]) - len(b))
    g["buckets"] = [x + y for x, y in zip(g["buckets"], b)]


def _finish(g: dict) -> dict:
    buckets = g.pop("buckets")
    total_ms = g.pop("latency_ms_sum")
    g["tokens"] = g["prompt_tokens"] + g["completion_tokens"]
    g["latency_avg_ms"] = round(total_ms / g["requests"], 1) if g["requests"] else None
    g["latency_p50_ms"] = percentile(buckets, 0.50)
    g["latency_p95_ms"] = percentile(buckets, 0.95)
    return g


def target_info(db: DbSession, targets: set) -> dict[str, TargetInfo]:
    """"local" -> Local replicas; "pm:<id>" -> "<provider>/<model>", its provider and whether that is
    outside your network (a provider model deleted since: "deleted provider model #id", counted as
    cloud)."""
    ids = {int(t[3:]) for t in targets if isinstance(t, str) and t.startswith("pm:") and t[3:].isdigit()}
    rows = db.execute(select(ProviderModel.id, ProviderModel.upstream_model, Provider.id, Provider.name,
                             Provider.external)
                      .join(Provider, Provider.id == ProviderModel.provider_id).where(ProviderModel.id.in_(ids))).all() \
        if ids else []
    out = {t: TargetInfo(f"unknown target {t}", "deleted", "Deleted providers", True) for t in targets}
    out["local"] = TargetInfo("Local replicas", "local", "Local replicas", False)
    out.update({f"pm:{i}": TargetInfo(f"{p}/{m}", f"p:{pid}", p, bool(ext)) for i, m, pid, p, ext in rows})
    out.update({f"pm:{i}": TargetInfo(f"deleted provider model #{i}", "deleted", "Deleted providers", True)
                for i in ids if f"pm:{i}" not in {f"pm:{r[0]}" for r in rows}})
    return out

