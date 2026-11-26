"""The sample extension's own table, in a branch of its own that needs the community schema it was built on.

Revision ID: ext_sample_0001
"""

import sqlalchemy as sa
from alembic import op

revision = "ext_sample_0001"
down_revision = None
branch_labels = ("ext_sample",)
depends_on = "0024_stable_ids"


def upgrade() -> None:
    op.create_table("ext_sample_notes", sa.Column("id", sa.Integer, primary_key=True),
                    sa.Column("text", sa.String(200), nullable=False))


def downgrade() -> None:
    op.drop_table("ext_sample_notes")
