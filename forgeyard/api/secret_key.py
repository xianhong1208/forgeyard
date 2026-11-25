"""Download a backup of the secret key (and Forgeyard's CA), sealed with a passphrase (1.4.2, #127).

- `GET /api/secret-key`: what a backup would hold (key id, whether the CA is in it) and the last
  confirmed backup. Admins only.
- `POST /api/secret-key/backup`: the admin's password again (checked like a sign-in: the same
  back-off, and a wrong one is audited), a current code from their authenticator app when they have one
  (1.6.0, #39), and a passphrase they choose (12+ characters, typed twice,
  not their password). Answers the file (forgeyard/security/keybackup.py has the format) with
  Cache-Control: no-store, as an attachment named after the key id and the date. Neither the
  passphrase nor the file is kept or logged. A download counts as the backup the setup advisor asks
  for, for that key id. Restore: `python -m forgeyard secrets restore <file>`.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DbSession

from forgeyard import advisor, audit
from forgeyard.api.deps import Caller, fail, require_admin
from forgeyard.config import get_settings
from forgeyard.db import ServerSetting, get_db, utcnow
from forgeyard.security import crypto, keybackup, mfa, passwords, tls
from forgeyard.security.ratelimit import limiter, retry_after

router = APIRouter(prefix="/api/secret-key", tags=["secret key"])


def _last_backup(db: DbSession) -> dict | None:
    row = db.get(ServerSetting, advisor.BACKUP_SETTING)
    return dict(row.value) if row is not None and isinstance(row.value, dict) else None


@router.get("")
def secret_key_info(caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    try:
        c = keybackup.current()
    except crypto.SecretKeyError as e:
        raise fail(409, "no_key", str(e)) from None
    last = _last_backup(db)
    return {
        "kid": c.kid,
        "source": "env" if crypto.key_from_env() else "file",
        "includes_ca": c.tls is not None,
        "tls_mode": tls.mode(),
        "has_password": caller.user.password_hash is not None,
        "has_mfa": mfa.has_factor(db, caller.user.id),
        "passphrase_min": keybackup.PASSPHRASE_MIN,
        "backed_up": last if last and last.get("kid") == c.kid else None,
    }


class BackupIn(BaseModel):
    password: str = Field(max_length=1024)
    passphrase: str = Field(max_length=keybackup.PASSPHRASE_MAX)
    passphrase_confirm: str = Field(max_length=keybackup.PASSPHRASE_MAX)
    # A current code from the admin's authenticator app; needed when they have one.
    code: Optional[str] = Field(default=None, max_length=64)


@router.post("/backup")
def download_backup(body: BackupIn, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    settings = get_settings()
    user = caller.user
    # The password check is a sign-in: the same per-account and per-address back-off, so a stolen
    # session cannot be used to guess the admin's password here either.
    wait = limiter.login_wait(user.username, caller.ip, settings.login_free_failures, settings.login_ip_failures)
    if wait:
        raise fail(429, "rate_limited", "Too many wrong passwords. Wait before trying again.",
                   retry_after=retry_after(wait))
    if user.password_hash is None:
        raise fail(409, "no_password", "Your account has no password to confirm this with. Set one first (an admin can "
                                       "send you a link), then download the backup.")
    problem = keybackup.passphrase_problem(body.passphrase)
    if problem:
        raise fail(422, "weak_passphrase", problem)
    if body.passphrase != body.passphrase_confirm:
        raise fail(422, "passphrase_mismatch", "The two passphrases are not the same.")
    if body.passphrase == body.password:
        raise fail(422, "passphrase_is_password", "Choose a passphrase that is not your password: the backup must stay "
                                                  "safe if your password is ever known.")
    two_factor = mfa.has_factor(db, user.id)
    if two_factor and not (body.code or "").strip():
        raise fail(400, "mfa_code_required", "Type the code your authenticator app shows now.")
    if not passwords.verify_password(user.password_hash, body.password):
        limiter.login_failed(user.username, caller.ip)
        audit.record(db, "secret_key.backup_failed", actor=user, ip=caller.ip, reason="wrong_password")
        db.commit()
        # 401 with its own code: the console tells it apart from an ended session.
        raise fail(401, "wrong_password", "That is not your password.")
    if two_factor and mfa.check_totp(db, user.id, body.code) is None:
        limiter.login_failed(user.username, caller.ip)
        audit.record(db, "secret_key.backup_failed", actor=user, ip=caller.ip, reason="wrong_code")
        db.commit()
        raise fail(400, "mfa_code_invalid", "That code is not right.")
    limiter.login_succeeded(user.username)
    try:
        contents = keybackup.current(settings)
    except crypto.SecretKeyError as e:
        raise fail(409, "no_key", str(e)) from None
    now = utcnow()
    data = keybackup.seal(contents, body.passphrase, now=now)
    name = keybackup.filename(contents.kid, now)
    # The setup advisor's "Back up the secret key" is done for this key id.
    row = db.get(ServerSetting, advisor.BACKUP_SETTING)
    if row is None:
        row = ServerSetting(key=advisor.BACKUP_SETTING)
        db.add(row)
    row.value = {"kid": contents.kid, "by": user.username, "at": now.isoformat(), "how": "download"}
    row.updated_by = user.username
    row.updated_at = now
    audit.record(db, "secret_key.backup_downloaded", actor=user, ip=caller.ip, kid=contents.kid,
                 includes_ca=contents.tls is not None, file=name)
    db.commit()
    return Response(data, media_type="application/octet-stream", headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "Cache-Control": "no-store",
        "Pragma": "no-cache",
    })
