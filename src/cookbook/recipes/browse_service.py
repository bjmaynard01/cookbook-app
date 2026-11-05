"""Read-only query helpers for the public home, category, and tag pages."""
from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import case, false, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Category, Recipe, Tag, recipe_categories, recipe_tags

FEATURED_RECIPE_LIMIT = 6
POPULAR_TAG_LIMIT = 12


@dataclass(frozen=True, slots=True)
class RecipeCardData:
    recipe: Recipe
    categories: tuple[Category, ...]
    tags: tuple[Tag, ...]


@dataclass(frozen=True, slots=True)
class CategorySummary:
    category: Category
    recipe_count: int
    thumbnail_path: str | None = None


@dataclass(frozen=True, slots=True)
class TagSummary:
    tag: Tag
    recipe_count: int


@dataclass(frozen=True, slots=True)
class HomeBrowseData:
    featured: tuple[RecipeCardData, ...]
    categories: tuple[CategorySummary, ...]
    popular_tags: tuple[TagSummary, ...]


@dataclass(frozen=True, slots=True)
class CategoryBrowseData:
    category: Category
    recipes: tuple[RecipeCardData, ...]
    tags: tuple[TagSummary, ...]
    selected_tag: Tag | None
    max_minutes: int | None


@dataclass(frozen=True, slots=True)
class TagBrowseData:
    tag: Tag
    recipes: tuple[RecipeCardData, ...]


async def _recipe_cards(
    db: AsyncSession, recipes: list[Recipe]
) -> tuple[RecipeCardData, ...]:
    if not recipes:
        return ()

    recipe_ids = [recipe.id for recipe in recipes]
    categories_by_recipe: dict[str, list[Category]] = {recipe_id: [] for recipe_id in recipe_ids}
    category_rows = await db.execute(
        select(recipe_categories.c.recipe_id, Category)
        .join(Category, Category.id == recipe_categories.c.category_id)
        .where(recipe_categories.c.recipe_id.in_(recipe_ids))
        .order_by(Category.position)
    )
    for recipe_id, category in category_rows:
        categories_by_recipe[recipe_id].append(category)

    tags_by_recipe: dict[str, list[Tag]] = {recipe_id: [] for recipe_id in recipe_ids}
    tag_rows = await db.execute(
        select(recipe_tags.c.recipe_id, Tag)
        .join(Tag, Tag.id == recipe_tags.c.tag_id)
        .where(recipe_tags.c.recipe_id.in_(recipe_ids))
        .order_by(Tag.name)
    )
    for recipe_id, tag in tag_rows:
        tags_by_recipe[recipe_id].append(tag)

    return tuple(
        RecipeCardData(
            recipe=recipe,
            categories=tuple(categories_by_recipe[recipe.id]),
            tags=tuple(tags_by_recipe[recipe.id]),
        )
        for recipe in recipes
    )


async def get_home_browse_data(db: AsyncSession) -> HomeBrowseData:
    """Read featured cards, categories in owner order, and most-used tags."""
    featured_rows = (
        await db.execute(
            select(Recipe)
            .where(Recipe.home_featured.is_(True))
            .order_by(
                case((Recipe.home_position.is_(None), 1), else_=0),
                Recipe.home_position.asc(),
                Recipe.title.asc(),
                Recipe.id.asc(),
            )
            .limit(FEATURED_RECIPE_LIMIT)
        )
    ).scalars().all()

    category_rows = await db.execute(
        select(Category, func.count(Recipe.id))
        .select_from(Category)
        .outerjoin(recipe_categories, recipe_categories.c.category_id == Category.id)
        .outerjoin(Recipe, Recipe.id == recipe_categories.c.recipe_id)
        .group_by(Category.id, Category.slug, Category.name, Category.position)
        .order_by(Category.position)
    )
    categories = [
        CategorySummary(category=category, recipe_count=int(count))
        for category, count in category_rows
    ]

    tag_rows = await db.execute(
        select(Tag, func.count(recipe_tags.c.recipe_id))
        .select_from(Tag)
        .join(recipe_tags, recipe_tags.c.tag_id == Tag.id)
        .group_by(Tag.id, Tag.slug, Tag.name)
        .order_by(func.count(recipe_tags.c.recipe_id).desc(), Tag.name.asc())
        .limit(POPULAR_TAG_LIMIT)
    )
    popular_tags = tuple(
        TagSummary(tag=tag, recipe_count=int(count)) for tag, count in tag_rows
    )

    thumbnail_rows = await db.execute(
        select(recipe_categories.c.category_id, Recipe.image_path)
        .join(Recipe, Recipe.id == recipe_categories.c.recipe_id)
        .where(Recipe.image_path.is_not(None))
        .order_by(Recipe.updated_at.desc(), Recipe.id.asc())
    )
    thumbnails: dict[str, str] = {}
    for category_id, image_path in thumbnail_rows:
        thumbnails.setdefault(category_id, image_path)
    categories = [
        CategorySummary(
            category=row.category,
            recipe_count=row.recipe_count,
            thumbnail_path=thumbnails.get(row.category.id),
        )
        for row in categories
    ]

    return HomeBrowseData(
        featured=await _recipe_cards(db, list(featured_rows)),
        categories=tuple(categories),
        popular_tags=popular_tags,
    )


