"""Budgets (1.2): what cloud requests may cost per UTC calendar month, per API key, person and
provider, in micro-USD.

- A budget applies to requests that go to a provider outside your network. Local replicas and
  providers inside your network cost nothing and are never limited by a budget.
- **Reserved before sending.** Before a request is sent to a cloud provider, its estimated cost (the
  prompt's size, about 3 bytes a token on the high side, plus `max_tokens` or
  BUDGET_DEFAULT_MAX_TOKENS, at the provider model's prices) is reserved against every budget that
  applies: the key's, its person's and the provider's. If any of them cannot cover it (spent +
  reserved + this request > budget), the request is not sent to that provider: another target, or a
  local tier, may still serve it. When nothing else can, it gets 402 `budget_exhausted`.
- **Settled at the end** with the request's real cost (forgeyard/gateway/pricing.py); a request that
  failed or was refused releases its reservation. A request whose real cost is unknown (it was
  answered but reported no tokens) counts at its reservation, in memory, until the server restarts.
- **A model without a price** cannot be reserved: under any budget it is blocked (402
  `budget_unpriced`) until an admin sets its prices. Without a budget it is used as before.
- **Alerts** at 80% and 100% of a budget's spend: one audit event per scope, month and threshold
  (the table `budget_alerts` makes it once, also across restarts). A request refused by a budget
  that is already past 80% counts as reaching 100%.

The ledger lives in this one server process, under a lock, so concurrent requests cannot reserve
the same money twice. It is seeded once per month from `usage_daily.cost_micros` (after writing what
the usage recorder still holds), and settling a request and counting its usage happen under the
same lock, so a request is counted exactly once whether it finished before or after the seeding.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from forgeyard import audit
from forgeyard.db import BudgetAlert, ProviderModel, UsageDaily, new_session
from forgeyard.gateway import pricing
from forgeyard.gateway.errors import GatewayError
from forgeyard.gateway.quirks import Usage

log = logging.getLogger("forgeyard.gateway")

SCOPES = ("key", "person", "provider")
THRESHOLDS = (80, 100)
PICOS = pricing.PICOS_PER_MICRO           # pico-USD per micro-USD
BYTES_PER_TOKEN = 3
HEADER = "X-Forgeyard-Budget-Exhausted"
STATUS = 402                              # Payment Required: not retried by OpenAI's SDKs, unlike 429


# ------------------------------------------------------------------ months
def month_of(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m")


def next_month(month: str) -> str:
    y, m = int(month[:4]), int(month[5:7])
    return f"{y + (m == 12):04d}-{m % 12 + 1:02d}"


def resets_at(month: str) -> datetime:
    """When a month's budgets start again: the first of the next month, 00:00 UTC."""
    return datetime.fromisoformat(next_month(month) + "-01T00:00:00+00:00")


def usd(micros: Optional[int]) -> str:
    if micros is None:
        return "unknown"
    return f"${micros / 1_000_000:,.2f}" if micros >= 10_000 else f"${micros / 1_000_000:.6f}".rstrip("0")


# ------------------------------------------------------------------ what applies
@dataclass(frozen=True)
class Limit:
    scope: str                # key | person | provider
    id: int
    micros: int               # the budget
    name: str                 # for messages, alerts and the header

    @property
    def slot(self) -> tuple[str, int]:
        return self.scope, self.id

    @property
    def token(self) -> str:
        """What X-Forgeyard-Budget-Exhausted says: key, person or provider:<name>."""
        return f"provider:{self.name}" if self.scope == "provider" else self.scope

    def what(self) -> str:
        return {"key": f"this API key ({self.name})", "person": f"the person {self.name}",
                "provider": f"the provider {self.name}"}[self.scope]


def limits_for(caller: Any, provider_id: int, provider: str, provider_budget: Optional[int]) -> list[Limit]:
    """The budgets a request from `caller` to a cloud provider is reserved against."""
    out = []
    if getattr(caller, "source", "") == "api" and caller.key_id and caller.budget_micros is not None:
        out.append(Limit("key", caller.key_id, int(caller.budget_micros), caller.key_name or f"key #{caller.key_id}"))
    if caller.user_id and caller.user_budget_micros is not None:
        out.append(Limit("person", caller.user_id, int(caller.user_budget_micros), caller.username))
    if provider_budget is not None:
        out.append(Limit("provider", provider_id, int(provider_budget), provider))
    return out


