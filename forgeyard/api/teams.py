"""Teams (1.6.1, #190): forgeyard/teams.py says what a team and its roles are.

- `GET /api/teams`: admins and operators every team (operators choose one when they deploy); everyone else the teams
  they are in ("My teams"), with their role.
- `POST /api/teams` (admins): a new team, empty.
- `GET /api/teams/{name}`: a team's page, for admins and its members: members; for admins and its managers also its
  keys, deployments and this month's cloud spend.
- `PATCH /api/teams/{name}` (admins): its name, title and monthly cloud budget.
- `DELETE /api/teams/{name}` (admins): 409 `team_has_keys` while one of its keys still works (revoke them first) and
  409 `team_has_deployments` while a deployment belongs to it (move or delete them first). Its revoked and expired keys
  go with it; usage rows keep its id, so history stays.
- `PUT /api/teams/{name}/members/{username}`: admins add anyone with either role, and change roles; a team's managers
  add people as members.
- `DELETE /api/teams/{name}/members/{username}`: admins remove anyone; managers remove members (not managers).

Everything that changes a team is in the audit log (`team.*`).
"""

from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session as DbSession, selectinload

from forgeyard import audit, teams as tm
from forgeyard.api.deps import Caller, can_run_workloads, current, fail, require_admin
from forgeyard.db import ApiKey, Deployment, Team, TeamMember, User, as_utc, get_db, utcnow
from forgeyard.gateway import budgets as gbudgets, usage as gusage

router = APIRouter(prefix="/api/teams", tags=["teams"])

MAX_BUDGET = 10**15        # micro-USD, as for keys
Role = Literal["member", "manager"]


class TeamIn(BaseModel):
    name: str = Field(max_length=64)
    title: Optional[str] = Field(default=None, max_length=128)
    budget_micros: Optional[int] = Field(default=None, ge=0, le=MAX_BUDGET)


class TeamPatchIn(BaseModel):
    # A field left out keeps its value; budget_micros null removes the budget.
    name: Optional[str] = Field(default=None, max_length=64)
    title: Optional[str] = Field(default=None, min_length=1, max_length=128)
    budget_micros: Optional[int] = Field(default=None, ge=0, le=MAX_BUDGET)


class MemberIn(BaseModel):
    role: Role = "member"


def _name(raw: str) -> str:
    name = (raw or "").strip().lower()
    if not tm.NAME_RE.match(name):
        raise fail(422, "bad_team_name", "Use 2-64 characters: lowercase letters, digits, '.', '_' or '-'.")
    return name


def _get(db: DbSession, name: str, caller: Caller) -> tuple[Team, Optional[str]]:
    """The team and the caller's role in it. Someone who is not in it (and not an admin) cannot learn that it exists."""
    team = tm.by_name(db, name)
    role = tm.role_in(db, caller.user, team.id) if team is not None else None
    if team is None or (role is None and caller.user.role != "admin"):
        raise fail(404, "not_found", "No such team.")
    return team, role


def _manager(db: DbSession, name: str, caller: Caller) -> tuple[Team, Optional[str]]:
    team, role = _get(db, name, caller)
    if caller.user.role != "admin" and role != "manager":
        raise fail(403, "not_team_manager", "Only admins and the team's managers can do this.")
    return team, role


def _budget(team: Team, spend: gbudgets.Spend) -> dict:
    from forgeyard.api.keys import budget_out

    return budget_out(team.budget_micros, spend.spent.get(("team", team.id), 0), spend.held.get(("team", team.id), 0))


def _live(k: ApiKey) -> bool:
    exp = as_utc(k.expires_at)
    return k.revoked_at is None and (exp is None or exp > utcnow())


def team_out(db: DbSession, team: Team, caller: Caller, role: Optional[str], spend: gbudgets.Spend,
             counts: Optional[dict] = None) -> dict:
    counts = counts if counts is not None else _counts(db, {team.id})
    c = counts.get(team.id, {})
    return {"id": team.id, "name": team.name, "title": team.title, "created_by": team.created_by,
            "created_at": team.created_at, "my_role": role,
            "can_manage": caller.user.role == "admin" or role == "manager",
            "members": c.get("members", 0), "managers": c.get("managers", 0), "keys": c.get("keys", 0),
            "deployments": c.get("deployments", 0), "budget": _budget(team, spend)}


