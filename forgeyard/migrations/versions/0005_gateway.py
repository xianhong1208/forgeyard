"""Gateway: API keys, daily usage, and each model replica's internal key.

Revision ID: 0005_gateway
Revises: 0004_models
Create Date: 2026-11-10 18:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0005_gateway'
down_revision = '0004_models'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('replicas', schema=None) as batch_op:
        batch_op.add_column(sa.Column('secret', sa.String(length=64), nullable=True))

    op.create_table('api_keys',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('public_id', sa.String(length=36), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('prefix', sa.String(length=16), nullable=False),
    sa.Column('hash', sa.String(length=64), nullable=False),
    sa.Column('models', sa.JSON(), nullable=True),
    sa.Column('rpm', sa.Integer(), nullable=True),
    sa.Column('tpm', sa.Integer(), nullable=True),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('revoked_by', sa.String(length=64), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('hash'),
    sa.UniqueConstraint('public_id')
    )
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_api_keys_user_id'), ['user_id'], unique=False)

    op.create_table('usage_daily',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('day', sa.String(length=10), nullable=False),
    sa.Column('source', sa.String(length=8), nullable=False),
    sa.Column('key_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=64), nullable=False),
    sa.Column('model', sa.String(length=128), nullable=False),
    sa.Column('deployment_id', sa.Integer(), nullable=True),
    sa.Column('requests', sa.Integer(), nullable=False),
    sa.Column('errors', sa.Integer(), nullable=False),
    sa.Column('limited', sa.Integer(), nullable=False),
    sa.Column('prompt_tokens', sa.BigInteger(), nullable=False),
    sa.Column('completion_tokens', sa.BigInteger(), nullable=False),
    sa.Column('latency_ms_sum', sa.BigInteger(), nullable=False),
    sa.Column('latency_buckets', sa.JSON(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('usage_daily', schema=None) as batch_op:
        batch_op.create_index('ix_usage_daily_slot', ['day', 'source', 'key_id', 'user_id', 'model'], unique=True)
        batch_op.create_index('ix_usage_daily_user_day', ['user_id', 'day'], unique=False)
        batch_op.create_index('ix_usage_daily_deployment_day', ['deployment_id', 'day'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('usage_daily', schema=None) as batch_op:
        batch_op.drop_index('ix_usage_daily_deployment_day')
        batch_op.drop_index('ix_usage_daily_user_day')
        batch_op.drop_index('ix_usage_daily_slot')
    op.drop_table('usage_daily')
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_api_keys_user_id'))
    op.drop_table('api_keys')
    with op.batch_alter_table('replicas', schema=None) as batch_op:
        batch_op.drop_column('secret')
