"""v2: ordered recipe body — recipe_sections + recipe_items (DL-3, D4)

`recipe_sections` holds ordered named body blocks; `recipe_items` holds every
ordered line with `raw_text` as the source of truth (D4, extended by DL-3 to
every section kind). `kind` is a native MariaDB ENUM. Both tables are InnoDB /
utf8mb4 (the MariaDB server defaults the existing v0/v1 migrations rely on).
No timestamps: body rows are wholesale-replaced on save (later plan);
provenance lives on `recipes`.

Revision ID: 52473474a186
Revises: a901619780e2
Create Date: 2026-10-13 10:39:08.541425
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '52473474a186'
down_revision: Union[str, None] = 'a901619780e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('recipe_sections',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('recipe_id', sa.String(length=36), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('heading', sa.String(length=200), nullable=True),
    sa.Column('kind', sa.Enum('ingredients', 'steps', 'note', name='recipe_section_kind'), nullable=False),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id'], name='recfk_sections_recipe', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('recipe_id', 'position', name='uq_sections_recipe_position')
    )
    op.create_index(op.f('ix_recipe_sections_recipe_id'), 'recipe_sections', ['recipe_id'], unique=False)

    op.create_table('recipe_items',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('section_id', sa.String(length=36), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('raw_text', sa.Text(), nullable=False),
    sa.Column('quantity_text', sa.String(length=50), nullable=True),
    sa.Column('unit', sa.String(length=50), nullable=True),
    sa.Column('item', sa.String(length=300), nullable=True),
    sa.Column('preparation', sa.String(length=300), nullable=True),
    sa.ForeignKeyConstraint(['section_id'], ['recipe_sections.id'], name='recfk_items_section', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('section_id', 'position', name='uq_items_section_position')
    )
    op.create_index(op.f('ix_recipe_items_section_id'), 'recipe_items', ['section_id'], unique=False)


def downgrade() -> None:
    # FK-safe order: drop children before their parents.
    op.drop_index(op.f('ix_recipe_items_section_id'), table_name='recipe_items')
    op.drop_table('recipe_items')
    op.drop_index(op.f('ix_recipe_sections_recipe_id'), table_name='recipe_sections')
    op.drop_table('recipe_sections')
