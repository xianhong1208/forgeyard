"""Monthly cloud budgets (1.2; forgeyard/gateway/budgets.py): what is spent this month against them,
and setting the budgets of people and providers.

- `GET /api/budgets`: this month (UTC), when it resets, and per scope the budget, what was spent and
  what is reserved by requests in flight, with a level (ok, warning at 80%, exhausted at 100%). Your
  own keys and yourself; admins with ?everyone=true: every key and person with a budget or spend
  this month, and every provider outside your network. Revoked keys are left out (1.4): what they
  spent stays in their person's and provider's totals.
- 1.6.1 (#190): `teams`: the teams you manage (admins with ?everyone=true: every team), each with its budget. A
  team's budget is set with `PATCH /api/teams/{name}` (admins).
- `PUT /api/budgets/people/{id}` and `PUT /api/budgets/providers/{id}`: admins only. A key's budget is
  set with `PATCH /api/keys/{id}` (its owner may set it too, unless an admin locked it).

Every change is in the audit log; null removes a budget (unlimited).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import audit, teams as tm
from forgeyard.api.deps import Caller, current, fail, require_admin
from forgeyard.api.keys import MAX_BUDGET, budget_out, key_status
from forgeyard.db import ApiKey, Provider, Team, User, get_db, utcnow
from forgeyard.gateway import budgets as gbudgets

router = APIRouter(prefix="/api/budgets", tags=["budgets"])


class BudgetIn(BaseModel):
    # Micro-USD per UTC calendar month; None: no budget.
    budget_micros: Optional[int] = Field(default=None, ge=0, le=MAX_BUDGET)


def _of(spend: gbudgets.Spend, scope: str, ident: int, budget: Optional[int]) -> dict:
    return budget_out(budget, spend.spent.get((scope, ident), 0), spend.held.get((scope, ident), 0))


@router.get("")
def get_budgets(everyone: bool = Query(default=False), caller: Caller = Depends(current),
                db: DbSession = Depends(get_db)):
    admin = caller.user.role == "admin"
    everyone = everyone and admin
    spend = gbudgets.ledger.snapshot()
    keys_q = select(ApiKey).options(selectinload(ApiKey.user)).order_by(ApiKey.name, ApiKey.id)
    if not everyone:
        keys_q = keys_q.where(ApiKey.user_id == caller.user.id)
    keys = []
    for k in db.scalars(keys_q).all():
        b = _of(spend, "key", k.id, k.budget_micros)
        if everyone and k.budget_micros is None and not b["spent_micros"]:
            continue
        status = key_status(k)
        if status == "revoked" or (status != "active" and not b["spent_micros"]):
            continue  # 1.4: a revoked key is never listed; its spend stays in its person's and provider's
        keys.append({"id": k.public_id, "name": k.name, "prefix": k.prefix, "status": status,
                     "user": {"id": k.user_id, "username": k.user.username if k.user else None},
                     "team": tm.brief(k.team) if k.team_id is not None else None,
                     "locked_by": k.budget_locked_by, **b})
    people_q = select(User).order_by(User.username)
    if not everyone:
        people_q = people_q.where(User.id == caller.user.id)
    people = [{"id": u.id, "username": u.username, "status": u.status, **_of(spend, "person", u.id, u.budget_micros)}
              for u in db.scalars(people_q).all()]
    providers = []
    if admin:
        for p in db.scalars(select(Provider).where(Provider.external.is_(True)).order_by(Provider.name)).all():
            providers.append({"id": p.id, "name": p.name, "kind": p.kind, "enabled": p.enabled,
                              **_of(spend, "provider", p.id, p.budget_micros)})
    teams_q = select(Team).order_by(Team.title, Team.name)
    if not everyone:
        teams_q = teams_q.where(Team.id.in_(tm.managed_ids(db, caller.user)))
    teams = [{**tm.brief(t), **_of(spend, "team", t.id, t.budget_micros)} for t in db.scalars(teams_q).all()]
    return {"month": spend.month, "resets_at": gbudgets.resets_at(spend.month), "is_admin": admin,
            "everyone": everyone, "keys": keys, "people": people, "providers": providers, "teams": teams}


@router.put("/people/{user_id}")
def set_person_budget(user_id: int, body: BudgetIn, caller: Caller = Depends(require_admin),
                      db: DbSession = Depends(get_db)):
    u = db.get(User, user_id)
    if u is None:
        raise fail(404, "not_found", "No such user.")
    before = u.budget_micros
    if before != body.budget_micros:
        u.budget_micros = body.budget_micros
        audit.record(db, "user.budget_changed", actor=caller.user, target=u.username, ip=caller.ip, user_id=u.id,
                     before=before, after=body.budget_micros)
        db.commit()
    spend = gbudgets.ledger.snapshot()
    return {"person": {"id": u.id, "username": u.username, **_of(spend, "person", u.id, u.budget_micros)}}


@router.put("/providers/{provider_id}")
def set_provider_budget(provider_id: int, body: BudgetIn, caller: Caller = Depends(require_admin),
                        db: DbSession = Depends(get_db)):
    p = db.get(Provider, provider_id)
    if p is None:
        raise fail(404, "not_found", "No such provider.")
    if not p.external and body.budget_micros is not None:
        raise fail(422, "not_cloud", f"{p.name} is inside your network: requests to it cost nothing, so it has no "
                                     "budget. Budgets are for cloud providers.")
    before = p.budget_micros
    if before != body.budget_micros:
        p.budget_micros = body.budget_micros
        p.updated_at = utcnow()
        audit.record(db, "provider.changed", actor=caller.user, target=p.name, ip=caller.ip, provider_id=p.id,
                     changes={"budget_micros": {"before": before, "after": body.budget_micros}})
        db.commit()
    spend = gbudgets.ledger.snapshot()
    return {"provider": {"id": p.id, "name": p.name, **_of(spend, "provider", p.id, p.budget_micros)}}
