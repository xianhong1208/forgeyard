"""Trying an engine before switching to it (1.5.2, issue #95, PR 4): one row per trial run.

- engine_trials: one row per trial of an engine (usually a copy "Try this version" made, compared with its built-in
  engine): the engine and its built-in, the engine version, the catalog model and the machine it ran on (kept by name
  too, so the row outlives them), the GPUs, the parameters, the estimate and what the replica reserved, who asked and
  when, its state (running | passed | failed | cancelled), the phase while it runs, the checks
  ([{id, label, value, expected, ok, note}]), the comparison with the built-in engine, the measurements it took and a
  one-line summary. `deployment_id` names the temporary deployment while the trial runs (NULL once it is removed;
  the foreign key sets it NULL if the deployment goes some other way).

A new table: no existing row changes. Going down drops it; a trial still running then leaves its temporary deployment
behind as an ordinary one (an older server does not know trials), so finish or cancel trials before downgrading.

Revision ID: 0018_engine_trials
Revises: 0017_engine_downloads
Create Date: 2026-11-23 20:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0018_engine_trials'
down_revision = '0017_engine_downloads'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'engine_trials',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('recipe', sa.String(length=40), nullable=False),
        sa.Column('builtin', sa.String(length=40), nullable=True),
        sa.Column('engine', sa.String(length=40), nullable=False),
        sa.Column('engine_version', sa.String(length=64), nullable=False),
        sa.Column('model_id', sa.Integer(), nullable=True),
        sa.Column('model_name', sa.String(length=64), nullable=False),
        sa.Column('task', sa.String(length=16), nullable=False),
        sa.Column('node_id', sa.Integer(), nullable=True),
        sa.Column('machine_name', sa.String(length=128), nullable=False),
        sa.Column('gpus', sa.JSON(), nullable=False),
        sa.Column('params', sa.JSON(), nullable=False),
        sa.Column('estimate', sa.JSON(), nullable=False),
        sa.Column('reserved', sa.BigInteger(), nullable=True),
        sa.Column('deployment_id', sa.Integer(), nullable=True),
        sa.Column('requested_by', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ready_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('state', sa.String(length=16), nullable=False),
        sa.Column('phase', sa.String(length=16), nullable=True),
        sa.Column('checks', sa.JSON(), nullable=False),
        sa.Column('comparison', sa.JSON(), nullable=True),
        sa.Column('measurements', sa.JSON(), nullable=False),
        sa.Column('summary', sa.String(length=500), nullable=True),
        sa.ForeignKeyConstraint(['model_id'], ['models.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['node_id'], ['nodes.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('engine_trials', schema=None) as batch_op:
        batch_op.create_index('ix_engine_trials_recipe', ['recipe', 'id'], unique=False)
        batch_op.create_index('ix_engine_trials_state', ['state'], unique=False)
        batch_op.create_index('ix_engine_trials_deployment_id', ['deployment_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('engine_trials', schema=None) as batch_op:
        batch_op.drop_index('ix_engine_trials_deployment_id')
        batch_op.drop_index('ix_engine_trials_state')
        batch_op.drop_index('ix_engine_trials_recipe')
    op.drop_table('engine_trials')
