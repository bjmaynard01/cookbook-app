"""Task 16 anonymous recipe reading and legacy URL routing tests."""
from __future__ import annotations

import asyncio
import io
import uuid

import httpx
from PIL import Image
from sqlalchemy import delete, select

from cookbook.app import create_app
from cookbook.auth.sessions import create_session, session_cookie_name
from cookbook.config import get_settings
from cookbook.db import get_engine, session_scope
from cookbook.models import Category, Recipe, RecipeUrlAlias, Session, Tag, User
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
                assert unmatched.status_code == 302
                assert unmatched.headers["location"] == "/login"

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