async def get_category_browse_data(
    db: AsyncSession,
    slug: str,
    *,
    max_minutes: int | None = None,
    tag_slug: str | None = None,
) -> CategoryBrowseData | None:
    """Read one category and its filtered recipes; unknown category → ``None``."""
    category = (
        await db.execute(select(Category).where(Category.slug == slug).limit(1))
    ).scalar_one_or_none()
    if category is None:
        return None

    selected_tag = None
    if tag_slug:
        selected_tag = (
            await db.execute(select(Tag).where(Tag.slug == tag_slug).limit(1))
        ).scalar_one_or_none()

    recipe_query = (
        select(Recipe)
        .join(recipe_categories, recipe_categories.c.recipe_id == Recipe.id)
        .where(recipe_categories.c.category_id == category.id)
    )
    if max_minutes is not None:
        recipe_query = recipe_query.where(
            Recipe.total_minutes.is_not(None), Recipe.total_minutes <= max_minutes
        )
    if selected_tag is not None:
        recipe_query = recipe_query.where(
            Recipe.id.in_(
                select(recipe_tags.c.recipe_id).where(
                    recipe_tags.c.tag_id == selected_tag.id
                )
            )
        )
    elif tag_slug:
        recipe_query = recipe_query.where(false())
    recipes = list(
        (
            await db.execute(
                recipe_query.order_by(
                    Recipe.updated_at.desc(), Recipe.title.asc(), Recipe.id.asc()
                )
            )
        )
        .scalars()
        .all()
    )

    category_tags = await db.execute(
        select(Tag, func.count(recipe_tags.c.recipe_id))
        .select_from(Tag)
        .join(recipe_tags, recipe_tags.c.tag_id == Tag.id)
        .join(Recipe, Recipe.id == recipe_tags.c.recipe_id)
        .join(recipe_categories, recipe_categories.c.recipe_id == Recipe.id)
        .where(recipe_categories.c.category_id == category.id)
        .group_by(Tag.id, Tag.slug, Tag.name)
        .order_by(Tag.name.asc())
    )
    tags = tuple(
        TagSummary(tag=tag, recipe_count=int(count)) for tag, count in category_tags
    )

    return CategoryBrowseData(
        category=category,
        recipes=await _recipe_cards(db, recipes),
        tags=tags,
        selected_tag=selected_tag,
        max_minutes=max_minutes,
    )


async def get_tag_browse_data(db: AsyncSession, slug: str) -> TagBrowseData | None:
    """Read one semantic tag and its recipes; unknown tag → ``None``."""
    tag = (
        await db.execute(select(Tag).where(Tag.slug == slug).limit(1))
    ).scalar_one_or_none()
    if tag is None:
        return None
    recipes = list(
        (
            await db.execute(
                select(Recipe)
                .join(recipe_tags, recipe_tags.c.recipe_id == Recipe.id)
                .where(recipe_tags.c.tag_id == tag.id)
                .order_by(Recipe.updated_at.desc(), Recipe.title.asc(), Recipe.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return TagBrowseData(tag=tag, recipes=await _recipe_cards(db, recipes))
