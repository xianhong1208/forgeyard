"""Newer versions of the built-in engines (1.5.2, #95): an admin's check, its last result, and trying a version.

POST /api/engine-updates/check (admins) asks the registries and GitHub now, through the server's egress rules
(forgeyard.models.engineversions), keeps the result in one `server_settings` row and audits the check. Nothing runs
on a timer. GET /api/engine-updates (everyone signed in) returns the last result, with when it was checked and by
whom, or null before the first check, and what each built-in engine's project signs (forgeyard.models.signatures).
Not under /api/recipes: "updates" is a valid recipe name.

POST /api/recipes/{name}/try {version, name?} (admins, PR 2): a custom copy of a built-in engine pinned to a version
the last check found (forgeyard.models.enginetry): 201 with the new recipe. Its images' recorded ids go into
`image_pins` (forgeyard.workloads.pins); the copy is audited as `recipe.created` with where it came from.
"""

from __future__ import annotations

import threading
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forgeyard import audit
from forgeyard.api.deps import current, fail, require_admin
from forgeyard.config import get_settings
from forgeyard.db import CustomRecipe, User, new_session, utcnow
from forgeyard.models import engineversions, enginetry, fetch, recipes, signatures
from forgeyard.workloads import pins
from forgeyard.workloads import registry as oci

router = APIRouter(prefix="/api", tags=["engines"])

# One check at a time: a second admin's click while one runs is told so (it would only ask the same again).
_running = threading.Lock()
# One Try at a time: two copies of one name, or two writes of one pin, never race.
_trying = threading.Lock()


def _signed() -> dict:
    """{built-in recipe: what its engine's project signs}, for the Try dialog."""
    return {n: signatures.about(r.engine) for n, (r, _) in recipes.builtin_recipes().items()}


@router.get("/engine-updates")
def last_check(request: Request):
    db = new_session()
    try:
        caller = current(request, db)
        return {"result": engineversions.stored(db), "can_check": caller.user.role == "admin", "signatures": _signed()}
    finally:
        db.close()


@router.post("/engine-updates/check")
def check_now(request: Request):
    # The database is not held while the registries answer (up to engineversions.CHECK_SECONDS).
    db = new_session()
    try:
        caller = require_admin(current(request, db))
        user_id, username, ip = caller.user.id, caller.user.username, caller.ip
    finally:
        db.close()
    if not _running.acquire(blocking=False):
        raise fail(409, "check_running", "A check for newer engine versions is running already; its result shows "
                                         "here when it ends.")
    try:
        result = engineversions.check_all(by=username, allow_private=get_settings().model_allow_private_hosts)
    finally:
        _running.release()
    db = new_session()
    try:
        engineversions.store(db, result, username)
        audit.record(db, "recipe.updates_checked", actor=db.get(User, user_id), ip=ip, **engineversions.summary(result))
        db.commit()
    finally:
        db.close()
    return {"result": result, "can_check": True, "signatures": _signed()}


class TryIn(BaseModel):
    version: str = Field(min_length=1, max_length=40)
    name: Optional[str] = Field(default=None, max_length=64)


def _name_problem(db, name: str) -> Optional[tuple[int, str, str]]:
    if not recipes.NAME_RE.match(name):
        return 422, "invalid_name", "name: 1-40 characters, a-z, 0-9 and '-'."
    if name in recipes.builtin_recipes():
        return 409, "builtin_name", f"{name} is a built-in recipe; give the copy another name."
    if db.scalar(select(CustomRecipe.id).where(CustomRecipe.name == name)) is not None:
        return 409, "name_taken", f"A recipe named {name} already exists. Give the copy another name."
    return None


@router.post("/recipes/{name}/try", status_code=201)
def try_version(name: str, body: TryIn, request: Request):
    from forgeyard.api.models import recipe_out

    db = new_session()
    try:
        caller = require_admin(current(request, db))
        user_id, username, ip = caller.user.id, caller.user.username, caller.ip
        found = recipes.builtin_recipes().get(name)
        if found is None:
            if db.scalar(select(CustomRecipe.id).where(CustomRecipe.name == name)) is not None:
                raise fail(409, "not_builtin", "Try this version is for built-in engines: a custom engine is changed "
                                               "with Edit.")
            raise fail(404, "not_found", "No such recipe.")
        builtin, builtin_text = found
        try:
            _, cand = enginetry.candidate_of(engineversions.stored(db), builtin, body.version.strip())
        except enginetry.TryError as e:
            raise fail(e.status, e.code, e.text, **e.extra) from None
        new_name = (body.name or "").strip() or enginetry.default_name(builtin.name, cand["version"])
        problem = _name_problem(db, new_name)
        if problem:
            raise fail(*problem)
    finally:
        db.close()

    if not _trying.acquire(blocking=False):
        raise fail(409, "try_running", "Another copy is being made right now. Try again when it is done.")
    try:
        allow_private = get_settings().model_allow_private_hosts

        def policy() -> fetch.Policy:
            return fetch.Policy(allow_private=allow_private, timeout=engineversions.REQUEST_TIMEOUT)

        try:
            plan = enginetry.plan_copy(builtin, builtin_text, cand, new_name, client=oci.Registry(policy),
                                       gh=engineversions.Checker(allow_private=allow_private), policy=policy(),
                                       data_dir=get_settings().data_dir)
            made_at = utcnow().replace(microsecond=0).isoformat()
            text, recipe = enginetry.build(plan, made_by=username, made_at=made_at)
        except enginetry.TryError as e:
            raise fail(e.status, e.code, e.text, **e.extra) from None
        db = new_session()
        try:
            problem = _name_problem(db, new_name)
            if problem:
                raise fail(*problem)
            db.add(CustomRecipe(name=new_name, yaml=text, created_by=username))
            for ref, entry in plan.entries.items():
                pins.record(db, ref, entry, plan.image_signatures.get(ref), username)
            audit.record(db, "recipe.created", actor=db.get(User, user_id), target=new_name, ip=ip,
                         engine=builtin.engine, based_on=builtin.name, from_version=builtin.version,
                         version=plan.version, images=[*plan.images.values(), *(a["url"] for a in plan.archives.values())],
                         left_out=[x["variant"] for x in plan.left_out] or None,
                         signatures={n: (plan.image_signatures[ref].get("status") if ref in plan.image_signatures else None)
                                     for n, ref in plan.images.items()}
                         | {n: a["signature"].get("status") for n, a in plan.archives.items()})
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                raise fail(409, "name_taken", f"A recipe named {new_name} already exists. Give the copy another "
                                              "name.") from None
            pins.refresh(db)
        finally:
            db.close()
    finally:
        _trying.release()
    return {"recipe": recipe_out(recipe, text), "left_out": plan.left_out,
            "signatures": {n: plan.image_signatures.get(ref) for n, ref in plan.images.items()}
            | {n: a["signature"] for n, a in plan.archives.items()}}
