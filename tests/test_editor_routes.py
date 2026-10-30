"""Task 10 authenticated recipe editor route tests."""
from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from sqlalchemy import delete, select

from cookbook.auth.sessions import create_session, session_cookie_name
from cookbook.app import create_app
from cookbook.db import get_engine, session_scope
from cookbook.models import Category, Recipe, RecipeUrlAlias, Session, Tag, User, recipe_tags


def test_editor_dashboard_requires_an_editor_session() -> None:
    async def run() -> None:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/edit")
        assert response.status_code == 401
        assert response.headers["cache-control"] == "private, no-store"

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
                    assert root.status_code == 302
                    assert root.headers["location"] == "/edit"
                    assert root.headers["cache-control"] == "private, no-store"
                    dashboard = await client.get("/edit")
                    assert dashboard.status_code == 200, dashboard.text
                    assert dashboard.headers["cache-control"] == "private, no-store"
                    assert "Recipe editor" in dashboard.text
                    form = await client.get("/recipes/new")
                    assert form.status_code == 200, form.text
                    assert form.headers["cache-control"] == "private, no-store"
                    assert 'name="csrf_token"' in form.text
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
