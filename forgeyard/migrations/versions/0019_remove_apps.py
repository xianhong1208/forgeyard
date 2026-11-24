"""Apps are removed (1.5.3, issue #89; deprecated in 1.4, #88).

An app was a workload that is not a model deployment's (runtime "container" or "process", made through the old
`POST /api/workloads`). This migration:

1. writes one audit entry per app (`workload.removed_in_upgrade`, actor "forgeyard"): its name, kind, runtime,
   image or command, ports, the names of its environment variables (never their values), its GPUs, who made it
   and when, where it was exposed at /x/, and where its replicas ran. So nothing disappears without a record, and
   the audit log is the place an admin (or an auditor) finds what was there;
2. deletes the apps with their replicas and events. Nothing is force-killed here: deleting a replica takes it
   out of the work list its machine's agent is sent, and the agent stops and removes what is no longer wanted the
   moment it is next sent that list (on reconnecting to the upgraded server, or later for a machine that was
   offline), the same way as deleting an app did before;
3. writes one audit entry per API key whose /x/ setting was not "none" (`api_key.x_access_removed`), then drops
   `api_keys.workloads`, `api_keys.workload_list`, `api_keys.workloads_locked_by` and `workloads.gateway`.

A model deployment's workload (runtime "model", including an engine trial's) is never touched, nor is anything
a deployment points at.

Going down adds the four columns back (every key "none", nothing exposed). It cannot bring the removed apps back:
their rows are gone, and the audit entries say what they were. The way back to the apps is the backup taken before
the upgrade (see the guide's reference, "Apps (removed in 1.5.3)").

Revision ID: 0019_remove_apps
Revises: 0018_engine_trials
Create Date: 2026-11-23 22:00:00.000000
"""

import json
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = '0019_remove_apps'
down_revision = '0018_engine_trials'
branch_labels = None
depends_on = None

REMOVED_IN = "1.5.3"
AUDIT_APP = "workload.removed_in_upgrade"
AUDIT_KEY = "api_key.x_access_removed"
ACTOR = "forgeyard"

workloads = sa.table('workloads', sa.column('id', sa.Integer), sa.column('name', sa.String), sa.column('kind', sa.String),
                     sa.column('runtime', sa.String), sa.column('spec', sa.JSON), sa.column('desired_state', sa.String),
                     sa.column('owner_id', sa.Integer), sa.column('created_by', sa.String),
                     sa.column('created_at', sa.DateTime(timezone=True)), sa.column('gateway', sa.JSON))
replicas = sa.table('replicas', sa.column('id', sa.Integer), sa.column('workload_id', sa.Integer),
                    sa.column('ordinal', sa.Integer), sa.column('node_id', sa.Integer), sa.column('state', sa.String),
                    sa.column('gpus', sa.JSON), sa.column('container', sa.String), sa.column('pid', sa.Integer),
                    sa.column('final', sa.Boolean))
events = sa.table('workload_events', sa.column('workload_id', sa.Integer))
deployments = sa.table('deployments', sa.column('workload_id', sa.Integer))
nodes = sa.table('nodes', sa.column('id', sa.Integer), sa.column('name', sa.String))
users = sa.table('users', sa.column('id', sa.Integer), sa.column('username', sa.String))
keys = sa.table('api_keys', sa.column('id', sa.Integer), sa.column('public_id', sa.String), sa.column('name', sa.String),
                sa.column('prefix', sa.String), sa.column('user_id', sa.Integer), sa.column('workloads', sa.String),
                sa.column('workload_list', sa.JSON), sa.column('workloads_locked_by', sa.String),
                sa.column('revoked_at', sa.DateTime(timezone=True)))
audit = sa.table('audit_events', sa.column('at', sa.DateTime(timezone=True)), sa.column('action', sa.String),
                 sa.column('actor_id', sa.Integer), sa.column('actor_name', sa.String), sa.column('target', sa.String),
                 sa.column('ip', sa.String), sa.column('detail', sa.JSON))


def _json(value):
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _iso(value):
    return value.isoformat() if isinstance(value, datetime) else (str(value) if value is not None else None)


def _cut(text, most=500):
    return text if len(text) <= most else text[: most - 1] + "…"


def _gpus_wanted(spec: dict):
    gpus = ((spec.get("resources") or {}).get("gpus") or {}) if isinstance(spec, dict) else {}
    if not isinstance(gpus, dict):
        return None
    if gpus.get("indices") is not None:
        return "GPU " + ", ".join(f"#{i}" for i in gpus["indices"])
    count = gpus.get("count") or 0
    return f"{count} GPU{'s' if count != 1 else ''}" if count else None


