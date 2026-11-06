"""Public recipe reading, browse listings, and legacy URL routing tests."""
from __future__ import annotations

import asyncio
import io
import uuid
from datetime import datetime

import httpx
from PIL import Image
from sqlalchemy import delete, select, update

from cookbook.app import create_app
from cookbook.auth.sessions import create_session, session_cookie_name
from cookbook.config import get_settings
from cookbook.db import get_engine, session_scope
from cookbook.models import Category, Recipe, RecipeUrlAlias, Session, Tag, User, recipe_tags
from cookbook.recipes import (
    RecipeDraft,
    RecipeIngredientDraft,
    RecipeProvenanceDraft,
    RecipeSectionDraft,
    RecipeStepDraft,
    create_recipe,
    set_recipe_image,
    validate_for_save,
)
from cookbook.recipes.images import delete_stored_image, store_image


async def _create_recipe(
    *,
    slug: str,
    title: str,
    category_id: str,
    category_name: str,
    tag_name: str,
    source_system: str | None = "web",
    source_url: str | None = "https://recipes.example/pie",
    source_name: str | None = "Recipes Example",
    image_path: str | None = None,
) -> str:
    provenance = None
    if source_system or source_url or source_name:
        provenance = RecipeProvenanceDraft(
            source_system=source_system,
            source_id=f"legacy-{uuid.uuid4().hex}" if source_system == "wordpress" else None,
            source_url=source_url,
            source_name=source_name,
            extraction_method="jsonld" if source_system == "web" else None,
        )
    draft = RecipeDraft(
        title=title,
        description="A short introduction for the cook.",
        servings_text="Serves four",
        prep_minutes=12,
        cook_minutes=30,
        total_minutes=42,
        tags=[tag_name],
        provenance=provenance,
        sections=[
            RecipeSectionDraft(
                heading="For the filling",
                kind="ingredients",
                lines=[
                    RecipeIngredientDraft(
                        raw_text="2 tbsp olive oil",
                        quantity_text="2",
                        unit="tbsp",
                        item="olive oil",
                    ),
                    RecipeIngredientDraft(raw_text="Salt to taste"),
                ],
            ),
            RecipeSectionDraft(
                heading="Steps",
                kind="steps",
                lines=[RecipeStepDraft(body="Warm the oil, then season.")],
            ),
            RecipeSectionDraft(
                heading="Cook's note",
                kind="note",
                lines=[RecipeStepDraft(body="Taste before serving.")],
            ),
        ],
    )
    async with session_scope() as db:
        save_data = validate_for_save(draft, category_names=[category_name])
        recipe = await create_recipe(
            db,
            save_data,
            category_ids=[category_id],
            slug=slug,
        )
        if image_path is not None:
            await set_recipe_image(db, recipe.id, image_path)
        return recipe.id


def _test_app(monkeypatch, media_dir) -> object:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "task16-public-test-secret")
    monkeypatch.setenv("MEDIA_DIR", str(media_dir))
    get_settings.cache_clear()
    return create_app()


