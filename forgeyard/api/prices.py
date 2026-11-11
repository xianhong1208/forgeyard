"""Prices of provider models from OpenRouter's public model list (1.2), for cost tracking.

Admins enter prices by hand on a provider's page (price_source "manual"), or ask for a refresh from
OpenRouter: Forgeyard then reads https://openrouter.ai/api/v1/models (public, no key is sent)
through the egress rules, matches the providers' models to it, and shows the differences. Nothing
changes until an admin applies the rows they ticked:

- a model matches by its own id on an OpenRouter provider ("exact"), by the id an admin confirmed
  before ("confirmed", kept as price_ref), or by a guess from its name ("suggested": the vendor's
  prefix, without a date suffix…), which counts only once an admin applies it;
- prices entered by hand are kept unless the admin ticks them to be overwritten;
- the values applied must be the ones the admin was shown: the list is read again, and a row whose
  prices changed in between is skipped, to be reviewed again.

There is no automatic background sync and no bundled price file. OpenRouter's prices are usually the
providers' list prices, but not always (spot-check them; docs/guide/cloud-providers.md).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

import httpx
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import audit
from forgeyard.api.deps import Caller, fail, require_admin
from forgeyard.config import get_settings
from forgeyard.db import Provider, ProviderModel, get_db, utcnow
from forgeyard.gateway import egress, pricing, quirks

router = APIRouter(prefix="/api/prices", tags=["prices"])

FETCH_TIMEOUT = 30.0
MAX_BYTES = 32 * 1024**2
REF_MAX = 256


class PreviewIn(BaseModel):
    provider_id: Optional[int] = None          # None: every provider outside your network
    # Matches the admin chose in the dialog: provider model id -> OpenRouter model id.
    refs: dict[int, str] = Field(default_factory=dict, max_length=2000)


class ApplyItem(BaseModel):
    model_id: int
    ref: str = Field(min_length=1, max_length=REF_MAX)
    # What the admin was shown (micro-USD per million tokens); applied only if OpenRouter still says so.
    price_input: Optional[int] = Field(default=None, ge=0)
    price_output: Optional[int] = Field(default=None, ge=0)
    price_cache_read: Optional[int] = Field(default=None, ge=0)
    price_cache_write: Optional[int] = Field(default=None, ge=0)
    overwrite_manual: bool = False


class ApplyIn(BaseModel):
    items: list[ApplyItem] = Field(min_length=1, max_length=2000)


async def fetch_catalog() -> dict[str, pricing.Prices]:
    """OpenRouter's prices, {model id: Prices}. No key, through the egress rules (https, the host
    allowlist, the proxy); failures are 4xx/5xx with a message to show."""
    url = get_settings().openrouter_models_url.strip()
    base, _, last = url.rpartition("/")
    try:
        full, host_headers, extensions = await egress.prepare(base, last, external=True, allow_http=False)
    except egress.EgressError as e:
        raise fail(422, "egress_refused", f"OpenRouter's model list cannot be read from here: {e}.") from None
    headers = {"Accept": "application/json", "User-Agent": "forgeyard-gateway", **host_headers}
    try:
        r = await egress.client().get(full, headers=headers, extensions=extensions,
                                      timeout=httpx.Timeout(FETCH_TIMEOUT, connect=10.0))
    except httpx.HTTPError as e:
        raise fail(502, "catalog_unreachable", f"OpenRouter could not be reached ({type(e).__name__}).") from None
    if r.status_code != 200:
        raise fail(502, "catalog_error", f"OpenRouter's model list answered HTTP {r.status_code}.")
    if len(r.content) > MAX_BYTES:
        raise fail(502, "catalog_unreadable", "OpenRouter's model list is larger than expected; nothing was changed.")
    try:
        known = pricing.catalog(json.loads(r.content))
    except ValueError:
        raise fail(502, "catalog_unreadable", "OpenRouter's model list is not JSON; nothing was changed.") from None
    if not known:
        raise fail(502, "catalog_unreadable", "OpenRouter's model list has no prices; nothing was changed.")
    return known


def _models(db: DbSession, provider_id: Optional[int]) -> list[ProviderModel]:
    q = (select(ProviderModel).join(Provider, Provider.id == ProviderModel.provider_id)
         .options(selectinload(ProviderModel.provider)).order_by(Provider.name, ProviderModel.upstream_model))
    if provider_id is not None:
        if db.get(Provider, provider_id) is None:
            raise fail(404, "not_found", "No such provider.")
        q = q.where(Provider.id == provider_id)
    # A provider inside your network costs nothing: its models are not priced.
    return [pm for pm in db.scalars(q).all() if pm.provider.external]


def _prices(pm: ProviderModel) -> dict:
    return {name: getattr(pm, name) for name in pricing.PRICE_FIELDS}


def preview_rows(models: list[ProviderModel], known: dict[str, pricing.Prices], refs: dict[int, str]) -> list[dict]:
    rows = []
    for pm in models:
        kind = quirks.get(pm.provider.kind)
        chosen = (refs.get(pm.id) or "").strip()[:REF_MAX] or None
        ref, how = pricing.match(pm.provider.kind, kind.catalog_vendor, pm.upstream_model, chosen or pm.price_ref, known)
        before = _prices(pm)
        after = known[ref].as_fields() if ref and ref in known else None
        rows.append({"model_id": pm.id, "provider_id": pm.provider_id, "provider": pm.provider.name,
                     "kind": pm.provider.kind, "upstream_model": pm.upstream_model, "api": pm.api, "ref": ref,
                     "match": how, "manual": pm.price_source == "manual", "source": pm.price_source,
                     "checked_at": pm.price_checked_at, "before": before, "after": after,
                     "changed": after is not None and after != before})
    return rows


@router.post("/openrouter/preview")
async def preview(body: PreviewIn, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    """What a refresh from OpenRouter would change (old -> new per model). Changes nothing."""
    models = _models(db, body.provider_id)
    known = await fetch_catalog()
    rows = preview_rows(models, known, body.refs)
    return {"checked_at": datetime.now(timezone.utc), "listed": len(known), "rows": rows,
            "matched": sum(1 for r in rows if r["after"] is not None),
            "changed": sum(1 for r in rows if r["changed"])}


@router.post("/openrouter/apply")
async def apply(body: ApplyIn, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    """Apply the rows an admin confirmed: OpenRouter is read again, and each row is applied only if
    its prices are still the ones shown; prices entered by hand only with overwrite_manual."""
    known = await fetch_catalog()
    ids = {i.model_id for i in body.items}
    found = {pm.id: pm for pm in db.scalars(select(ProviderModel).where(ProviderModel.id.in_(ids))
                                            .options(selectinload(ProviderModel.provider))).all()}
    applied, skipped, changes = [], [], []
    now = utcnow()
    for item in body.items:
        pm = found.get(item.model_id)
        ref = item.ref.strip()
        if pm is None or not pm.provider.external:
            skipped.append({"model_id": item.model_id, "reason": "not_found"})
            continue
        if ref not in known:
            skipped.append({"model_id": pm.id, "reason": "not_listed"})
            continue
        new = known[ref].as_fields()
        if new != {name: getattr(item, name) for name in pricing.PRICE_FIELDS}:
            skipped.append({"model_id": pm.id, "reason": "changed"})
            continue
        if pm.price_source == "manual" and not item.overwrite_manual:
            skipped.append({"model_id": pm.id, "reason": "manual"})
            continue
        before = _prices(pm)
        for name, value in new.items():
            setattr(pm, name, value)
        pm.price_source, pm.price_checked_at, pm.price_ref = "openrouter", now, ref
        applied.append(pm.id)
        changes.append({"model": f"{pm.provider.name}/{pm.upstream_model}", "ref": ref,
                        **({"before": before, "after": new} if before != new else {})})
    if applied:
        audit.record(db, "provider.prices_refreshed", actor=caller.user, target="openrouter", ip=caller.ip,
                     models=changes[:50], count=len(applied))
    db.commit()
    return {"applied": applied, "skipped": skipped}
