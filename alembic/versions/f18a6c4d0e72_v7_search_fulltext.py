"""Add MariaDB FULLTEXT indexes for public recipe search.

Revision ID: f18a6c4d0e72
Revises: e4c7a91d2f30
Create Date: 2026-11-05 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "f18a6c4d0e72"
down_revision: Union[str, None] = "e4c7a91d2f30"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ft_recipes_title", "recipes", ["title"], mysql_prefix="FULLTEXT"
    )
    op.create_index(
        "ft_recipes_description", "recipes", ["description"], mysql_prefix="FULLTEXT"
    )
    op.create_index(
        "ft_recipe_items_raw_text", "recipe_items", ["raw_text"], mysql_prefix="FULLTEXT"
    )
    op.create_index(
        "ft_categories_name", "categories", ["name"], mysql_prefix="FULLTEXT"
    )
    op.create_index("ft_tags_name", "tags", ["name"], mysql_prefix="FULLTEXT")


def downgrade() -> None:
    op.drop_index("ft_tags_name", table_name="tags")
    op.drop_index("ft_categories_name", table_name="categories")
    op.drop_index("ft_recipe_items_raw_text", table_name="recipe_items")
    op.drop_index("ft_recipes_description", table_name="recipes")
    op.drop_index("ft_recipes_title", table_name="recipes")
