"""v6: authentication schema — sessions + login_attempts + auth_locks (S1)

Creates the three auth-plan tables (C0.1 / C1.2 / C0.2 complete contracts)
and seeds the two fixed auth_locks rows. SCHEMA ONLY: no service, policy,
session, or lockout behavior; the sliding-TTL semantics are documented on
the models (S4/S6 contract), not implemented here.

`sessions` (C0.1):
  - PK `id_hash` CHAR(64) NOT NULL — sha256 hex of the raw token; the raw
    token is never stored (no token column in this table).
  - `user_id` VARCHAR(36) NOT NULL, FK `fk_sessions_user` -> users.id
    ON DELETE CASCADE.
  - `csrf_token` CHAR(32) NOT NULL (per-session CSRF token).
  - `created_ip` VARCHAR(45) NULL (client IP at issue, C4 identity).
  - `created_at` / `last_seen_at` / `expires_at` DATETIME(6) NOT NULL —
    app-supplied, no server defaults.
  - non-unique index `ix_sessions_user_id` [user_id].

`login_attempts` (C1.2):
  - PK `id` VARCHAR(36) (Python _uuid default; no server default).
  - `username` VARCHAR(60) / `ip` VARCHAR(45) NOT NULL (canonicalization is
    a later S6 service concern, not schema).
  - `attempts` / `lock_count` INT NOT NULL DEFAULT 0 (server defaults).
  - `first_failed_at` / `last_failed_at` DATETIME NOT NULL (app-supplied).
  - `locked_until` DATETIME NULL (NULL = unlocked).
  - UNIQUE `uq_login_attempts_pair` (username, ip); non-unique
    `ix_login_attempts_locked` [locked_until].

`auth_locks` (C0.2):
  - PK `name` VARCHAR(40).
  - seeded rows: ('first_admin'), ('last_admin') — seeded HERE, fixed set;
    no runtime re-seeding path.

InnoDB / utf8mb4 on all three tables. No changes to `users` or any earlier
schema object. FK-safe order: all three parents (users, recipes, …) already
exist, so table-creation order is independent; downgrade drops child
tables before parents is N/A here (no v6-internal FKs), but the FK on
sessions.user_id is dropped with its table. NEVER executed in this task.

Revision ID: e4c7a91d2f30
Revises: b7a2f9c4d1e8
Create Date: 2026-10-16 13:05:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.mysql import DATETIME as MySQLDateTime


revision: str = 'e4c7a91d2f30'
down_revision: Union[str, None] = 'b7a2f9c4d1e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# C5 lock rows (fixed set, seeded exactly once by this revision):
#   first_admin — serializes zero-or-one-admin bootstrap writes (Invariant A)
#   last_admin  — serializes "at least one active admin survives" writes (Invariant B)
_AUTH_LOCK_SEED = ["first_admin", "last_admin"]


def upgrade() -> None:
    op.create_table(
        'sessions',
        sa.Column('id_hash', sa.CHAR(length=64), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('csrf_token', sa.CHAR(length=32), nullable=False),
        sa.Column('created_ip', sa.String(length=45), nullable=True),
        # fsp=6 -> DATETIME(6) in the live DDL (generic sa.DateTime() would
        # render a bare DATETIME — C0.1 requires the 6-digit precision).
        sa.Column('created_at', MySQLDateTime(fsp=6), nullable=False),
        sa.Column('last_seen_at', MySQLDateTime(fsp=6), nullable=False),
        sa.Column('expires_at', MySQLDateTime(fsp=6), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'],
                               name='fk_sessions_user', ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id_hash'),
        mysql_engine='InnoDB', mysql_charset='utf8mb4'
    )
    op.create_index('ix_sessions_user_id', 'sessions', ['user_id'], unique=False)

    op.create_table(
        'login_attempts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('username', sa.String(length=60), nullable=False),
        sa.Column('ip', sa.String(length=45), nullable=False),
        sa.Column('attempts', sa.Integer(), server_default=sa.text('0'),
                  nullable=False),
        sa.Column('first_failed_at', sa.DateTime(), nullable=False),
        sa.Column('last_failed_at', sa.DateTime(), nullable=False),
        sa.Column('lock_count', sa.Integer(), server_default=sa.text('0'),
                  nullable=False),
        sa.Column('locked_until', sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('username', 'ip', name='uq_login_attempts_pair'),
        mysql_engine='InnoDB', mysql_charset='utf8mb4'
    )
    op.create_index('ix_login_attempts_locked', 'login_attempts',
                    ['locked_until'], unique=False)

    op.create_table(
        'auth_locks',
        sa.Column('name', sa.String(length=40), nullable=False),
        sa.PrimaryKeyConstraint('name'),
        mysql_engine='InnoDB', mysql_charset='utf8mb4'
    )
    op.bulk_insert(
        sa.table('auth_locks', sa.column('name')),
        [{"name": n} for n in _AUTH_LOCK_SEED],
    )


def downgrade() -> None:
    # FK-safe drop order: drop each table's explicit indexes first, then the
    # tables themselves (sessions.user_id FK to users.id is dropped with the
    # table; no v6-internal FKs exist so there is no child-before-parent
    # ordering within v6). Provide it for completeness of the forward-only
    # chain — NEVER executed in this task.
    op.drop_index('ix_login_attempts_locked', table_name='login_attempts')
    op.drop_table('login_attempts')
    op.drop_index('ix_sessions_user_id', table_name='sessions')
    op.drop_table('sessions')
    op.drop_table('auth_locks')
