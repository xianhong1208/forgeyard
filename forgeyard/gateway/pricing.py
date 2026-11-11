"""What gateway requests cost (1.2): USD only, stored as integer micro-USD.

A request's cost is worked out once, when it finishes (forgeyard/gateway/proxy.py: Job.finish),
and added to its usage row, so changing a price later never rewrites history.

- A local replica, or a provider inside your network (not external), costs 0.
- When the upstream reports its own cost (OpenRouter's `usage.cost`), that is the cost.
- Otherwise, with the provider model's prices (micro-USD per million tokens):

      (prompt - cached - cache_write) x input + cached x cache_read
        + cache_write x cache_write_price + completion x output

  Cached and cache-write tokens are part of `prompt` and reasoning tokens part of `completion` in
  the OpenAI shape (forgeyard/gateway/quirks.py: read_usage), so nothing is counted twice:
  reasoning is billed at the output price, inside `completion`.
- A price that is needed and not set (input for a prompt, output for a completion) makes the cost
  unknown: NULL, never 0, and the request is counted as "unpriced". A cache price that is not set
  falls back to the input price, and the request is counted as an estimate; so is every request to
  a provider kind that does not report cached tokens (Anthropic's compatibility endpoint).
- No tokens reported: a request that was refused or failed costs 0 (providers do not bill those);
  one that was answered, or broke off while answering, is unpriced.

Costs are summed in pico-USD (micro-USD x 10^-6) in memory and rounded to micro-USD when written,
so many cheap requests do not each lose half a micro-dollar.

The other half of this module reads OpenRouter's public model list (`/api/v1/models`, USD per token
as strings) for the "Refresh prices from OpenRouter" action (forgeyard/api/prices.py).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from forgeyard.gateway.quirks import Usage

PICOS_PER_MICRO = 1_000_000
PICOS_PER_USD = 10**12
PRICE_FIELDS = ("price_input", "price_output", "price_cache_read", "price_cache_write")

# How a request's cost was worked out; usage rows count the last four.
LOCAL, NONE, COMPUTED, REPORTED, ESTIMATED, UNPRICED = "local", "none", "computed", "reported", "estimated", "unpriced"


@dataclass(frozen=True)
class Prices:
    """Micro-USD per million tokens; None while unknown."""

    input: Optional[int] = None
    output: Optional[int] = None
    cache_read: Optional[int] = None
    cache_write: Optional[int] = None

    @classmethod
    def of(cls, pm: Any) -> "Prices":
        return cls(input=pm.price_input, output=pm.price_output, cache_read=pm.price_cache_read,
                   cache_write=pm.price_cache_write)

    def as_fields(self) -> dict[str, Optional[int]]:
        return dict(zip(PRICE_FIELDS, (self.input, self.output, self.cache_read, self.cache_write)))


@dataclass(frozen=True)
class Cost:
    picos: Optional[int]       # pico-USD; None: unknown
    method: str

    @property
    def micros(self) -> Optional[int]:
        return None if self.picos is None else micros_of(self.picos)


def micros_of(picos: int) -> int:
    """Pico-USD to micro-USD, half up."""
    return (int(picos) + PICOS_PER_MICRO // 2) // PICOS_PER_MICRO


def usd_to_picos(usd: float) -> int:
    return int((Decimal(str(usd)) * PICOS_PER_USD).to_integral_value())


def cost_of(u: Usage, prices: Optional[Prices], *, cloud: bool, served: bool, reports_cached: bool = True) -> Cost:
    """The cost of one finished request. `cloud`: it went to a provider outside your network;
    `served`: the upstream answered it (or was answering when it broke off)."""
    if not cloud:
        return Cost(0, LOCAL)
    if u.cost is not None:
        return Cost(usd_to_picos(u.cost), REPORTED)
    if not (u.prompt or u.completion):
        return Cost(None, UNPRICED) if served else Cost(0, NONE)
    p = prices or Prices()
    uncached = max(0, u.prompt - u.cached - u.cache_write)
    estimated = not reports_cached and u.prompt > 0
    picos = 0
    for tokens, price, fallback in ((uncached, p.input, None), (u.cached, p.cache_read, p.input),
                                    (u.cache_write, p.cache_write, p.input), (u.completion, p.output, None)):
        if not tokens:
            continue
        if price is None:
            if fallback is None:
                return Cost(None, UNPRICED)
            price, estimated = fallback, True
        picos += tokens * price
    return Cost(picos, ESTIMATED if estimated else COMPUTED)


# ------------------------------------------------------------------ OpenRouter's model list
def per_token_to_price(value: Any) -> Optional[int]:
    """OpenRouter's USD per token (a string like "0.00000015") to micro-USD per million tokens.
    Negative values (its routers' "-1": variable) and anything unreadable are unknown."""
    if value is None or isinstance(value, bool):
        return None
    try:
        d = Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    if not d.is_finite() or d < 0:
        return None
    return int((d * PICOS_PER_USD).to_integral_value())


def catalog(obj: Any) -> dict[str, Prices]:
    """{OpenRouter model id: its prices} from /api/v1/models."""
    items = obj.get("data") if isinstance(obj, dict) else None
    out: dict[str, Prices] = {}
    for m in items if isinstance(items, list) else []:
        if not isinstance(m, dict) or not isinstance(m.get("id"), str) or not isinstance(m.get("pricing"), dict):
            continue
        pr = m["pricing"]
        prices = Prices(input=per_token_to_price(pr.get("prompt")), output=per_token_to_price(pr.get("completion")),
                        cache_read=per_token_to_price(pr.get("input_cache_read")),
                        cache_write=per_token_to_price(pr.get("input_cache_write")))
        if prices.input is None and prices.output is None:
            continue
        out[m["id"][:256]] = prices
    return out


_DATE = re.compile(r"-(\d{8}|\d{4}-\d{2}-\d{2})$")
_VERSION = re.compile(r"(?<=\d)-(?=\d(?!\d{3}))")


def candidates(upstream_model: str) -> list[str]:
    """Spellings of a provider's model name that OpenRouter may use: as is, without a date suffix,
    and with version dashes as dots ("claude-sonnet-4-5-20250929" -> "claude-sonnet-4.5")."""
    name = upstream_model.strip().lower()
    out = [name]
    undated = _DATE.sub("", name)
    for n in (undated, _VERSION.sub(".", name), _VERSION.sub(".", undated)):
        if n not in out:
            out.append(n)
    return out


def match(kind: str, vendor: str, upstream_model: str, ref: Optional[str], known: dict[str, Prices]) -> tuple[Optional[str], str]:
    """(OpenRouter id, how it matched) for a provider model:

    - "confirmed": the id an admin confirmed before (stored on the model as price_ref);
    - "exact": an OpenRouter provider's model, by its own id;
    - "suggested": the provider's model name under its vendor's prefix ("openai/gpt-4o-mini"), or an
      OpenAI-compatible server's model whose name is an OpenRouter id; an admin confirms it;
    - "not_listed": the confirmed id is not (or no longer) listed; "none": nothing matched."""
    if ref:
        return (ref, "confirmed") if ref in known else (ref, "not_listed")
    if kind == "openrouter":
        return (upstream_model, "exact") if upstream_model in known else (None, "none")
    lowered = {k.lower(): k for k in known}
    for name in candidates(upstream_model):
        for guess in ([f"{vendor}/{name}"] if vendor else []) + ([name] if "/" in name else []):
            if guess in lowered:
                return lowered[guess], "suggested"
    return None, "none"
