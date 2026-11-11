"""Cloud providers and tiered routes (1.2).

- providers, provider_models: model APIs outside Forgeyard (the provider key stored encrypted);
- route_targets: who answers for a route, in tiers. Every existing route becomes one tier-0 target,
  its deployment, with the same upstream model name, so it behaves exactly as before;
- gateway_routes: no longer tied 1:1 to a deployment's workload; the flags of the spill rules;
- api_keys.cloud: "off" for every existing key (nothing is sent out after the upgrade);
- usage_daily: who answered (target), cached and reasoning tokens, and spilled requests.

The downgrade keeps routes that still have a local target (the deployment that created them) and
drops cloud-only routes, provider targets and the providers themselves.

Revision ID: 0009_providers
Revises: 0008_encrypt_secrets
Create Date: 2026-11-11 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0009_providers'
down_revision = '0008_encrypt_secrets'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('providers',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=64), nullable=False),
    sa.Column('kind', sa.String(length=24), nullable=False),
    sa.Column('base_url', sa.String(length=512), nullable=False),
    sa.Column('api_key', sa.Text(), nullable=True),
    sa.Column('key_last4', sa.String(length=8), nullable=True),
    sa.Column('headers', sa.JSON(), nullable=False),
    sa.Column('external', sa.Boolean(), nullable=False),
    sa.Column('allow_http', sa.Boolean(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.Column('timeout_s', sa.Integer(), nullable=True),
    sa.Column('created_by', sa.String(length=64), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name')
    )
    op.create_table('provider_models',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('provider_id', sa.Integer(), nullable=False),
    sa.Column('upstream_model', sa.String(length=256), nullable=False),
    sa.Column('api', sa.String(length=32), nullable=False),
    sa.Column('display_name', sa.String(length=128), nullable=True),
    sa.Column('context_length', sa.Integer(), nullable=True),
    sa.Column('drop_params', sa.JSON(), nullable=False),
    sa.Column('price_input', sa.BigInteger(), nullable=True),
    sa.Column('price_output', sa.BigInteger(), nullable=True),
    sa.Column('price_cache_read', sa.BigInteger(), nullable=True),
    sa.Column('price_cache_write', sa.BigInteger(), nullable=True),
    sa.Column('price_source', sa.String(length=16), nullable=True),
    sa.Column('price_checked_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['provider_id'], ['providers.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('provider_models', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_provider_models_provider_id'), ['provider_id'], unique=False)
        batch_op.create_index('ix_provider_models_name', ['provider_id', 'upstream_model', 'api'], unique=True)

    # The routes as they are, before their table is rebuilt (SQLite rebuilds it in batch mode).
    old = op.get_bind().execute(sa.text("SELECT id, deployment_id, upstream_model FROM gateway_routes")).all()

    with op.batch_alter_table('gateway_routes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('local_only', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('spill_on_long_prompt', sa.Boolean(), nullable=False, server_default=sa.false()))
        batch_op.add_column(sa.Column('wait_ms', sa.Integer(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('created_by', sa.String(length=64), nullable=True))
        batch_op.alter_column('deployment_id', existing_type=sa.Integer(), nullable=True)
        batch_op.drop_index(batch_op.f('ix_gateway_routes_workload_id'))
        batch_op.drop_column('workload_id')
        batch_op.drop_column('upstream_model')

    op.create_table('route_targets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('route_id', sa.Integer(), nullable=False),
    sa.Column('tier', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(length=16), nullable=False),
    sa.Column('deployment_id', sa.Integer(), nullable=True),
    sa.Column('provider_model_id', sa.Integer(), nullable=True),
    sa.Column('upstream_model', sa.String(length=256), nullable=True),
    sa.Column('weight', sa.Integer(), nullable=False),
    sa.Column('enabled', sa.Boolean(), nullable=False),
    sa.ForeignKeyConstraint(['deployment_id'], ['deployments.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['provider_model_id'], ['provider_models.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['route_id'], ['gateway_routes.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('route_targets', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_route_targets_deployment_id'), ['deployment_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_route_targets_provider_model_id'), ['provider_model_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_route_targets_route_id'), ['route_id'], unique=False)

    # Every route so far: one tier-0 target, its deployment, with the name its engine expects.
    if old:
        targets = sa.table('route_targets', sa.column('route_id', sa.Integer), sa.column('tier', sa.Integer),
                           sa.column('kind', sa.String), sa.column('deployment_id', sa.Integer),
                           sa.column('upstream_model', sa.String), sa.column('weight', sa.Integer),
                           sa.column('enabled', sa.Boolean))
        op.bulk_insert(targets, [{"route_id": rid, "tier": 0, "kind": "local", "deployment_id": dep,
                                  "upstream_model": upstream, "weight": 1, "enabled": True}
                                 for rid, dep, upstream in old])

    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.add_column(sa.Column('cloud', sa.String(length=8), nullable=False, server_default='off'))
        batch_op.add_column(sa.Column('cloud_locked_by', sa.String(length=64), nullable=True))

    with op.batch_alter_table('usage_daily', schema=None) as batch_op:
        batch_op.add_column(sa.Column('target', sa.String(length=32), nullable=False, server_default='local'))
        batch_op.add_column(sa.Column('cached_tokens', sa.BigInteger(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('reasoning_tokens', sa.BigInteger(), nullable=False, server_default=sa.text('0')))
        batch_op.add_column(sa.Column('spilled', sa.Integer(), nullable=False, server_default=sa.text('0')))
        batch_op.drop_index('ix_usage_daily_slot')
        batch_op.create_index('ix_usage_daily_slot', ['day', 'source', 'key_id', 'user_id', 'model', 'target'],
                              unique=True)


def downgrade() -> None:
    conn = op.get_bind()
    # Usage of the same slot from different targets is merged into one row again.
    rows = conn.execute(sa.text(
        "SELECT day, source, key_id, user_id, model, MIN(id), SUM(requests), SUM(errors), SUM(limited), "
        "SUM(prompt_tokens), SUM(completion_tokens), SUM(latency_ms_sum), COUNT(*) FROM usage_daily "
        "GROUP BY day, source, key_id, user_id, model HAVING COUNT(*) > 1")).all()
    for day, source, key_id, user_id, model, keep, req, errs, lim, pt, ct, lat, _n in rows:
        conn.execute(sa.text("DELETE FROM usage_daily WHERE day = :d AND source = :s AND key_id = :k AND user_id = :u "
                             "AND model = :m AND id <> :keep"),
                     {"d": day, "s": source, "k": key_id, "u": user_id, "m": model, "keep": keep})
        conn.execute(sa.text("UPDATE usage_daily SET requests = :r, errors = :e, limited = :l, prompt_tokens = :p, "
                             "completion_tokens = :c, latency_ms_sum = :t WHERE id = :keep"),
                     {"r": req, "e": errs, "l": lim, "p": pt, "c": ct, "t": lat, "keep": keep})
    with op.batch_alter_table('usage_daily', schema=None) as batch_op:
        batch_op.drop_index('ix_usage_daily_slot')
        batch_op.create_index('ix_usage_daily_slot', ['day', 'source', 'key_id', 'user_id', 'model'], unique=True)
        batch_op.drop_column('spilled')
        batch_op.drop_column('reasoning_tokens')
        batch_op.drop_column('cached_tokens')
        batch_op.drop_column('target')

    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_column('cloud_locked_by')
        batch_op.drop_column('cloud')

    # Each deployment's route gets back the upstream name of its own target. Read first: the tables
    # change below (and SQLite rebuilds gateway_routes, which would cascade into route_targets).
    own = dict(conn.execute(sa.text(
        "SELECT g.id, t.upstream_model FROM gateway_routes g JOIN route_targets t ON t.route_id = g.id "
        "WHERE t.kind = 'local' AND t.deployment_id = g.deployment_id")).all())
    with op.batch_alter_table('route_targets', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_route_targets_route_id'))
        batch_op.drop_index(batch_op.f('ix_route_targets_provider_model_id'))
        batch_op.drop_index(batch_op.f('ix_route_targets_deployment_id'))
    op.drop_table('route_targets')
    # Routes without the deployment that created them cannot exist before 1.2.
    op.execute("DELETE FROM gateway_routes WHERE deployment_id IS NULL")
    with op.batch_alter_table('gateway_routes', schema=None) as batch_op:
        batch_op.add_column(sa.Column('upstream_model', sa.String(length=256), nullable=True))
        batch_op.add_column(sa.Column('workload_id', sa.Integer(), nullable=True))
    op.execute("UPDATE gateway_routes SET upstream_model = served_name, "
               "workload_id = (SELECT d.workload_id FROM deployments d WHERE d.id = gateway_routes.deployment_id)")
    for rid, upstream in own.items():
        if upstream:
            conn.execute(sa.text("UPDATE gateway_routes SET upstream_model = :u WHERE id = :i"),
                         {"u": upstream, "i": rid})
    with op.batch_alter_table('gateway_routes', schema=None) as batch_op:
        batch_op.alter_column('upstream_model', existing_type=sa.String(length=256), nullable=False)
        batch_op.alter_column('workload_id', existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column('deployment_id', existing_type=sa.Integer(), nullable=False)
        batch_op.create_foreign_key('fk_gateway_routes_workload_id_workloads', 'workloads', ['workload_id'], ['id'],
                                    ondelete='CASCADE')
        batch_op.create_index(batch_op.f('ix_gateway_routes_workload_id'), ['workload_id'], unique=False)
        batch_op.drop_column('created_by')
        batch_op.drop_column('wait_ms')
        batch_op.drop_column('spill_on_long_prompt')
        batch_op.drop_column('local_only')

    with op.batch_alter_table('provider_models', schema=None) as batch_op:
        batch_op.drop_index('ix_provider_models_name')
        batch_op.drop_index(batch_op.f('ix_provider_models_provider_id'))
    op.drop_table('provider_models')
    op.drop_table('providers')