def app_detail(row, reps, node_names: dict, user_names: dict) -> dict:
    """What the audit entry of a removed app records (no secret: environment variables by name only)."""
    spec = _json(row.spec) or {}
    rt = (spec.get("runtime") or {}) if isinstance(spec, dict) else {}
    words = []
    if isinstance(rt.get("command"), list):
        words += [str(w) for w in rt["command"]]
    elif rt.get("command"):
        words.append(str(rt["command"]))
    words += [str(w) for w in rt.get("args") or []]
    gateway = _json(row.gateway) or {}
    placed = []
    for r in reps:
        where = node_names.get(r.node_id, f"machine {r.node_id}") if r.node_id is not None else "not placed"
        bits = [r.state or "?"]
        cards = [g.get("index") for g in (_json(r.gpus) or []) if isinstance(g, dict)]
        if cards:
            bits.append("GPU " + ", ".join(f"#{i}" for i in cards))
        if r.container:
            bits.append(f"container {r.container}")
        elif r.pid:
            bits.append(f"pid {r.pid}")
        placed.append(f"{r.ordinal} on {where} ({', '.join(bits)})")
    machine_id = spec.get("machine_id") if isinstance(spec, dict) else None
    return {k: v for k, v in {
        "reason": f"Apps were removed in Forgeyard {REMOVED_IN} (#89): the upgrade deleted this one; its machines' "
                  "agents stop and remove what it ran when they are next sent their work list.",
        "workload_id": row.id, "kind": row.kind, "runtime": row.runtime, "desired_state": row.desired_state,
        "created_by": row.created_by, "owner": user_names.get(row.owner_id) if row.owner_id is not None else None,
        "created_at": _iso(row.created_at),
        "image": rt.get("image"), "command": _cut(" ".join(words)) if words else None,
        "ports": ", ".join(str(p.get("name")) for p in spec.get("ports") or [] if isinstance(p, dict)) or None,
        "env_names": ", ".join(sorted((spec.get("env") or {}).keys())) or None,
        "replicas": spec.get("replicas"), "gpus": _gpus_wanted(spec),
        "machine": node_names.get(machine_id, f"machine {machine_id}") if machine_id is not None else None,
        "exposed_at": f"/x/{row.name}" if isinstance(gateway, dict) and gateway.get("expose") else None,
        "placed": "; ".join(placed) or None,
    }.items() if v is not None}


def upgrade() -> None:
    conn = op.get_bind()
    now = datetime.now(timezone.utc)
    node_names = {r.id: r.name for r in conn.execute(sa.select(nodes.c.id, nodes.c.name))}
    user_names = {r.id: r.username for r in conn.execute(sa.select(users.c.id, users.c.username))}
    # Never a deployment's workload, whatever its runtime says.
    owned = sa.select(deployments.c.workload_id).where(deployments.c.workload_id.is_not(None))
    apps = conn.execute(sa.select(workloads).where(workloads.c.runtime != "model", workloads.c.id.not_in(owned))
                        .order_by(workloads.c.id)).all()
    for app in apps:
        reps = conn.execute(sa.select(replicas).where(replicas.c.workload_id == app.id)
                            .order_by(replicas.c.ordinal)).all()
        conn.execute(audit.insert().values(at=now, action=AUDIT_APP, actor_id=None, actor_name=ACTOR,
                                           target=(app.name or "")[:128], ip=None,
                                           detail=app_detail(app, reps, node_names, user_names)))
    ids = [a.id for a in apps]
    if ids:
        conn.execute(events.delete().where(events.c.workload_id.in_(ids)))
        conn.execute(replicas.delete().where(replicas.c.workload_id.in_(ids)))
        conn.execute(workloads.delete().where(workloads.c.id.in_(ids)))

    for k in conn.execute(sa.select(keys).where(sa.or_(keys.c.workloads != "none", keys.c.workload_list.is_not(None),
                                                       keys.c.workloads_locked_by.is_not(None)))
                          .order_by(keys.c.id)).all():
        names = _json(k.workload_list)
        if (k.workloads or "none") == "none" and not names and not k.workloads_locked_by:
            continue  # a JSON null stored as such: nothing was allowed
        detail = {"reason": f"API keys' /x/ access was removed with apps in Forgeyard {REMOVED_IN} (#89); the key "
                            "itself keeps working for models.",
                  "key": k.public_id, "prefix": k.prefix, "owner": user_names.get(k.user_id),
                  "workloads": k.workloads or "none", "workload_list": ", ".join(map(str, names)) if names else None,
                  "locked_by": k.workloads_locked_by, "revoked": True if k.revoked_at is not None else None}
        conn.execute(audit.insert().values(at=now, action=AUDIT_KEY, actor_id=None, actor_name=ACTOR,
                                           target=(k.name or "")[:128], ip=None,
                                           detail={n: v for n, v in detail.items() if v is not None}))

    # Plain ALTER TABLE … DROP COLUMN (SQLite 3.35+), never batch mode: rebuilding api_keys or workloads on SQLite
    # would drop the old table, and with foreign keys on that deletes their dependants with it (see 0013).
    op.drop_column('api_keys', 'workloads_locked_by')
    op.drop_column('api_keys', 'workload_list')
    op.drop_column('api_keys', 'workloads')
    op.drop_column('workloads', 'gateway')


def downgrade() -> None:
    # The columns as 0013 made them: every key "none", nothing exposed. The removed apps do not come back.
    op.add_column('workloads', sa.Column('gateway', sa.JSON(), nullable=True))
    op.add_column('api_keys', sa.Column('workloads', sa.String(length=8), nullable=False, server_default='none'))
    op.add_column('api_keys', sa.Column('workload_list', sa.JSON(), nullable=True))
    op.add_column('api_keys', sa.Column('workloads_locked_by', sa.String(length=64), nullable=True))
