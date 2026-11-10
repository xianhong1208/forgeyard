"""Usage statistics: requests, errors, limit refusals, prompt and completion tokens and latency, per
UTC day, source (an API key, or the console's playground), key, user and model.

Requests are counted in memory and written to `usage_daily` every few seconds (and when the
server stops, and before the Usage page reads), so a busy gateway does not write the database
once per request. Latency is kept as a histogram per row (`BUCKETS_MS`), so p50 and p95 can be read
for any selection of rows; they are interpolated within a bucket.

Tokens come from the engine's own `usage` (prompt_tokens, completion_tokens; embeddings and rerank
report only input tokens). Only counting, never billing.
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

from forgeyard.db import ApiKey, UsageDaily, new_session

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
    latency_ms_sum: int = 0
    buckets: list[int] = field(default_factory=lambda: [0] * (len(BUCKETS_MS) + 1))


def day_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()


class Recorder:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._pending: dict[tuple, _Count] = {}

    def reset(self) -> None:
        with self._lock:
            self._pending.clear()

    def record(self, *, source: str, key_id: int, user_id: int, username: str, model: str,
               deployment_id: Optional[int], status: int, latency_ms: float, prompt_tokens: int = 0,
               completion_tokens: int = 0, limited: bool = False) -> None:
        slot = (day_of(self.clock()), source, int(key_id or 0), int(user_id or 0), username or "", (model or "")[:128])
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
            try:
                for (day, source, key_id, user_id, username, model), c in pending.items():
                    row = db.scalar(select(UsageDaily).where(
                        UsageDaily.day == day, UsageDaily.source == source, UsageDaily.key_id == key_id,
                        UsageDaily.user_id == user_id, UsageDaily.model == model))
                    if row is None:
                        row = UsageDaily(day=day, source=source, key_id=key_id, user_id=user_id, username=username,
                                         model=model, requests=0, errors=0, limited=0, prompt_tokens=0,
                                         completion_tokens=0, latency_ms_sum=0,
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
                    row.latency_ms_sum += c.latency_ms_sum
                    merged = list(row.latency_buckets or [])
                    merged += [0] * (len(c.buckets) - len(merged))
                    row.latency_buckets = [a + b for a, b in zip(merged, c.buckets)]
                db.commit()
            except Exception:
                db.rollback()
                with self._lock:  # keep the counts for the next try
                    for slot, c in pending.items():
                        self._pending.setdefault(slot, _Count())
                        mine = self._pending[slot]
                        mine.requests += c.requests
                        mine.errors += c.errors
                        mine.limited += c.limited
                        mine.prompt_tokens += c.prompt_tokens
                        mine.completion_tokens += c.completion_tokens
                        mine.latency_ms_sum += c.latency_ms_sum
                        mine.buckets = [a + b for a, b in zip(mine.buckets, c.buckets)]
                        mine.deployment_id = mine.deployment_id or c.deployment_id
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
GROUPS = ("day", "model", "key", "user", "source")


def summary(db: DbSession, *, days: int = 7, user_id: Optional[int] = None, deployment_id: Optional[int] = None,
            group_by: str = "model", today: Optional[str] = None) -> dict:
    """Totals and one row per group over the last `days` days (today included)."""
    end = datetime.fromisoformat(today).date() if today else datetime.now(timezone.utc).date()
    first = (end - timedelta(days=max(1, days) - 1)).isoformat()
    q = select(UsageDaily).where(UsageDaily.day >= first, UsageDaily.day <= end.isoformat())
    if user_id is not None:
        q = q.where(UsageDaily.user_id == user_id)
    if deployment_id is not None:
        q = q.where(UsageDaily.deployment_id == deployment_id)
    rows = db.scalars(q).all()
    keys = {k.id: k for k in db.scalars(select(ApiKey).where(ApiKey.id.in_({r.key_id for r in rows if r.key_id}))).all()}

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
        return r.model, r.model

    groups: dict[str, dict] = {}
    total = _empty("total", "Total")
    for r in rows:
        gid, name = label(r)
        g = groups.setdefault(gid, _empty(gid, name))
        for target in (g, total):
            _add(target, r)
    out = [_finish(g) for g in groups.values()]
    if group_by == "day":
        out.sort(key=lambda g: g["id"])
    else:
        out.sort(key=lambda g: (-g["requests"], g["name"]))
    return {"from": first, "to": end.isoformat(), "days": days, "group_by": group_by, "total": _finish(total),
            "rows": out}


def _empty(gid: str, name: str) -> dict:
    return {"id": gid, "name": name, "requests": 0, "errors": 0, "limited": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "latency_ms_sum": 0, "buckets": [0] * (len(BUCKETS_MS) + 1)}


def _add(g: dict, r: UsageDaily) -> None:
    g["requests"] += r.requests
    g["errors"] += r.errors
    g["limited"] += r.limited
    g["prompt_tokens"] += r.prompt_tokens
    g["completion_tokens"] += r.completion_tokens
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
