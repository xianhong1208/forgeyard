"""Workloads, their replicas and events; per-machine GPU allowlist, runtimes and features.

Revision ID: 0003_workloads
Revises: 0002_machines
Create Date: 2026-11-09 12:10:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0003_workloads'
down_revision = '0002_machines'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('nodes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('gpu_allowlist', sa.JSON(), nullable=True))
        # Existing machines get empty values; their agent fills them on the next hello.
        batch_op.add_column(sa.Column('runtime_info', sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
        batch_op.add_column(sa.Column('features', sa.JSON(), nullable=False, server_default=sa.text("'[]'")))
    with op.batch_alter_table('nodes', schema=None) as batch_op:
        batch_op.alter_column('runtime_info', server_default=None)
        batch_op.alter_column('features', server_default=None)

    op.create_table('workloads',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=48), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('runtime', sa.String(length=16), nullable=False),
    sa.Column('spec', sa.JSON(), nullable=False),
    sa.Column('desired_state', sa.String(length=16), nullable=False),
    sa.Column('generation', sa.Integer(), nullable=False),
    sa.Column('owner_id', sa.Integer(), nullable=True),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['owner_id'], ['users.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('replicas',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('uid', sa.String(length=32), nullable=False),
    sa.Column('workload_id', sa.Integer(), nullable=False),
    sa.Column('ordinal', sa.Integer(), nullable=False),
    sa.Column('node_id', sa.Integer(), nullable=True),
    sa.Column('generation', sa.Integer(), nullable=False),
    sa.Column('gpus', sa.JSON(), nullable=False),
    sa.Column('state', sa.String(length=16), nullable=False),
    sa.Column('reason', sa.String(length=400), nullable=True),
    sa.Column('exit_code', sa.Integer(), nullable=True),
    sa.Column('attempt', sa.Integer(), nullable=False),
    sa.Column('restarts', sa.Integer(), nullable=False),
    sa.Column('final', sa.Boolean(), nullable=False),
    sa.Column('pid', sa.Integer(), nullable=True),
    sa.Column('container', sa.String(length=128), nullable=True),
    sa.Column('ports', sa.JSON(), nullable=False),
    sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('log_tail', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['workload_id'], ['workloads.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('uid')
    )
    with op.batch_alter_table('replicas', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_replicas_node_id'), ['node_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_replicas_workload_id'), ['workload_id'], unique=False)

    op.create_table('workload_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('workload_id', sa.Integer(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('replica', sa.Integer(), nullable=True),
    sa.Column('kind', sa.String(length=32), nullable=False),
    sa.Column('message', sa.String(length=500), nullable=False),
    sa.ForeignKeyConstraint(['workload_id'], ['workloads.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('workload_events', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_workload_events_workload_id'), ['workload_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('workload_events', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_workload_events_workload_id'))
    op.drop_table('workload_events')
    with op.batch_alter_table('replicas', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_replicas_workload_id'))
        batch_op.drop_index(batch_op.f('ix_replicas_node_id'))
    op.drop_table('replicas')
    op.drop_table('workloads')
    with op.batch_alter_table('nodes', schema=None) as batch_op:
        batch_op.drop_column('features')
        batch_op.drop_column('runtime_info')
        batch_op.drop_column('gpu_allowlist')