def test_public_recipe_page_renders_readable_ordered_content_and_local_image(
    monkeypatch, tmp_path
) -> None:
    app = _test_app(monkeypatch, tmp_path)
    slug = f"task16-public-{uuid.uuid4().hex[:12]}"
    tag_name = f"task16-tag-{uuid.uuid4().hex[:12]}"
    image_buffer = io.BytesIO()
    Image.new("RGB", (3, 2), color="orange").save(image_buffer, format="PNG")
    stored_image = store_image(image_buffer.getvalue(), tmp_path, max_bytes=1024)
    recipe_id: str | None = None
    tag_id: str | None = None

    async def run() -> None:
        nonlocal recipe_id, tag_id
        try:
            async with session_scope() as db:
                category = (
                    await db.execute(select(Category).where(Category.name == "Breakfast"))
                ).scalar_one()
                category_id, category_name = category.id, category.name
            recipe_id = await _create_recipe(
                slug=slug,
                title="Task 16 Sunday Pie",
                category_id=category_id,
                category_name=category_name,
                tag_name=tag_name,
                image_path=stored_image.relative_path,
            )
            async with session_scope() as db:
                tag_id = (
                    await db.execute(select(Tag.id).where(Tag.name == tag_name))
                ).scalar_one()

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(f"/r/{slug}")
                assert response.status_code == 200, response.text
                assert response.headers["cache-control"] == "public, max-age=300, s-maxage=600"
                assert "set-cookie" not in response.headers
                assert response.text.count('aria-label="Primary navigation"') == 1
                assert 'href="/#categories-title">Browse</a>' in response.text
                assert "@media print" in response.text and ".site-nav" in response.text
                assert "Task 16 Sunday Pie" in response.text
                assert "A short introduction for the cook." in response.text
                assert response.text.count("A short introduction for the cook.") == 1
                assert "Serves four" in response.text
                assert "Prep" in response.text and "12 min" in response.text
                assert "Cook" in response.text and "30 min" in response.text
                assert "Total" in response.text and "42 min" in response.text
                assert "For the filling" in response.text
                assert "2 tbsp olive oil" in response.text
                assert "Salt to taste" in response.text
                assert "Warm the oil, then season." in response.text
                assert "Taste before serving." in response.text
                assert f"/media/{stored_image.relative_path}" in response.text
                assert f'href="/c/breakfast"' in response.text
                assert f'href="/t/{tag_name}"' in response.text
                assert "From <a" in response.text and "Recipes Example</a>" in response.text
                assert 'href="https://recipes.example/pie"' in response.text
                assert "@media print" in response.text
                assert "back-to-top" in response.text

                missing = await client.get("/r/no-such-task16-recipe")
                assert missing.status_code == 404
                assert missing.headers["cache-control"] == "private, no-store"
        finally:
            async with session_scope() as db:
                if recipe_id:
                    await db.execute(
                        delete(RecipeUrlAlias).where(RecipeUrlAlias.recipe_id == recipe_id)
                    )
                    await db.execute(delete(Recipe).where(Recipe.id == recipe_id))
                if tag_id:
                    await db.execute(delete(Tag).where(Tag.id == tag_id))
            delete_stored_image(tmp_path, stored_image.relative_path)
            await get_engine().dispose()
            get_settings.cache_clear()

    asyncio.run(run())


def test_public_page_hides_wordpress_provenance_and_rejects_unsafe_source_link(
    monkeypatch, tmp_path
) -> None:
    app = _test_app(monkeypatch, tmp_path)
    slug = f"task16-unsafe-source-{uuid.uuid4().hex[:12]}"
    wordpress_slug = f"task16-wp-{uuid.uuid4().hex[:12]}"
    tag_name = f"task16-source-tag-{uuid.uuid4().hex[:12]}"
    recipe_ids: list[str] = []
    tag_id: str | None = None

    async def run() -> None:
        nonlocal tag_id
        try:
            async with session_scope() as db:
                category = (
                    await db.execute(select(Category).where(Category.name == "Breakfast"))
                ).scalar_one()
                category_id, category_name = category.id, category.name
            recipe_ids.append(await _create_recipe(
                slug=slug,
                title="Task 16 Web Pie",
                category_id=category_id,
                category_name=category_name,
                tag_name=tag_name,
                source_system="web",
                source_url="javascript:alert(1)",
                source_name="Recipes Example",
                image_path="../../outside.png",
            ))
            recipe_ids.append(await _create_recipe(
                slug=wordpress_slug,
                title="Task 16 Legacy Pie",
                category_id=category_id,
                category_name=category_name,
                tag_name=tag_name,
                source_system="wordpress",
                source_url="https://wordpress.example/legacy",
                source_name="WordPress import",
            ))
            async with session_scope() as db:
                tag_id = (
                    await db.execute(select(Tag.id).where(Tag.name == tag_name))
                ).scalar_one()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get(f"/r/{slug}")
                assert response.status_code == 200, response.text
                assert "From Recipes Example" in response.text
                assert "javascript:alert(1)" not in response.text
                assert 'href="javascript:alert(1)"' not in response.text
                assert "../../outside.png" not in response.text
                assert "Salt to taste" in response.text
                assert 'class="hero-image"' not in response.text

                wordpress = await client.get(f"/r/{wordpress_slug}")
                assert wordpress.status_code == 200, wordpress.text
                assert "From WordPress import" not in wordpress.text
                assert "wordpress.example" not in wordpress.text
        finally:
            async with session_scope() as db:
                if recipe_ids:
                    await db.execute(
                        delete(RecipeUrlAlias).where(RecipeUrlAlias.recipe_id.in_(recipe_ids))
                    )
                    await db.execute(delete(Recipe).where(Recipe.id.in_(recipe_ids)))
                if tag_id:
                    await db.execute(delete(Tag).where(Tag.id == tag_id))
            await get_engine().dispose()
            get_settings.cache_clear()

    asyncio.run(run())


