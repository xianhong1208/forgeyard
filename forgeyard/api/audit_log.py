"""Reading the audit log (admins only)."""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard.api.deps import Caller, require_admin
from forgeyard.db import AuditEvent, get_db

router = APIRouter(prefix="/api/audit", tags=["audit"])


@router.get("")
def list_events(
    limit: int = Query(50, ge=1, le=200),
    before: Optional[int] = Query(None, ge=1, description="Return events with an id below this (paging)."),
    action: Optional[str] = Query(None, max_length=48, description="Prefix, e.g. 'auth.' or 'user.created'."),
    actor: Optional[str] = Query(None, max_length=64),
    _: Caller = Depends(require_admin),
    db: DbSession = Depends(get_db),
):
    stmt = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit + 1)
    if before:
        stmt = stmt.where(AuditEvent.id < before)
    if action:
        stmt = stmt.where(AuditEvent.action.startswith(action, autoescape=True))
    if actor:
        stmt = stmt.where(AuditEvent.actor_name == actor.strip().lower())
    rows = db.scalars(stmt).all()
    more = len(rows) > limit
    rows = rows[:limit]
    return {
        "events": [
            {"id": e.id, "at": e.at, "action": e.action, "actor": e.actor_name, "target": e.target,
             "ip": e.ip, "detail": e.detail}
            for e in rows
        ],
        "next_before": rows[-1].id if more else None,
    }
