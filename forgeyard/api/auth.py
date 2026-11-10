"""Setup, sign-in, sign-out, registration, own password and own sessions."""

from __future__ import annotations

import threading
from typing import Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from forgeyard import audit
from forgeyard.api import links
from forgeyard.api.tls import tls_state
from forgeyard.api.deps import (
    Caller,
    cookie_secure,
    current,
    fail,
    normalise_username,
    optional_user,
    user_out,
)
from forgeyard.config import get_settings
from forgeyard.db import Session, User, get_db, utcnow
from forgeyard.security import passwords, secrets_store, sessions
from forgeyard.security.client import client_ip
from forgeyard.security.ratelimit import limiter, retry_after

router = APIRouter(prefix="/api/auth", tags=["auth"])
_setup_lock = threading.Lock()


class SetupIn(BaseModel):
    setup_token: str = Field(max_length=200)
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)
    email: Optional[str] = Field(default=None, max_length=255)


class LoginIn(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1024)


class RegisterIn(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=1024)
    email: Optional[str] = Field(default=None, max_length=255)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=1024)
    new_password: str = Field(max_length=1024)


class LinkCheckIn(BaseModel):
    token: str = Field(max_length=200)


class LinkUseIn(BaseModel):
    token: str = Field(max_length=200)
    password: str = Field(max_length=1024)


def has_users(db: DbSession) -> bool:
    return (db.scalar(select(func.count()).select_from(User)) or 0) > 0


def _start_session(request: Request, response: Response, db: DbSession, user: User) -> None:
    token, _ = sessions.create(db, user, client_ip(request), request.headers.get("user-agent"))
    user.last_login_at = utcnow()
    response.set_cookie(
        sessions.COOKIE,
        token,
        max_age=get_settings().session_max_days * 86400,
        httponly=True,
        samesite="strict",
        secure=cookie_secure(request),
        path="/",
    )


def _check_password(password: str, username: str) -> None:
    problem = passwords.policy_error(password, username)
    if problem:
        raise fail(422, "weak_password", problem)


@router.get("/state")
def state(request: Request, db: DbSession = Depends(get_db)):
    """What the sign-in screens need before anyone is signed in."""
    user, ended = optional_user(request, db)
    return {
        "needs_setup": not has_users(db),
        "registration": get_settings().registration,
        "user": user_out(user) if user else None,
        # Why this browser's session ended, so the sign-in page can say so.
        "ended": ended,
        # HTTPS or not: admins get a warning while the server speaks plain HTTP.
        "tls": tls_state(request),
    }


@router.post("/setup")
def setup(body: SetupIn, request: Request, response: Response, db: DbSession = Depends(get_db)):
    ip = client_ip(request)
    wait = limiter.login_wait("__setup__", ip, get_settings().login_free_failures, get_settings().login_ip_failures)
    if wait:
        raise fail(429, "rate_limited", "Too many tries. Wait a moment.", retry_after=retry_after(wait))
    with _setup_lock:
        if has_users(db):
            raise fail(409, "already_set_up", "Forgeyard already has an owner. Sign in instead.")
        if not secrets_store.setup_token_matches(body.setup_token):
            limiter.login_failed("__setup__", ip)
            audit.record(db, "setup.token_rejected", ip=ip)
            db.commit()
            raise fail(403, "bad_setup_token",
                       "That setup token is not right. It is printed in the server log at startup.")
        username = normalise_username(body.username)
        _check_password(body.password, username)
        owner = User(username=username, email=body.email or None, role="admin", status="active", is_owner=True,
                     password_hash=passwords.hash_password(body.password))
        db.add(owner)
        db.flush()
        audit.record(db, "setup.owner_created", actor=owner, target=username, ip=ip)
        _start_session(request, response, db, owner)
        db.commit()
        secrets_store.discard_setup_token()
    return {"user": user_out(owner)}


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: DbSession = Depends(get_db)):
    settings = get_settings()
    ip = client_ip(request)
    name = body.username.strip().lower()
    wait = limiter.login_wait(name, ip, settings.login_free_failures, settings.login_ip_failures)
    if wait:
        raise fail(429, "rate_limited", "Too many failed sign-ins. Wait before trying again.",
                   retry_after=retry_after(wait))
    user = db.scalar(select(User).where(User.username == name))
    if not passwords.verify_password(user.password_hash if user else None, body.password):
        limiter.login_failed(name, ip)
        audit.record(db, "auth.login_failed", actor_name=name[:64], ip=ip)
        db.commit()
        raise fail(401, "invalid_credentials", "Wrong username or password.")
    if user.status == "pending":
        raise fail(403, "pending_approval", "Your account is waiting for an admin to approve it.")
    if user.status != "active":
        raise fail(403, "account_disabled", "This account is disabled. Ask an admin.")
    limiter.login_succeeded(name)
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(body.password)
    _start_session(request, response, db, user)
    audit.record(db, "auth.login", actor=user, ip=ip)
    db.commit()
    return {"user": user_out(user)}


