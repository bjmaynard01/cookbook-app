from __future__ import annotations

import asyncio
import io
import uuid

import httpx
from PIL import Image
from sqlalchemy import delete, select

from cookbook.auth.sessions import create_session, session_cookie_name
from cookbook.app import create_app
from cookbook.config import get_settings
from cookbook.db import get_engine, session_scope
from cookbook.models import Category, Recipe, RecipeUrlAlias, Session, User
from cookbook.recipes import (
    RecipeDraft,
    RecipeIngredientDraft,
    RecipeSectionDraft,
    create_recipe,
    validate_for_save,
)

BREAKFAST_ID = "412b94fd-8106-40b8-863b-46e2b1f1daff"


def _image_bytes(image_format: str) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (3, 2), color=(20, 110, 175)).save(buffer, format=image_format)
    return buffer.getvalue()


def test_editor_can_upload_replace_remove_and_delete_recipe_image(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "task11-image-route-test-secret")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("UPLOAD_MAX_BYTES", "1024")
    get_settings.cache_clear()

    async def run() -> None:
        app = create_app()
        token = uuid.uuid4().hex[:10]
        user_id = str(uuid.uuid4())
        recipe_id: str | None = None
        alias_path = f"/r/task11-image-{token}"
        image_path: str | None = None
        png = _image_bytes("PNG")
        jpeg = _image_bytes("JPEG")
        try:
            async with session_scope() as db:
                user = User(
                    id=user_id,
                    username=f"task11-image-{token}",
                    password_hash="test-only",
                    role="editor",
                    is_active=True,
                )
                db.add(user)
                category = (
                    await db.execute(select(Category).where(Category.id == BREAKFAST_ID))
                ).scalar_one()
                recipe = await create_recipe(
                    db,
                    validate_for_save(
                        RecipeDraft(
                            title=f"Task 11 image {token}",
                            sections=[
                                RecipeSectionDraft(
                                    heading="Ingredients",
                                    kind="ingredients",
                                    lines=[RecipeIngredientDraft(raw_text="1 cup flour")],
                                )
                            ],
                        ),
                        category_names=[category.name],
                    ),
                    category_ids=[category.id],
                    actor_user_id=user_id,
                )
                recipe_id = recipe.id

            raw_token, auth_session = await create_session(
                user_id, settings=app.state.settings
            )
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                client.cookies.set(session_cookie_name(secure=False), raw_token)
                csrf = auth_session.csrf_token
                form = await client.get(f"/recipes/{recipe_id}/edit")
                assert form.status_code == 200
                assert "No image added yet" in form.text

                no_csrf = await client.post(
                    f"/recipes/{recipe_id}/image",
                    files={"image": ("photo.png", png, "image/png")},
                )
                assert no_csrf.status_code == 403

                uploaded = await client.post(
                    f"/recipes/{recipe_id}/image",
                    data={"csrf_token": csrf},
                    files={"image": ("misleading.jpg", png, "image/jpeg")},
                )
                assert uploaded.status_code == 303
                assert uploaded.headers["location"].endswith("?image=saved")
                async with session_scope() as db:
                    recipe = await db.get(Recipe, recipe_id)
                    image_path = recipe.image_path
                    assert image_path and image_path.endswith(".png")
                    assert recipe.title == f"Task 11 image {token}"
                stored_file = tmp_path / "media" / image_path
                assert stored_file.read_bytes() == png
                too_large_file = await client.post(
                    f"/recipes/{recipe_id}/image",
                    data={"csrf_token": csrf},
                    files={"image": ("large.png", b"x" * 1025, "image/png")},
                )
                assert too_large_file.status_code == 413
                assert stored_file.read_bytes() == png

                replacement = await client.post(
                    f"/recipes/{recipe_id}/image",
                    data={"csrf_token": csrf},
                    files={"image": ("replacement.webp", jpeg, "image/webp")},
                )
                assert replacement.status_code == 303
                assert replacement.headers["location"].endswith("?image=saved")
                old_path = image_path
                async with session_scope() as db:
                    recipe = await db.get(Recipe, recipe_id)
                    image_path = recipe.image_path
                    assert image_path and image_path.endswith(".jpg")
                assert not (tmp_path / "media" / old_path).exists()
                assert (await client.get(f"/media/{old_path}")).status_code == 404
                updated_form = await client.get(replacement.headers["location"])
                assert f"/media/{image_path}" in updated_form.text
                assert "Recipe image saved." in updated_form.text

                client.cookies.clear()
                served = await client.get(f"/media/{image_path}")
                assert served.status_code == 200
                assert served.content == jpeg
                assert served.headers["content-type"] == "image/jpeg"
                assert served.headers["x-content-type-options"] == "nosniff"
                assert served.headers["cache-control"] == "public, max-age=31536000, immutable"
                assert (await client.get("/media/../secret.png")).status_code == 404
                client.cookies.set(session_cookie_name(secure=False), raw_token)

                invalid = await client.post(
                    f"/recipes/{recipe_id}/image",
                    data={"csrf_token": csrf},
                    files={"image": ("invalid.png", b"not an image", "image/png")},
                )
                assert invalid.status_code == 303
                assert invalid.headers["location"].endswith("?image=invalid")
                assert (tmp_path / "media" / image_path).read_bytes() == jpeg
                async with session_scope() as db:
                    recipe = await db.get(Recipe, recipe_id)
                    assert recipe.image_path == image_path

                removed = await client.post(
                    f"/recipes/{recipe_id}/image/remove", data={"csrf_token": csrf}
                )
                assert removed.status_code == 303
                assert removed.headers["location"].endswith("?image=removed")
                assert not (tmp_path / "media" / image_path).exists()
                async with session_scope() as db:
                    recipe = await db.get(Recipe, recipe_id)
                    assert recipe.image_path is None

                reuploaded = await client.post(
                    f"/recipes/{recipe_id}/image",
                    data={"csrf_token": csrf},
                    files={"image": ("new.png", png, "image/png")},
                )
                assert reuploaded.status_code == 303
                async with session_scope() as db:
                    recipe = await db.get(Recipe, recipe_id)
                    image_path = recipe.image_path
                    assert image_path and image_path.endswith(".png")

                async with session_scope() as db:
                    db.add(RecipeUrlAlias(path=alias_path, recipe_id=recipe_id))
                blocked = await client.post(
                    f"/recipes/{recipe_id}/delete", data={"csrf_token": csrf}
                )
                assert blocked.status_code == 409
                assert (tmp_path / "media" / image_path).exists()

                async with session_scope() as db:
                    await db.execute(delete(RecipeUrlAlias).where(RecipeUrlAlias.recipe_id == recipe_id))
                deleted = await client.post(
                    f"/recipes/{recipe_id}/delete", data={"csrf_token": csrf}
                )
                assert deleted.status_code == 303
                assert not (tmp_path / "media" / image_path).exists()
                async with session_scope() as db:
                    assert await db.get(Recipe, recipe_id) is None
        finally:
            async with session_scope() as db:
                if recipe_id:
                    await db.execute(delete(RecipeUrlAlias).where(RecipeUrlAlias.recipe_id == recipe_id))
                    recipe = await db.get(Recipe, recipe_id)
                    if recipe:
                        await db.delete(recipe)
                await db.execute(delete(Session).where(Session.user_id == user_id))
                await db.execute(delete(User).where(User.id == user_id))
            await get_engine().dispose()
            get_settings.cache_clear()

    asyncio.run(run())
