"""Server-side sessions.

The cookie holds a random token; the database holds only sha256(token). Every request
looks the session up, so signing out, changing a password, disabling an account or an
admin's force-logout end it at once: a copied cookie stops working on the next request.
Idle (2 h) and absolute (7 d) limits are checked here, on the server, not by cookie expiry.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session as DbSession

from forgeyard.config import get_settings
from forgeyard.db import Session, User, as_utc, utcnow

COOKIE = "forgeyard_session"
# last_seen_at is written at most this often, so reads do not turn into a write each time.
TOUCH_EVERY = timedelta(seconds=60)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(db: DbSession, user: User, ip: Optional[str], user_agent: Optional[str]) -> tuple[str, Session]:
    token = secrets.token_urlsafe(32)
    now = utcnow()
    row = Session(
        id=token_hash(token),
        user_id=user.id,
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(days=get_settings().session_max_days),
        ip=ip,
        user_agent=(user_agent or "")[:255] or None,
    )
    db.add(row)
    db.flush()
    return token, row


def end_reason(row: Session) -> Optional[str]:
    """Why a stored session no longer counts, or None while it is live."""
    if row.revoked_at is not None:
        return row.revoked_reason or "signed_out"
    now = utcnow()
    if now >= as_utc(row.expires_at):
        return "expired"
    if now - as_utc(row.last_seen_at) >= timedelta(minutes=get_settings().session_idle_minutes):
        return "idle"
    return None


def resolve(db: DbSession, token: Optional[str]) -> tuple[Optional[Session], Optional[str]]:
    """(session, None) for a live session, else (None, reason)."""
    if not token:
        return None, "no_session"
    row = db.get(Session, token_hash(token))
    if row is None:
        return None, "no_session"
    reason = end_reason(row)
    if reason:
        return None, reason
    if row.user.status != "active":
        return None, "account_disabled"
    if utcnow() - as_utc(row.last_seen_at) >= TOUCH_EVERY:
        row.last_seen_at = utcnow()
        db.commit()
    return row, None


def revoke(db: DbSession, row: Session, reason: str) -> None:
    if row.revoked_at is None:
        row.revoked_at = utcnow()
        row.revoked_reason = reason
        db.flush()


def revoke_all(db: DbSession, user_id: int, reason: str, keep: Optional[str] = None) -> int:
    """End every live session of a user (except `keep`, a session id). Returns how many."""
    stmt = (
        update(Session)
        .where(Session.user_id == user_id, Session.revoked_at.is_(None))
        .values(revoked_at=utcnow(), revoked_reason=reason)
    )
    if keep:
        stmt = stmt.where(Session.id != keep)
    result = db.execute(stmt)
    db.flush()
    return result.rowcount or 0


def live_sessions(db: DbSession, user_id: int) -> list[Session]:
    rows = db.scalars(
        select(Session).where(Session.user_id == user_id, Session.revoked_at.is_(None)).order_by(
            Session.last_seen_at.desc()
        )
    ).all()
    return [r for r in rows if end_reason(r) is None]
