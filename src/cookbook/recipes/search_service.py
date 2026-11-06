"""MariaDB-backed public recipe search and filter data."""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.dialects.mysql import match
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import (
    Category,
    Recipe,
    RecipeItem,
    RecipeSection,
    Tag,
    recipe_categories,
    recipe_tags,
)
from .browse_service import (
    CategorySummary,
    RecipeCardData,
    TagSummary,
    build_recipe_cards,
)

SEARCH_MAX_LENGTH = 200
_TOKEN_PATTERN = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class SearchData:
    query: str
    tokens: tuple[str, ...]
    recipes: tuple[RecipeCardData, ...]
    categories: tuple[CategorySummary, ...]
    tags: tuple[TagSummary, ...]
    selected_category: Category | None
    selected_tag: Tag | None
    max_minutes: int | None
    suggested_categories: tuple[CategorySummary, ...]
    suggested_tags: tuple[TagSummary, ...]


def _tokens(query: str) -> tuple[str, ...]:
    seen: set[str] = set()
    result: list[str] = []
    for raw in _TOKEN_PATTERN.findall(query.casefold()):
        # One-character words tend to match nearly every recipe. Numeric
        # quantities remain useful (for example, searching for "2").
        if len(raw) < 2 and not raw.isdigit():
            continue
        if raw not in seen:
            seen.add(raw)
            result.append(raw)
    return tuple(result)


def _contains(column, tokens: tuple[str, ...]):
    return or_(*(column.like(f"%{token}%") for token in tokens))


def _related_score(model, association, association_column, query: str):
    return (
        select(func.max(match(model.name, against=query).in_natural_language_mode()))
        .select_from(association.join(model, model.id == association_column))
        .where(association.c.recipe_id == Recipe.id)
        .correlate(Recipe)
        .scalar_subquery()
    )


def _body_score(query: str, kind: str):
    return (
        select(func.max(match(RecipeItem.raw_text, against=query).in_natural_language_mode()))
        .select_from(RecipeItem)
        .join(RecipeSection, RecipeSection.id == RecipeItem.section_id)
        .where(RecipeSection.recipe_id == Recipe.id, RecipeSection.kind == kind)
        .correlate(Recipe)
        .scalar_subquery()
    )


def _body_contains(tokens: tuple[str, ...], kind: str):
    return exists(
        select(1)
        .select_from(RecipeItem)
        .join(RecipeSection, RecipeSection.id == RecipeItem.section_id)
        .where(
            RecipeSection.recipe_id == Recipe.id,
            RecipeSection.kind == kind,
            _contains(RecipeItem.raw_text, tokens),
        )
    )


async def _filter_options(
    db: AsyncSession,
) -> tuple[tuple[CategorySummary, ...], tuple[TagSummary, ...]]:
    category_rows = await db.execute(
        select(Category, func.count(Recipe.id))
        .select_from(Category)
        .outerjoin(recipe_categories, recipe_categories.c.category_id == Category.id)
        .outerjoin(Recipe, Recipe.id == recipe_categories.c.recipe_id)
        .group_by(Category.id, Category.slug, Category.name, Category.position)
        .order_by(Category.position, Category.name)
    )
    categories = tuple(
        CategorySummary(category=category, recipe_count=int(count))
        for category, count in category_rows
    )
    tag_rows = await db.execute(
        select(Tag, func.count(recipe_tags.c.recipe_id))
        .select_from(Tag)
        .join(recipe_tags, recipe_tags.c.tag_id == Tag.id)
        .group_by(Tag.id, Tag.slug, Tag.name)
        .order_by(func.count(recipe_tags.c.recipe_id).desc(), Tag.name.asc())
    )
    tags = tuple(TagSummary(tag=tag, recipe_count=int(count)) for tag, count in tag_rows)
    return categories, tags


