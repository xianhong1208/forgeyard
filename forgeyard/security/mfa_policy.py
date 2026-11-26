"""Required two-factor sign-in (1.6.0, #39, PR 2): admins can require an authenticator app of admins or of everyone.

One `server_settings` row, KEY: {"level": off | admins | everyone, "grace_days": n, "admins_since": time,
"everyone_since": time}. A `*_since` time is when the setting started to cover that group: it is set when the level
widens to cover the group and cleared when it stops covering it, so moving from admins to everyone gives the other
people a new grace period and leaves the admins' as it was.

A person the setting covers who has no authenticator has until `start + grace_days` (start = the later of the group's
`since` and the account's creation) to add one. Until then they sign in as before and are told how long they have.
After that their session reaches only their own two-factor setup (and sign-out, and who they are): every other
console API answers 403 `mfa_enrolment_required`. Nobody is locked out by it, the owner included: the password still
signs in, and the setup page is always reachable. Someone who lost both the phone and the recovery codes is reset by
an admin (Users), or, for the owner or the last admin, with `forgeyard users reset-mfa NAME` on the server host.

API keys are not affected: the gateway never looks at this setting.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy.orm import Session as DbSession

from forgeyard.db import ServerSetting, User, as_utc, utcnow
from forgeyard.security import mfa

KEY = "mfa_required"
LEVELS = ("off", "admins", "everyone")
DEFAULT_GRACE_DAYS = 7
MAX_GRACE_DAYS = 90
DUE_CODE = "mfa_enrolment_required"


def now() -> datetime:
    """The time the grace period is measured at (tests move it)."""
    return utcnow()


@dataclass(frozen=True)
class Policy:
    level: str = "off"
    grace_days: int = DEFAULT_GRACE_DAYS
    admins_since: Optional[datetime] = None
    everyone_since: Optional[datetime] = None

    def since_for(self, user: User) -> Optional[datetime]:
        """When the setting started to cover this account's group; None when it does not cover it."""
        if user.role == "admin" and self.level in ("admins", "everyone"):
            return self.admins_since
        return self.everyone_since if self.level == "everyone" else None

    def out(self) -> dict:
        return {"mfa_required": self.level, "mfa_grace_days": self.grace_days,
                "admins_since": self.admins_since, "everyone_since": self.everyone_since}


def _time(value) -> Optional[datetime]:
    if not value:
        return None
    try:
        return as_utc(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


def load(db: DbSession) -> Policy:
    row = db.get(ServerSetting, KEY)
    v = row.value if row is not None and isinstance(row.value, dict) else {}
    level = v.get("level") if v.get("level") in LEVELS else "off"
    days = v.get("grace_days")
    days = days if isinstance(days, int) and 0 <= days <= MAX_GRACE_DAYS else DEFAULT_GRACE_DAYS
    if level == "off":
        return Policy(grace_days=days)
    return Policy(level, days, _time(v.get("admins_since")), _time(v.get("everyone_since")))


def save(db: DbSession, level: str, grace_days: int, by: str) -> tuple[Policy, Policy]:
    """Store a new level and grace period. Returns (before, after)."""
    before = load(db)
    at = now()
    admins = level in ("admins", "everyone")
    everyone = level == "everyone"
    after = Policy(level, grace_days,
                   (before.admins_since or at) if admins else None,
                   (before.everyone_since or at) if everyone else None)
    row = db.get(ServerSetting, KEY)
    if row is None:
        row = ServerSetting(key=KEY)
        db.add(row)
    row.value = {"level": level, "grace_days": grace_days,
                 "admins_since": after.admins_since.isoformat() if after.admins_since else None,
                 "everyone_since": after.everyone_since.isoformat() if after.everyone_since else None}
    row.updated_by = by
    row.updated_at = at
    db.flush()
    return before, after


def deadline(policy: Policy, user: User) -> Optional[datetime]:
    """By when this account must have an authenticator, if the setting covers it (whether it has one or not)."""
    since = policy.since_for(user)
    if since is None:
        return None
    start = max(since, as_utc(user.created_at) or since)
    return start + timedelta(days=policy.grace_days)


def enrolment(db: DbSession, user: User, *, policy: Optional[Policy] = None,
              has_factor: Optional[bool] = None) -> Optional[dict]:
    """{"by": time, "due": bool} for an account the setting covers that has no authenticator yet; None otherwise.
    `due` is True once the grace period is over: the account's sessions then reach only the two-factor setup."""
    policy = policy or load(db)
    if policy.level == "off":
        return None
    if user.password_hash is None:  # 1.6.0 PR 3: it can only sign in through the identity provider, which does MFA
        return None
    by = deadline(policy, user)
    if by is None:
        return None
    if has_factor is None:
        has_factor = mfa.has_factor(db, user.id)
    if has_factor:
        return None
    return {"by": by, "due": now() >= by}


def due(db: DbSession, user: User) -> bool:
    e = enrolment(db, user)
    return bool(e and e["due"])