def _counts(db: DbSession, ids: set[int]) -> dict[int, dict]:
    out: dict[int, dict] = {i: {} for i in ids}
    if not ids:
        return out
    for tid, role, n in db.execute(select(TeamMember.team_id, TeamMember.role, func.count())
                                   .where(TeamMember.team_id.in_(ids)).group_by(TeamMember.team_id, TeamMember.role)):
        out[tid]["members"] = out[tid].get("members", 0) + n
        if role == "manager":
            out[tid]["managers"] = n
    for k in db.scalars(select(ApiKey).where(ApiKey.team_id.in_(ids))).all():
        if _live(k):
            out[k.team_id]["keys"] = out[k.team_id].get("keys", 0) + 1
    for tid, n in db.execute(select(Deployment.team_id, func.count()).where(Deployment.team_id.in_(ids))
                             .group_by(Deployment.team_id)):
        out[tid]["deployments"] = n
    return out


@router.get("")
def list_teams(caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    admin = caller.user.role == "admin"
    mine = dict(db.execute(select(TeamMember.team_id, TeamMember.role).where(TeamMember.user_id == caller.user.id)).all())
    q = select(Team).order_by(Team.title, Team.name)
    if not can_run_workloads(caller.user):
        # Admins and operators see every team (operators choose one when they deploy); others the teams they are in.
        q = q.where(Team.id.in_(set(mine)))
    rows = db.scalars(q).all()
    spend = gbudgets.ledger.snapshot()
    counts = _counts(db, {t.id for t in rows})
    return {"teams": [team_out(db, t, caller, mine.get(t.id), spend, counts) for t in rows], "is_admin": admin,
            "month": spend.month, "resets_at": gbudgets.resets_at(spend.month)}


@router.post("", status_code=201)
def create_team(body: TeamIn, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    name = _name(body.name)
    if tm.by_name(db, name) is not None:
        raise fail(409, "team_name_taken", f"A team named {name} already exists.")
    title = (body.title or "").strip() or name
    team = Team(name=name, title=title, budget_micros=body.budget_micros, created_by=caller.user.username)
    db.add(team)
    db.flush()
    audit.record(db, "team.created", actor=caller.user, target=name, ip=caller.ip, team_id=team.id, title=title,
                 budget_micros=body.budget_micros)
    db.commit()
    db.refresh(team)
    return {"team": team_out(db, team, caller, None, gbudgets.ledger.snapshot())}


@router.get("/{name}")
def get_team(name: str, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    team, role = _get(db, name, caller)
    spend = gbudgets.ledger.snapshot()
    out = team_out(db, team, caller, role, spend)
    members = db.scalars(select(TeamMember).where(TeamMember.team_id == team.id)
                         .options(selectinload(TeamMember.user)).order_by(TeamMember.role.desc(), TeamMember.id)).all()
    out["member_list"] = [{"username": m.user.username, "role": m.role, "status": m.user.status,
                           "added_by": m.added_by, "added_at": m.added_at}
                          for m in sorted(members, key=lambda m: (m.role != "manager", m.user.username))]
    if out["can_manage"]:
        from forgeyard.api.keys import key_status

        keys = db.scalars(select(ApiKey).where(ApiKey.team_id == team.id)
                          .order_by(ApiKey.revoked_at.is_not(None), ApiKey.created_at.desc(), ApiKey.id.desc())).all()
        out["key_list"] = [{"id": k.public_id, "name": k.name, "prefix": k.prefix, "status": key_status(k),
                            "created_by": k.created_by, "created_at": k.created_at, "last_used_at": k.last_used_at}
                           for k in keys]
    out["deployment_list"] = [{"id": d.id, "name": d.name, "served_name": d.served_name}
                              for d in db.scalars(select(Deployment).where(Deployment.team_id == team.id)
                                                  .order_by(Deployment.name)).all()]
    return {"team": out, "month": spend.month, "resets_at": gbudgets.resets_at(spend.month)}


@router.patch("/{name}")
def change_team(name: str, body: TeamPatchIn, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    team = tm.by_name(db, name)
    if team is None:
        raise fail(404, "not_found", "No such team.")
    fields = body.model_fields_set
    before = {"name": team.name, "title": team.title, "budget_micros": team.budget_micros}
    if "name" in fields and body.name is not None:
        new = _name(body.name)
        if new != team.name and tm.by_name(db, new) is not None:
            raise fail(409, "team_name_taken", f"A team named {new} already exists.")
        team.name = new
    if "title" in fields and body.title is not None:
        team.title = body.title.strip() or team.title
    if "budget_micros" in fields:
        team.budget_micros = body.budget_micros
    after = {"name": team.name, "title": team.title, "budget_micros": team.budget_micros}
    changes = {k: {"before": before[k], "after": after[k]} for k in before if before[k] != after[k]}
    if changes:
        audit.record(db, "team.changed", actor=caller.user, target=team.name, ip=caller.ip, team_id=team.id,
                     changes=changes)
        db.commit()
    db.refresh(team)
    return {"team": team_out(db, team, caller, tm.role_in(db, caller.user, team.id), gbudgets.ledger.snapshot())}


@router.delete("/{name}")
def delete_team(name: str, caller: Caller = Depends(require_admin), db: DbSession = Depends(get_db)):
    team = tm.by_name(db, name)
    if team is None:
        raise fail(404, "not_found", "No such team.")
    keys = db.scalars(select(ApiKey).where(ApiKey.team_id == team.id)).all()
    live = [k for k in keys if _live(k)]
    if live:
        raise fail(409, "team_has_keys", f"{len(live)} of the team's API keys still work. Revoke them first.",
                   keys=len(live))
    deployments = db.scalar(select(func.count()).select_from(Deployment).where(Deployment.team_id == team.id)) or 0
    if deployments:
        raise fail(409, "team_has_deployments", f"{deployments} deployments belong to the team. Move them to another "
                                                "team (or to none) or delete them first.", deployments=deployments)
    # What its keys did up to now is written with the team's id before they go (usage rows keep it).
    gusage.recorder.flush()
    for k in keys:
        db.delete(k)
    members = db.scalar(select(func.count()).select_from(TeamMember).where(TeamMember.team_id == team.id)) or 0
    db.execute(delete(TeamMember).where(TeamMember.team_id == team.id))
    db.delete(team)
    audit.record(db, "team.deleted", actor=caller.user, target=team.name, ip=caller.ip, team_id=team.id,
                 title=team.title, members=members, removed_keys=len(keys) or None)
    db.commit()
    return {"ok": True}


@router.put("/{name}/members/{username}")
def set_member(name: str, username: str, body: MemberIn, caller: Caller = Depends(current),
               db: DbSession = Depends(get_db)):
    team, _ = _manager(db, name, caller)
    admin = caller.user.role == "admin"
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None:
        raise fail(404, "no_such_user", f"No account named {username}.")
    row = db.scalar(select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == user.id))
    if not admin and (body.role != "member" or row is not None):
        # Managers add people as members; who manages a team, and roles, are an admin's choice.
        raise fail(403, "admins_only", "Only admins choose a team's managers and change roles.")
    if row is None:
        row = TeamMember(team_id=team.id, user_id=user.id, role=body.role, added_by=caller.user.username)
        db.add(row)
        audit.record(db, "team.member_added", actor=caller.user, target=team.name, ip=caller.ip, team_id=team.id,
                     member=user.username, role=body.role)
    elif row.role != body.role:
        audit.record(db, "team.member_changed", actor=caller.user, target=team.name, ip=caller.ip, team_id=team.id,
                     member=user.username, before=row.role, after=body.role)
        row.role = body.role
    db.commit()
    return {"member": {"username": user.username, "role": row.role, "status": user.status}}


@router.delete("/{name}/members/{username}")
def remove_member(name: str, username: str, caller: Caller = Depends(current), db: DbSession = Depends(get_db)):
    team, _ = _manager(db, name, caller)
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    row = db.scalar(select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == user.id)) \
        if user is not None else None
    if row is None:
        raise fail(404, "not_a_member", f"{username} is not in this team.")
    if caller.user.role != "admin" and row.role != "member":
        raise fail(403, "admins_only", "Only admins remove a team's managers.")
    db.delete(row)
    audit.record(db, "team.member_removed", actor=caller.user, target=team.name, ip=caller.ip, team_id=team.id,
                 member=user.username, role=row.role)
    db.commit()
    return {"ok": True}