def test_legacy_alias_middleware_redirects_exact_recipe_and_category_paths(monkeypatch, tmp_path) -> None:
    app = _test_app(monkeypatch, tmp_path)
    slug = f"task16-alias-{uuid.uuid4().hex[:12]}"
    recipe_alias = f"/?p={uuid.uuid4().int % 900000 + 100000}"
    category_alias = f"/?page_id={uuid.uuid4().int % 900000 + 100000}"
    tag_name = f"task16-alias-tag-{uuid.uuid4().hex[:12]}"
    recipe_id: str | None = None
    category_id: str | None = None
    alias_paths = [recipe_alias, category_alias]
    user_id: str | None = None

    async def run() -> None:
        nonlocal recipe_id, category_id, user_id, tag_name
        try:
            async with session_scope() as db:
                category = (
                    await db.execute(select(Category).where(Category.name == "Breakfast"))
                ).scalar_one()
                category_id = category.id
                category_name = category.name
            recipe_id = await _create_recipe(
                slug=slug,
                title="Task 16 Alias Pie",
                category_id=category_id,
                category_name=category_name,
                tag_name=tag_name,
                source_system=None,
                source_url=None,
                source_name=None,
            )
            async with session_scope() as db:
                db.add(RecipeUrlAlias(path=recipe_alias, recipe_id=recipe_id))
                db.add(RecipeUrlAlias(path=category_alias, category_id=category_id))
                user_id = str(uuid.uuid4())
                db.add(
                    User(
                        id=user_id,
                        username=f"task16-{uuid.uuid4().hex}",
                        password_hash="test-only",
                        role="editor",
                        is_active=True,
                    )
                )
                tag_id = (
                    await db.execute(select(Tag.id).where(Tag.name == tag_name))
                ).scalar_one()

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                recipe_redirect = await client.get(recipe_alias, follow_redirects=False)
                assert recipe_redirect.status_code == 301
                assert recipe_redirect.headers["location"] == f"/r/{slug}"
                assert recipe_redirect.headers["cache-control"] == "public, max-age=300, s-maxage=600"

                category_redirect = await client.get(category_alias, follow_redirects=False)
                assert category_redirect.status_code == 301
                assert category_redirect.headers["location"] == "/c/breakfast"

                unmatched = await client.get(f"{recipe_alias}&extra=1", follow_redirects=False)
                assert unmatched.status_code == 200
                assert "What would you like to cook?" in unmatched.text

                raw_token, _ = await create_session(user_id, settings=app.state.settings)
                client.cookies.set(session_cookie_name(secure=False), raw_token)
                authenticated = await client.get(recipe_alias, follow_redirects=False)
                assert authenticated.status_code == 301
                assert authenticated.headers["cache-control"] == "private, no-store"
        finally:
            async with session_scope() as db:
                if recipe_id:
                    await db.execute(
                        delete(RecipeUrlAlias).where(RecipeUrlAlias.path.in_(alias_paths))
                    )
                    await db.execute(delete(Recipe).where(Recipe.id == recipe_id))
                if user_id:
                    await db.execute(delete(Session).where(Session.user_id == user_id))
                    await db.execute(delete(User).where(User.id == user_id))
                tag = (
                    await db.execute(select(Tag).where(Tag.name == tag_name))
                ).scalar_one_or_none()
                if tag is not None:
                    await db.delete(tag)
            await get_engine().dispose()
            get_settings.cache_clear()

    asyncio.run(run())


