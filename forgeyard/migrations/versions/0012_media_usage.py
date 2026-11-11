"""Usage units for the new kinds of models in 1.3, all added at once so later PRs only fill them.

usage_daily gains one column per unit, each 0 for every existing row (server default 0), so old
rows keep their meaning (they counted none of these):

- media_items: images, audio clips and videos sent in chat (1.3, multimodal chat);
- characters: text turned into speech;
- images: images generated;
- kilopixels: their size, in thousands of pixels (1024 x 1024 is 1,049);
- media_ms: audio or video produced, in milliseconds;
- bytes_in, bytes_out: request and answer bodies (passthrough workloads);
- busy_ms: time a replica spent on the requests (passthrough workloads, image generation).

Revision ID: 0012_media_usage
Revises: 0011_budgets
Create Date: 2026-11-11 20:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0012_media_usage'
down_revision = '0011_budgets'
branch_labels = None
depends_on = None

COLUMNS = ('media_items', 'characters', 'images', 'kilopixels', 'media_ms', 'bytes_in', 'bytes_out', 'busy_ms')


def upgrade() -> None:
    # Plain ALTER TABLE … ADD COLUMN, never batch mode (a table rebuild; see 0011_budgets).
    for name in COLUMNS:
        op.add_column('usage_daily', sa.Column(name, sa.BigInteger(), nullable=False, server_default=sa.text('0')))


def downgrade() -> None:
    # Plain ALTER TABLE … DROP COLUMN (SQLite 3.35+).
    for name in reversed(COLUMNS):
        op.drop_column('usage_daily', name)
