"""Two-factor sign-in (1.6.0, issue #39, PR 1): authenticator apps, recovery codes and half-done sign-ins.

- mfa_factors: an account's second factors. For now only authenticator apps (kind `totp`): a name, the shared secret
  (encrypted with the server's secret key, bound to the column `mfa_factors.secret`), the last time step a code was
  accepted for (each step counts once), when it was added and last used.
- recovery_codes: single-use codes that stand in for the second factor; only their SHA-256 is stored.
- login_challenges: a sign-in whose password was right and whose code is still to come. The browser holds a random
  token; only its SHA-256 is stored, with an expiry (5 minutes) and the number of wrong codes so far.
- sessions.mfa_at: when the session proved a second factor (NULL: password alone).

All three tables belong to a user and go with them (ON DELETE CASCADE). No existing row changes: every account
starts without a factor and signs in exactly as before. Going down drops the tables and the column, so accounts sign
in with their password alone again.

Revision ID: 0020_two_factor
Revises: 0019_remove_apps
Create Date: 2026-11-25 12:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


revision = '0020_two_factor'
down_revision = '0019_remove_apps'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'mfa_factors',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('secret', sa.Text(), nullable=False),
        sa.Column('last_step', sa.BigInteger(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_used_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('mfa_factors', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_mfa_factors_user_id'), ['user_id'], unique=False)

    op.create_table(
        'recovery_codes',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('hash', sa.String(length=64), nullable=False),
        sa.Column('used_at', sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('recovery_codes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_recovery_codes_user_id'), ['user_id'], unique=False)

    op.create_table(
        'login_challenges',
        sa.Column('id', sa.String(length=64), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('login_challenges', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_login_challenges_user_id'), ['user_id'], unique=False)

    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('mfa_at', sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('sessions', schema=None) as batch_op:
        batch_op.drop_column('mfa_at')
    with op.batch_alter_table('login_challenges', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_login_challenges_user_id'))
    op.drop_table('login_challenges')
    with op.batch_alter_table('recovery_codes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_recovery_codes_user_id'))
    op.drop_table('recovery_codes')
    with op.batch_alter_table('mfa_factors', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_mfa_factors_user_id'))
    op.drop_table('mfa_factors')
