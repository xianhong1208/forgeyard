"""Machines (nodes), their accelerators and one-time join tokens.

Revision ID: 0002_machines
Revises: 0001_accounts
Create Date: 2026-11-09 08:42:11.462816
"""

from alembic import op
import sqlalchemy as sa


revision = '0002_machines'
down_revision = '0001_accounts'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('join_tokens',
    sa.Column('id', sa.String(length=64), nullable=False),
    sa.Column('public_id', sa.String(length=36), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('node_id', sa.Integer(), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('public_id')
    )
    op.create_table('nodes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=128), nullable=False),
    sa.Column('hostname', sa.String(length=255), nullable=True),
    sa.Column('credential_hash', sa.String(length=64), nullable=True),
    sa.Column('status', sa.String(length=16), nullable=False),
    sa.Column('labels', sa.JSON(), nullable=False),
    sa.Column('runtimes', sa.JSON(), nullable=False),
    sa.Column('platform', sa.JSON(), nullable=False),
    sa.Column('collectors', sa.JSON(), nullable=False),
    sa.Column('simulated', sa.Boolean(), nullable=False),
    sa.Column('agent_version', sa.String(length=32), nullable=True),
    sa.Column('protocol', sa.Integer(), nullable=True),
    sa.Column('ip', sa.String(length=64), nullable=True),
    sa.Column('joined_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('connected_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_heartbeat_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_by', sa.String(length=64), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('credential_hash')
    )
    op.create_table('accelerators',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('node_id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(length=160), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('vendor', sa.String(length=16), nullable=False),
    sa.Column('index', sa.Integer(), nullable=False),
    sa.Column('model', sa.String(length=160), nullable=False),
    sa.Column('arch', sa.String(length=32), nullable=True),
    sa.Column('uuid', sa.String(length=128), nullable=True),
    sa.Column('vram_total', sa.BigInteger(), nullable=True),
    sa.Column('vram_free', sa.BigInteger(), nullable=True),
    sa.Column('vram_used', sa.BigInteger(), nullable=True),
    sa.Column('unified_memory', sa.Boolean(), nullable=False),
    sa.Column('driver', sa.String(length=64), nullable=True),
    sa.Column('utilization', sa.Integer(), nullable=True),
    sa.Column('temperature', sa.Integer(), nullable=True),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('accelerators', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_accelerators_node_id'), ['node_id'], unique=False)
        batch_op.create_index('ix_accelerators_node_key', ['node_id', 'key'], unique=True)



def downgrade() -> None:
    with op.batch_alter_table('accelerators', schema=None) as batch_op:
        batch_op.drop_index('ix_accelerators_node_key')
        batch_op.drop_index(batch_op.f('ix_accelerators_node_id'))

    op.drop_table('accelerators')
    op.drop_table('nodes')
    op.drop_table('join_tokens')
