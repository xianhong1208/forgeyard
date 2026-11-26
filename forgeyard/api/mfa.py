"""My account's two-factor sign-in (1.6.0, #39): authenticator apps and recovery codes.

- `GET /api/auth/mfa/factors`: my authenticators and how many recovery codes are left.
- `POST /api/auth/mfa/totp/start`: a new secret, its otpauth:// URI (the QR code) and a sealed copy for the
  confirmation. Nothing is saved yet.
- `POST /api/auth/mfa/totp`: the sealed copy, the first code from the app and my password (or, when I already have
  a factor, a current code from it). The factor is saved; the first one also makes 10 recovery codes, shown once.
- `POST /api/auth/mfa/factors/{id}/remove` and `POST /api/auth/mfa/recovery-codes`: again only with my password or
  a current code. Removing the last factor removes the recovery codes too.

Every password or code typed here counts like a sign-in (the same back-off per account and address).

The first three take `signed_in`, not `current`: someone whose required two-factor setup is overdue (PR 2,
security/mfa_policy.py) can still add an authenticator, and nothing else.
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from forgeyard import audit
from forgeyard.api.deps import Caller, current, fail, signed_in
from forgeyard.config import get_settings
from forgeyard.db import MfaFactor, get_db, utcnow
from forgeyard.security import mfa, passwords, totp
from forgeyard.security.ratelimit import limiter, retry_after

router = APIRouter(prefix="/api/auth/mfa", tags=["auth"])

DEFAULT_NAME = "Authenticator app"


class ReauthIn(BaseModel):
    """Who I am, again: my password, or a current code from one of my authenticators."""

    password: Optional[str] = Field(default=None, max_length=1024)
    code: Optional[str] = Field(default=None, max_length=64)


class TotpConfirmIn(ReauthIn):
    enrolment: str = Field(max_length=2000)
    new_code: str = Field(max_length=64)
    name: Optional[str] = Field(default=None, max_length=mfa.NAME_MAX)


def reauth(db: DbSession, caller: Caller, body: ReauthIn) -> str:
    """'code' or 'password' when one of them is right; else an error (and the try counts like a sign-in)."""
    settings = get_settings()
    user = caller.user
    wait = limiter.login_wait(user.username, caller.ip, settings.login_free_failures, settings.login_ip_failures)
    if wait:
        raise fail(429, "rate_limited", "Too many wrong tries. Wait before trying again.", retry_after=retry_after(wait))
    if body.code:
        if mfa.check_totp(db, user.id, body.code):
            limiter.login_succeeded(user.username)
            return "code"
        limiter.login_failed(user.username, caller.ip)
        raise fail(400, "mfa_code_invalid", "That code is not right.")
    if body.password:
        if passwords.verify_password(user.password_hash, body.password):
            limiter.login_succeeded(user.username)
            return "password"
        limiter.login_failed(user.username, caller.ip)
        raise fail(400, "wrong_password", "The password is not right.")
    raise fail(400, "reauth_required", "Type your password, or a current code from your authenticator app.")


def _status(db: DbSession, user_id: int) -> dict:
    return {"factors": [mfa.factor_out(f) for f in mfa.factors(db, user_id)],
            "recovery_codes_left": mfa.recovery_left(db, user_id), "max_factors": mfa.MAX_FACTORS}


@router.get("/factors")
def my_factors(caller: Caller = Depends(signed_in), db: DbSession = Depends(get_db)):
    return _status(db, caller.user.id)


@router.post("/totp/start")
def start_totp(caller: Caller = Depends(signed_in), db: DbSession = Depends(get_db)):
    if len(mfa.factors(db, caller.user.id)) >= mfa.MAX_FACTORS:
        raise fail(409, "mfa_factor_limit", f"An account can have at most {mfa.MAX_FACTORS} authenticators.")
    return mfa.start_enrolment(caller.user)


@router.post("/totp")
def confirm_totp(body: TotpConfirmIn, caller: Caller = Depends(signed_in), db: DbSession = Depends(get_db)):
    user = caller.user
    secret = mfa.open_enrolment(user, body.enrolment)
    if secret is None:
        raise fail(400, "mfa_enrolment_expired", "This setup took too long. Start again with a new QR code.")
    if db.scalar(select(func.count()).select_from(MfaFactor).where(MfaFactor.user_id == user.id)) >= mfa.MAX_FACTORS:
        raise fail(409, "mfa_factor_limit", f"An account can have at most {mfa.MAX_FACTORS} authenticators.")
    step = totp.match_step(totp.secret_bytes(secret), body.new_code, now=mfa.clock())
    if step is None:
        raise fail(400, "mfa_new_code_invalid", "That code does not match the new authenticator. Check the time on "
                                                "the phone, and type the code it shows now.")
    first = not mfa.has_factor(db, user.id)
    how = reauth(db, caller, body)
    name = " ".join((body.name or "").split())[:mfa.NAME_MAX] or DEFAULT_NAME
    factor = MfaFactor(user_id=user.id, kind="totp", name=name, secret=secret, last_step=step, last_used_at=utcnow())
    db.add(factor)
    db.flush()
    codes = mfa.new_recovery_codes(db, user.id) if first else None
    caller.session.mfa_at = utcnow()
    audit.record(db, "auth.mfa_enrolled", actor=user, ip=caller.ip, factor=name, kind="totp", confirmed_with=how,
                 recovery_codes=len(codes) if codes else None)
    db.commit()
    return {"factor": mfa.factor_out(factor), "recovery_codes": codes, **_status(db, user.id)}


@router.post("/factors/{factor_id}/remove")
def remove_factor(factor_id: int, body: ReauthIn, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    user = caller.user
    factor = db.scalar(select(MfaFactor).where(MfaFactor.id == factor_id, MfaFactor.user_id == user.id))
    if factor is None:
        raise fail(404, "not_found", "No such authenticator.")
    how = reauth(db, caller, body)
    name = factor.name
    db.delete(factor)
    db.flush()
    last = not mfa.has_factor(db, user.id)
    if last:
        mfa.remove_all(db, user.id)  # recovery codes only stand in for a factor
    audit.record(db, "auth.mfa_removed", actor=user, ip=caller.ip, factor=name, confirmed_with=how,
                 two_factor_off=last or None)
    db.commit()
    return _status(db, user.id)


@router.post("/recovery-codes")
def new_recovery_codes(body: ReauthIn, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    user = caller.user
    if not mfa.has_factor(db, user.id):
        raise fail(409, "mfa_not_enabled", "Add an authenticator app first.")
    how = reauth(db, caller, body)
    codes = mfa.new_recovery_codes(db, user.id)
    audit.record(db, "auth.recovery_codes_created", actor=user, ip=caller.ip, count=len(codes), confirmed_with=how)
    db.commit()
    return {"recovery_codes": codes, **_status(db, user.id)}
