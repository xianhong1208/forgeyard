"""Secrets encrypted at rest: Hugging Face tokens, replica keys and workload environment values.

Makes the secret key (data_dir/secret.key) when there is none, records its id, and encrypts the
values that were stored in plain text. This is the only place plain-text values are read: after it,
a value that is not encrypted is an error. The downgrade decrypts them again.

Revision ID: 0008_encrypt_secrets
Revises: 0007_preferences
Create Date: 2026-11-10 23:00:00.000000
"""

import json
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa

from forgeyard.security import crypto


revision = '0008_encrypt_secrets'
down_revision = '0007_preferences'
branch_labels = None
depends_on = None

deployments = sa.table('deployments', sa.column('id', sa.Integer), sa.column('hf_token', sa.Text))
replicas = sa.table('replicas', sa.column('id', sa.Integer), sa.column('secret', sa.Text))
workloads = sa.table('workloads', sa.column('id', sa.Integer), sa.column('spec', sa.JSON))
settings = sa.table('server_settings', sa.column('key', sa.String), sa.column('value', sa.JSON),
                    sa.column('updated_by', sa.String), sa.column('updated_at', sa.DateTime(timezone=True)))


def _spec(value):
    return json.loads(value) if isinstance(value, str) else value


def _convert(conn, *, forward: bool) -> int:
    box = crypto.box()
    changed = 0

    def seal(value, purpose):
        return value if crypto.is_sealed(value) else box.seal(value, purpose)

    def unseal(value, purpose):
        return box.open(value, purpose) if crypto.is_sealed(value) else value

    fix = seal if forward else unseal
    for row in conn.execute(sa.select(deployments.c.id, deployments.c.hf_token)
                            .where(deployments.c.hf_token.is_not(None))).all():
        new = fix(row.hf_token, "deployments.hf_token")
        if new != row.hf_token:
            conn.execute(deployments.update().where(deployments.c.id == row.id).values(hf_token=new))
            changed += 1
    for row in conn.execute(sa.select(replicas.c.id, replicas.c.secret).where(replicas.c.secret.is_not(None))).all():
        new = fix(row.secret, "replicas.secret")
        if new != row.secret:
            conn.execute(replicas.update().where(replicas.c.id == row.id).values(secret=new))
            changed += 1
    for row in conn.execute(sa.select(workloads.c.id, workloads.c.spec)).all():
        spec = _spec(row.spec)
        env = spec.get("env") if isinstance(spec, dict) else None
        if not isinstance(env, dict) or not env:
            continue
        new_env = {k: fix(str(v), f"workloads.spec.env.{k}") for k, v in env.items()}
        if new_env != env:
            conn.execute(workloads.update().where(workloads.c.id == row.id).values(spec={**spec, "env": new_env}))
            changed += 1
    return changed


def upgrade() -> None:
    # A replica's internal key no longer fits 64 characters once encrypted.
    with op.batch_alter_table('replicas', schema=None) as batch_op:
        batch_op.alter_column('secret', existing_type=sa.String(length=64), type_=sa.Text(), existing_nullable=True)
    conn = op.get_bind()
    box = crypto.box()  # makes data_dir/secret.key on a new or upgraded server
    changed = _convert(conn, forward=True)
    conn.execute(settings.delete().where(settings.c.key == crypto.SETTING_KEY))
    conn.execute(settings.insert().values(key=crypto.SETTING_KEY, value={"kid": box.kid}, updated_by="migration",
                                          updated_at=datetime.now(timezone.utc)))
    if changed:
        crypto.log.info("Encrypted %d stored secret(s) with key %s.", changed, box.kid)
        # The plain-text values still sit in old pages (SQLite's WAL and free space, PostgreSQL's
        # dead row versions): forgeyard.migrate rewrites the tables once the migration committed.
        crypto.scrub_after_migration = True


def downgrade() -> None:
    conn = op.get_bind()
    _convert(conn, forward=False)
    conn.execute(settings.delete().where(settings.c.key == crypto.SETTING_KEY))
    with op.batch_alter_table('replicas', schema=None) as batch_op:
        batch_op.alter_column('secret', existing_type=sa.Text(), type_=sa.String(length=64), existing_nullable=True)
