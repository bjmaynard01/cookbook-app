"""v1: recipes table

Revision ID: a901619780e2
Revises: 6d22befb6ec9
Create Date: 2026-10-10 16:36:22.869299
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'a901619780e2'
down_revision: Union[str, None] = '6d22befb6ec9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('recipes',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('slug', sa.String(length=200), nullable=False),
    sa.Column('title', sa.String(length=300), nullable=False),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('servings_text', sa.String(length=100), nullable=True),
    sa.Column('servings_number', sa.DECIMAL(precision=10, scale=2), nullable=True),
    sa.Column('prep_minutes', sa.Integer(), nullable=True),
    sa.Column('cook_minutes', sa.Integer(), nullable=True),
    sa.Column('total_minutes', sa.Integer(), nullable=True),
    sa.Column('home_featured', sa.Boolean(), server_default=sa.text('false'), nullable=False),
    sa.Column('home_position', sa.Integer(), nullable=True),
    sa.Column('source_system', sa.String(length=20), nullable=True),
    sa.Column('source_id', sa.String(length=64), nullable=True),
    sa.Column('source_name', sa.String(length=300), nullable=True),
    sa.Column('source_url', sa.String(length=2000), nullable=True),
    sa.Column('extraction_method', sa.String(length=20), nullable=True),
    sa.Column('imported_at', sa.DateTime(), nullable=True),
    sa.Column('published_date', sa.Date(), nullable=True),
    sa.Column('image_path', sa.String(length=500), nullable=True),
    sa.Column('created_by', sa.String(length=36), nullable=True),
    sa.Column('last_modified_by', sa.String(length=36), nullable=True),
    sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()'), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name='recfk_created_by', ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['last_modified_by'], ['users.id'], name='recfk_modified_by', ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug', name='uq_recipes_slug'),
    sa.UniqueConstraint('source_system', 'source_id', name='uq_recipes_source')
    )


def downgrade() -> None:
    op.drop_table('recipes')
