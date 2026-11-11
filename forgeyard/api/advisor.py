"""The setup advisor for admins (forgeyard/advisor.py): what is broken, risky or useless in this
server's setup, with a link to where it is fixed.

- `GET /api/advisor`: the findings this admin sees, the ones they snoozed, and the counts (the nav
  badge shows errors plus warnings).
- `POST /api/advisor/snooze`: snooze an info or warning finding for 30 days, for this admin only
  (stored in their preferences), or wake it again. Errors cannot be snoozed.
- `POST /api/advisor/secret-key-backup`: an admin confirms a backup of the secret key with the id
  shown. The reminder comes back when the key changes. The key itself never leaves the server.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DbSession

from forgeyard import advisor, audit
from forgeyard.api.deps import Caller, fail, require_admin
from forgeyard.config import get_settings
from forgeyard.db import ServerSetting, get_db, utcnow

router = APIRouter(prefix="/api/advisor", tags=["advisor"])


def _host(request: Request) -> Optional[str]:
    host = (request.headers.get("host") or "").strip().lower()
    if host.startswith("["):
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host or None


def report(db: DbSession, caller: Caller, request: Request) -> dict:
    now = utcnow()
    findings = advisor.run(db, get_settings(), now=now, request_host=_host(request))
    shown, snoozed = advisor.split(findings, caller.user, now)
    counts = {s: sum(1 for f in shown if f["severity"] == s) for s in advisor.SEVERITIES}
    return {"findings": shown, "snoozed": snoozed, "counts": counts, "badge": counts["error"] + counts["warning"],
            "snooze_days": advisor.SNOOZE_DAYS, "checked_at": now.isoformat()}


@router.get("")
def get_advisor(request: Request, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    return report(db, caller, request)


class SnoozeIn(BaseModel):
    key: str = Field(min_length=1, max_length=200)
    # false: show it again now.
    snooze: bool = True


@router.post("/snooze")
def snooze(body: SnoozeIn, request: Request, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    now = utcnow()
    if body.snooze:
        found = {f.key: f for f in advisor.run(db, get_settings(), now=now, request_host=_host(request))}
        f = found.get(body.key)
        if f is None:
            raise fail(404, "no_such_finding", "There is no such finding now (it may have been fixed).")
        if not f.snoozable:
            raise fail(422, "cannot_snooze", "Errors cannot be snoozed: they stay until they are fixed.")
        advisor.set_snooze(caller.user, body.key, now + timedelta(days=advisor.SNOOZE_DAYS), now)
    else:
        advisor.set_snooze(caller.user, body.key, None, now)
    db.commit()
    return report(db, caller, request)


class BackupIn(BaseModel):
    kid: str = Field(min_length=1, max_length=16)


@router.post("/secret-key-backup")
def confirm_backup(body: BackupIn, request: Request, caller: Caller = Depends(require_admin),
                   db: DbSession = Depends(get_db)):
    """The admin says they have a backup of the key with this id (shown in the reminder). A stale id
    (the key changed since the page was loaded) is refused, so a backup of an old key never counts."""
    from forgeyard.security import crypto

    kid = crypto.box().kid
    if body.kid != kid:
        raise fail(409, "key_changed", f"The secret key's id is {kid} now: back that one up, then confirm again.")
    row = db.get(ServerSetting, advisor.BACKUP_SETTING)
    if row is None:
        row = ServerSetting(key=advisor.BACKUP_SETTING)
        db.add(row)
    now = utcnow()
    row.value = {"kid": kid, "by": caller.user.username, "at": now.isoformat()}
    row.updated_by = caller.user.username
    row.updated_at = now
    audit.record(db, "secret_key.backup_confirmed", actor=caller.user, ip=caller.ip, kid=kid)
    db.commit()
    return report(db, caller, request)
