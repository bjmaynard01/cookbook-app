"""Task 10 authenticated recipe editor route tests."""
from __future__ import annotations

import asyncio
import io
import uuid

import httpx
import pytest
from PIL import Image
from sqlalchemy import delete, select

from cookbook.auth.sessions import create_session, session_cookie_name
from cookbook.app import create_app
from cookbook.db import get_engine, session_scope
from cookbook.models import Category, Recipe, RecipeUrlAlias, Session, Tag, User, recipe_tags
from cookbook.recipes import routes as recipe_routes
from cookbook.recipes.ingest import FetchError


def test_editor_dashboard_requires_an_editor_session() -> None:
    async def run() -> None:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/edit")
        assert response.status_code == 401
        assert response.headers["cache-control"] == "private, no-store"

    asyncio.run(run())


def test_app_ingest_pool_uses_configured_concurrency_limit(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "task15-test-secret")
    monkeypatch.setenv("INGEST_MAX_CONCURRENT", "1")
    from cookbook.config import get_settings

    get_settings.cache_clear()
    try:
        app = create_app()
        semaphore = app.state.ingest_semaphore

        async def run() -> None:
            assert await semaphore.acquire()
            second = asyncio.create_task(semaphore.acquire())
            await asyncio.sleep(0)
            assert not second.done()
            semaphore.release()
            assert await second
            semaphore.release()

        asyncio.run(run())
    finally:
        get_settings.cache_clear()


