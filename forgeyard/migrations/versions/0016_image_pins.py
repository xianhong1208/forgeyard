"""Engine copies (1.5.2, issue #95): the recorded ids of the images "Try this version" pins.

- image_pins: one row per pinned image reference (`repo:tag@sha256:…`): its index, manifest and config digests and a
  content id per platform (the shape of forgeyard/recipes/images.lock.json's entries), the signature the server
  verified for it, who recorded it and when. A new table: no existing row changes, and going down drops it (the
  copies' recipes stay; their images then work as any custom recipe's do: pulled directly by digest).

Revision ID: 0016_image_pins
Revises: 0015_downloads
Create Date: 2026-11-23 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0016_image_pins'
down_revision = '0015_downloads'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'image_pins',
        sa.Column('ref', sa.String(length=255), nullable=False),
        sa.Column('entry', sa.JSON(), nullable=False),
        sa.Column('signature', sa.JSON(), nullable=True),
        sa.Column('recorded_by', sa.String(length=64), nullable=False),
        sa.Column('recorded_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('ref'),
    )


def downgrade() -> None:
    op.drop_table('image_pins')
