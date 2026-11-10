"""API keys: limits an admin set on someone else's key are locked for its owner.

Revision ID: 0006_key_locks
Revises: 0005_gateway
Create Date: 2026-11-10 20:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0006_key_locks'
down_revision = '0005_gateway'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.add_column(sa.Column('rpm_locked_by', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('tpm_locked_by', sa.String(length=64), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_column('tpm_locked_by')
        batch_op.drop_column('rpm_locked_by')
