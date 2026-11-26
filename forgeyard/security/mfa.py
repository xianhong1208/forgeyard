"""Two-factor sign-in (1.6.0, #39): authenticator apps (TOTP), recovery codes and half-done sign-ins.

- A factor's secret is encrypted with the server's secret key (mfa_factors.secret). A code is accepted for the
  current 30 s step or one either side, and each step once per factor: the step is claimed with a conditional
  UPDATE, so two requests with the same code cannot both win.
- Recovery codes: 10 per set, 16 characters (about 79 random bits), shown once. Only their SHA-256 is stored, each works
  once (claimed the same way), and a new set voids the old one. They exist only while the account has a factor.
- A sign-in with a factor is two steps: the password makes a login challenge (a random token for the browser, its
  hash in the database, 5 minutes, 5 wrong codes), and the code turns the challenge into a session.
- Enrolling: the server makes the secret and hands the browser a sealed copy (encrypted and bound to the account,
  10 minutes) with it; the factor is saved only when the browser sends the sealed copy back with a correct code.
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from datetime import timedelta
from typing import Optional

from sqlalchemy import delete, exists, func, select, update
from sqlalchemy.orm import Session as DbSession

from forgeyard.db import LoginChallenge, MfaFactor, RecoveryCode, User, as_utc, utcnow
from forgeyard.security import crypto, totp
from forgeyard.security.sessions import token_hash

CHALLENGE_TTL = timedelta(minutes=5)
CHALLENGE_ATTEMPTS = 5
ENROL_TTL = 600            # seconds a started enrolment stays good
ENROL_PURPOSE = "mfa.enrolment"
RECOVERY_COUNT = 10
MAX_FACTORS = 5
NAME_MAX = 64
# Lower case without look-alikes (no 0/o, 1/l/i): 31 characters, so 16 of them are about 79 random bits.
RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
RECOVERY_LENGTH = 16


def clock() -> float:
    """Seconds since the epoch, for codes and enrolments (tests move it)."""
    return time.time()


# ------------------------------------------------------------------ factors
def factors(db: DbSession, user_id: int) -> list[MfaFactor]:
    return list(db.scalars(select(MfaFactor).where(MfaFactor.user_id == user_id).order_by(MfaFactor.id)).all())


def has_factor(db: DbSession, user_id: int) -> bool:
    return bool(db.scalar(select(exists().where(MfaFactor.user_id == user_id))))


def users_with_factor(db: DbSession) -> set[int]:
    return set(db.scalars(select(MfaFactor.user_id).distinct()).all())


def factor_out(f: MfaFactor) -> dict:
    return {"id": f.id, "kind": f.kind, "name": f.name, "created_at": f.created_at, "last_used_at": f.last_used_at}


def check_totp(db: DbSession, user_id: int, code: str) -> Optional[MfaFactor]:
    """The factor a current authenticator code belongs to, with its step claimed (it never counts again), or None."""
    if not totp.looks_like_code(code):
        return None
    now = clock()
    for f in factors(db, user_id):
        key = totp.secret_bytes(f.secret)
        if key is None:
            continue
        step = totp.match_step(key, code, after=f.last_step, now=now)
        if step is None:
            continue
        # Claim the step: a second request with the same code (or an older one) finds it taken.
        claimed = db.execute(update(MfaFactor).where(
            MfaFactor.id == f.id, (MfaFactor.last_step.is_(None)) | (MfaFactor.last_step < step))
            .values(last_step=step, last_used_at=utcnow())).rowcount
        if claimed == 1:
            db.flush()
            db.refresh(f)
            return f
    return None


# ------------------------------------------------------------------ recovery codes
def normalise_recovery(code: str) -> str:
    return re.sub(r"[\s-]+", "", code or "").lower()


def recovery_hash(code: str) -> str:
    return hashlib.sha256(normalise_recovery(code).encode()).hexdigest()


def _format(raw: str) -> str:
    return "-".join(raw[i:i + 4] for i in range(0, len(raw), 4))


def new_recovery_codes(db: DbSession, user_id: int) -> list[str]:
    """A new set of codes for the account (the old set stops working). The codes are returned once, never stored."""
    db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
    codes = [_format("".join(secrets.choice(RECOVERY_ALPHABET) for _ in range(RECOVERY_LENGTH)))
             for _ in range(RECOVERY_COUNT)]
    for c in codes:
        db.add(RecoveryCode(user_id=user_id, hash=recovery_hash(c)))
    db.flush()
    return codes


def recovery_left(db: DbSession, user_id: int) -> int:
    return db.scalar(select(func.count()).select_from(RecoveryCode).where(
        RecoveryCode.user_id == user_id, RecoveryCode.used_at.is_(None))) or 0


def use_recovery_code(db: DbSession, user_id: int, code: str) -> bool:
    """True when `code` is one of the account's unused recovery codes; it is then used up."""
    norm = normalise_recovery(code)
    if len(norm) != RECOVERY_LENGTH:
        return False
    row = db.scalar(select(RecoveryCode).where(RecoveryCode.user_id == user_id, RecoveryCode.hash == recovery_hash(norm)))
    if row is None:
        return False
    claimed = db.execute(update(RecoveryCode).where(RecoveryCode.id == row.id, RecoveryCode.used_at.is_(None))
                         .values(used_at=utcnow())).rowcount
    db.flush()
    return claimed == 1


