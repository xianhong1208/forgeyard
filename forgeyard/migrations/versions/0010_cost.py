"""Cost tracking (1.2): what gateway requests cost, and where a provider model's prices came from.

- usage_daily: cost_micros (micro-USD), and the requests that were unpriced, priced by the
  upstream's own figure, or estimated. Rows from before have no cost: 0, as if local.
- provider_models: price_ref, the OpenRouter model id an admin confirmed for its prices.

Revision ID: 0010_cost
Revises: 0009_providers
Create Date: 2026-11-11 10:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0010_cost'
down_revision = '0009_providers'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('usage_daily', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cost_micros', sa.BigInteger(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('unpriced', sa.Integer(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('cost_reported', sa.Integer(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('cost_estimated', sa.Integer(), nullable=False, server_default=sa.text('0')))
    with op.batch_alter_table('provider_models', schema=None) as batch_op:
        batch_op.add_column(sa.Column('price_ref', sa.String(length=256), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('provider_models', schema=None) as batch_op:
        batch_op.drop_column('price_ref')
    with op.batch_alter_table('usage_daily', schema=None) as batch_op:
        batch_op.drop_column('cost_estimated')
        batch_op.drop_column('cost_reported')
        batch_op.drop_column('unpriced')
        batch_op.drop_column('cost_micros')
