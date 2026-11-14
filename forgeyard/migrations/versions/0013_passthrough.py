"""Workloads behind the gateway at /x/<name> (1.3, generic passthrough).

- workloads.gateway: how the workload is exposed ({expose, port, read_timeout, max_body_mb,
  concurrency, key_concurrency}); NULL for every existing workload: not exposed.
- api_keys.workloads: which exposed workloads a key may call: none | listed | all. "none" for every
  existing key, so no key gains access to anything by the upgrade.
- api_keys.workload_list: the names a "listed" key may call (NULL otherwise).
- api_keys.workloads_locked_by: the admin who set the setting on someone else's key.

Revision ID: 0013_passthrough
Revises: 0012_media_usage
Create Date: 2026-11-14 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0013_passthrough'
down_revision = '0012_media_usage'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ALTER TABLE … ADD COLUMN, never batch mode (a table rebuild; see 0011_budgets).
    op.add_column('workloads', sa.Column('gateway', sa.JSON(), nullable=True))
    op.add_column('api_keys', sa.Column('workloads', sa.String(length=8), nullable=False, server_default='none'))
    op.add_column('api_keys', sa.Column('workload_list', sa.JSON(), nullable=True))
    op.add_column('api_keys', sa.Column('workloads_locked_by', sa.String(length=64), nullable=True))


def downgrade() -> None:
    # Plain ALTER TABLE … DROP COLUMN (SQLite 3.35+): rebuilding api_keys or workloads on SQLite would
    # drop the old table, and with foreign keys on that deletes their dependants with it.
    op.drop_column('api_keys', 'workloads_locked_by')
    op.drop_column('api_keys', 'workload_list')
    op.drop_column('api_keys', 'workloads')
    op.drop_column('workloads', 'gateway')
