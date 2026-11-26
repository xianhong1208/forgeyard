"""Teams (1.6.1, #190): who is in which team, and what a team role allows.

A team role is a second, narrower layer next to a person's install-wide role (admin, operator, user):

- a **member** is in the team: the team's page lists them and the teams they are in;
- a **manager** also adds and removes members, makes and revokes the team's API keys, sees the team's usage, and
  stops, starts and changes the team's deployments (not the engine or its extra arguments: those run on machines,
  which stays with admins and operators).

Admins do all of that for every team, and only admins make, rename and delete teams, choose managers and set a team's
cloud budget. Admins and operators keep their rights on every deployment, with or without a team.

An install without teams behaves exactly as before 1.6.1: nothing here is asked until a key or a deployment has a team.
"""

from __future__ import annotations

import re
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from forgeyard.db import Deployment, Team, TeamMember, User

ROLES = ("member", "manager")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")


def role_in(db: DbSession, user: User, team_id: Optional[int]) -> Optional[str]:
    """`member`, `manager` or None (not in the team, or no team)."""
    if team_id is None:
        return None
    return db.scalar(select(TeamMember.role).where(TeamMember.team_id == team_id, TeamMember.user_id == user.id))


def manages(db: DbSession, user: User, team_id: Optional[int]) -> bool:
    """May this person manage the team (its members, keys and usage)? Admins manage every team."""
    if team_id is None:
        return False
    return user.role == "admin" or role_in(db, user, team_id) == "manager"


def managed_ids(db: DbSession, user: User) -> set[int]:
    """The teams this person manages (as a manager; an admin's rights on every team are not listed here)."""
    return set(db.scalars(select(TeamMember.team_id).where(TeamMember.user_id == user.id,
                                                           TeamMember.role == "manager")).all())


def by_name(db: DbSession, name: str) -> Optional[Team]:
    return db.scalar(select(Team).where(Team.name == (name or "").strip().lower()))


def can_manage_deployment(db: DbSession, user: User, d: Deployment) -> bool:
    """Admins and operators manage every deployment; a team's managers manage the team's."""
    from forgeyard.api.deps import can_run_workloads

    return can_run_workloads(user) or (d.team_id is not None and role_in(db, user, d.team_id) == "manager")


def brief(team: Optional[Team]) -> Optional[dict]:
    return {"id": team.id, "name": team.name, "title": team.title} if team is not None else None