@router.post("/logout")
def logout(request: Request, response: Response, db: DbSession = Depends(get_db)):
    row, _ = sessions.resolve(db, request.cookies.get(sessions.COOKIE))
    if row is not None:
        sessions.revoke(db, row, "signed_out")
        audit.record(db, "auth.logout", actor=row.user, ip=client_ip(request))
        db.commit()
    response.delete_cookie(sessions.COOKIE, path="/", httponly=True, samesite="strict",
                           secure=cookie_secure(request))
    return {"ok": True}


@router.get("/me")
def me(caller: Caller = Depends(current)):
    return {"user": user_out(caller.user), "session": {
        "id": caller.session.public_id,
        "expires_at": caller.session.expires_at,
        "idle_minutes": get_settings().session_idle_minutes,
    }}


@router.post("/register")
def register(body: RegisterIn, request: Request, db: DbSession = Depends(get_db)):
    if get_settings().registration != "approval" or not has_users(db):
        raise fail(403, "registration_closed", "Self-registration is off. Ask an admin for an account.")
    username = normalise_username(body.username)
    _check_password(body.password, username)
    ip = client_ip(request)
    wait = limiter.login_wait("__register__", ip, get_settings().login_free_failures, get_settings().login_ip_failures)
    if wait:
        raise fail(429, "rate_limited", "Too many tries. Wait a moment.", retry_after=retry_after(wait))
    if db.scalar(select(User).where(User.username == username)):
        limiter.login_failed("__register__", ip)
        raise fail(409, "username_taken", "That username is taken.")
    user = User(username=username, email=body.email or None, role="user", status="pending",
                password_hash=passwords.hash_password(body.password))
    db.add(user)
    db.flush()
    audit.record(db, "user.registered", actor_name=username, target=username, ip=ip)
    db.commit()
    return {"status": "pending"}


@router.post("/password")
def change_password(body: PasswordChangeIn, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    user = caller.user  # same DB session as the route (FastAPI caches get_db per request)
    if not passwords.verify_password(user.password_hash, body.current_password):
        # 400, not 401: a wrong current password must not look like an ended session.
        raise fail(400, "wrong_password", "The current password is not right.")
    _check_password(body.new_password, user.username)
    user.password_hash = passwords.hash_password(body.new_password)
    ended = sessions.revoke_all(db, user.id, "password_changed", keep=caller.session.id)
    audit.record(db, "auth.password_changed", actor=user, ip=caller.ip, other_sessions_ended=ended)
    db.commit()
    return {"ok": True, "other_sessions_ended": ended}


@router.get("/sessions")
def my_sessions(caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    return {"sessions": [
        {
            "id": s.public_id,
            "current": s.id == caller.session.id,
            "created_at": s.created_at,
            "last_seen_at": s.last_seen_at,
            "expires_at": s.expires_at,
            "ip": s.ip,
            "user_agent": s.user_agent,
        }
        for s in sessions.live_sessions(db, caller.user.id)
    ]}


@router.delete("/sessions/{public_id}")
def end_session(public_id: str, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    row = db.scalar(select(Session).where(Session.public_id == public_id, Session.user_id == caller.user.id))
    if row is None or row.revoked_at is not None:
        raise fail(404, "not_found", "No such session.")
    sessions.revoke(db, row, "signed_out_elsewhere")
    audit.record(db, "auth.session_ended", actor=caller.user, ip=caller.ip, session=public_id)
    db.commit()
    return {"ok": True, "current": row.id == caller.session.id}


@router.post("/sessions/end-others")
def end_other_sessions(caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    ended = sessions.revoke_all(db, caller.user.id, "signed_out_elsewhere", keep=caller.session.id)
    audit.record(db, "auth.other_sessions_ended", actor=caller.user, ip=caller.ip, count=ended)
    db.commit()
    return {"ended": ended}


@router.post("/password-link/check")
def check_link(body: LinkCheckIn, db: DbSession = Depends(get_db)):
    link = links.find(db, body.token)
    if link is None:
        raise fail(404, "link_invalid", "This link has expired or was already used. Ask an admin for a new one.")
    user = db.get(User, link.user_id)
    return {"username": user.username, "expires_at": link.expires_at}


@router.post("/password-link")
def use_link(body: LinkUseIn, request: Request, db: DbSession = Depends(get_db)):
    ip = client_ip(request)
    wait = limiter.login_wait("__link__", ip, get_settings().login_free_failures, get_settings().login_ip_failures)
    if wait:
        raise fail(429, "rate_limited", "Too many tries. Wait a moment.", retry_after=retry_after(wait))
    link = links.find(db, body.token)
    if link is None:
        limiter.login_failed("__link__", ip)
        raise fail(404, "link_invalid", "This link has expired or was already used. Ask an admin for a new one.")
    user = db.get(User, link.user_id)
    _check_password(body.password, user.username)
    user.password_hash = passwords.hash_password(body.password)
    link.used_at = utcnow()
    sessions.revoke_all(db, user.id, "password_changed")
    audit.record(db, "auth.password_set_by_link", actor=user, ip=ip)
    db.commit()
    return {"username": user.username}
