"""Opt-in end-to-end backup/restore drill against a disposable MariaDB.

Set COOKBOOK_BACKUP_RESTORE_TEST_DB_URL to a MariaDB URL whose database name
starts with ``cookbook_task20_``. The test creates uniquely suffixed sibling
schemas and drops only those schemas after the drill. Run this inside the app
image so the packaged ``mariadb`` and ``mariadb-dump`` clients are available.
No .env file is read by the test or its explicit Alembic configuration.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.engine import make_url

from cookbook import app as app_module
from cookbook import db as db_module
from cookbook.cli_backup import run_backup_command, run_restore_command
from cookbook.config import Settings
from cookbook.models import Category, MigrationRecipeLog, MigrationRun
from cookbook.recipes.drafts import (
    RecipeDraft,
    RecipeIngredientDraft,
    RecipeProvenanceDraft,
    RecipeSectionDraft,
    RecipeStepDraft,
)
from cookbook.recipes.service import create_recipe
from cookbook.recipes.validation import validate_for_save


TEST_DB_URL_ENV = "COOKBOOK_BACKUP_RESTORE_TEST_DB_URL"
OLDER_REVISION = "e4c7a91d2f30"  # v6; v7 adds the full-text indexes
CURRENT_HEAD = "f18a6c4d0e72"


def _explicit_alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.attributes["database_url"] = database_url
    return config


def _settings_for_url(database_url: str, media_dir: Path) -> Settings:
    url = make_url(database_url)
    return Settings(
        _env_file=None,
        APP_NAME="Task 20 restore drill",
        SECRET_KEY="task20-disposable-integration-key",
        DB_HOST=url.host or "127.0.0.1",
        DB_PORT=url.port or 3306,
        DB_NAME=url.database,
        DB_USER=url.username,
        DB_PASSWORD=url.password,
        MEDIA_DIR=media_dir,
    )


async def _create_database(server_url: str, name: str) -> None:
    engine = create_async_engine(server_url, echo=False)
    try:
        identifier = engine.dialect.identifier_preparer.quote(name)
        async with engine.begin() as connection:
            await connection.execute(text(f"CREATE DATABASE {identifier} CHARACTER SET utf8mb4"))
    finally:
        await engine.dispose()


async def _drop_database(server_url: str, name: str) -> None:
    engine = create_async_engine(server_url, echo=False)
    try:
        identifier = engine.dialect.identifier_preparer.quote(name)
        async with engine.begin() as connection:
            await connection.execute(text(f"DROP DATABASE IF EXISTS {identifier}"))
    finally:
        await engine.dispose()


async def _seed_source(settings: Settings, media_dir: Path, slug: str) -> str:
    db_module.get_engine.cache_clear()
    db_module.get_session.cache_clear()
    import io

    from PIL import Image
    from cookbook.recipes.images import store_image

    image_buffer = io.BytesIO()
    Image.new("RGB", (2, 2), color="orange").save(image_buffer, format="PNG")
    image_name = store_image(image_buffer.getvalue(), media_dir, max_bytes=1024).relative_path

    async with db_module.session_scope() as session:
        category = Category(
            id=str(uuid.uuid4()),
            slug=f"task20-{uuid.uuid4().hex[:12]}",
            name="Task 20 Drill",
            position=1000,
        )
        session.add(category)
        await session.flush()
        draft = RecipeDraft(
            title="Task 20 Restore Drill Recipe",
            description="A seeded recipe that survives a real backup and restore.",
            servings_text="Serves two",
            prep_minutes=5,
            cook_minutes=10,
            total_minutes=15,
            tags=["restore-drill"],
            provenance=RecipeProvenanceDraft(
                source_system="wordpress",
                source_id="task20-restore-drill-source",
                source_name="Task 20 disposable fixture",
                source_url="https://example.invalid/task20-restore-drill",
            ),
            sections=[
                RecipeSectionDraft(
                    heading="Ingredients",
                    kind="ingredients",
                    lines=[RecipeIngredientDraft(raw_text="1 cup test beans")],
                ),
                RecipeSectionDraft(
                    heading="Steps",
                    kind="steps",
                    lines=[RecipeStepDraft(body="Cook the test beans until tender.")],
                ),
            ],
        )
        save_data = validate_for_save(draft, category_names=[category.name])
        recipe = await create_recipe(
            session,
            save_data,
            category_ids=[category.id],
            slug=slug,
        )
        recipe.image_path = image_name

        run_id = str(uuid.uuid4())
        session.add(
            MigrationRun(
                id=run_id,
                source_file="task20-disposable-fixture.xml",
                file_sha256=hashlib.sha256(b"task20-disposable-fixture").hexdigest(),
                status="completed",
                recipes_total=1,
                recipes_imported=1,
                images_ok=1,
            )
        )
        await session.flush()
        session.add(
            MigrationRecipeLog(
                id=str(uuid.uuid4()),
                run_id=run_id,
                source_post_id="task20-restore-drill-source",
                recipe_id=recipe.id,
                outcome="imported",
                detail="seeded only for disposable backup/restore verification",
            )
        )

    engine = db_module.get_engine()
    await engine.dispose()
    db_module.get_session.cache_clear()
    db_module.get_engine.cache_clear()
    return image_name


async def _read_revision(database_url: str) -> str:
    engine = create_async_engine(database_url, echo=False)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            return result.scalar_one()
    finally:
        await engine.dispose()


async def _assert_audit_and_request_app(settings: Settings, slug: str, image_name: str) -> None:
    db_module.get_engine.cache_clear()
    db_module.get_session.cache_clear()
    db_module.get_settings = lambda: settings
    app_module.get_settings = lambda: settings
    app = app_module.create_app()
    try:
        async with db_module.session_scope() as session:
            log = (await session.execute(select(MigrationRecipeLog))).scalar_one()
            assert log.source_post_id == "task20-restore-drill-source"
            assert log.outcome == "imported"

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://task20.test"
        ) as client:
            ready = await client.get("/readyz")
            assert ready.status_code == 200, ready.text
            page = await client.get(f"/r/{slug}")
            assert page.status_code == 200, page.text
            assert "Task 20 Restore Drill Recipe" in page.text
            assert f"/media/{image_name}" in page.text
            image = await client.get(f"/media/{image_name}")
            assert image.status_code == 200
            assert image.content.startswith(b"\x89PNG\r\n\x1a\n")
    finally:
        engine = db_module.get_engine()
        await engine.dispose()
        db_module.get_session.cache_clear()
        db_module.get_engine.cache_clear()


def test_backup_restore_drill_round_trips_and_migrates_forward(tmp_path, monkeypatch):
    raw_url = os.environ.get(TEST_DB_URL_ENV)
    if not raw_url:
        pytest.skip(f"set {TEST_DB_URL_ENV} to opt into disposable MariaDB verification")
    if not shutil.which("mariadb") or not shutil.which("mariadb-dump"):
        pytest.skip("run this integration test inside the app image with MariaDB client tools")

    admin_url = make_url(raw_url)
    if admin_url.get_backend_name() != "mysql" or not admin_url.username:
        pytest.fail(f"{TEST_DB_URL_ENV} must be a MariaDB URL with an administrative user")
    if not admin_url.database or not admin_url.database.startswith("cookbook_task20_"):
        pytest.fail(f"{TEST_DB_URL_ENV} database name must start with cookbook_task20_ (safety guard)")

    token = uuid.uuid4().hex[:12]
    source_name = f"cookbook_task20_source_{token}"
    target_name = f"cookbook_task20_target_{token}"
    server_url = admin_url.set(database=None).render_as_string(hide_password=False)
    source_url = admin_url.set(database=source_name).render_as_string(hide_password=False)
    target_url = admin_url.set(database=target_name).render_as_string(hide_password=False)
    created: list[str] = []

    try:
        for name in (source_name, target_name):
            asyncio.run(_create_database(server_url, name))
            created.append(name)

        # Build an old but supported schema, seed representative recipe/media/
        # import-audit rows, and create a paired backup at the older revision.
        command.upgrade(_explicit_alembic_config(source_url), OLDER_REVISION)
        source_media = tmp_path / "source-media"
        source_media.mkdir()
        source_settings = _settings_for_url(source_url, source_media)
        monkeypatch.setattr(db_module, "get_settings", lambda: source_settings)
        monkeypatch.setattr(app_module, "get_settings", lambda: source_settings)
        db_module.get_engine.cache_clear()
        db_module.get_session.cache_clear()
        slug = f"task20-restore-{token}"
        image_name = asyncio.run(_seed_source(source_settings, source_media, slug))

        cli_settings = SimpleNamespace(
            db_host=source_settings.db_host,
            db_port=source_settings.db_port,
            db_name=source_settings.db_name,
            db_user=source_settings.db_user,
            db_password=source_settings.db_password,
            database_url=source_settings.database_url,
            media_dir=source_media,
        )
        monkeypatch.setattr("cookbook.cli_backup.cli_config", lambda: cli_settings)
        backup_dir = tmp_path / "backup"
        backup_dir.mkdir()
        assert run_backup_command(SimpleNamespace(out=backup_dir)) == 0
        sql_path = next(backup_dir.glob("cookbook-*.sql.gz"))
        media_archive = next(backup_dir.glob("media-*.tar.gz"))
        manifest = next(backup_dir.glob("manifest-*.json"))

        target_media = tmp_path / "restored-media"
        target_media.mkdir()
        restore_args = SimpleNamespace(
            sql=sql_path,
            media_archive=media_archive,
            manifest=manifest,
            db_url=target_url,
            media_dir=target_media,
            replace=False,
        )
        restore_status = run_restore_command(restore_args)
        if restore_status != 0:
            from cookbook import cli_backup

            actual_counts, actual_revision = cli_backup._async_db_snapshot(target_url, None)
            expected_manifest = cli_backup.load_manifest(manifest)
            assert actual_revision == expected_manifest["alembic_revision"], (
                "restored Alembic revision mismatch"
            )
            assert actual_counts == expected_manifest["table_row_counts"], (
                "restored table row counts differ from the backup manifest"
            )
        assert restore_status == 0
        assert (target_media / image_name).is_file()
        assert asyncio.run(_read_revision(target_url)) == OLDER_REVISION

        # Restores stay at their recorded revision. The normal forward path
        # then upgrades this v6 target to current head, and check finds no drift.
        target_alembic = _explicit_alembic_config(target_url)
        command.upgrade(target_alembic, "head")
        command.check(target_alembic)
        assert asyncio.run(_read_revision(target_url)) == CURRENT_HEAD

        target_settings = _settings_for_url(target_url, target_media)
        monkeypatch.setattr(db_module, "get_settings", lambda: target_settings)
        monkeypatch.setattr(app_module, "get_settings", lambda: target_settings)
        asyncio.run(_assert_audit_and_request_app(target_settings, slug, image_name))
    finally:
        db_module.get_session.cache_clear()
        db_module.get_engine.cache_clear()
        for name in reversed(created):
            asyncio.run(_drop_database(server_url, name))
