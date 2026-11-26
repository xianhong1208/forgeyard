"""GPU hours (1.6.1, issue #190): the GPU time model replicas hold, per UTC day, deployment, team, machine and GPU model.

- gpu_usage_daily: one row per (day, deployment, team, machine, GPU model) with the GPU-milliseconds held (weighted by
  each replica's share of a card), a CPU replica's reserved RAM × time, and the busy part. No foreign keys: rows outlive
  deployments, teams and machines. Written by the usage recorder (forgeyard/workloads/gpu_hours.py).

A new table: no existing row changes. Counting starts when a server with this migration runs (nothing is filled in for
the time before). Going down drops the table and the GPU hours counted with it.

Revision ID: 0023_gpu_usage
Revises: 0022_teams
Create Date: 2026-11-26 21:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0023_gpu_usage'
down_revision = '0022_teams'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'gpu_usage_daily',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('day', sa.String(length=10), nullable=False),
        sa.Column('deployment_id', sa.Integer(), nullable=False),
        sa.Column('team_id', sa.Integer(), nullable=False),
        sa.Column('node_id', sa.Integer(), nullable=False),
        sa.Column('vendor', sa.String(length=16), nullable=False),
        sa.Column('gpu_model', sa.String(length=160), nullable=False),
        sa.Column('gpu_ms', sa.BigInteger(), nullable=False),
        sa.Column('ram_mib_ms', sa.BigInteger(), nullable=False),
        sa.Column('busy_ms', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_gpu_usage_daily_slot', 'gpu_usage_daily',
                    ['day', 'deployment_id', 'team_id', 'node_id', 'gpu_model'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_gpu_usage_daily_slot', table_name='gpu_usage_daily')
    op.drop_table('gpu_usage_daily')
