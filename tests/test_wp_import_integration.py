"""Importer apply/resume behavior on the explicitly bound disposable DB."""
from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from pathlib import Path

from PIL import Image
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test_wp_import import WXR_FIXTURE
from cookbook.config import cli_config
from cookbook.models import Category, MigrationRecipeLog, MigrationRun, Recipe, RecipeUrlAlias, recipe_categories
from cookbook.wp_import import run_wp_import, verify_wp_run


def _full_fixture() -> tuple[bytes, set[str], set[str]]:
    token = uuid.uuid4().int % 80_000_000 + 10_000_000
    post_ids = {str(token), str(token + 1)}
    page_ids = {slug: str(token + 2 + index) for index, slug in enumerate((
        "main-dishes", "drinks", "desserts", "side-dishes", "appetizers",
        "baking", "fermentation", "breakfast",
    ))}
    fixture = WXR_FIXTURE.replace(b"<wp:post_id>100</wp:post_id>", f"<wp:post_id>{token}</wp:post_id>".encode())
    fixture = fixture.replace(b"<wp:post_id>101</wp:post_id>", f"<wp:post_id>{token + 1}</wp:post_id>".encode())
    fixture = fixture.replace(b"<wp:post_id>16</wp:post_id>", f"<wp:post_id>{page_ids['breakfast']}</wp:post_id>".encode())
    fixture = fixture.replace(
        b'<category domain="category" nicename="bbq">BBQ</category>\n  </item>',
        b'<category domain="category" nicename="bbq">BBQ</category>'
        b'<wp:postmeta><wp:meta_key>_thumbnail_id</wp:meta_key><wp:meta_value>502</wp:meta_value></wp:postmeta>\n  </item>',
    )
    titles = {
        "main-dishes": "Main Dishes", "drinks": "Drinks", "desserts": "Desserts",
        "side-dishes": "Side Dishes", "appetizers": "Appetizers", "baking": "Baking",
        "fermentation": "Fermentation",
    }
    pages = "".join(
        f"<item><title>{title}</title><wp:post_id>{page_ids[slug]}</wp:post_id>"
        "<wp:post_type>page</wp:post_type><wp:status>publish</wp:status></item>"
        for slug, title in titles.items()
    ).encode()
    second_attachment = (
        b'<item><title>ribs-original.jpg</title><link>https://cookbook.maynardfolks.com/uploads/ribs-original.jpg</link>'
        b'<wp:post_id>502</wp:post_id><wp:post_type>attachment</wp:post_type>'
        b'<wp:attachment_url>https://cookbook.maynardfolks.com/uploads/ribs-original.jpg</wp:attachment_url></item>'
    )
    return fixture.replace(b" </channel>", b" " + pages + second_attachment + b"</channel>"), post_ids, set(page_ids.values())


