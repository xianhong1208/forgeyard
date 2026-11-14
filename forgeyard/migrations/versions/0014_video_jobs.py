"""Video jobs (1.3, /v1/videos): one row per video asked through the gateway, holding who asked (the
key, or the person in the Playground), the deployment and replica making it, the engine's own job id,
its status, the size and seconds asked, its timestamps, when its result expires, and its error.

A new table: no existing row changes.

Revision ID: 0014_video_jobs
Revises: 0013_passthrough
Create Date: 2026-11-14 18:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0014_video_jobs'
down_revision = '0013_passthrough'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'video_jobs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('public_id', sa.String(length=40), nullable=False),
        sa.Column('source', sa.String(length=8), nullable=False),
        sa.Column('key_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=False),
        sa.Column('model', sa.String(length=128), nullable=False),
        sa.Column('deployment_id', sa.Integer(), nullable=True),
        sa.Column('replica_uid', sa.String(length=32), nullable=False),
        sa.Column('replica_epoch', sa.String(length=32), nullable=False),
        sa.Column('upstream_id', sa.String(length=128), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('progress', sa.Integer(), nullable=False),
        sa.Column('size', sa.String(length=16), nullable=False),
        sa.Column('seconds', sa.Integer(), nullable=False),
        sa.Column('frames', sa.Integer(), nullable=False),
        sa.Column('fps', sa.Integer(), nullable=False),
        sa.Column('duration_ms', sa.Integer(), nullable=True),
        sa.Column('gpus', sa.Integer(), nullable=False),
        sa.Column('error_code', sa.String(length=40), nullable=True),
        sa.Column('error', sa.String(length=400), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('deadline_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('keep_s', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('removed', sa.Boolean(), nullable=False),
        sa.Column('counted', sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('public_id'),
    )
    op.create_index('ix_video_jobs_owner', 'video_jobs', ['source', 'key_id', 'user_id'])
    op.create_index(op.f('ix_video_jobs_deployment_id'), 'video_jobs', ['deployment_id'])
    op.create_index(op.f('ix_video_jobs_status'), 'video_jobs', ['status'])
    op.create_index(op.f('ix_video_jobs_expires_at'), 'video_jobs', ['expires_at'])


def downgrade() -> None:
    op.drop_index(op.f('ix_video_jobs_expires_at'), table_name='video_jobs')
    op.drop_index(op.f('ix_video_jobs_status'), table_name='video_jobs')
    op.drop_index(op.f('ix_video_jobs_deployment_id'), table_name='video_jobs')
    op.drop_index('ix_video_jobs_owner', table_name='video_jobs')
    op.drop_table('video_jobs')
