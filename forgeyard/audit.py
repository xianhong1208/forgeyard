"""The audit log: who did what, when, from where."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session as DbSession

from forgeyard.db import AuditEvent, User


def record(
    db: DbSession,
    action: str,
    *,
    actor: Optional[User] = None,
    actor_name: Optional[str] = None,
    target: Optional[str] = None,
    ip: Optional[str] = None,
    **detail,
) -> None:
    db.add(
        AuditEvent(
            action=action,
            actor_id=actor.id if actor else None,
            actor_name=actor.username if actor else actor_name,
            target=target,
            ip=ip,
            detail={k: v for k, v in detail.items() if v is not None},
        )
    )