def test_apply_resume_and_verify_on_isolated_mariadb(monkeypatch) -> None:
    workspace = Path.cwd().resolve()
    work_dir = workspace / f".task19-integration-{uuid.uuid4().hex}"
    if not work_dir.is_relative_to(workspace) or work_dir.exists():
        raise RuntimeError("Refusing an unsafe or reused integration-test workspace path.")
    work_dir.mkdir()
    content, post_ids, page_ids = _full_fixture()
    source = work_dir / "fixture.xml"
    source.write_bytes(content)
    media_dir = work_dir / "media"
    buffer = io.BytesIO()
    Image.new("RGB", (3, 3), "red").save(buffer, format="JPEG")
    jpeg = buffer.getvalue()

    async def fake_fetch(url: str, *, max_bytes: int):
        if url.endswith("ribs-original.jpg"):
            return b"not an image", "image/jpeg"
        return jpeg, "image/jpeg"

    monkeypatch.setattr("cookbook.wp_import.fetch_public_bytes", fake_fetch)

    async def exercise() -> None:
        settings = cli_config()
        if (settings.db_host, settings.db_port, settings.db_name) != ("127.0.0.1", 33061, "cookbook"):
            raise RuntimeError("Importer DB tests require the isolated MariaDB at 127.0.0.1:33061/cookbook.")
        engine = create_async_engine(settings.database_url, pool_pre_ping=True)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        had_others = False
        try:
            async with factory() as session:
                had_others = await session.scalar(select(Category.id).where(Category.slug == "others")) is not None

            dry_run = await run_wp_import(factory, source, media_dir=media_dir)
            assert dry_run["mode"] == "dry-run"
            assert dry_run["published_posts"] == 2
            assert dry_run["missing_category_pages"] == []
            async with factory() as session:
                assert await session.scalar(select(MigrationRun.id).where(
                    MigrationRun.file_sha256 == hashlib.sha256(content).hexdigest()
                )) is None
                assert (await session.scalar(select(Category.id).where(Category.slug == "others")) is not None) == had_others

            first = await run_wp_import(factory, source, media_dir=media_dir, apply=True)
            assert first["recipes_imported"] == 2 and first["recipes_errors"] == 0
            assert first["images_ok"] == 1 and first["images_failed"] == 1

            second = await run_wp_import(factory, source, media_dir=media_dir, apply=True, resume=True)
            assert second["recipes_imported"] == 0 and second["recipes_skipped"] == 2
            assert second["recipes_errors"] == 0

            verified = await verify_wp_run(factory, second["run_id"], media_dir=media_dir)
            assert verified["passed"] is True
            first_verified = await verify_wp_run(factory, first["run_id"], media_dir=media_dir)
            assert first_verified["passed"] is True
            assert first_verified["checks"]["image_accounting_matches"] is True
            assert len(first_verified["checks"]["image_failures"]) == 1

            async with factory() as session:
                recipes = list((await session.execute(select(Recipe).where(
                    Recipe.source_system == "wordpress", Recipe.source_id.in_(post_ids),
                ))).scalars())
                assert len(recipes) == 2
                assert sum(bool(recipe.image_path) for recipe in recipes) == 1
        finally:
            digest = hashlib.sha256(content).hexdigest()
            async with factory() as session:
                async with session.begin():
                    recipe_ids = list((await session.execute(select(Recipe.id).where(
                        Recipe.source_system == "wordpress", Recipe.source_id.in_(post_ids),
                    ))).scalars())
                    if recipe_ids:
                        await session.execute(delete(RecipeUrlAlias).where(RecipeUrlAlias.recipe_id.in_(recipe_ids)))
                    exact_page_paths = [f"/?page_id={page_id}" for page_id in page_ids]
                    await session.execute(delete(RecipeUrlAlias).where(RecipeUrlAlias.path.in_(exact_page_paths)))
                    await session.execute(delete(Recipe).where(
                        Recipe.source_system == "wordpress", Recipe.source_id.in_(post_ids),
                    ))
                    run_ids = select(MigrationRun.id).where(MigrationRun.file_sha256 == digest)
                    await session.execute(delete(MigrationRecipeLog).where(MigrationRecipeLog.run_id.in_(run_ids)))
                    await session.execute(delete(MigrationRun).where(MigrationRun.file_sha256 == digest))
                    if not had_others:
                        created_others = (await session.execute(select(Category.id).where(
                            Category.slug == "others",
                        ))).scalar_one_or_none()
                        if created_others is not None:
                            used_by_recipe = (await session.execute(select(recipe_categories.c.recipe_id).where(
                                recipe_categories.c.category_id == created_others,
                            ).limit(1))).first() is not None
                            used_by_alias = (await session.execute(select(RecipeUrlAlias.id).where(
                                RecipeUrlAlias.category_id == created_others,
                            ).limit(1))).first() is not None
                            if not used_by_recipe and not used_by_alias:
                                await session.execute(delete(Category).where(Category.id == created_others))
            await engine.dispose()

    try:
        asyncio.run(exercise())
    finally:
        recipe_image_dir = media_dir / "recipes"
        if recipe_image_dir.exists():
            for image_path in recipe_image_dir.iterdir():
                if image_path.is_file() and image_path.parent == recipe_image_dir:
                    image_path.unlink()
            recipe_image_dir.rmdir()
            media_dir.rmdir()
        source.unlink(missing_ok=True)
        work_dir.rmdir()
