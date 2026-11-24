"""`forgeyard check-apps`: what the 1.5.3 upgrade removes (or removed), without changing anything (#89).

Run it with the new version's code against the server's database before starting the upgraded server (which
migrates at once), e.g. with the server stopped:

    python -m forgeyard check-apps            # or: docker run --rm -v <data>:/data <the 1.5.3 server image> \\
                                              #       python -m forgeyard check-apps

- on a database the upgrade has not reached yet (before migration 0019): the apps it will stop and delete, and
  the API keys whose /x/ access it will take away; exit code 1 when there is anything, so a script can stop;
- on one it has reached: what it removed, from the audit log; exit code 0.

It only reads (no migration, no write), with the same settings as the server (DATA_DIR, DATABASE_URL). A running
1.5.2 server answers the same questions over its API; the guide's reference ("Apps (removed in 1.5.3)") shows how.
"""

from __future__ import annotations

import json
from typing import Optional

import sqlalchemy as sa

from forgeyard import removed


def _json(value):
    if isinstance(value, (str, bytes)):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def _runs(spec: dict) -> Optional[str]:
    rt = (spec or {}).get("runtime") or {}
    if rt.get("image"):
        return f"container {rt['image']}"
    words = ([*rt["command"]] if isinstance(rt.get("command"), list) else [rt["command"]] if rt.get("command") else [])
    words += list(rt.get("args") or [])
    return ("process " + " ".join(map(str, words)))[:200] if words else None


def inspect(engine: sa.Engine) -> dict:
    """{"state": "pending" | "done" | "empty", "apps": [...], "keys": [...]} for this database."""
    insp = sa.inspect(engine)
    tables = set(insp.get_table_names())
    if "workloads" not in tables or "api_keys" not in tables:
        return {"state": "empty", "apps": [], "keys": []}
    key_cols = {c["name"] for c in insp.get_columns("api_keys")}
    with engine.connect() as conn:
        if "workloads" not in key_cols:
            rows = conn.execute(sa.text("SELECT action, target, at, detail FROM audit_events WHERE action IN (:a, :k) "
                                        "ORDER BY id"), {"a": removed.AUDIT_APP, "k": removed.AUDIT_KEY}).all()
            return {"state": "done",
                    "apps": [{"name": r.target, "removed_at": str(r.at), **(_json(r.detail) or {})}
                             for r in rows if r.action == removed.AUDIT_APP],
                    "keys": [{"name": r.target, "removed_at": str(r.at), **(_json(r.detail) or {})}
                             for r in rows if r.action == removed.AUDIT_KEY]}
        wl_cols = {c["name"] for c in insp.get_columns("workloads")}
        gateway = "w.gateway" if "gateway" in wl_cols else "NULL"
        apps = []
        for r in conn.execute(sa.text(
                f"SELECT w.id, w.name, w.kind, w.runtime, w.desired_state, w.created_by, w.spec, {gateway} AS gateway "
                "FROM workloads w WHERE w.runtime <> 'model' AND w.id NOT IN "
                "(SELECT workload_id FROM deployments WHERE workload_id IS NOT NULL) ORDER BY w.id")).all():
            spec = _json(r.spec) or {}
            gw = _json(r.gateway) or {}
            apps.append({"id": r.id, "name": r.name, "kind": r.kind, "runtime": r.runtime, "desired_state": r.desired_state,
                         "created_by": r.created_by, "runs": _runs(spec),
                         "exposed_at": f"/x/{r.name}" if isinstance(gw, dict) and gw.get("expose") else None})
        keys = []
        for r in conn.execute(sa.text(
                "SELECT k.name, k.prefix, k.workloads, k.workload_list, k.workloads_locked_by, k.revoked_at, u.username "
                "FROM api_keys k LEFT JOIN users u ON u.id = k.user_id ORDER BY k.id")).all():
            names = _json(r.workload_list)
            if (r.workloads or "none") == "none" and not names and not r.workloads_locked_by:
                continue
            keys.append({"name": r.name, "prefix": r.prefix, "owner": r.username, "workloads": r.workloads or "none",
                         "workload_list": names or None, "locked_by": r.workloads_locked_by,
                         "revoked": r.revoked_at is not None})
        return {"state": "pending", "apps": apps, "keys": keys}


def report(found: dict) -> str:
    if found["state"] == "empty":
        where = f" (looked at {found['looked_at']}; set DATA_DIR or DATABASE_URL as for the server)" \
            if found.get("looked_at") else ""
        return f"No Forgeyard database here yet{where}: nothing to remove."
    lines = []
    if found["state"] == "done":
        if not found["apps"] and not found["keys"]:
            return (f"This database is already on {removed.REMOVED_IN} or later, and the upgrade removed nothing "
                    "(no apps, no API key with /x/ access).")
        lines.append(f"This database is already on {removed.REMOVED_IN} or later. The upgrade removed (audit log, "
                     f"actions {removed.AUDIT_APP} and {removed.AUDIT_KEY}):")
        for a in found["apps"]:
            lines.append(f"  app {a['name']}: {a.get('runtime', '?')} {a.get('image') or a.get('command') or ''}".rstrip()
                         + f" (removed {a['removed_at']})")
        for k in found["keys"]:
            lines.append(f"  API key {k['name']} ({k.get('prefix', '?')}, {k.get('owner') or '?'}): /x/ access "
                         f"{k.get('workloads', '?')} (removed {k['removed_at']})")
        return "\n".join(lines)
    if not found["apps"] and not found["keys"]:
        return (f"Nothing to remove: no apps and no API key with /x/ access. Upgrading to {removed.REMOVED_IN} changes "
                "nothing you use here.")
    lines.append(f"Upgrading to Forgeyard {removed.REMOVED_IN} will remove (and record each in the audit log):")
    for a in found["apps"]:
        bits = [a["kind"], a["desired_state"], a["runs"] or a["runtime"], f"by {a['created_by'] or '?'}"]
        if a["exposed_at"]:
            bits.append(f"exposed at {a['exposed_at']}")
        lines.append(f"  app {a['name']} (id {a['id']}): " + ", ".join(bits))
    for k in found["keys"]:
        what = k["workloads"] + (f": {', '.join(k['workload_list'])}" if k["workload_list"] else "")
        lines.append(f"  API key {k['name']} ({k['prefix']}, {k['owner'] or '?'}{', revoked' if k['revoked'] else ''}): "
                     f"/x/ access {what}; the key keeps working for models")
    lines.append(f"Deploy what you need as a model first, and take a backup. See {removed.GUIDE} and {removed.RELEASE_NOTES}")
    return "\n".join(lines)


def run(as_json: bool = False) -> int:
    from pathlib import Path

    from forgeyard.config import get_settings
    from forgeyard.db import get_engine

    url = sa.engine.make_url(get_settings().db_url)
    if url.get_backend_name() == "sqlite" and url.database and not Path(url.database).exists():
        # Connecting would make an empty file there; say where we looked instead.
        found = {"state": "empty", "apps": [], "keys": [], "looked_at": url.database}
    else:
        found = inspect(get_engine())
    print(json.dumps(found, indent=2, default=str) if as_json else report(found))
    return 1 if found["state"] == "pending" and (found["apps"] or found["keys"]) else 0
