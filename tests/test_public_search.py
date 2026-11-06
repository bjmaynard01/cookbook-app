"""Public recipe search ranking, filtering, and empty-state behavior."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import httpx
from sqlalchemy import delete, select

from cookbook.app import create_app
from cookbook.config import get_settings
from cookbook.db import get_engine, session_scope
from cookbook.models import (
    Category,
    Recipe,
    Tag,
)
from cookbook.recipes import (
    RecipeDraft,
    RecipeIngredientDraft,
    RecipeSectionDraft,
    RecipeStepDraft,
    create_recipe,
    validate_for_save,
)


def _test_app(monkeypatch) -> object:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "task18-search-test-secret")
    monkeypatch.setenv("MEDIA_DIR", str(Path.cwd() / ".task18-test-media"))
    get_settings.cache_clear()
    return create_app()


async def _create_search_recipe(
    *,
    slug: str,
    title: str,
    category: Category,
    tag_name: str,
    description: str = "A short introduction for the cook.",
    ingredient: str = "2 tbsp olive oil",
    step: str = "Warm the oil, then season.",
    total_minutes: int | None = 20,
) -> str:
    draft = RecipeDraft(
        title=title,
        description=description,
        servings_text="Serves four",
        prep_minutes=5,
        cook_minutes=15 if total_minutes is not None else None,
        total_minutes=total_minutes,
        tags=[tag_name],
        sections=[
            RecipeSectionDraft(
                heading="Ingredients",
                kind="ingredients",
                lines=[RecipeIngredientDraft(raw_text=ingredient)],
            ),
            RecipeSectionDraft(
                heading="Steps", kind="steps", lines=[RecipeStepDraft(body=step)]
            ),
        ],
    )
    async with session_scope() as db:
        save_data = validate_for_save(draft, category_names=[category.name])
        recipe = await create_recipe(
            db, save_data, category_ids=[category.id], slug=slug
        )
        return recipe.id


def test_search_ranks_title_then_semantic_content_then_description_and_steps(
    monkeypatch,
) -> None:
    app = _test_app(monkeypatch)
    suffix = uuid.uuid4().hex[:10]
    recipe_ids: list[str] = []
    tag_ids: list[str] = []
    slugs = [
        f"task18-{key}-{suffix}"
        for key in ("title", "ingredient", "tag", "description", "steps", "untimed")
    ]

    async def run() -> None:
        nonlocal tag_ids
        try:
            async with session_scope() as db:
                category = (
                    await db.execute(select(Category).where(Category.slug == "breakfast"))
                ).scalar_one()
            definitions = [
                ("Cardamom", "ordinary introduction", "plain flour", "Bake until golden.", "baking", 20),
                ("Cocoa Bread", "ordinary introduction", "ground cardamom", "Bake until golden.", "baking", 20),
                ("Almond Tart", "ordinary introduction", "plain flour", "Bake until golden.", "cardamom", 20),
                ("Lemon Loaf", "A cardamom scented loaf.", "plain flour", "Bake until golden.", "baking", 20),
                ("Orange Loaf", "ordinary introduction", "plain flour", "Fold in cardamom.", "baking", 20),
                ("Untimed Supper", "ordinary introduction", "plain flour", "Bake until golden.", "cardamom", None),
            ]
            for slug, (title, description, ingredient, step, tag_name, minutes) in zip(slugs, definitions):
                recipe_ids.append(await _create_search_recipe(
                    slug=slug, title=title, category=category, tag_name=f"task18-{tag_name}-{suffix}",
                    description=description, ingredient=ingredient, step=step,
                    total_minutes=minutes,
                ))
            async with session_scope() as db:
                target_tag = (
                    await db.execute(select(Tag).where(Tag.name == f"task18-cardamom-{suffix}"))
                ).scalar_one()
                target_tag_slug = target_tag.slug
                tag_ids = list((await db.execute(
                    select(Tag.id).where(Tag.name.like(f"task18-%-{suffix}"))
                )).scalars().all())
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                    response = await client.get("/search", params={"q": "cardamom"})
                    assert response.status_code == 200, response.text
                    assert response.headers["cache-control"] == "public, max-age=300, s-maxage=600"
                    ordered_results = response.text.split('class="recipe-card-list"', 1)[1]
                    positions = {
                        title: ordered_results.index(title)
                        for title in (
                            "Cardamom", "Cocoa Bread", "Almond Tart", "Lemon Loaf", "Orange Loaf"
                        )
                    }
                    assert positions["Cardamom"] < positions["Cocoa Bread"]
                    assert positions["Cocoa Bread"] < positions["Lemon Loaf"]
                    assert positions["Almond Tart"] < positions["Lemon Loaf"]
                    assert positions["Orange Loaf"] > positions["Cocoa Bread"]

                    partial = await client.get("/search", params={"q": "cardam"})
                    assert partial.status_code == 200
                    assert "Cardamom" in partial.text

                    filtered = await client.get("/search", params={
                        "q": "cardamom", "category": "breakfast",
                        "tag": target_tag_slug, "max_minutes": 30,
                    })
                    assert filtered.status_code == 200
                    assert "Almond Tart" in filtered.text
                    assert "Untimed Supper" not in filtered.text
                    assert "Cocoa Bread" not in filtered.text

                    empty = await client.get("/search")
                    assert empty.status_code == 200
                    assert "Enter a word or ingredient" in empty.text

                    no_matches = await client.get("/search", params={
                        "q": "zzquux", "category": "breakfast"
                    })
                    assert no_matches.status_code == 200
                    assert "zzquux" in no_matches.text
                    assert "Breakfast" in no_matches.text
                    assert "Try a category" in no_matches.text or "Try a tag" in no_matches.text
        finally:
            async with session_scope() as db:
                if recipe_ids:
                    await db.execute(delete(Recipe).where(Recipe.id.in_(recipe_ids)))
                tag_ids = list((await db.execute(
                    select(Tag.id).where(Tag.name.like(f"task18-%-{suffix}"))
                )).scalars().all())
                if tag_ids:
                    await db.execute(delete(Tag).where(Tag.id.in_(tag_ids)))
            await get_engine().dispose()
            get_settings.cache_clear()

    asyncio.run(run())