async def search_recipes(
    db: AsyncSession,
    *,
    query: str,
    category_slug: str | None = None,
    tag_slug: str | None = None,
    max_minutes: int | None = None,
) -> SearchData:
    """Return ranked recipes and the values needed to render all search controls."""
    query = query.strip()[:SEARCH_MAX_LENGTH]
    tokens = _tokens(query)
    categories, tags = await _filter_options(db)
    selected_category = None
    selected_tag = None
    if category_slug:
        selected_category = (
            await db.execute(select(Category).where(Category.slug == category_slug).limit(1))
        ).scalar_one_or_none()
    if tag_slug:
        selected_tag = (
            await db.execute(select(Tag).where(Tag.slug == tag_slug).limit(1))
        ).scalar_one_or_none()

    recipes: tuple[RecipeCardData, ...] = ()
    if tokens:
        title_fulltext = match(Recipe.title, against=query).in_natural_language_mode()
        description_fulltext = match(Recipe.description, against=query).in_natural_language_mode()
        category_fulltext = _related_score(
            Category, recipe_categories, recipe_categories.c.category_id, query
        )
        tag_fulltext = _related_score(Tag, recipe_tags, recipe_tags.c.tag_id, query)
        ingredient_fulltext = _body_score(query, "ingredients")
        steps_fulltext = _body_score(query, "steps")

        title_partial = _contains(Recipe.title, tokens)
        category_partial = exists(
            select(1).select_from(recipe_categories)
            .join(Category, Category.id == recipe_categories.c.category_id)
            .where(recipe_categories.c.recipe_id == Recipe.id, _contains(Category.name, tokens))
        )
        tag_partial = exists(
            select(1).select_from(recipe_tags)
            .join(Tag, Tag.id == recipe_tags.c.tag_id)
            .where(recipe_tags.c.recipe_id == Recipe.id, _contains(Tag.name, tokens))
        )
        ingredient_partial = _body_contains(tokens, "ingredients")
        steps_partial = _body_contains(tokens, "steps")
        description_partial = _contains(Recipe.description, tokens)

        title_hit = or_(title_partial, title_fulltext > 0)
        secondary_hit = or_(
            category_partial, tag_partial, ingredient_partial,
            category_fulltext > 0, tag_fulltext > 0, ingredient_fulltext > 0,
        )
        tertiary_hit = or_(
            description_partial, steps_partial,
            description_fulltext > 0, steps_fulltext > 0,
        )
        tier = case(
            (title_hit, 1000),
            (secondary_hit, 100),
            (tertiary_hit, 10),
            else_=0,
        )
        relevance = (
            func.coalesce(title_fulltext, 0) * 10
            + func.coalesce(category_fulltext, 0)
            + func.coalesce(tag_fulltext, 0)
            + func.coalesce(ingredient_fulltext, 0)
            + func.coalesce(description_fulltext, 0) * 0.1
            + func.coalesce(steps_fulltext, 0) * 0.1
        )
        recipe_query = select(Recipe).where(or_(title_hit, secondary_hit, tertiary_hit))
        if selected_category is not None:
            recipe_query = recipe_query.where(
                Recipe.id.in_(
                    select(recipe_categories.c.recipe_id).where(
                        recipe_categories.c.category_id == selected_category.id
                    )
                )
            )
        elif category_slug:
            recipe_query = recipe_query.where(False)
        if selected_tag is not None:
            recipe_query = recipe_query.where(
                Recipe.id.in_(
                    select(recipe_tags.c.recipe_id).where(recipe_tags.c.tag_id == selected_tag.id)
                )
            )
        elif tag_slug:
            recipe_query = recipe_query.where(False)
        if max_minutes is not None:
            recipe_query = recipe_query.where(
                Recipe.total_minutes.is_not(None), Recipe.total_minutes <= max_minutes
            )
        result_rows = await db.execute(
            recipe_query.order_by(
                tier.desc(),
                relevance.desc(),
                Recipe.updated_at.desc(),
                Recipe.title.asc(),
                Recipe.id.asc(),
            )
        )
        recipes = await build_recipe_cards(db, list(result_rows.scalars().all()))
    category_matches = tuple(
        item for item in categories
        if (selected_category is None or item.category.id != selected_category.id)
        and any(token in item.category.name.casefold() for token in tokens)
    )
    tag_matches = tuple(
        item for item in tags
        if (selected_tag is None or item.tag.id != selected_tag.id)
        and any(token in item.tag.name.casefold() for token in tokens)
    )
    suggested_categories = category_matches[:3] or categories[:3]
    suggested_tags = tag_matches[:5] or tags[:5]
    return SearchData(
        query=query,
        tokens=tokens,
        recipes=recipes,
        categories=categories,
        tags=tags,
        selected_category=selected_category,
        selected_tag=selected_tag,
        max_minutes=max_minutes,
        suggested_categories=suggested_categories,
        suggested_tags=suggested_tags,
    )