def test_public_home_renders_featured_categories_tags_and_search(monkeypatch, tmp_path) -> None:
    app = _test_app(monkeypatch, tmp_path)
    slugs = [f"task17-home-{uuid.uuid4().hex[:10]}" for _ in range(8)]
    common_tag = f"task17-popular-{uuid.uuid4().hex[:10]}"
    less_used_tag = f"task17-other-{uuid.uuid4().hex[:10]}"
    recipe_ids: list[str] = []
    tag_ids: list[str] = []

    async def run() -> None:
        nonlocal tag_ids
        try:
            async with session_scope() as db:
                breakfast = (
                    await db.execute(select(Category).where(Category.name == "Breakfast"))
                ).scalar_one()
                desserts = (
                    await db.execute(select(Category).where(Category.name == "Desserts"))
                ).scalar_one()
                categories = {row.name: row for row in (breakfast, desserts)}
            for index in range(7):
                recipe_ids.append(await _create_recipe(
                    slug=slugs[index], title=f"Task 17 Featured {index + 1}",
                    category_id=categories["Breakfast"].id, category_name="Breakfast",
                    tag_name=common_tag,
                ))
            recipe_ids.append(await _create_recipe(
                slug=slugs[7], title="Task 17 Unfeatured", category_id=categories["Desserts"].id,
                category_name="Desserts", tag_name=less_used_tag,
            ))
            async with session_scope() as db:
                for index in range(7):
                    await db.execute(
                        update(Recipe).where(Recipe.id == recipe_ids[index]).values(
                            home_featured=True, home_position=7 - index
                        )
                    )
                tag_ids = list(
                    (await db.execute(select(Tag.id).where(Tag.name.in_([common_tag, less_used_tag]))))
                    .scalars().all()
                )
                for index in range(12):
                    extra_tag = Tag(
                        name=f"task17-topic-{index:02}",
                        slug=f"task17-topic-{index:02}",
                    )
                    db.add(extra_tag)
                    await db.flush()
                    tag_ids.append(extra_tag.id)
                    await db.execute(
                        recipe_tags.insert().values(
                            recipe_id=recipe_ids[7], tag_id=extra_tag.id
                        )
                    )

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                response = await client.get("/")
                assert response.status_code == 200, response.text
                assert response.headers["cache-control"] == "public, max-age=300, s-maxage=600"
                assert response.text.count('aria-label="Primary navigation"') == 1
                assert 'href="/#categories-title">Browse</a>' in response.text
                featured = response.text.split('id="featured-recipes"', 1)[1].split(
                    'id="categories"', 1
                )[0]
                assert featured.index(f'/r/{slugs[6]}') < featured.index(f'/r/{slugs[5]}')
                assert slugs[0] not in featured
                assert slugs[7] not in featured
                assert "Task 17 Featured 1" not in featured
                categories_html = response.text.split('id="categories"', 1)[1].split(
                    'id="popular-tags"', 1
                )[0]
                assert categories_html.index('href="/c/breakfast"') < categories_html.index(
                    'href="/c/fermentation"'
                )
                assert "7 recipes" in categories_html
                assert f'href="/t/{common_tag}"' in response.text
                popular_html = response.text.split('id="popular-tags"', 1)[1]
                assert popular_html.index(common_tag) < popular_html.index(less_used_tag)
                assert "task17-topic-09" in popular_html
                assert "task17-topic-10" not in popular_html
                assert 'action="/search"' in response.text
                assert 'class="recipe-placeholder"' in response.text

                async with session_scope() as db:
                    await db.execute(
                        update(Recipe).where(Recipe.id.in_(recipe_ids[:7])).values(
                            home_featured=False
                        )
                    )
                empty_featured = await client.get("/")
                featured_empty_html = empty_featured.text.split(
                    'id="featured-recipes"', 1
                )[1].split('id="categories"', 1)[0]
                assert "No featured recipes yet." in featured_empty_html
        finally:
            async with session_scope() as db:
                if recipe_ids:
                    await db.execute(delete(Recipe).where(Recipe.id.in_(recipe_ids)))
                if tag_ids:
                    await db.execute(delete(Tag).where(Tag.id.in_(tag_ids)))
            await get_engine().dispose()
            get_settings.cache_clear()

    asyncio.run(run())


