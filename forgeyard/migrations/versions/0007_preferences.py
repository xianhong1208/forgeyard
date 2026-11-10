"""Users: small per-person settings of the web UI (the Get started checklist hidden or not).

Revision ID: 0007_preferences
Revises: 0006_key_locks
Create Date: 2026-11-10 21:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0007_preferences'
down_revision = '0006_key_locks'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('preferences', sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_column('preferences')
