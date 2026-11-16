"""Download to… (1.4, issue #35): a model, and by default its engine, downloaded to chosen machines before
anything is deployed there.

- model_downloads: one row per (model, machine) request: what (the catalog model, the GGUF file, the recipe
  whose engine comes too), where (the machine), its state and progress, how it was fetched, who asked, when,
  and why it failed or was cancelled. A new table: no existing row changes.
- nodes.fetch_via: how a machine fetches models, engines and images: "auto" for every existing machine
  (directly, as before; relaying through the server comes in a later 1.4 change).

Revision ID: 0015_downloads
Revises: 0014_video_jobs
Create Date: 2026-11-16 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0015_downloads'
down_revision = '0014_video_jobs'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Plain ALTER TABLE … ADD COLUMN, never batch mode (a table rebuild; see 0011_budgets).
    op.add_column('nodes', sa.Column('fetch_via', sa.String(length=16), nullable=False, server_default='auto'))
    op.create_table(
        'model_downloads',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('uid', sa.String(length=32), nullable=False),
        sa.Column('model_id', sa.Integer(), nullable=False),
        sa.Column('node_id', sa.Integer(), nullable=False),
        sa.Column('file', sa.String(length=512), nullable=True),
        sa.Column('recipe', sa.String(length=40), nullable=True),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('via', sa.String(length=16), nullable=False),
        sa.Column('source_node_id', sa.Integer(), nullable=True),
        sa.Column('download', sa.JSON(), nullable=True),
        sa.Column('reason', sa.String(length=400), nullable=True),
        sa.Column('requested_by', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['model_id'], ['models.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['source_node_id'], ['nodes.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('uid'),
    )
    op.create_index(op.f('ix_model_downloads_model_id'), 'model_downloads', ['model_id'])
    op.create_index(op.f('ix_model_downloads_node_id'), 'model_downloads', ['node_id'])
    op.create_index(op.f('ix_model_downloads_state'), 'model_downloads', ['state'])


def downgrade() -> None:
    op.drop_index(op.f('ix_model_downloads_state'), table_name='model_downloads')
    op.drop_index(op.f('ix_model_downloads_node_id'), table_name='model_downloads')
    op.drop_index(op.f('ix_model_downloads_model_id'), table_name='model_downloads')
    op.drop_table('model_downloads')
    # Plain ALTER TABLE … DROP COLUMN (SQLite 3.35+): rebuilding nodes on SQLite would drop the old table,
    # and with foreign keys on that deletes its dependants (accelerators, replicas' machine) with it.
    op.drop_column('nodes', 'fetch_via')