def test_category_and_tag_pages_apply_ordered_recipe_and_structured_filters(monkeypatch, tmp_path) -> None:
    app = _test_app(monkeypatch, tmp_path)
    slugs = {
        name: f"task17-{name}-{uuid.uuid4().hex[:10]}"
        for name in ("quick", "slow", "untimed", "elsewhere", "other-tag")
    }
    common_tag = f"task17-filter-{uuid.uuid4().hex[:10]}"
    other_tag = f"task17-secondary-{uuid.uuid4().hex[:10]}"
    recipe_ids: list[str] = []
    tag_ids: list[str] = []
    timestamps = {
        "quick": datetime(2026, 1, 5),
        "slow": datetime(2026, 1, 3),
        "untimed": datetime(2026, 1, 4),
        "elsewhere": datetime(2026, 1, 2),
        "other-tag": datetime(2026, 1, 1),
    }

    async def run() -> None:
        nonlocal tag_ids
        try:
            async with session_scope() as db:
                breakfast = (
                    await db.execute(select(Category).where(Category.name == "Breakfast"))
                ).scalar_one()
                desserts = (
                    await db.execute(select(Category).where(Category.name == "Desserts"))
                ).scalar_one()
                category_ids = {"breakfast": breakfast.id, "desserts": desserts.id}
                category_names = {"breakfast": breakfast.name, "desserts": desserts.name}

            recipe_specs = [
                ("quick", "Browse Quick", "breakfast", common_tag, 20),
                ("slow", "Browse Slow", "breakfast", common_tag, 42),
                ("untimed", "Browse Untimed", "breakfast", common_tag, None),
                ("elsewhere", "Browse Other Category", "desserts", common_tag, 15),
                ("other-tag", "Browse Other Tag", "breakfast", other_tag, 15),
            ]
            for key, title, category_key, tag, minutes in recipe_specs:
                recipe_ids.append(await _create_recipe(
                    slug=slugs[key], title=title, category_id=category_ids[category_key],
                    category_name=category_names[category_key], tag_name=tag,
                    image_path="../../outside.png" if key == "other-tag" else None,
                ))
                async with session_scope() as db:
                    await db.execute(
                        update(Recipe).where(Recipe.id == recipe_ids[-1]).values(
                            total_minutes=minutes, updated_at=timestamps[key]
                        )
                    )
            async with session_scope() as db:
                tag_rows = list(
                    (await db.execute(select(Tag.id, Tag.name).where(Tag.name.in_([common_tag, other_tag]))))
                    .all()
                )
                tag_ids = [row.id for row in tag_rows]
                other_tag_id = next(row.id for row in tag_rows if row.name == other_tag)
                await db.execute(
                    recipe_tags.insert().values(recipe_id=recipe_ids[2], tag_id=other_tag_id)
                )

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                category = await client.get("/c/breakfast")
                assert category.status_code == 200, category.text
                assert category.headers["cache-control"] == "public, max-age=300, s-maxage=600"
                assert category.text.index("Browse Quick") < category.text.index("Browse Untimed")
                assert category.text.index("Browse Untimed") < category.text.index("Browse Slow")
                assert "A short introduction for the cook." in category.text
                assert "42 min" in category.text and "Serves four" in category.text
                assert "../../outside.png" not in category.text
                assert 'class="recipe-placeholder"' in category.text
                assert 'class="time-filters"' in category.text
                assert 'class="semantic-tags"' in category.text

                filtered = await client.get(
                    f"/c/breakfast?tag={common_tag}&max_minutes=30"
                )
                assert filtered.status_code == 200
                assert "Browse Quick" in filtered.text
                assert "Browse Slow" not in filtered.text
                assert "Browse Untimed" not in filtered.text
                assert "Browse Other Category" not in filtered.text
                assert "Browse Other Tag" not in filtered.text

                unknown_tag_filter = await client.get(
                    "/c/breakfast?tag=task17-no-such-filter-tag"
                )
                assert unknown_tag_filter.status_code == 200
                assert "No recipes match this view yet." in unknown_tag_filter.text

                tag_page = await client.get(f"/t/{common_tag}")
                assert tag_page.status_code == 200, tag_page.text
                assert tag_page.text.index("Browse Quick") < tag_page.text.index("Browse Untimed")
                assert "Browse Other Category" in tag_page.text
                assert f'href="/c/desserts"' in tag_page.text
                assert f'href="/t/{other_tag}"' in tag_page.text
        finally:
            async with session_scope() as db:
                if recipe_ids:
                    await db.execute(delete(Recipe).where(Recipe.id.in_(recipe_ids)))
                if tag_ids:
                    await db.execute(delete(Tag).where(Tag.id.in_(tag_ids)))
            await get_engine().dispose()
            get_settings.cache_clear()

    asyncio.run(run())


def test_unknown_category_and_tag_pages_are_private_404s(monkeypatch, tmp_path) -> None:
    app = _test_app(monkeypatch, tmp_path)

    async def run() -> None:
        try:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                empty_home = await client.get("/")
                assert "No featured recipes yet." in empty_home.text
                assert "Tags will appear here as recipes are added." in empty_home.text
                empty_category = await client.get("/c/fermentation")
                assert empty_category.status_code == 200
                assert "No recipes match this view yet." in empty_category.text
                for path in ("/c/no-such-task17-category", "/t/no-such-task17-tag"):
                    response = await client.get(path)
                    assert response.status_code == 404
                    assert response.headers["cache-control"] == "private, no-store"
        finally:
            await get_engine().dispose()
            get_settings.cache_clear()

    asyncio.run(run())