def check_code(db: DbSession, user_id: int, code: str, *, recovery: bool = True) -> Optional[str]:
    """'totp' or 'recovery' when the code is good (and now used), else None. Six digits are an authenticator code,
    anything else a recovery code (when `recovery` allows it)."""
    if totp.looks_like_code(code):
        return "totp" if check_totp(db, user_id, code) else None
    if recovery and use_recovery_code(db, user_id, code):
        return "recovery"
    return None


def remove_all(db: DbSession, user_id: int) -> int:
    """Every factor and recovery code of the account (and its half-done sign-ins). Returns how many factors."""
    n = db.execute(delete(MfaFactor).where(MfaFactor.user_id == user_id)).rowcount or 0
    db.execute(delete(RecoveryCode).where(RecoveryCode.user_id == user_id))
    db.execute(delete(LoginChallenge).where(LoginChallenge.user_id == user_id))
    db.flush()
    return n


# ------------------------------------------------------------------ enrolment
def start_enrolment(user: User) -> dict:
    secret = totp.new_secret()
    sealed = crypto.box().seal(json.dumps({"u": user.id, "s": secret, "exp": int(clock()) + ENROL_TTL}),
                               ENROL_PURPOSE)
    return {"secret": secret, "uri": totp.otpauth_uri(secret, user.username), "enrolment": sealed,
            "expires_in": ENROL_TTL}


def open_enrolment(user: User, sealed: str) -> Optional[str]:
    """The secret of an enrolment this server started for this account, still in time; else None."""
    try:
        data = json.loads(crypto.box().open(sealed, ENROL_PURPOSE))
    except (crypto.SecretError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("u") != user.id or not isinstance(data.get("s"), str):
        return None
    if not isinstance(data.get("exp"), int) or data["exp"] < clock():
        return None
    return data["s"] if totp.secret_bytes(data["s"]) else None


# ------------------------------------------------------------------ half-done sign-ins
def new_challenge(db: DbSession, user: User) -> str:
    now = utcnow()
    db.execute(delete(LoginChallenge).where(LoginChallenge.expires_at < now))
    token = secrets.token_urlsafe(32)
    db.add(LoginChallenge(id=token_hash(token), user_id=user.id, created_at=now, expires_at=now + CHALLENGE_TTL,
                          attempts=0))
    db.flush()
    return token


def find_challenge(db: DbSession, token: Optional[str]) -> Optional[LoginChallenge]:
    """A live challenge: known and in time (the wrong code that uses up the last try deletes it)."""
    if not token:
        return None
    row = db.get(LoginChallenge, token_hash(token))
    if row is None:
        return None
    if utcnow() >= as_utc(row.expires_at):
        return None
    return row


def wrong_code(db: DbSession, row: LoginChallenge) -> int:
    """Count a wrong code against the challenge. Returns the tries left; at 0 the challenge is gone."""
    db.execute(update(LoginChallenge).where(LoginChallenge.id == row.id)
               .values(attempts=LoginChallenge.attempts + 1))
    db.flush()
    db.refresh(row)
    left = max(0, CHALLENGE_ATTEMPTS - row.attempts)
    if left == 0:
        db.execute(delete(LoginChallenge).where(LoginChallenge.id == row.id))
        db.flush()
    return left


def finish_challenge(db: DbSession, row: LoginChallenge) -> bool:
    """Use the challenge up. False when another request already did."""
    done = db.execute(delete(LoginChallenge).where(LoginChallenge.id == row.id)).rowcount
    db.flush()
    return done == 1
