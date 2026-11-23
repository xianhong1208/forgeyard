"""Newer versions of the built-in engines (1.5.2, #95 PR 1): an admin's check and its last result.

POST /api/engine-updates/check (admins) asks the registries and GitHub now, through the server's egress rules
(forgeyard.models.engineversions), keeps the result in one `server_settings` row and audits the check. Nothing runs
on a timer. GET /api/engine-updates (everyone signed in) returns the last result, with when it was checked and by
whom, or null before the first check. Not under /api/recipes: "updates" is a valid recipe name.
"""

from __future__ import annotations

import threading

from fastapi import APIRouter, Request

from forgeyard import audit
from forgeyard.api.deps import current, fail, require_admin
from forgeyard.config import get_settings
from forgeyard.db import new_session
from forgeyard.models import engineversions

router = APIRouter(prefix="/api", tags=["engines"])

# One check at a time: a second admin's click while one runs is told so (it would only ask the same again).
_running = threading.Lock()


@router.get("/engine-updates")
def last_check(request: Request):
    db = new_session()
    try:
        caller = current(request, db)
        return {"result": engineversions.stored(db), "can_check": caller.user.role == "admin"}
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
        from forgeyard.db import User

        engineversions.store(db, result, username)
        audit.record(db, "recipe.updates_checked", actor=db.get(User, user_id), ip=ip, **engineversions.summary(result))
        db.commit()
    finally:
        db.close()
    return {"result": result, "can_check": True}
