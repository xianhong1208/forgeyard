"""Trial runs of an engine (1.5.2, #95, PR 4; forgeyard/models/trials.py).

POST /api/recipes/{name}/trials {model, machine_id, params?, timeout_minutes?, dry_run?} (operators and admins, who
    may deploy: a trial holds a card or RAM while it runs, like a deployment): starts a trial of that engine with that
    catalog model on that machine; 201 with the trial. `params` left out: those of a deployment of the same model on
    the built-in engine, else the engine's defaults. `dry_run`: only the estimate, where it would go and the
    parameters it would use (the dialog's preview); nothing is created.
GET /api/recipes/{name}/trials (everyone signed in, like the engine page): the engine's trials, newest first, and
    whether the caller may start or cancel one.
GET /api/engine-trials/{id} (everyone signed in): one trial with its checks, events and measurements.
POST /api/engine-trials/{id}/cancel (operators and admins): stops a running trial and removes its deployment.

Starting, finishing and cancelling are audited (engine_trial.started, engine_trial.finished, engine_trial.cancelled).
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard.api.deps import Caller, can_run_workloads, current, fail, require_operator
from forgeyard.db import CatalogModel, EngineTrial, Node, get_db
from forgeyard.models import catalog, scheduler, trials
from forgeyard.workloads import placement

router = APIRouter(prefix="/api", tags=["engines"])

HISTORY = 50


class TrialIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(max_length=64)                 # catalog name (or id)
    machine_id: int = Field(ge=1)
    params: Optional[dict[str, Any]] = None
    timeout_minutes: int = Field(default=trials.START_MINUTES_DEFAULT, ge=trials.START_MINUTES_MIN,
                                 le=trials.START_MINUTES_MAX)
    dry_run: bool = False


def _model(db: DbSession, ref: str) -> CatalogModel:
    m = db.scalar(select(CatalogModel).where(CatalogModel.name == ref))
    if m is None and ref.isdigit():
        m = db.get(CatalogModel, int(ref))
    if m is None:
        raise fail(404, "model_not_found", f"No model {ref!r} in the catalog.")
    return m


@router.post("/recipes/{name}/trials", status_code=201)
def start_trial(name: str, body: TrialIn, response: Response, caller: Caller = Depends(require_operator),
                db: DbSession = Depends(get_db)):
    recipe = catalog.get_recipe(db, name)
    if recipe is None:
        raise fail(404, "not_found", "No such recipe.")
    m = _model(db, body.model)   # (a model the engine does not serve: 422 incompatible, from build_context)
    node = db.get(Node, body.machine_id)
    if node is None or node.revoked_at is not None:
        raise fail(404, "machine_not_found", "No such machine.")
    if body.dry_run:
        response.status_code = 200   # nothing was made
        ctx, params, source = trials.plan(db, recipe, m, node, body.params)

        class _Dep:
            served_name = m.name

        ctx.deployment = _Dep()  # type: ignore[assignment]
        with placement._lock:
            pv = scheduler.preview(db, ctx)
        from forgeyard.models.pages import engine_on

        return {"estimate": ctx.estimate.as_dict(), "placement": pv, "params": ctx.params, "params_from": source,
                "engine_on": engine_on(node, recipe), "online": node.status == "online",
                "running": trials.running_count(db), "limit": trials.MAX_RUNNING}
    try:
        t = trials.start(db, recipe=recipe, m=m, node=node, raw=body.params, minutes=body.timeout_minutes,
                         by=caller.user, ip=caller.ip)
    except trials.TrialError as e:
        db.rollback()
        raise fail(e.status, e.code, e.text, **e.extra) from None
    db.expire_all()
    return {"trial": trials.trial_out(db.get(EngineTrial, t.id))}


@router.get("/recipes/{name}/trials")
def list_trials(name: str, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    rows = db.scalars(select(EngineTrial).where(EngineTrial.recipe == name).order_by(EngineTrial.id.desc())
                      .limit(HISTORY)).all()
    if not rows and catalog.get_recipe(db, name) is None:
        raise fail(404, "not_found", "No such recipe.")
    return {"trials": [trials.trial_out(t, detail=False) for t in rows], "can_run": can_run_workloads(caller.user),
            "limit": trials.MAX_RUNNING}


def _get(db: DbSession, trial_id: int) -> EngineTrial:
    t = db.get(EngineTrial, trial_id)
    if t is None:
        raise fail(404, "not_found", "No such trial.")
    return t


@router.get("/engine-trials/{trial_id}")
def get_trial(trial_id: int, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    return {"trial": trials.trial_out(_get(db, trial_id)), "can_run": can_run_workloads(caller.user)}


@router.post("/engine-trials/{trial_id}/cancel")
def cancel_trial(trial_id: int, caller: Caller = Depends(require_operator), db: DbSession = Depends(get_db)):
    t = _get(db, trial_id)
    if t.state != trials.RUNNING:
        raise fail(409, "not_running", f"The trial is {t.state}: there is nothing to cancel.")
    trials.cancel(db, t, caller.user, caller.ip)
    db.expire_all()
    return {"trial": trials.trial_out(_get(db, trial_id))}
