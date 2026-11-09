"""One-time set-password links. The URL carries a random token; only its hash is stored."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Optional

from sqlalchemy import update
from sqlalchemy.orm import Session as DbSession

from forgeyard.db import PasswordLink, User, as_utc, utcnow
from forgeyard.security.sessions import token_hash

NEW_ACCOUNT_TTL = timedelta(hours=72)
RESET_TTL = timedelta(hours=24)


def issue(db: DbSession, user: User, ttl: timedelta) -> str:
    # A new link replaces any unused one for the same account.
    db.execute(update(PasswordLink).where(PasswordLink.user_id == user.id, PasswordLink.used_at.is_(None))
               .values(used_at=utcnow()))
    token = secrets.token_urlsafe(32)
    db.add(PasswordLink(id=token_hash(token), user_id=user.id, expires_at=utcnow() + ttl))
    db.flush()
    return token


def find(db: DbSession, token: Optional[str]) -> Optional[PasswordLink]:
    if not token:
        return None
    link = db.get(PasswordLink, token_hash(token))
    if link is None or link.used_at is not None or utcnow() >= as_utc(link.expires_at):
        return None
    return link


def url(base: str, token: str) -> str:
    # The token goes in the fragment: browsers never send it to the server or in a Referer.
    return f"{base}/set-password#token={token}"
