"""Single sign-on through an OpenID Connect provider (1.6.0, issue #39, PR 3).

- identity_providers: the provider people sign in through (the community edition keeps one row): its name, issuer,
  client id and client secret (encrypted with the server's secret key, bound to the column
  `identity_providers.client_secret`), scopes, the role claim and its mapping, what happens to someone new (`approve`
  or `automatic`), the allowed e-mail domains, whether it is on, who made it and when.
- user_identities: an account at that provider linked to a Forgeyard account, found by (issuer, subject), never by
  e-mail. Both foreign keys cascade: deleting the account or the provider removes the link.
- sessions.auth_method: `password` (every existing session) or `sso`; sessions.fresh_until: until when a session
  counts as freshly signed in through the provider (NULL for every existing session).

No existing row changes meaning: with no provider, sign-in, two-factor sign-in and API keys work exactly as before.
Going down drops the two tables and the two columns; accounts made through the provider stay, and sign in with a
password once an admin sends them a set-password link.

Revision ID: 0021_single_sign_on
Revises: 0020_two_factor
Create Date: 2026-11-26 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0021_single_sign_on'
down_revision = '0020_two_factor'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'identity_providers',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('issuer', sa.String(length=512), nullable=False),
        sa.Column('client_id', sa.String(length=255), nullable=False),
        sa.Column('client_secret', sa.Text(), nullable=True),
        sa.Column('scopes', sa.String(length=255), nullable=False),
        sa.Column('role_claim', sa.String(length=64), nullable=True),
        sa.Column('role_mapping', sa.JSON(), nullable=False),
        sa.Column('new_accounts', sa.String(length=16), nullable=False),
        sa.Column('allowed_domains', sa.JSON(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_table(
        'user_identities',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('provider_id', sa.Integer(), nullable=False),
        sa.Column('issuer', sa.String(length=512), nullable=False),
        sa.Column('subject', sa.String(length=255), nullable=False),
        sa.Column('email', sa.String(length=255), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['provider_id'], ['identity_providers.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('issuer', 'subject', name='uq_user_identities_issuer_subject'),
    )
    with op.batch_alter_table('user_identities', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_identities_provider_id'), ['provider_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_user_identities_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('auth_method', sa.String(length=16), server_default='password', nullable=False))
        batch_op.add_column(sa.Column('fresh_until', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.drop_column('fresh_until')
        batch_op.drop_column('auth_method')
    with op.batch_alter_table('user_identities', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_identities_user_id'))
        batch_op.drop_index(batch_op.f('ix_user_identities_provider_id'))
    op.drop_table('user_identities')
    op.drop_table('identity_providers')
