"""v3: organization — categories/tags + join tables + DL-4 seed

Creates the four Task 6 organization tables and seeds the eight DL-4
category rows (the seed data lives in this migration, not in runtime
constants; the importer later owns the separate "Others" category).

Deletion rules (owner-approved):
- recipe_categories.recipe_id -> recipes.id    ON DELETE CASCADE
- recipe_categories.category_id -> categories.id ON DELETE RESTRICT (DL-6:
  an in-use category must not be deleted while memberships point at it).
- recipe_tags.recipe_id -> recipes.id          ON DELETE CASCADE
- recipe_tags.tag_id -> tags.id                ON DELETE CASCADE (DL-7:
  deleting an in-use tag is allowed and removes its memberships).

All four tables are InnoDB / utf8mb4.

Revision ID: a2b53a9f0000
Revises: 52473474a186
Create Date: 2026-10-13 12:25:11.541352
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a2b53a9f0000'
down_revision: Union[str, None] = '52473474a186'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# DL-4 locked seed set (spec §3.4 EVIDENCE). Order = lane order (1..8).
# "Others" is deliberately NOT seeded here — the §5.2 import creates it when
# it assigns the unmenued posts; the DB must not pre-create a phantom row.
_CATEGORY_SEED = [
    ("412b94fd-8106-40b8-863b-46e2b1f1daff", "breakfast", "Breakfast", 1),
    ("26857e44-4786-4068-8faf-607d4a5a83a1", "fermentation", "Fermentation", 2),
    ("8fb7691b-6dd7-4cbc-a337-6a62d2a53b7f", "appetizers", "Appetizers", 3),
    ("ac5980f3-4d82-4f63-9ac7-73602ddbb682", "drinks", "Drinks", 4),
    ("3889e580-bb77-4a11-a87f-97c501f5ea87", "main-dishes", "Main Dishes", 5),
    ("d51b5466-d402-4157-a09f-6e63588913bc", "side-dishes", "Side Dishes", 6),
    ("58777180-feb6-42a5-a4b1-e6fb24e45bea", "baking", "Baking", 7),
    ("bf86f19e-3ce9-4159-a558-bba87672e822", "desserts", "Desserts", 8),
]


def upgrade() -> None:
    import sqlalchemy as sa

    op.create_table('categories',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('slug', sa.String(length=200), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug', name='uq_categories_slug'),
    sa.UniqueConstraint('position', name='uq_categories_position'),
    mysql_engine='InnoDB', mysql_charset='utf8mb4'
    )

    op.create_table('tags',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('slug', sa.String(length=200), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug', name='uq_tags_slug'),
    mysql_engine='InnoDB', mysql_charset='utf8mb4'
    )

    op.create_table('recipe_categories',
    sa.Column('recipe_id', sa.String(length=36), nullable=False),
    sa.Column('category_id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['category_id'], ['categories.id'],
        name='recfk_recipe_categories_category', ondelete='RESTRICT'),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id'],
        name='recfk_recipe_categories_recipe', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('recipe_id', 'category_id'),
    mysql_engine='InnoDB', mysql_charset='utf8mb4'
    )
    op.create_index('ix_recipe_categories_category_id',
        'recipe_categories', ['category_id'], unique=False)

    op.create_table('recipe_tags',
    sa.Column('recipe_id', sa.String(length=36), nullable=False),
    sa.Column('tag_id', sa.String(length=36), nullable=False),
    sa.ForeignKeyConstraint(['recipe_id'], ['recipes.id'],
        name='recfk_recipe_tags_recipe', ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['tag_id'], ['tags.id'],
        name='recfk_recipe_tags_tag', ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('recipe_id', 'tag_id'),
    mysql_engine='InnoDB', mysql_charset='utf8mb4'
    )
    op.create_index('ix_recipe_tags_tag_id', 'recipe_tags', ['tag_id'], unique=False)

    # DL-4 seed: eight categories, locked UUIDs, dense positions 1..8.
    bind = op.get_bind()
    for _id, slug, name, position in _CATEGORY_SEED:
        bind.execute(
            sa.text(
                "INSERT INTO categories (id, slug, name, position) "
                "VALUES (:id, :slug, :name, :position)"
            ),
            {"id": _id, "slug": slug, "name": name, "position": position},
        )


def downgrade() -> None:
    # FK-safe drop order:
    #   recipe_tags  ->  recipe_categories  ->  tags  ->  categories
    # (the join tables, their secondary indexes, then the parents, in that
    # order). The seed-deletion query is unnecessary: dropping `categories`
    # deletes its seed rows with the table. Never run in this task — provided
    # for completeness of the forward-only chain.
    op.drop_index('ix_recipe_tags_tag_id', table_name='recipe_tags')
    op.drop_table('recipe_tags')
    op.drop_index('ix_recipe_categories_category_id', table_name='recipe_categories')
    op.drop_table('recipe_categories')
    op.drop_table('tags')
    op.drop_table('categories')
