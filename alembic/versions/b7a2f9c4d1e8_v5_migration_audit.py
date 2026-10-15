"""v5: migration audit — migration_runs + migration_recipe_log (spec §3.6, D8)

Creates the D8 audit trail: ``migration_runs`` (one row per importer
invocation) and ``migration_recipe_log`` (per-source-post outcome of a run).
This locks the audit SHAPE only — no importer, no CLI, no seeds (spec §3.6,
D8; the §5.2 importer and §5.3 verification gate own the writes/reads later).

`migration_runs`:
  - PK `id` VARCHAR(36) (Python `_uuid` default; NOT a server default).
  - `started_at` DATETIME NOT NULL, server `CURRENT_TIMESTAMP`.
  - `status` ENUM('running','completed','failed') NOT NULL — Python default
    'running' only; NO server status default (the importer always supplies the
    status it wants).
  - six INT counters (recipes_total/imported/skipped/errors, images_ok/failed)
    NOT NULL DEFAULT 0 (server default 0 — proven independently of the Python
    default by the audit tests).
  - `file_sha256` CHAR(64) — CHAR is intentional (DL-5's fixed-length-hash
    exception). `operator_id` -> users.id ON DELETE SET NULL (attribution
    only, D10).
  - explicit non-unique index `ix_migration_runs_operator_id` on [operator_id].

`migration_recipe_log`:
  - PK `id` VARCHAR(36).
  - `run_id` NOT NULL -> migration_runs.id ON DELETE CASCADE.
  - `recipe_id` NULL -> recipes.id ON DELETE SET NULL (audit rows are never
    destroyed — only their recipe reference is cleared).
  - `outcome` ENUM('imported','skipped','error') NOT NULL.
  - explicit non-unique indexes `ix_migration_recipe_log_run_id` [run_id] and
    `ix_migration_recipe_log_recipe_id` [recipe_id].

No `migration_tag_log` (spec §3.6: not needed in v1). InnoDB / utf8mb4.
FK names (`recfk_run_operator` / `recfk_log_run` / `recfk_log_recipe`) are
spelled out so the live DDL and the model metadata agree.

Both tables: FK to `users.id` / `recipes.id` / `migration_runs.id` (which must
exist), so the parent (migration_runs) is created before the child
(migration_recipe_log).

Revision ID: b7a2f9c4d1e8
Revises: 3fcee3b79cca
Create Date: 2026-10-14 13:20:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7a2f9c4d1e8'
down_revision: Union[str, None] = '3fcee3b79cca'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Parent first: migration_recipe_log.run_id FK -> migration_runs.id.
    op.create_table(
        'migration_runs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('started_at', sa.DateTime(),
                  server_default=sa.text('now()'), nullable=False),
        sa.Column('finished_at', sa.DateTime(), nullable=True),
        sa.Column('source_file', sa.String(length=500), nullable=True),
        sa.Column('file_sha256', sa.CHAR(length=64), nullable=True),
        sa.Column('status',
                  sa.Enum('running', 'completed', 'failed',
                          name='migration_run_status'), nullable=False),
        sa.Column('recipes_total', sa.Integer(), server_default=sa.text('0'),
                  nullable=False),
        sa.Column('recipes_imported', sa.Integer(), server_default=sa.text('0'),
                  nullable=False),
        sa.Column('recipes_skipped', sa.Integer(), server_default=sa.text('0'),
                  nullable=False),
        sa.Column('recipes_errors', sa.Integer(), server_default=sa.text('0'),
                  nullable=False),
        sa.Column('images_ok', sa.Integer(), server_default=sa.text('0'),
                  nullable=False),
        sa.Column('images_failed', sa.Integer(), server_default=sa.text('0'),
                  nullable=False),
        sa.Column('operator_id', sa.String(length=36), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['operator_id'], ['users.id'],
                               name='recfk_run_operator', ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        mysql_engine='InnoDB', mysql_charset='utf8mb4'
    )
    op.create_index('ix_migration_runs_operator_id', 'migration_runs',
                    ['operator_id'], unique=False)

    op.create_table(
        'migration_recipe_log',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('run_id', sa.String(length=36), nullable=False),
        sa.Column('source_post_id', sa.String(length=64), nullable=True),
        sa.Column('recipe_id', sa.String(length=36), nullable=True),
        sa.Column('outcome',
                  sa.Enum('imported', 'skipped', 'error',
                          name='migration_log_outcome'), nullable=False),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['run_id'], ['migration_runs.id'],
                               name='recfk_log_run', ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id'],
                               name='recfk_log_recipe', ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id'),
        mysql_engine='InnoDB', mysql_charset='utf8mb4'
    )
    op.create_index('ix_migration_recipe_log_run_id', 'migration_recipe_log',
                    ['run_id'], unique=False)
    op.create_index('ix_migration_recipe_log_recipe_id', 'migration_recipe_log',
                    ['recipe_id'], unique=False)


def downgrade() -> None:
    # FK-safe order: drop child (migration_recipe_log) and its indexes BEFORE
    # the parent (migration_runs). NEVER executed in this task — provided for
    # completeness of the forward-only chain.
    op.drop_index('ix_migration_recipe_log_recipe_id',
                  table_name='migration_recipe_log')
    op.drop_index('ix_migration_recipe_log_run_id',
                  table_name='migration_recipe_log')
    op.drop_table('migration_recipe_log')
    op.drop_index('ix_migration_runs_operator_id', table_name='migration_runs')
    op.drop_table('migration_runs')
