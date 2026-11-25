"""Shared pieces for the API routes: errors, the signed-in user, role checks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session as DbSession, object_session

from forgeyard.config import get_settings
from forgeyard.db import Session, User, get_db
from forgeyard.security import mfa, mfa_policy, sessions
from forgeyard.security.client import client_ip

ROLES = ("admin", "operator", "user")
USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


def fail(status: int, code: str, message: str, **extra) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": message, **extra})


def normalise_username(raw: str) -> str:
    name = raw.strip().lower()
    if not USERNAME_RE.match(name):
        raise fail(422, "bad_username", "Use 2-64 characters: lowercase letters, digits, '.', '_' or '-'.")
    return name


@dataclass
class Caller:
    user: User
    session: Session
    ip: str


def signed_in(request: Request, db: DbSession = Depends(get_db)) -> Caller:
    """The signed-in person, even one whose required two-factor setup is overdue. Only the routes such a person
    may still use take this (who am I, and adding an authenticator: api/auth.py `me`, api/mfa.py); every other
    route takes `current`."""
    row, reason = sessions.resolve(db, request.cookies.get(sessions.COOKIE))
    if row is None:
        # 401 is the only status the UI treats as "go to the sign-in page".
        raise fail(401, "not_signed_in" if reason == "no_session" else "session_ended",
                   "Sign in to continue.", reason=reason)
    return Caller(user=row.user, session=row, ip=client_ip(request))


def current(request: Request, db: DbSession = Depends(get_db)) -> Caller:
    """The signed-in person (also called directly, with the request and a DB session, by routes that work in a
    thread)."""
    caller = signed_in(request, db)
    # 1.6.0 (#39): two-factor sign-in is required of this account and its grace period is over.
    if mfa_policy.due(db, caller.user):
        raise fail(403, mfa_policy.DUE_CODE, "Add an authenticator app in My account first: two-factor sign-in "
                                             "is required for your account.")
    return caller


def require_admin(caller: Caller = Depends(current)) -> Caller:
    if caller.user.role != "admin":
        raise fail(403, "forbidden", "Only admins can do this.")
    return caller


# Who may create, stop and delete workloads. Creating one is running code on a machine, so
# only roles trusted with that (see the design's security section); users get API keys in M4.
WORKLOAD_ROLES = ("admin", "operator")


def can_run_workloads(user: User) -> bool:
    return user.role in WORKLOAD_ROLES


def require_operator(caller: Caller = Depends(current)) -> Caller:
    if not can_run_workloads(caller.user):
        raise fail(403, "forbidden", "Only admins and operators can do this.")
    return caller


def request_is_https(request: Request) -> bool:
    """The browser talks HTTPS: to this server directly, or to a trusted proxy in front of it."""
    if request.url.scheme == "https":
        return True
    peer = request.client.host if request.client else ""
    return peer in get_settings().trusted_proxy_list and request.headers.get("x-forwarded-proto") == "https"


def cookie_secure(request: Request) -> bool:
    mode = get_settings().cookie_secure.strip().lower()
    if mode in ("true", "1", "yes"):
        return True
    if mode in ("false", "0", "no"):
        return False
    return request_is_https(request)


def public_base(request: Request) -> str:
    configured = get_settings().public_url.rstrip("/")
    if configured:
        return configured
    base = str(request.base_url).rstrip("/")
    if base.startswith("http://") and request_is_https(request):  # behind a TLS proxy
        base = "https://" + base[len("http://"):]
    return base


def user_out(u: User, has_mfa: Optional[bool] = None, policy: Optional[mfa_policy.Policy] = None) -> dict:
    db = object_session(u)
    if has_mfa is None:  # a list passes it, from one query for everyone
        has_mfa = bool(db is not None and u.id is not None and mfa.has_factor(db, u.id))
    enrolment = None
    if db is not None and u.id is not None:  # (a list passes the policy too, read once)
        enrolment = mfa_policy.enrolment(db, u, policy=policy, has_factor=has_mfa)
    return {
        "id": u.id,
        "username": u.username,
        "email": u.email,
        "role": u.role,
        "status": u.status,
        "is_owner": u.is_owner,
        "has_password": u.password_hash is not None,
        "has_mfa": has_mfa,
        # 1.6.0 (#39): {"by", "due"} while two-factor sign-in is required of this account and it has no
        # authenticator; due: the grace period is over and its sessions reach only the setup.
        "mfa_enrolment": enrolment,
        "created_at": u.created_at,
        "last_login_at": u.last_login_at,
    }


def optional_user(request: Request, db: DbSession) -> tuple[Optional[User], Optional[str]]:
    """The signed-in user, or None and why the browser's session ended (None if it had none)."""
    row, reason = sessions.resolve(db, request.cookies.get(sessions.COOKIE))
    if row:
        return row.user, None
    return None, None if reason == "no_session" else reason
