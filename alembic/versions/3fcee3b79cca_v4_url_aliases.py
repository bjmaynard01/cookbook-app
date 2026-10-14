"""v4: recipe_url_aliases — legacy URL retention with exactly-one target

Creates `recipe_url_aliases`: a full legacy path (e.g. `/?p=1234`,
`/?page_id=16`) mapped to exactly one existing recipe OR category
(spec section 3.5, D6/D17). Both target FKs use ON DELETE RESTRICT
(DL-6, owner-approved) so an
alias always retains a valid target — a referenced recipe/category cannot be
deleted until its aliases are deliberately reassigned. The named
`chk_alias_exactly_one` CHECK rejects rows with both targets set and rows with
neither. `path` is the observed string, never normalized. No seed data, no
redirects here (the app-side 301 middleware is the read plan).

Both FK names (`recfk_aliases_recipe` / `recfk_aliases_category`) and both
explicit non-unique indexes (`ix_recipe_url_aliases_recipe_id` /
`ix_recipe_url_aliases_category_id`) are spelled out so the live DDL and the
model metadata agree. InnoDB / utf8mb4.

Revision ID: 3fcee3b79cca
Revises: a2b53a9f0000
Create Date: 2026-10-14 11:55:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3fcee3b79cca'
down_revision: Union[str, None] = 'a2b53a9f0000'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'recipe_url_aliases',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('path', sa.String(length=255), nullable=False),
        sa.Column('recipe_id', sa.String(length=36), nullable=True),
        sa.Column('category_id', sa.String(length=36), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
        sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id'],
                                name='recfk_aliases_recipe', ondelete='RESTRICT'),
        sa.ForeignKeyConstraint(['category_id'], ['categories.id'],
                                name='recfk_aliases_category', ondelete='RESTRICT'),
        sa.CheckConstraint(
            '(recipe_id IS NOT NULL) <> (category_id IS NOT NULL)',
            name='chk_alias_exactly_one'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('path', name='uq_aliases_path'),
        mysql_engine='InnoDB', mysql_charset='utf8mb4'
    )
    op.create_index('ix_recipe_url_aliases_recipe_id', 'recipe_url_aliases',
                    ['recipe_id'], unique=False)
    op.create_index('ix_recipe_url_aliases_category_id', 'recipe_url_aliases',
                    ['category_id'], unique=False)


def downgrade():
    # FK-safe order: drop the secondary indexes first, then the table (which
    # holds the alias rows). Never executed in this task — provided for
    # completeness of the forward-only chain.
    op.drop_index('ix_recipe_url_aliases_category_id', table_name='recipe_url_aliases')
    op.drop_index('ix_recipe_url_aliases_recipe_id', table_name='recipe_url_aliases')
    op.drop_table('recipe_url_aliases')