def test_ingest_launcher_and_fetch_failure_land_in_editor_form(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "task15-test-secret")
    from cookbook.config import get_settings

    get_settings.cache_clear()

    async def fail_fetch(url: str):
        raise FetchError("The recipe source could not be fetched.")

    monkeypatch.setattr(recipe_routes, "fetch_page", fail_fetch, raising=False)

    async def run() -> None:
        app = create_app()
        user_id = str(uuid.uuid4())
        try:
            async with session_scope() as db:
                db.add(
                    User(
                        id=user_id,
                        username=f"task15-{uuid.uuid4().hex}",
                        password_hash="test-only",
                        role="editor",
                        is_active=True,
                    )
                )
            raw_token, auth_session = await create_session(
                user_id, settings=app.state.settings
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                client.cookies.set(session_cookie_name(secure=False), raw_token)
                dashboard = await client.get("/edit")
                assert dashboard.status_code == 200
                assert 'action="/recipes/ingest"' in dashboard.text
                assert 'name="url"' in dashboard.text

                no_csrf = await client.post(
                    "/recipes/ingest", data={"url": "https://recipes.example/pie"}
                )
                assert no_csrf.status_code == 403

                response = await client.post(
                    "/recipes/ingest",
                    data={
                        "csrf_token": auth_session.csrf_token,
                        "url": "https://recipes.example/pie",
                    },
                )
                assert response.status_code == 200, response.text
                assert response.headers["cache-control"] == "private, no-store"
                assert 'data-ingest-state="fetch-failed"' in response.text
                assert "recipe source could not be fetched" in response.text.lower()
                assert "https://recipes.example/pie" in response.text

                async def slow_fetch(url: str):
                    await asyncio.sleep(0.02)

                monkeypatch.setattr(recipe_routes, "INGEST_TIMEOUT_SECONDS", 0.001)
                monkeypatch.setattr(recipe_routes, "fetch_page", slow_fetch)
                timed_out = await client.post(
                    "/recipes/ingest",
                    data={
                        "csrf_token": auth_session.csrf_token,
                        "url": "https://recipes.example/slow",
                    },
                )
                assert timed_out.status_code == 200
                assert 'data-ingest-state="fetch-failed"' in timed_out.text
                assert "exceeded the time limit" in timed_out.text
        finally:
            async with session_scope() as db:
                await db.execute(delete(Session).where(Session.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
            await get_engine().dispose()
            get_settings.cache_clear()

    asyncio.run(run())


def test_ingest_prefills_existing_form_and_save_keeps_provenance(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "task15-test-secret")
    from cookbook.config import get_settings
    from cookbook.recipes.drafts import (
        RecipeDraft,
        RecipeIngredientDraft,
        RecipeSectionDraft,
        RecipeStepDraft,
    )
    from cookbook.recipes.ingest import FetchedPage

    get_settings.cache_clear()
    source_url = "https://recipes.example/pie"

    async def fake_fetch(url: str):
        return FetchedPage(
            source_url=url,
            final_url=url,
            status_code=200,
            content_type="text/html",
            html="",
            title="Pumpkin Pie recipe",
            site_name="Recipes Example",
        )

    async def fake_parse(page):
        return RecipeDraft(
            title="Pumpkin Pie",
            sections=[
                RecipeSectionDraft(
                    heading="Ingredients",
                    kind="ingredients",
                    lines=[RecipeIngredientDraft(raw_text="1 cup pumpkin")],
                ),
                RecipeSectionDraft(
                    heading="Steps",
                    kind="steps",
                    lines=[RecipeStepDraft(body="Bake until set.")],
                ),
            ],
            suggested_categories=["Breakfast"],
            provenance={"source_url": page.source_url, "extraction_method": "jsonld"},
        )

    async def fail_image(url: str, **kwargs):
        raise FetchError("The source image could not be fetched.")

    monkeypatch.setattr(recipe_routes, "fetch_page", fake_fetch)
    monkeypatch.setattr(recipe_routes, "parse_recipe_page_with_fallback", fake_parse)
    monkeypatch.setattr(recipe_routes, "fetch_public_bytes", fail_image, raising=False)

    async def run() -> None:
        app = create_app()
        user_id = str(uuid.uuid4())
        recipe_id: str | None = None
        try:
            async with session_scope() as db:
                await db.execute(delete(Recipe).where(Recipe.source_url == source_url))
                db.add(
                    User(
                        id=user_id,
                        username=f"task15-{uuid.uuid4().hex}",
                        password_hash="test-only",
                        role="editor",
                        is_active=True,
                    )
                )
                category_id = (
                    await db.execute(
                        select(Category.id).where(Category.name == "Breakfast")
                    )
                ).scalar_one()
            raw_token, auth_session = await create_session(
                user_id, settings=app.state.settings
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                client.cookies.set(session_cookie_name(secure=False), raw_token)
                preview = await client.post(
                    "/recipes/ingest",
                    data={"csrf_token": auth_session.csrf_token, "url": source_url + "#instructions"},
                )
                assert preview.status_code == 200, preview.text
                assert 'data-ingest-state="full"' in preview.text
                assert 'name="source_url" value="https://recipes.example/pie"' in preview.text
                assert 'name="source_name" value="Recipes Example"' in preview.text
                assert 'name="extraction_method" value="jsonld"' in preview.text
                assert 'name="category_ids"' in preview.text
                assert "Pumpkin Pie" in preview.text
                assert "1 cup pumpkin" in preview.text
                assert "Bake until set." in preview.text
                async with session_scope() as db:
                    assert (
                        await db.execute(
                            select(Recipe.id).where(Recipe.source_url == source_url)
                        )
                    ).scalar_one_or_none() is None

                saved = await client.post(
                    "/recipes",
                    data={
                        "csrf_token": auth_session.csrf_token,
                        "title": "Pumpkin Pie",
                        "category_ids": category_id,
                        "source_url": source_url,
                        "source_name": "Recipes Example",
                        "extraction_method": "jsonld",
                        "hero_image": "https://images.example/pie.png",
                        "sections[0][heading]": "Ingredients",
                        "sections[0][kind]": "ingredients",
                        "sections[0][lines][0][raw_text]": "1 cup pumpkin",
                        "sections[1][heading]": "Steps",
                        "sections[1][kind]": "steps",
                        "sections[1][lines][0][body]": "Bake until set.",
                    },
                )
                assert saved.status_code == 303, saved.text
                assert "image=download_failed" in saved.headers["location"]
                async with session_scope() as db:
                    recipe = (
                        await db.execute(
                            select(Recipe).where(Recipe.source_url == source_url)
                        )
                    ).scalar_one()
                    recipe_id = recipe.id
                    assert recipe.source_name == "Recipes Example"
                    assert recipe.extraction_method == "jsonld"
                    assert recipe.imported_at is not None
                    assert recipe.image_path is None
                duplicate_preview = await client.post(
                    "/recipes/ingest",
                    data={"csrf_token": auth_session.csrf_token, "url": source_url},
                )
                assert duplicate_preview.status_code == 200
                assert "A recipe from this source already exists" in duplicate_preview.text
                assert f'href="/recipes/{recipe_id}/edit"' in duplicate_preview.text
                edit_page = await client.get(f"/recipes/{recipe_id}/edit")
                assert edit_page.status_code == 200
                assert "Source: Recipes Example" in edit_page.text
        finally:
            async with session_scope() as db:
                await db.execute(delete(Recipe).where(Recipe.source_url == source_url))
                if recipe_id:
                    await db.execute(delete(Recipe).where(Recipe.id == recipe_id))
                await db.execute(delete(Session).where(Session.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
            await get_engine().dispose()
            get_settings.cache_clear()

    asyncio.run(run())


def test_ingest_distinguishes_empty_and_partial_parse_states(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "task15-test-secret")
    from cookbook.config import get_settings
    from cookbook.recipes.drafts import RecipeDraft, RecipeIngredientDraft, RecipeSectionDraft
    from cookbook.recipes.ingest import FetchedPage

    get_settings.cache_clear()

    async def fake_fetch(url: str):
        return FetchedPage(
            source_url=url,
            final_url=url,
            status_code=200,
            content_type="text/html",
            html="",
        )

    async def fake_parse(page):
        if page.source_url.endswith("/empty"):
            return RecipeDraft(
                title="",
                parse_notes=["Optional LLM parsing is not configured."],
                provenance={"source_url": page.source_url},
            )
        return RecipeDraft(
            title="Berry Tart",
            sections=[
                RecipeSectionDraft(
                    heading="Ingredients",
                    kind="ingredients",
                    lines=[RecipeIngredientDraft(raw_text="2 cups berries")],
                )
            ],
            parse_notes=["Some recipe timing information could not be interpreted."],
            provenance={"source_url": page.source_url, "extraction_method": "microdata"},
        )

    monkeypatch.setattr(recipe_routes, "fetch_page", fake_fetch)
    monkeypatch.setattr(recipe_routes, "parse_recipe_page_with_fallback", fake_parse)

    async def run() -> None:
        app = create_app()
        user_id = str(uuid.uuid4())
        try:
            async with session_scope() as db:
                db.add(
                    User(
                        id=user_id,
                        username=f"task15-{uuid.uuid4().hex}",
                        password_hash="test-only",
                        role="editor",
                        is_active=True,
                    )
                )
            raw_token, auth_session = await create_session(
                user_id, settings=app.state.settings
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                client.cookies.set(session_cookie_name(secure=False), raw_token)
                for path, state in (("empty", "nothing-found"), ("partial", "partial")):
                    response = await client.post(
                        "/recipes/ingest",
                        data={
                            "csrf_token": auth_session.csrf_token,
                            "url": f"https://recipes.example/{path}",
                        },
                    )
                    assert response.status_code == 200, response.text
                    assert f'data-ingest-state="{state}"' in response.text
                    assert f"https://recipes.example/{path}" in response.text
                    if path == "empty":
                        assert "Optional LLM parsing is not configured." in response.text
                    else:
                        assert "Some recipe timing information could not be interpreted." in response.text
                        assert 'name="ingest_state" value="partial"' in response.text
                        invalid_save = await client.post(
                            "/recipes",
                            data={
                                "csrf_token": auth_session.csrf_token,
                                "ingest_state": "partial",
                                "ingest_message": "Some recipe fields were found.",
                                "parse_notes": "Some recipe timing information could not be interpreted.",
                                "source_url": "https://recipes.example/partial",
                                "extraction_method": "microdata",
                                "title": "Berry Tart",
                                "sections[0][heading]": "Ingredients",
                                "sections[0][kind]": "ingredients",
                                "sections[0][lines][0][raw_text]": "2 cups berries",
                            },
                        )
                        assert invalid_save.status_code == 400
                        assert 'data-ingest-state="partial"' in invalid_save.text
                        assert "Some recipe timing information could not be interpreted." in invalid_save.text
                assert "Berry Tart" in response.text
        finally:
            async with session_scope() as db:
                await db.execute(delete(Session).where(Session.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
            await get_engine().dispose()
            get_settings.cache_clear()

    asyncio.run(run())


def test_save_stores_remote_image_only_in_local_media(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "task15-test-secret")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path))
    from cookbook.config import get_settings

    get_settings.cache_clear()
    image_buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color="tomato").save(image_buffer, format="PNG")
    image_content = image_buffer.getvalue()

    async def fake_image(url: str, **kwargs):
        return image_content, "image/png"

    monkeypatch.setattr(recipe_routes, "fetch_public_bytes", fake_image)

    async def run() -> None:
        app = create_app()
        user_id = str(uuid.uuid4())
        source_url = f"https://recipes.example/{uuid.uuid4().hex}"
        recipe_id: str | None = None
        try:
            async with session_scope() as db:
                db.add(
                    User(
                        id=user_id,
                        username=f"task15-{uuid.uuid4().hex}",
                        password_hash="test-only",
                        role="editor",
                        is_active=True,
                    )
                )
                category_id = (
                    await db.execute(
                        select(Category.id).where(Category.name == "Breakfast")
                    )
                ).scalar_one()
            raw_token, auth_session = await create_session(
                user_id, settings=app.state.settings
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                client.cookies.set(session_cookie_name(secure=False), raw_token)
                response = await client.post(
                    "/recipes",
                    data={
                        "csrf_token": auth_session.csrf_token,
                        "title": "Local image pie",
                        "category_ids": category_id,
                        "source_url": source_url,
                        "extraction_method": "jsonld",
                        "hero_image": "https://images.example/pie.png",
                        "sections[0][heading]": "Ingredients",
                        "sections[0][kind]": "ingredients",
                        "sections[0][lines][0][raw_text]": "1 cup pumpkin",
                    },
                )
                assert response.status_code == 303, response.text
                async with session_scope() as db:
                    recipe = (
                        await db.execute(
                            select(Recipe).where(Recipe.source_url == source_url)
                        )
                    ).scalar_one()
                    recipe_id = recipe.id
                    image_path = recipe.image_path
                    assert image_path is not None
                    assert image_path.startswith("recipes/")
                local_image = tmp_path / image_path
                assert local_image.is_file()
                served = await client.get(f"/media/{image_path}")
                assert served.status_code == 200
                assert served.headers["content-type"] == "image/png"
                assert served.content == image_content
        finally:
            async with session_scope() as db:
                if recipe_id:
                    await db.execute(delete(Recipe).where(Recipe.id == recipe_id))
                else:
                    await db.execute(delete(Recipe).where(Recipe.source_url == source_url))
                await db.execute(delete(Session).where(Session.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
            await get_engine().dispose()
            get_settings.cache_clear()

    asyncio.run(run())


def test_editor_and_admin_can_open_private_dashboard_and_form(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "task10-test-secret")
    from cookbook.config import get_settings

    get_settings.cache_clear()

    async def run() -> None:
        app = create_app()
        user_ids: list[str] = []
        try:
            async with session_scope() as db:
                for role in ("editor", "admin"):
                    user_id = str(uuid.uuid4())
                    user_ids.append(user_id)
                    db.add(
                        User(
                            id=user_id,
                            username=f"task10-{role}-{uuid.uuid4().hex}",
                            password_hash="test-only",
                            role=role,
                            is_active=True,
                        )
                    )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                for user_id in user_ids:
                    raw_token, _ = await create_session(
                        user_id, settings=app.state.settings
                    )
                    client.cookies.set(session_cookie_name(secure=False), raw_token)
                    root = await client.get("/")
                    assert root.status_code == 200
                    assert "What would you like to cook?" in root.text
                    assert 'href="/edit"' in root.text
                    assert root.text.count('aria-label="Primary navigation"') == 1
                    assert 'href="/#categories-title">Browse</a>' in root.text
                    assert 'href="/recipes/new">New recipe</a>' in root.text
                    assert root.headers["cache-control"] == "private, no-store"
                    dashboard = await client.get("/edit")
                    assert dashboard.status_code == 200, dashboard.text
                    assert dashboard.headers["cache-control"] == "private, no-store"
                    assert "Recipe editor" in dashboard.text
                    assert dashboard.text.count('aria-label="Primary navigation"') == 1
                    assert 'action="/logout"' in dashboard.text
                    assert 'href="/">Home</a>' in dashboard.text
                    assert 'href="/search">Search</a>' in dashboard.text
                    assert 'href="/edit">Recipes</a>' in dashboard.text
                    assert 'href="/recipes/new">New recipe</a>' in dashboard.text
                    form = await client.get("/recipes/new")
                    assert form.status_code == 200, form.text
                    assert form.headers["cache-control"] == "private, no-store"
                    assert 'name="csrf_token"' in form.text
                    assert form.text.count('aria-label="Primary navigation"') == 1
                    assert 'href="/edit">Recipes</a>' in form.text
                    assert 'href="/recipes/new">New recipe</a>' in form.text
                    assert 'action="/logout"' in form.text
                    client.cookies.clear()
        finally:
            async with session_scope() as db:
                await db.execute(delete(Session).where(Session.user_id.in_(user_ids)))
                await db.execute(delete(User).where(User.id.in_(user_ids)))
            await get_engine().dispose()
            get_settings.cache_clear()

    asyncio.run(run())


def test_recipe_editor_create_update_filter_and_alias_delete_guard(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "task10-test-secret")
    from cookbook.config import get_settings

    get_settings.cache_clear()

    async def run() -> None:
        app = create_app()
        token = uuid.uuid4().hex[:10]
        username = f"task10-editor-{token}"
        user_id = str(uuid.uuid4())
        recipe_id: str | None = None
        tag_id: str | None = None
        category_id: str | None = None
        try:
            async with session_scope() as db:
                db.add(
                    User(
                        id=user_id,
                        username=username,
                        password_hash="test-only",
                        role="editor",
                        is_active=True,
                    )
                )
                category_id = (
                    await db.execute(
                        select(Category.id).where(Category.name == "Breakfast")
                    )
                ).scalar_one()
            raw_token, auth_session = await create_session(
                user_id, settings=app.state.settings
            )
            csrf_token = auth_session.csrf_token
            cookie_name = session_cookie_name(secure=False)
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                client.cookies.set(cookie_name, raw_token)
                bad_csrf = await client.post(
                    "/recipes", data={"title": f"Should not save {token}"}
                )
                assert bad_csrf.status_code == 403
                assert bad_csrf.headers["cache-control"] == "private, no-store"

                invalid = await client.post(
                    "/recipes",
                    data={
                        "csrf_token": csrf_token,
                        "title": f"Keep this title {token}",
                        "category_ids": category_id,
                    },
                )
                assert invalid.status_code == 400
                assert f"Keep this title {token}" in invalid.text
                assert "ingredient" in invalid.text.lower()

                create_fields = {
                    "csrf_token": csrf_token,
                    "title": f"Editor recipe {token}",
                    "description": "Saved from the editor form.",
                    "category_ids": category_id,
                    "tags": f"task10-tag-{token}",
                    "servings_text": "serves three",
                    "servings_number": "3",
                    "prep_minutes": "5",
                    "cook_minutes": "12",
                    "sections[0][heading]": "Ingredients",
                    "sections[0][kind]": "ingredients",
                    "sections[0][lines][0][raw_text]": "1 cup flour",
                    "sections[0][lines][0][quantity_text]": "1 cup",
                    "sections[0][lines][0][item]": "flour",
                    "sections[1][heading]": "Steps",
                    "sections[1][kind]": "steps",
                    "sections[1][lines][0][body]": "Mix and cook.",
                }
                invalid_number_fields = dict(create_fields)
                invalid_number_fields["servings_number"] = "not-a-number"
                invalid_number = await client.post(
                    "/recipes", data=invalid_number_fields
                )
                assert invalid_number.status_code == 400
                assert f"Editor recipe {token}" in invalid_number.text
                assert "1 cup flour" in invalid_number.text
                assert "not-a-number" in invalid_number.text

                created = await client.post("/recipes", data=create_fields)
                assert created.status_code == 303, created.text
                assert created.headers["cache-control"] == "private, no-store"
                async with session_scope() as db:
                    recipe = (
                        await db.execute(
                            select(Recipe).where(Recipe.title == f"Editor recipe {token}")
                        )
                    ).scalar_one()
                    recipe_id = recipe.id
                    tag_id = (
                        await db.execute(
                            select(recipe_tags.c.tag_id).where(
                                recipe_tags.c.recipe_id == recipe_id
                            )
                        )
                    ).scalar_one()

                listing = await client.get(
                    "/edit",
                    params={
                        "q": f"Editor recipe {token}",
                        "category_id": category_id,
                        "tag_id": tag_id,
                        "sort": "title",
                    },
                )
                assert listing.status_code == 200
                assert f"Editor recipe {token}" in listing.text

                edit_page = await client.get(f"/recipes/{recipe_id}/edit")
                assert edit_page.status_code == 200, edit_page.text
                assert "1 cup flour" in edit_page.text
                assert 'value="1 cup"' in edit_page.text
                update_fields = dict(create_fields)
                update_fields.update(
                    {
                        "title": f"Renamed recipe {token}",
                        "slug": f"editor-custom-{token}",
                        "sections[0][lines][0][raw_text]": "2 cups flour",
                    }
                )
                updated = await client.post(
                    f"/recipes/{recipe_id}", data=update_fields
                )
                assert updated.status_code == 303, updated.text
                async with session_scope() as db:
                    saved = await db.get(Recipe, recipe_id)
                    assert saved is not None
                    assert saved.slug == f"editor-custom-{token}"
                    assert saved.title == f"Renamed recipe {token}"
                    alias = (
                        await db.execute(
                            select(RecipeUrlAlias).where(
                                RecipeUrlAlias.path == f"/r/editor-recipe-{token}"
                            )
                        )
                    ).scalar_one()
                    assert alias.recipe_id == recipe_id

                blocked = await client.post(
                    f"/recipes/{recipe_id}/delete", data={"csrf_token": csrf_token}
                )
                assert blocked.status_code == 409, blocked.text
                assert "cannot be deleted" in blocked.text

                missing_update = await client.post(
                    f"/recipes/missing-{token}", data=create_fields
                )
                assert missing_update.status_code == 404
        finally:
            if recipe_id is not None:
                async with session_scope() as db:
                    await db.execute(
                        delete(RecipeUrlAlias).where(
                            RecipeUrlAlias.recipe_id == recipe_id
                        )
                    )
                    await db.execute(delete(Recipe).where(Recipe.id == recipe_id))
                    if tag_id:
                        await db.execute(delete(Tag).where(Tag.id == tag_id))
            async with session_scope() as db:
                await db.execute(delete(Session).where(Session.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
            await get_engine().dispose()
            get_settings.cache_clear()

    asyncio.run(run())


def test_editor_delete_route_deletes_unaliased_recipe(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "task10-test-secret")
    from cookbook.config import get_settings

    get_settings.cache_clear()

    async def run() -> None:
        app = create_app()
        token = uuid.uuid4().hex[:10]
        user_id = str(uuid.uuid4())
        recipe_id: str | None = None
        tag_id: str | None = None
        try:
            async with session_scope() as db:
                db.add(
                    User(
                        id=user_id,
                        username=f"task10-delete-{token}",
                        password_hash="test-only",
                        role="editor",
                        is_active=True,
                    )
                )
                category_id = (
                    await db.execute(
                        select(Category.id).where(Category.name == "Breakfast")
                    )
                ).scalar_one()
            raw_token, auth_session = await create_session(
                user_id, settings=app.state.settings
            )
            csrf_token = auth_session.csrf_token
            client_transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=client_transport, base_url="http://test") as client:
                client.cookies.set(session_cookie_name(secure=False), raw_token)
                fields = {
                    "csrf_token": csrf_token,
                    "title": f"Delete route recipe {token}",
                    "category_ids": category_id,
                    "sections[0][heading]": "Ingredients",
                    "sections[0][kind]": "ingredients",
                    "sections[0][lines][0][raw_text]": "1 apple",
                }
                created = await client.post("/recipes", data=fields)
                assert created.status_code == 303, created.text
                async with session_scope() as db:
                    recipe = (
                        await db.execute(
                            select(Recipe).where(Recipe.title == f"Delete route recipe {token}")
                        )
                    ).scalar_one()
                    recipe_id = recipe.id
                    tag_id = (
                        await db.execute(
                            select(recipe_tags.c.tag_id).where(
                                recipe_tags.c.recipe_id == recipe_id
                            )
                        )
                    ).scalar_one_or_none()
                deleted = await client.post(
                    f"/recipes/{recipe_id}/delete", data={"csrf_token": csrf_token}
                )
                assert deleted.status_code == 303
                assert deleted.headers["location"] == "/edit?deleted=1"
                async with session_scope() as db:
                    assert await db.get(Recipe, recipe_id) is None
                recipe_id = None
        finally:
            if tag_id:
                async with session_scope() as db:
                    await db.execute(delete(Tag).where(Tag.id == tag_id))
            async with session_scope() as db:
                await db.execute(delete(Session).where(Session.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
            await get_engine().dispose()
            get_settings.cache_clear()

    asyncio.run(run())
