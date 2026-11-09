"""User administration (admins only)."""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard import audit
from forgeyard.api import links
from forgeyard.api.deps import Caller, fail, normalise_username, public_base, require_admin, user_out
from forgeyard.db import User, get_db
from forgeyard.security import sessions

router = APIRouter(prefix="/api/users", tags=["users"])

Role = Literal["admin", "operator", "user"]
Status = Literal["active", "disabled"]


class UserCreateIn(BaseModel):
    username: str = Field(max_length=64)
    email: Optional[str] = Field(default=None, max_length=255)
    role: Role = "user"


class UserPatchIn(BaseModel):
    role: Optional[Role] = None
    status: Optional[Status] = None
    email: Optional[str] = Field(default=None, max_length=255)


def _get(db: DbSession, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise fail(404, "not_found", "No such user.")
    return user


@router.get("")
def list_users(_: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    users = db.scalars(select(User).order_by(User.status != "pending", User.username)).all()
    return {"users": [user_out(u) for u in users]}


@router.post("", status_code=201)
def create_user(body: UserCreateIn, request: Request, caller: Caller = Depends(require_admin),
                db: DbSession = Depends(get_db)):
    username = normalise_username(body.username)
    if db.scalar(select(User).where(User.username == username)):
        raise fail(409, "username_taken", "That username is taken.")
    user = User(username=username, email=body.email or None, role=body.role, status="active")
    db.add(user)
    db.flush()
    token = links.issue(db, user, links.NEW_ACCOUNT_TTL)
    audit.record(db, "user.created", actor=caller.user, target=username, ip=caller.ip, role=body.role)
    db.commit()
    return {"user": user_out(user), "set_password_url": links.url(public_base(request), token),
            "expires_hours": int(links.NEW_ACCOUNT_TTL.total_seconds() // 3600)}


@router.patch("/{user_id}")
def update_user(user_id: int, body: UserPatchIn, caller: Caller = Depends(require_admin),
                db: DbSession = Depends(get_db)):
    user = _get(db, user_id)
    changes: dict = {}
    if body.role is not None and body.role != user.role:
        if user.is_owner:
            raise fail(409, "owner_protected", "The owner is always an admin.")
        changes["role"] = [user.role, body.role]
        user.role = body.role
    if body.status is not None and body.status != user.status:
        if user.is_owner:
            raise fail(409, "owner_protected", "The owner cannot be disabled.")
        if user.id == caller.user.id:
            raise fail(409, "self_protected", "You cannot disable your own account.")
        changes["status"] = [user.status, body.status]
        approving = user.status == "pending" and body.status == "active"
        user.status = body.status
        if body.status == "disabled":
            changes["sessions_ended"] = sessions.revoke_all(db, user.id, "account_disabled")
        if approving:
            audit.record(db, "user.approved", actor=caller.user, target=user.username, ip=caller.ip)
    if body.email is not None and (body.email or None) != user.email:
        changes["email"] = True
        user.email = body.email or None
    if changes:
        audit.record(db, "user.updated", actor=caller.user, target=user.username, ip=caller.ip, **changes)
    db.commit()
    return {"user": user_out(user)}


@router.delete("/{user_id}")
def delete_user(user_id: int, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    user = _get(db, user_id)
    if user.is_owner:
        raise fail(409, "owner_protected", "The owner cannot be deleted.")
    if user.id == caller.user.id:
        raise fail(409, "self_protected", "You cannot delete your own account.")
    name = user.username
    db.delete(user)
    audit.record(db, "user.deleted", actor=caller.user, target=name, ip=caller.ip)
    db.commit()
    return {"ok": True}


@router.post("/{user_id}/password-link")
def reset_link(user_id: int, request: Request, caller: Caller = Depends(require_admin),
               db: DbSession = Depends(get_db)):
    user = _get(db, user_id)
    if user.status == "pending":
        raise fail(409, "pending", "Approve the account first.")
    token = links.issue(db, user, links.RESET_TTL)
    audit.record(db, "user.password_link_issued", actor=caller.user, target=user.username, ip=caller.ip)
    db.commit()
    return {"set_password_url": links.url(public_base(request), token),
            "expires_hours": int(links.RESET_TTL.total_seconds() // 3600)}


@router.post("/{user_id}/sign-out")
def force_sign_out(user_id: int, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    user = _get(db, user_id)
    ended = sessions.revoke_all(db, user.id, "signed_out_by_admin")
    audit.record(db, "user.signed_out", actor=caller.user, target=user.username, ip=caller.ip, count=ended)
    db.commit()
    return {"ended": ended}