def prompt_tokens(obj: Optional[dict]) -> int:
    """A prompt's size in tokens, on the high side: its messages (or prompt or input) and tools."""
    obj = obj or {}
    parts = [obj.get(k) for k in ("messages", "prompt", "input", "tools", "functions") if obj.get(k) is not None]
    size = sum(len(json.dumps(p, ensure_ascii=False).encode("utf-8")) for p in parts)
    return max(1, -(-size // BYTES_PER_TOKEN))


def estimate(obj: Optional[dict], max_tokens: Optional[int], endpoint: str, prices: pricing.Prices,
             default_max_tokens: int) -> Optional[int]:
    """The most a request may cost, in pico-USD; None when the model has no price for it."""
    completion = 0
    if endpoint != "embeddings":
        n = (obj or {}).get("n")
        n = n if isinstance(n, int) and not isinstance(n, bool) and 1 < n <= 128 else 1
        completion = (max_tokens or default_max_tokens) * n
    cost = pricing.cost_of(Usage(prompt=prompt_tokens(obj), completion=completion), prices, cloud=True, served=True)
    return cost.picos


# ------------------------------------------------------------------ outcomes
@dataclass
class Hold:
    """Money reserved for one request against some budgets."""

    picos: int
    limits: list[Limit]
    done: bool = False


@dataclass
class Blocked:
    limit: Limit
    month: str
    spent_picos: int
    held_picos: int
    need_picos: Optional[int]         # None: the model has no price
    model: str = ""

    @property
    def unpriced(self) -> bool:
        return self.need_picos is None

    def error(self) -> GatewayError:
        lim = self.limit
        spent, held = pricing.micros_of(self.spent_picos), pricing.micros_of(self.held_picos)
        reset = resets_at(self.month)
        when = reset.strftime("%Y-%m-%d 00:00 UTC")
        if self.unpriced:
            code = "budget_unpriced"
            message = (f"{self.model or 'This model'} has no price, so its cost cannot be reserved against the monthly "
                       f"budget of {lim.what()}. An admin needs to set its prices (Providers). Models served "
                       "locally still work.")
        else:
            code = "budget_exhausted"
            need = pricing.micros_of(self.need_picos or 0)
            message = (f"The monthly budget of {lim.what()} is used up: {usd(spent)} spent"
                       f"{f' and {usd(held)} reserved' if held else ''} of {usd(lim.micros)}, and this request may "
                       f"cost up to {usd(need)}. It resets on {when}. Models served locally still work.")
        extra = {"budget": {"scope": lim.scope, "name": lim.name, "budget_micros": lim.micros, "spent_micros": spent,
                            "reserved_micros": held,
                            "request_micros": None if self.unpriced else pricing.micros_of(self.need_picos or 0),
                            "month": self.month, "resets_at": reset.isoformat().replace("+00:00", "Z")}}
        return GatewayError(STATUS, code, message, headers={HEADER: lim.token}, extra=extra)


# ------------------------------------------------------------------ the ledger
@dataclass
class _Alert:
    limit: Limit
    month: str
    threshold: int
    spent_micros: int
    refused: bool = False


@dataclass
class Spend:
    month: str
    spent: dict = field(default_factory=dict)      # (scope, id) -> micro-USD
    held: dict = field(default_factory=dict)


class Ledger:
    def __init__(self, clock: Callable[[], float] = time.time):
        self.clock = clock
        self._lock = threading.Lock()
        self._month: Optional[str] = None
        self._spent: dict[tuple[str, int], int] = {}     # pico-USD settled this month
        self._held: dict[tuple[str, int], int] = {}      # pico-USD reserved by requests in flight
        self._alerted: set[tuple[str, int, str, int]] = set()
        self._pending: list[_Alert] = []

    def reset(self) -> None:
        with self._lock:
            self._month = None
            self._spent.clear()
            self._held.clear()
            self._alerted.clear()
            self._pending.clear()

    # -------------------------------------------------------------- seeding
    def ensure(self) -> None:
        """Read this month's spend if it was not read yet (a thread may block on the database)."""
        with self._lock:
            self._ensure()

    def _ensure(self) -> None:
        month = month_of(self.clock())
        if self._month == month:
            return
        from forgeyard.gateway.usage import recorder

        recorder.flush()          # what was counted before now is in the database, and only there
        db = new_session()
        try:
            spent = self._read(db, month)
            alerted = {(a.scope, a.scope_id, a.month, a.threshold) for a in
                       db.scalars(select(BudgetAlert).where(BudgetAlert.month == month)).all()}
        finally:
            db.close()
        self._spent = spent
        self._alerted = alerted
        self._month = month

    @staticmethod
    def _read(db: DbSession, month: str) -> dict[tuple[str, int], int]:
        rows = db.execute(select(UsageDaily.source, UsageDaily.key_id, UsageDaily.user_id, UsageDaily.target,
                                 func.sum(UsageDaily.cost_micros))
                          .where(UsageDaily.day >= f"{month}-01", UsageDaily.day < f"{next_month(month)}-01",
                                 UsageDaily.cost_micros > 0)
                          .group_by(UsageDaily.source, UsageDaily.key_id, UsageDaily.user_id, UsageDaily.target)).all()
        pm_ids = {int(t[3:]) for *_, t, _ in rows if t and t.startswith("pm:") and t[3:].isdigit()}
        provider_of = dict(db.execute(select(ProviderModel.id, ProviderModel.provider_id)
                                      .where(ProviderModel.id.in_(pm_ids))).all()) if pm_ids else {}
        out: dict[tuple[str, int], int] = {}
        for source, key_id, user_id, target, micros in rows:
            picos = int(micros or 0) * PICOS
            slots = []
            if source == "api" and key_id:
                slots.append(("key", int(key_id)))
            if user_id:
                slots.append(("person", int(user_id)))
            pid = provider_of.get(int(target[3:])) if target and target.startswith("pm:") and target[3:].isdigit() else None
            if pid is not None:
                slots.append(("provider", int(pid)))
            for s in slots:
                out[s] = out.get(s, 0) + picos
        return out

    # -------------------------------------------------------------- requests
    def reserve(self, limits: list[Limit], picos: Optional[int], model: str = ""):
        """A Hold, or Blocked by the first budget that cannot cover it."""
        with self._lock:
            self._ensure()
            for lim in limits:
                spent, held = self._spent.get(lim.slot, 0), self._held.get(lim.slot, 0)
                if picos is None or spent + held + picos > lim.micros * PICOS:
                    if spent + held >= lim.micros * PICOS * 80 // 100:
                        self._check(lim, refused=True)
                    return Blocked(lim, self._month or "", spent, held, picos, model)
            for lim in limits:
                self._held[lim.slot] = self._held.get(lim.slot, 0) + picos
            return Hold(picos, list(limits))

    def _free(self, hold: Optional[Hold]) -> None:
        if hold is None or hold.done:
            return
        hold.done = True
        for lim in hold.limits:
            left = self._held.get(lim.slot, 0) - hold.picos
            if left > 0:
                self._held[lim.slot] = left
            else:
                self._held.pop(lim.slot, None)

    def release(self, hold: Optional[Hold]) -> None:
        """The request was not sent, failed or was refused: its reservation is free again."""
        with self._lock:
            self._free(hold)

    def settle(self, hold: Optional[Hold], *, key_id: int, user_id: int, provider_id: Optional[int],
               picos: int, then: Optional[Callable[[], None]] = None) -> None:
        """A finished cloud request: free its reservation, add what it cost, and (under the same lock)
        count its usage with `then`, so seeding sees it exactly once."""
        with self._lock:
            self._free(hold)
            # Not read yet: the reading will find this request in the usage counted below. (After the
            # turn of a month, the next reading starts the new month from the usage rows.)
            if picos > 0 and self._month is not None:
                for slot in (("key", key_id), ("person", user_id), ("provider", provider_id)):
                    if slot[1]:
                        self._spent[slot] = self._spent.get(slot, 0) + picos
                for lim in hold.limits if hold else []:
                    self._check(lim)
            if then is not None:
                then()

    def _check(self, lim: Limit, refused: bool = False) -> None:
        spent = self._spent.get(lim.slot, 0)
        budget = lim.micros * PICOS
        for t in THRESHOLDS:
            reached = (spent > 0 and spent * 100 >= budget * t) or (refused and t == 100)
            mark = (lim.scope, lim.id, self._month or "", t)
            if reached and mark not in self._alerted:
                self._alerted.add(mark)
                self._pending.append(_Alert(lim, self._month or "", t, pricing.micros_of(spent),
                                            refused and spent < budget))

    # -------------------------------------------------------------- reading
    def snapshot(self) -> Spend:
        """This month's spend and reservations per (scope, id), in micro-USD."""
        with self._lock:
            self._ensure()
            return Spend(self._month or "", {k: pricing.micros_of(v) for k, v in self._spent.items()},
                         {k: pricing.micros_of(v) for k, v in self._held.items()})

    # -------------------------------------------------------------- alerts
    def write_alerts(self, db: Optional[DbSession] = None) -> int:
        """Write the alerts that were reached to the audit log, once each. Returns how many."""
        with self._lock:
            pending, self._pending = self._pending, []
        if not pending:
            return 0
        own = db is None
        db = db or new_session()
        written = 0
        try:
            for a in pending:
                try:
                    with db.begin_nested():
                        db.add(BudgetAlert(scope=a.limit.scope, scope_id=a.limit.id, month=a.month, threshold=a.threshold,
                                           spent_micros=a.spent_micros, budget_micros=a.limit.micros))
                        db.flush()
                        audit.record(db, "budget.exhausted" if a.threshold >= 100 else "budget.warning",
                                     actor_name="forgeyard", target=f"{a.limit.scope} {a.limit.name}"[:128],
                                     scope=a.limit.scope, scope_id=a.limit.id, name=a.limit.name, month=a.month,
                                     threshold=a.threshold, spent_micros=a.spent_micros, budget_micros=a.limit.micros,
                                     refused_request=a.refused or None)
                        db.flush()
                    written += 1
                except IntegrityError:
                    pass              # written before (another start of the server): once only
            db.commit()
        except Exception:
            db.rollback()
            with self._lock:
                self._pending[:0] = pending     # try again next time
            raise
        finally:
            if own:
                db.close()
        return written

    async def run_forever(self, interval: float) -> None:
        import asyncio

        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(self.write_alerts)
            except Exception:
                log.exception("Writing budget alerts failed; trying again")


ledger = Ledger()
