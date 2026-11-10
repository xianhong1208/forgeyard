"""Models: catalog, custom recipes, deployments, gateway routes, server settings; per-replica
rendered spec, model, engine, reservation and download progress; the agents' model caches.

Revision ID: 0004_models
Revises: 0003_workloads
Create Date: 2026-11-10 09:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0004_models'
down_revision = '0003_workloads'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('nodes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('model_cache', sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    with op.batch_alter_table('nodes', schema=None) as batch_op:
        batch_op.alter_column('model_cache', server_default=None)

    with op.batch_alter_table('replicas', schema=None) as batch_op:
        batch_op.add_column(sa.Column('spec', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('model', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('engine', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('vram', sa.BigInteger(), nullable=True))
        batch_op.add_column(sa.Column('download', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('ready_at', sa.DateTime(timezone=True), nullable=True))

    op.create_table('models',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('title', sa.String(length=120), nullable=True),
    sa.Column('source', sa.String(length=1024), nullable=False),
    sa.Column('kind', sa.String(length=8), nullable=False),
    sa.Column('ref', sa.String(length=128), nullable=True),
    sa.Column('revision', sa.String(length=64), nullable=True),
    sa.Column('file', sa.String(length=512), nullable=True),
    sa.Column('format', sa.String(length=16), nullable=False),
    sa.Column('task', sa.String(length=16), nullable=False),
    sa.Column('meta', sa.JSON(), nullable=False),
    sa.Column('engine_args', sa.JSON(), nullable=False),
    sa.Column('size_bytes', sa.BigInteger(), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('recipes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=40), nullable=False),
    sa.Column('yaml', sa.Text(), nullable=False),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('server_settings',
    sa.Column('key', sa.String(length=64), nullable=False),
    sa.Column('value', sa.JSON(), nullable=False),
    sa.Column('updated_by', sa.String(length=64), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('deployments',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=48), nullable=False),
    sa.Column('model_id', sa.Integer(), nullable=False),
    sa.Column('recipe', sa.String(length=40), nullable=False),
    sa.Column('params', sa.JSON(), nullable=False),
    sa.Column('served_name', sa.String(length=128), nullable=False),
    sa.Column('workload_id', sa.Integer(), nullable=False),
    sa.Column('estimate', sa.JSON(), nullable=False),
    sa.Column('hf_token', sa.Text(), nullable=True),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['model_id'], ['models.id'], ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['workload_id'], ['workloads.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name'),
    sa.UniqueConstraint('workload_id')
    )
    with op.batch_alter_table('deployments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_deployments_model_id'), ['model_id'], unique=False)

    op.create_table('gateway_routes',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('served_name', sa.String(length=128), nullable=False),
    sa.Column('api', sa.String(length=32), nullable=False),
    sa.Column('upstream_model', sa.String(length=256), nullable=False),
    sa.Column('deployment_id', sa.Integer(), nullable=False),
    sa.Column('workload_id', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['workload_id'], ['workloads.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('deployment_id'),
    sa.UniqueConstraint('served_name')
    )
    with op.batch_alter_table('gateway_routes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_gateway_routes_workload_id'), ['workload_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('gateway_routes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_gateway_routes_workload_id'))
    op.drop_table('gateway_routes')
    with op.batch_alter_table('deployments', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_deployments_model_id'))
    op.drop_table('deployments')
    op.drop_table('server_settings')
    op.drop_table('recipes')
    op.drop_table('models')
    with op.batch_alter_table('replicas', schema=None) as batch_op:
        batch_op.drop_column('ready_at')
        batch_op.drop_column('download')
        batch_op.drop_column('vram')
        batch_op.drop_column('engine')
        batch_op.drop_column('model')
        batch_op.drop_column('spec')
    with op.batch_alter_table('nodes', schema=None) as batch_op:
        batch_op.drop_column('model_cache')
