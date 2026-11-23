"""Download to… for an engine alone (1.5.2, issue #95): a download may name no model, only the engine (its recipe).

- model_downloads.model_id becomes nullable. Every existing row keeps its model; a new row without one fetches only
  the image or the program of the recipe's variant for that machine. Going down deletes those engine-only rows
  (an older server cannot show them) and makes the column NOT NULL again.

On SQLite this rebuilds model_downloads (batch mode: SQLite cannot change a column's NULL rule in place). That is
safe for this table: nothing references it, so dropping the old copy deletes nothing else (unlike nodes, see
0011_budgets and 0015_downloads).

Revision ID: 0017_engine_downloads
Revises: 0016_image_pins
Create Date: 2026-11-23 18:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0017_engine_downloads'
down_revision = '0016_image_pins'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('model_downloads', schema=None) as batch_op:
        batch_op.alter_column('model_id', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    op.execute("DELETE FROM model_downloads WHERE model_id IS NULL")
    with op.batch_alter_table('model_downloads', schema=None) as batch_op:
        batch_op.alter_column('model_id', existing_type=sa.Integer(), nullable=False)
