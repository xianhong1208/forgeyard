""""Download to…" (1.4, issue #35): download a catalog model, and by default its engine, to chosen machines
before deploying it (forgeyard/models/prefetch.py).

Everyone signed in sees the downloads; operators and admins start and cancel them (D9, the same people who
deploy). Each request and cancel is audited.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard.api.deps import Caller, can_run_workloads, current, fail, require_operator
from forgeyard.db import CatalogModel, ModelDownload, Node, get_db
from forgeyard.machines.hub import hub
from forgeyard.models import prefetch
from forgeyard.models.catalog import all_recipes

router = APIRouter(prefix="/api/downloads", tags=["downloads"])

LIST_LIMIT = 100


def _model(db: DbSession, model_id: int) -> CatalogModel:
    model = db.get(CatalogModel, model_id)
    if model is None:
        raise fail(404, "not_found", "No such model in the catalog.")
    if model.kind == "local":
        raise fail(422, "local_model", f"{model.name} is a directory on the machines themselves: there is nothing "
                                       "to download.")
    return model


def _recipes(db: DbSession, model: CatalogModel) -> list[str]:
    return [n for n, (r, _) in all_recipes(db).items() if model.format in r.formats and model.task in r.tasks]


def _recipe(db: DbSession, model: CatalogModel, name: Optional[str]):
    """The recipe whose engine comes too: the one named (it must serve this model), else the first that
    serves it (as the deploy page picks), else None."""
    names = _recipes(db, model)
    if name:
        if name not in names:
            raise fail(422, "recipe_not_compatible", f"The recipe {name} does not serve {model.name}.")
        return all_recipes(db)[name][0]
    return all_recipes(db)[names[0]][0] if names else None


@router.get("")
def list_downloads(model_id: Optional[int] = Query(default=None), machine_id: Optional[int] = Query(default=None),
                   caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    """The downloads under way first, then the finished ones, newest first."""
    q = (select(ModelDownload).options(selectinload(ModelDownload.model), selectinload(ModelDownload.node))
         .order_by(ModelDownload.id.desc()).limit(LIST_LIMIT))
    if model_id is not None:
        q = q.where(ModelDownload.model_id == model_id)
    if machine_id is not None:
        q = q.where(ModelDownload.node_id == machine_id)
    rows = list(db.scalars(q).all())
    rows.sort(key=lambda r: (r.state not in prefetch.ACTIVE, -r.id))
    return {"downloads": [prefetch.row_out(r) for r in rows], "can_download": can_run_workloads(caller.user)}


@router.get("/targets")
def download_targets(model_id: int = Query(), recipe: Optional[str] = Query(default=None, max_length=40),
                     engine: bool = Query(default=True), caller: Caller = Depends(current),
                     db: DbSession = Depends(get_db)):
    """For the dialog: each machine, and whether it can take this model (and the recipe's engine): it fits,
    it is already there, it is offline, its agent is too old, its disk is too small."""
    model = _model(db, model_id)
    chosen = _recipe(db, model, recipe)
    return {"model": {"id": model.id, "name": model.name, "title": model.title, "size_bytes": model.size_bytes,
                      "kind": model.kind, "file": model.file},
            "recipes": _recipes(db, model), "recipe": chosen.name if chosen is not None else None,
            "machines": prefetch.targets(db, model, chosen if engine else None),
            "can_download": can_run_workloads(caller.user)}


class DownloadIn(BaseModel):
    model_id: int
    machine_ids: list[int] = Field(min_length=1, max_length=64)
    # D7: also the engine of this recipe (default: the first recipe that serves the model), on by default.
    engine: bool = True
    recipe: Optional[str] = Field(default=None, max_length=40)


@router.post("", status_code=201)
def start_downloads(body: DownloadIn, caller: Caller = Depends(require_operator), db: DbSession = Depends(get_db)):
    model = _model(db, body.model_id)
    recipe = _recipe(db, model, body.recipe) if body.engine else None
    ids = list(dict.fromkeys(body.machine_ids))
    nodes = list(db.scalars(select(Node).options(selectinload(Node.accelerators)).where(Node.id.in_(ids))).all())
    missing = sorted(set(ids) - {n.id for n in nodes})
    if missing:
        raise fail(404, "not_found", f"No machine with id {missing[0]}.")
    created, skipped = prefetch.start(db, model, sorted(nodes, key=lambda n: n.name), recipe, caller.user.username,
                                      actor=caller.user, ip=caller.ip)
    if not created:
        db.rollback()
        first = skipped[0] if skipped else {"name": "", "reason": "nothing to do"}
        text = "; ".join(f"{s['name']}: {s['reason']}" for s in skipped) or first["reason"]
        raise fail(409, "nothing_to_download", f"Nothing was started. {text}"[:600], skipped=skipped)
    db.commit()
    for row in created:
        hub.poke(row.node_id)
    return {"downloads": [prefetch.row_out(r) for r in created], "skipped": skipped}


@router.post("/{download_id}/cancel")
def cancel_download(download_id: int, caller: Caller = Depends(require_operator), db: DbSession = Depends(get_db)):
    row = db.get(ModelDownload, download_id)
    if row is None:
        raise fail(404, "not_found", "No such download.")
    if row.state not in prefetch.ACTIVE:
        raise fail(409, "download_finished", f"This download is already {row.state}.")
    prefetch.cancel(db, row, caller.user.username, actor=caller.user, ip=caller.ip)
    db.commit()
    hub.poke(row.node_id)  # its agent stops it at once (the desired list no longer has it)
    return {"download": prefetch.row_out(row)}
