"""Budgets (1.2): monthly cloud spend limits in micro-USD per API key, person and provider, and the
80% / 100% alerts already written for a month.

- api_keys: budget_micros, budget_locked_by (the admin who set it on someone else's key).
- users: budget_micros.
- providers: budget_micros.
- budget_alerts: one row per scope, month and threshold, so an alert is audited once.

Every existing row gets no budget (NULL): unlimited, as before.

Revision ID: 0011_budgets
Revises: 0010_cost
Create Date: 2026-11-11 16:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0011_budgets'
down_revision = '0010_cost'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ALTER TABLE … ADD COLUMN: never a table rebuild (see downgrade()).
    op.add_column('api_keys', sa.Column('budget_micros', sa.BigInteger(), nullable=True))
    op.add_column('api_keys', sa.Column('budget_locked_by', sa.String(length=64), nullable=True))
    op.add_column('users', sa.Column('budget_micros', sa.BigInteger(), nullable=True))
    op.add_column('providers', sa.Column('budget_micros', sa.BigInteger(), nullable=True))
    op.create_table('budget_alerts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('scope', sa.String(length=16), nullable=False),
    sa.Column('scope_id', sa.Integer(), nullable=False),
    sa.Column('month', sa.String(length=7), nullable=False),
    sa.Column('threshold', sa.Integer(), nullable=False),
    sa.Column('spent_micros', sa.BigInteger(), nullable=False),
    sa.Column('budget_micros', sa.BigInteger(), nullable=False),
    sa.Column('at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('budget_alerts', schema=None) as batch_op:
        batch_op.create_index('ix_budget_alerts_once', ['scope', 'scope_id', 'month', 'threshold'], unique=True)


def downgrade() -> None:
    with op.batch_alter_table('budget_alerts', schema=None) as batch_op:
        batch_op.drop_index('ix_budget_alerts_once')
    op.drop_table('budget_alerts')
    # Plain ALTER TABLE … DROP COLUMN (SQLite 3.35+), not batch mode: rebuilding `users` or `providers`
    # on SQLite would drop the old table, and with foreign keys on that deletes every API key and
    # provider model with it.
    op.drop_column('providers', 'budget_micros')
    op.drop_column('users', 'budget_micros')
    op.drop_column('api_keys', 'budget_locked_by')
    op.drop_column('api_keys', 'budget_micros')
