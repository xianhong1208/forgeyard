"""Teams (1.6.1, issue #190): people who share API keys, deployments and a cloud budget.

- teams: a name (like a username), a title, a monthly cloud budget (NULL: none), who made it and when.
- team_members: a person in a team as `member` or `manager`; both foreign keys cascade.
- api_keys.team_id (NULL: a personal key, as every existing key), fixed when a key is made. api_keys.user_id becomes
  nullable: a team key belongs to its team, not to the person who made it, and survives that person.
  api_keys.created_by: who made a key (NULL on existing keys).
- deployments.team_id (NULL: no team, as every existing deployment).
- The database refuses to delete a team that a key or a deployment points at: the foreign keys keep the default
  (NO ACTION), which refuses like RESTRICT. No ON DELETE is written, because SQLite reads none back from a column
  added with ALTER TABLE, and the schema must read back as the models say.
- usage_daily.team_id, 0 on every existing row (no team), with an index (team_id, day) for the Usage page's team
  split. Not part of the slot index: a key's team never changes, so the key already decides the row.

No team exists after the upgrade and no existing row changes meaning: keys, usage, budgets and permissions work exactly
as before until an admin makes a team.

On SQLite, api_keys is rebuilt (batch mode: SQLite cannot change a column's NULL rule in place). That is safe for this
table: nothing references it, so dropping the old copy deletes nothing else. deployments is NOT rebuilt (gateway routes
and their targets reference it with ON DELETE CASCADE: dropping the old copy would delete them): its column is added
with a plain ALTER TABLE … ADD COLUMN … REFERENCES, which SQLite allows for a column whose default is NULL.

Going down deletes the team keys (an older server has no owner for them), then drops the columns and both tables.

Revision ID: 0022_teams
Revises: 0021_single_sign_on
Create Date: 2026-11-26 18:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0022_teams'
down_revision = '0021_single_sign_on'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'teams',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=128), nullable=False),
        sa.Column('budget_micros', sa.BigInteger(), nullable=True),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_teams_name', 'teams', ['name'], unique=True)
    op.create_table(
        'team_members',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('team_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('role', sa.String(length=16), nullable=False),
        sa.Column('added_by', sa.String(length=64), nullable=True),
        sa.Column('added_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['team_id'], ['teams.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('team_id', 'user_id', name='uq_team_members_team_user'),
    )
    op.create_index('ix_team_members_team_id', 'team_members', ['team_id'], unique=False)
    op.create_index('ix_team_members_user_id', 'team_members', ['user_id'], unique=False)

    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.add_column(sa.Column('team_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('created_by', sa.String(length=64), nullable=True))
        batch_op.alter_column('user_id', existing_type=sa.Integer(), nullable=True)
        batch_op.create_foreign_key('fk_api_keys_team_id_teams', 'teams', ['team_id'], ['id'])
        batch_op.create_index('ix_api_keys_team_id', ['team_id'], unique=False)

    if op.get_bind().dialect.name == 'sqlite':
        op.execute('ALTER TABLE deployments ADD COLUMN team_id INTEGER REFERENCES teams (id)')
    else:
        op.add_column('deployments', sa.Column('team_id', sa.Integer(), nullable=True))
        op.create_foreign_key('fk_deployments_team_id_teams', 'deployments', 'teams', ['team_id'], ['id'])
    op.create_index('ix_deployments_team_id', 'deployments', ['team_id'], unique=False)

    # Plain ALTER TABLE … ADD COLUMN with a default: no rebuild of the largest table.
    op.add_column('usage_daily', sa.Column('team_id', sa.Integer(), server_default='0', nullable=False))
    op.create_index('ix_usage_daily_team_day', 'usage_daily', ['team_id', 'day'], unique=False)


def downgrade() -> None:
    op.drop_index('ix_usage_daily_team_day', table_name='usage_daily')
    op.drop_column('usage_daily', 'team_id')

    op.drop_index('ix_deployments_team_id', table_name='deployments')
    if op.get_bind().dialect.name != 'sqlite':
        op.drop_constraint('fk_deployments_team_id_teams', 'deployments', type_='foreignkey')
    # SQLite (3.35+) drops a column together with its own REFERENCES clause, without a rebuild.
    op.drop_column('deployments', 'team_id')

    op.execute("DELETE FROM api_keys WHERE user_id IS NULL")
    with op.batch_alter_table('api_keys', schema=None) as batch_op:
        batch_op.drop_index('ix_api_keys_team_id')
        batch_op.drop_constraint('fk_api_keys_team_id_teams', type_='foreignkey')
        batch_op.alter_column('user_id', existing_type=sa.Integer(), nullable=False)
        batch_op.drop_column('created_by')
        batch_op.drop_column('team_id')

    op.drop_index('ix_team_members_user_id', table_name='team_members')
    op.drop_index('ix_team_members_team_id', table_name='team_members')
    op.drop_table('team_members')
    op.drop_index('ix_teams_name', table_name='teams')
    op.drop_table('teams')
