"""`python -m forgeyard secrets reset --yes`: the secret key is lost for good, start over.

Run with the server stopped. It makes a new key (the old key file, if any, is kept as
secret.key.lost-<time>), then for every stored secret the new key cannot read:

- Hugging Face tokens are dropped: enter them again on each deployment of a gated model;
- model replicas lose their internal key and are placed again, so they restart with a new one;
- workload environment values are emptied (the names stay), and the workload's events say which:
  delete the workload and create it again with the values;
- provider keys (1.2) are dropped: enter them again on the Providers page.

Nothing else changes: accounts, sessions, API keys and machines are hashed, not encrypted.
"""

from __future__ import annotations

import datetime as dt
import json

import sqlalchemy as sa

from forgeyard.db import Workload, get_engine, new_session
from forgeyard.migrate import upgrade
from forgeyard.security import crypto
from forgeyard.workloads import state as wstate

deployments = sa.table("deployments", sa.column("id", sa.Integer), sa.column("hf_token", sa.Text))
providers = sa.table("providers", sa.column("id", sa.Integer), sa.column("api_key", sa.Text),
                     sa.column("key_last4", sa.String))
replicas = sa.table("replicas", sa.column("id", sa.Integer), sa.column("secret", sa.Text),
                    sa.column("workload_id", sa.Integer))
workloads = sa.table("workloads", sa.column("id", sa.Integer), sa.column("spec", sa.JSON))
events = sa.table("workload_events", sa.column("workload_id", sa.Integer), sa.column("at", sa.DateTime(timezone=True)),
                  sa.column("replica", sa.Integer), sa.column("kind", sa.String), sa.column("message", sa.String))
settings_t = sa.table("server_settings", sa.column("key", sa.String), sa.column("value", sa.JSON),
                      sa.column("updated_by", sa.String), sa.column("updated_at", sa.DateTime(timezone=True)))


def _readable(box: crypto.Box, value, purpose: str) -> bool:
    try:
        box.open(value, purpose)
        return True
    except crypto.SecretError:
        return False


class NothingToReset(Exception):
    pass


def reset() -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    path = crypto.key_path()
    with get_engine().connect() as conn:
        kid = crypto.recorded_kid(conn)
    try:
        current = crypto.load_key(create=False)
    except crypto.SecretKeyError:
        current = None
    if current is not None and kid is not None and crypto.key_id(current) == kid:
        raise NothingToReset(f"The secret key in {path} is the one this database's secrets were encrypted with "
                             f"(key {kid}): nothing is lost, so nothing was reset.")
    if path.exists() and not crypto.key_from_env():
        path.rename(path.with_name(f"{path.name}.lost-{now.strftime('%Y%m%d%H%M%S')}"))
    crypto.reset()
    box = crypto.box()  # a new key file (or FORGEYARD_SECRET_KEY, if that is set)
    upgrade()
    counts = {"hf_tokens": 0, "replicas": 0, "env_values": 0, "provider_keys": 0}
    with get_engine().begin() as conn:
        for row in conn.execute(sa.select(deployments.c.id, deployments.c.hf_token)
                                .where(deployments.c.hf_token.is_not(None))).all():
            if not _readable(box, row.hf_token, "deployments.hf_token"):
                conn.execute(deployments.update().where(deployments.c.id == row.id).values(hf_token=None))
                counts["hf_tokens"] += 1
        for row in conn.execute(sa.select(providers.c.id, providers.c.api_key)
                                .where(providers.c.api_key.is_not(None))).all():
            if not _readable(box, row.api_key, "providers.api_key"):
                conn.execute(providers.update().where(providers.c.id == row.id).values(api_key=None, key_last4=None))
                counts["provider_keys"] += 1
        lost_keys: set[int] = set()
        for row in conn.execute(sa.select(replicas.c.id, replicas.c.secret, replicas.c.workload_id)
                                .where(replicas.c.secret.is_not(None))).all():
            if not _readable(box, row.secret, "replicas.secret"):
                conn.execute(replicas.update().where(replicas.c.id == row.id).values(secret=None))
                lost_keys.add(row.workload_id)
        for row in conn.execute(sa.select(workloads.c.id, workloads.c.spec)).all():
            spec = json.loads(row.spec) if isinstance(row.spec, str) else row.spec
            env = (spec or {}).get("env") if isinstance(spec, dict) else None
            if not isinstance(env, dict):
                continue
            lost = [k for k, v in env.items() if not _readable(box, v, f"workloads.spec.env.{k}")]
            if not lost:
                continue
            new_env = {k: (box.seal("", f"workloads.spec.env.{k}") if k in lost else v) for k, v in env.items()}
            conn.execute(workloads.update().where(workloads.c.id == row.id).values(spec={**spec, "env": new_env}))
            conn.execute(events.insert().values(
                workload_id=row.id, at=now, replica=None, kind="secret_reset",
                message=(f"The server's secret key was replaced: the values of {', '.join(sorted(lost))} were lost "
                         "and are now empty. Delete this workload and create it again with them.")[:500]))
            counts["env_values"] += len(lost)
        conn.execute(settings_t.delete().where(settings_t.c.key == crypto.SETTING_KEY))
        conn.execute(settings_t.insert().values(key=crypto.SETTING_KEY, value={"kid": box.kid},
                                                updated_by="secrets reset", updated_at=now))
    # Model replicas whose key was lost start again, like a restart from the UI: a new placement
    # gets a new key, and the agent replaces the replica because its generation changed. The
    # server places them when it starts.
    db = new_session()
    try:
        for workload_id in sorted(lost_keys):
            w = db.get(Workload, workload_id)
            if w is None:
                continue
            w.generation += 1
            for r in w.replicas:
                if not r.final:
                    wstate.reset_replica(r, w.generation)
                    counts["replicas"] += 1
            wstate.event(db, w, "secret_reset", "The server's secret key was replaced: its replicas start again "
                                                f"with new internal keys (generation {w.generation}).")
        db.commit()
    finally:
        db.close()
    summary = (f"Dropped {counts['hf_tokens']} Hugging Face token(s), placed {counts['replicas']} model replica(s) "
               f"again, emptied {counts['env_values']} workload environment value(s) and dropped "
               f"{counts['provider_keys']} provider key(s).")
    where = crypto.KEY_ENV if crypto.key_from_env() else str(path)
    return {"kid": box.kid, "key_file": where, **counts, "summary": summary}
