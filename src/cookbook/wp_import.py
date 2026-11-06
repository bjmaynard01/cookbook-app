"""Dry-run-first WordPress WXR import planning and execution."""
from __future__ import annotations

import hashlib
import html
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import (
    Category, MigrationRecipeLog, MigrationRun, Recipe, RecipeItem,
    RecipeSection, RecipeUrlAlias, recipe_categories,
)
from .recipes.drafts import RecipeDraft, RecipeProvenanceDraft
from .recipes.errors import RecipeServiceError
from .recipes.images import delete_stored_image, store_image
from .recipes.ingest import MAX_ASSET_BYTES, FetchError, fetch_public_bytes
from .recipes.service import create_recipe
from .recipes.validation import RecipeValidationError, validate_for_save
from .recipes.slugs import slugify
from .wxr import WxrDocument, WxrPost, parse_wxr

CATEGORY_NAMES = {
    "appetizers": "Appetizers", "baking": "Baking", "breakfast": "Breakfast",
    "desserts": "Desserts", "drinks": "Drinks", "fermentation": "Fermentation",
    "main-dishes": "Main Dishes", "side-dishes": "Side Dishes", "others": "Others",
}
OWNER_REVIEW_POSTS = {
    "1950": "Pinto Bean Recipes is a collection; preserve it as one source post and flag it for owner review.",
}
OWNER_APPROVED_MISSING_INGREDIENT_POSTS = {
    "210": ("Oven Baked Tacos", "Source directions are preserved; no separate ingredient list was published."),
    "331": ("Pumpkin Waffles with Apple Cider Maple Syrup", "Source directions are preserved; no separate ingredient list was published."),
    "342": ("MaMaw Pies", "The published source has no readable body; preserve its title and metadata without inventing ingredients."),
    "870": ("Oven Roasted Sausage & Potatoes", "Source directions are preserved; no separate ingredient list was published."),
}
MISSING_INGREDIENT_EXCEPTION = "missing_ingredient_lines"


def _line_count(post: WxrPost, kind: str) -> int:
    return sum(len(section.lines) for section in post.sections if section.kind == kind)


def _approved_missing_ingredient_reason(post: WxrPost) -> str | None:
    approved = OWNER_APPROVED_MISSING_INGREDIENT_POSTS.get(post.post_id)
    if not approved or html.unescape(post.title) != approved[0]:
        return None
    if _line_count(post, "ingredients") != 0:
        return None
    return "Owner-approved missing-ingredient exception: " + approved[1]


def _owner_review_flags(post: WxrPost) -> list[str]:
    flags = []
    if post.post_id in OWNER_REVIEW_POSTS:
        flags.append(OWNER_REVIEW_POSTS[post.post_id])
    exception = _approved_missing_ingredient_reason(post)
    if exception:
        flags.append(exception)
    return flags


def _allocate_slug(title: str, occupied: set[str]) -> tuple[str, bool]:
    base = slugify(title, fallback="recipe")
    candidate = base
    suffix = 2
    while candidate in occupied:
        end = f"-{suffix}"
        candidate = f"{base[:200 - len(end)].rstrip('-')}{end}"
        suffix += 1
    occupied.add(candidate)
    return candidate, candidate != base


def build_import_report(
    document: WxrDocument,
    *,
    existing_slugs: set[str] | None = None,
    source_ids: set[str] | None = None,
    existing_aliases: set[str] | None = None,
    source_slugs: dict[str, str] | None = None,
    source_recipe_ids: dict[str, str] | None = None,
    category_ids: dict[str, str] | None = None,
    alias_targets: dict[str, tuple[str | None, str | None]] | None = None,
) -> dict[str, Any]:
    """Build the reviewable, side-effect-free report for one WXR document."""
    occupied = set(existing_slugs or ())
    source_ids = source_ids or set()
    aliases = existing_aliases or set()
    source_slugs = source_slugs or {}
    source_recipe_ids = source_recipe_ids or {}
    category_ids = category_ids or {}
    alias_targets = alias_targets or {}
    occupied.update(
        path.removeprefix("/r/") for path in aliases if path.startswith("/r/")
    )
    rows: list[dict[str, Any]] = []
    for post in document.recipes:
        if post.post_id in source_ids and post.post_id in source_slugs:
            slug, collision = source_slugs[post.post_id], False
        else:
            slug, collision = _allocate_slug(post.title, occupied)
        review_flags = _owner_review_flags(post)
        warnings = list(post.warnings) + review_flags
        approved_exceptions = (
            [MISSING_INGREDIENT_EXCEPTION]
            if _approved_missing_ingredient_reason(post) else []
        )
        rows.append({
            "source_post_id": post.post_id,
            "title": post.title,
            "slug": slug,
            "slug_collision": collision,
            "source_slug": post.source_slug,
            "slug_regenerated": post.needs_slug_regeneration or slug != post.source_slug,
            "action": "skip" if post.post_id in source_ids else "import",
            "fields_found": {
                "title": True,
                "description": post.description is not None,
                "ingredient_lines": _line_count(post, "ingredients"),
                "step_lines": _line_count(post, "steps"),
                "image": post.image_url is not None,
                "servings": False,
                "times": False,
                "published_date": post.published_date.isoformat() if post.published_date else None,
            },
            "fields_empty": list(post.empty_fields),
            "categories": [
                {"slug": category, "evidence": list(post.category_evidence)}
                for category in post.category_slugs
            ],
            "legacy_path": post.legacy_path,
            "alias_conflict": post.legacy_path in aliases and (
                alias_targets.get(post.legacy_path, (None, None))[0]
                != source_recipe_ids.get(post.post_id)
            ),
            "image_url": post.image_url,
            "warnings": warnings,
            "review_flags": review_flags,
            "approved_exceptions": approved_exceptions,
        })
    pages = [
        {"source_id": source_id, "category": slug, "path": path,
         "alias_conflict": path in aliases and alias_targets.get(path, (None, None))[1] != category_ids.get(slug)}
        for source_id, slug, path in document.category_pages
    ]
    required_pages = set(CATEGORY_NAMES) - {"others"}
    present = {row["category"] for row in pages}
    return {
        "source_file": document.source_name,
        "published_posts": len(document.recipes),
        "all_posts": document.total_post_count,
        "drafts_excluded": len(document.excluded_posts),
        "recipes": rows,
        "category_pages": pages,
        "missing_category_pages": sorted(required_pages - present),
        "owner_review_flags": [
            {"source_post_id": row["source_post_id"], "title": row["title"], "detail": flag}
            for row in rows for flag in row["review_flags"]
        ],
        "approved_exceptions": [
            {"source_post_id": row["source_post_id"], "title": row["title"], "code": exception}
            for row in rows for exception in row["approved_exceptions"]
        ],
        "slug_collisions": [
            {"source_post_id": row["source_post_id"], "title": row["title"], "allocated_slug": row["slug"]}
            for row in rows if row["slug_collision"]
        ],
        "images_present": sum(bool(row["fields_found"]["image"]) for row in rows),
    }


def write_import_report(path: Path, report: dict[str, Any]) -> None:
    """Atomically write the complete JSON report."""
    destination = path.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", prefix=f".{destination.name}-", suffix=".tmp",
            dir=destination.parent, delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def _database_report(session: AsyncSession, document: WxrDocument) -> dict[str, Any]:
    slugs = set((await session.execute(select(Recipe.slug))).scalars())
    source_rows = (await session.execute(
        select(Recipe.source_id, Recipe.id, Recipe.slug).where(Recipe.source_system == "wordpress")
    )).all()
    categories = (await session.execute(select(Category.slug, Category.id))).all()
    alias_rows = (await session.execute(select(
        RecipeUrlAlias.path, RecipeUrlAlias.recipe_id, RecipeUrlAlias.category_id,
    ))).all()
    source_ids = {row.source_id for row in source_rows if row.source_id is not None}
    source_recipe_ids = {row.source_id: row.id for row in source_rows if row.source_id is not None}
    source_slugs = {row.source_id: row.slug for row in source_rows if row.source_id is not None}
    category_ids = {row.slug: row.id for row in categories}
    alias_targets = {row.path: (row.recipe_id, row.category_id) for row in alias_rows}
    return build_import_report(
        document, existing_slugs=slugs, source_ids=source_ids,
        existing_aliases=set(alias_targets), source_slugs=source_slugs,
        source_recipe_ids=source_recipe_ids, category_ids=category_ids,
        alias_targets=alias_targets,
    )


async def _ensure_categories(session: AsyncSession) -> dict[str, Category]:
    rows = list((await session.execute(select(Category).order_by(Category.position))).scalars())
    by_slug = {row.slug: row for row in rows}
    if "others" not in by_slug:
        by_slug["others"] = Category(
            slug="others", name="Others", position=max((row.position for row in rows), default=-1) + 1,
        )
        session.add(by_slug["others"])
        await session.flush()
    missing = set(CATEGORY_NAMES) - set(by_slug)
    if missing:
        raise RuntimeError("Required seeded categories are missing: " + ", ".join(sorted(missing)))
    return by_slug


async def _alias_if_free(
    session: AsyncSession, path: str, *, recipe_id: str | None = None, category_id: str | None = None,
) -> None:
    prior = (await session.execute(
        select(RecipeUrlAlias).where(RecipeUrlAlias.path == path).with_for_update()
    )).scalar_one_or_none()
    if prior:
        if (prior.recipe_id, prior.category_id) != (recipe_id, category_id):
            raise ValueError(f"Legacy alias already targets a different record: {path}")
        return
    session.add(RecipeUrlAlias(path=path, recipe_id=recipe_id, category_id=category_id))
    await session.flush()


def _validated_data(post: WxrPost, *, allow_missing_ingredients_for_import: bool = False):
    draft = RecipeDraft(
        title=post.title,
        description=post.description,
        sections=list(post.sections),
        provenance=RecipeProvenanceDraft(
            source_system="wordpress", source_id=post.post_id,
            source_url=post.source_url or f"https://cookbook.maynardfolks.com{post.legacy_path}",
            source_name="WordPress export", extraction_method="wxr",
            imported_at=datetime.now(timezone.utc).replace(tzinfo=None),
        ),
    )
    return validate_for_save(
        draft,
        category_names=[CATEGORY_NAMES[slug] for slug in post.category_slugs],
        allow_missing_ingredients_for_import=allow_missing_ingredients_for_import,
    )


async def _fetch_store_image(post: WxrPost, media_dir: Path) -> tuple[str | None, str | None]:
    if not post.image_url:
        return None, None
    try:
        content, _content_type = await fetch_public_bytes(post.image_url, max_bytes=MAX_ASSET_BYTES)
        stored = store_image(content, media_dir, max_bytes=MAX_ASSET_BYTES)
        return stored.relative_path, None
    except (FetchError, OSError, ValueError) as exc:
        return None, type(exc).__name__


async def run_wp_import(
    factory: async_sessionmaker[AsyncSession],
    file: Path,
    *,
    media_dir: Path,
    apply: bool = False,
    resume: bool = False,
    report_path: Path | None = None,
    operator_id: str | None = None,
) -> dict[str, Any]:
    """Parse/report by default; import only after explicit ``apply=True``."""
    if resume and not apply:
        raise ValueError("--resume requires --apply")
    if report_path is not None and report_path.resolve() == file.resolve():
        raise ValueError("Report destination must not overwrite the source WXR file.")
    document, contents = parse_wxr(file)
    digest = hashlib.sha256(contents).hexdigest()
    async with factory() as session:
        report = await _database_report(session, document)
    report["file_sha256"] = digest
    report["mode"] = "apply" if apply else "dry-run"
    if report_path is not None:
        write_import_report(report_path, report)
    if not apply:
        return report

    unapproved_missing_ingredients = [
        post.post_id for post in document.recipes
        if _line_count(post, "ingredients") == 0
        and _approved_missing_ingredient_reason(post) is None
    ]
    if unapproved_missing_ingredients:
        raise ValueError(
            "Posts without ingredient lines need an owner-approved exception before --apply: "
            + ", ".join(unapproved_missing_ingredients[:20])
        )

    async with factory() as session:
        available = set((await session.execute(select(Category.slug))).scalars())
    missing_db_categories = (set(CATEGORY_NAMES) - {"others"}) - available
    if missing_db_categories:
        raise ValueError("Required seeded categories are missing from the database: " + ", ".join(sorted(missing_db_categories)))
    if report["missing_category_pages"]:
        raise ValueError("WXR is missing category pages: " + ", ".join(report["missing_category_pages"]))

    collisions = [row["legacy_path"] for row in report["recipes"] if row["alias_conflict"]]
    collisions += [row["path"] for row in report["category_pages"] if row["alias_conflict"]]
    if collisions:
        raise ValueError("Legacy alias conflicts must be reviewed: " + ", ".join(collisions[:10]))

    async with factory() as session:
        async with session.begin():
            run = MigrationRun(
                source_file=str(file), file_sha256=digest, status="running",
                recipes_total=len(document.recipes), recipes_imported=0, recipes_skipped=0,
                recipes_errors=0, images_ok=0, images_failed=0, operator_id=operator_id,
                notes="resume requested" if resume else None,
            )
            session.add(run)
            await session.flush()
            run_id = run.id
    async with factory() as session:
        async with session.begin():
            categories = await _ensure_categories(session)
            for source_id, slug, path in document.category_pages:
                await _alias_if_free(session, path, category_id=categories[slug].id)

    imported = skipped = errors = images_ok = images_failed = 0
    for post in document.recipes:
        image_path: str | None = None
        try:
            async with factory() as check_session:
                existing = (await check_session.execute(select(Recipe.id).where(
                    Recipe.source_system == "wordpress", Recipe.source_id == post.post_id,
                ))).scalar_one_or_none()
                prior_logged = (await check_session.execute(
                    select(MigrationRecipeLog.id)
                    .join(MigrationRun, MigrationRun.id == MigrationRecipeLog.run_id)
                    .where(
                        MigrationRun.file_sha256 == digest,
                        MigrationRecipeLog.source_post_id == post.post_id,
                        MigrationRecipeLog.outcome.in_(["imported", "skipped"]),
                    ).limit(1)
                )).first() is not None
            if existing or prior_logged:
                async with factory() as session:
                    async with session.begin():
                        session.add(MigrationRecipeLog(
                            run_id=run_id, source_post_id=post.post_id, recipe_id=existing,
                            outcome="skipped", detail="WordPress source ID already imported or logged.",
                        ))
                skipped += 1
            else:
                image_path, image_error = await _fetch_store_image(post, media_dir)
                async with factory() as session:
                    async with session.begin():
                        categories = await _ensure_categories(session)
                        approved_exception = _approved_missing_ingredient_reason(post)
                        recipe = await create_recipe(
                            session, _validated_data(
                                post, allow_missing_ingredients_for_import=approved_exception is not None,
                            ),
                            category_ids=[categories[slug].id for slug in post.category_slugs],
                            slug=next(row["slug"] for row in report["recipes"] if row["source_post_id"] == post.post_id),
                            allow_missing_ingredients_for_import=approved_exception is not None,
                        )
                        recipe.published_date = post.published_date
                        recipe.image_path = image_path
                        await _alias_if_free(session, post.legacy_path, recipe_id=recipe.id)
                        detail = {"image": "failed" if image_error else ("ok" if image_path else "none"),
                                  "image_path": image_path, "image_error": image_error,
                                  "warnings": list(post.warnings),
                                  "approved_exceptions": (
                                      [MISSING_INGREDIENT_EXCEPTION] if approved_exception else []
                                  )}
                        detail["warnings"].extend(_owner_review_flags(post))
                        session.add(MigrationRecipeLog(
                            run_id=run_id, source_post_id=post.post_id, recipe_id=recipe.id,
                            outcome="imported", detail=json.dumps(detail, ensure_ascii=False),
                        ))
                imported += 1
                images_ok += int(bool(image_path))
                images_failed += int(bool(image_error))
        except (RecipeValidationError, RecipeServiceError, ValueError, OSError, FetchError, SQLAlchemyError) as exc:
            if image_path:
                delete_stored_image(media_dir, image_path)
            errors += 1
            async with factory() as session:
                async with session.begin():
                    session.add(MigrationRecipeLog(
                        run_id=run_id, source_post_id=post.post_id, recipe_id=None,
                        outcome="error", detail=json.dumps({"error": type(exc).__name__}),
                    ))
        async with factory() as session:
            async with session.begin():
                await session.execute(update(MigrationRun).where(MigrationRun.id == run_id).values(
                    recipes_imported=imported, recipes_skipped=skipped, recipes_errors=errors,
                    images_ok=images_ok, images_failed=images_failed,
                ))

    finished = datetime.now(timezone.utc).replace(tzinfo=None)
    async with factory() as session:
        async with session.begin():
            await session.execute(update(MigrationRun).where(MigrationRun.id == run_id).values(
                status="failed" if errors else "completed", finished_at=finished,
                recipes_imported=imported, recipes_skipped=skipped, recipes_errors=errors,
                images_ok=images_ok, images_failed=images_failed,
            ))
    report.update({
        "run_id": run_id, "recipes_imported": imported, "recipes_skipped": skipped,
        "recipes_errors": errors, "images_ok": images_ok, "images_failed": images_failed,
        "status": "failed" if errors else "completed",
    })
    if report_path is not None:
        write_import_report(report_path, report)
    return report


async def retry_failed_wp_images(
    factory: async_sessionmaker[AsyncSession],
    run_id: str,
    *,
    media_dir: Path,
    report_path: Path | None = None,
) -> dict[str, Any]:
    """Retry only failed original-image fetches for a completed import run.

    Recipe text and metadata are never rewritten. A successful retry stores
    the validated image and updates that post's existing migration audit entry.
    """
    async with factory() as session:
        run = await session.get(MigrationRun, run_id)
        if run is None:
            raise ValueError("Migration run was not found.")
        if run.status != "completed":
            raise ValueError("Only a completed migration run can retry images.")
        if not run.source_file:
            raise ValueError("Migration run has no source WXR path.")
        source = Path(run.source_file)
        digest = run.file_sha256
        logs = list((await session.execute(select(MigrationRecipeLog).where(
            MigrationRecipeLog.run_id == run_id,
            MigrationRecipeLog.outcome == "imported",
        ))).scalars())

    if not source.is_file():
        raise ValueError("The source WXR file is unavailable for image retry.")
    document, contents = parse_wxr(source)
    if hashlib.sha256(contents).hexdigest() != digest:
        raise ValueError("The source WXR changed since the migration run; image retry refused.")
    posts = {post.post_id: post for post in document.recipes}
    retried = 0

    for original_log in logs:
        try:
            detail = json.loads(original_log.detail or "{}")
        except (TypeError, ValueError) as exc:
            raise ValueError("A migration audit entry is not valid JSON; image retry refused.") from exc
        if detail.get("image") != "failed":
            continue
        post_id = str(original_log.source_post_id)
        post = posts.get(post_id)
        if post is None or not post.image_url:
            raise ValueError(f"Failed image audit for source post {post_id} does not match the WXR.")

        async with factory() as session:
            current_path = (await session.execute(select(Recipe.image_path).where(
                Recipe.source_system == "wordpress", Recipe.source_id == post_id,
            ))).scalar_one_or_none()
        if current_path:
            continue

        stored_path, image_error = await _fetch_store_image(post, media_dir)
        if image_error or not stored_path:
            continue
        raced_with_edit = False
        try:
            async with factory() as session:
                async with session.begin():
                    recipe = (await session.execute(select(Recipe).where(
                        Recipe.source_system == "wordpress", Recipe.source_id == post_id,
                    ).with_for_update())).scalar_one_or_none()
                    log = await session.get(MigrationRecipeLog, original_log.id)
                    if recipe is None or log is None:
                        raise ValueError(f"Imported recipe or audit entry for source post {post_id} disappeared.")
                    if recipe.image_path:
                        raced_with_edit = True
                    else:
                        recipe.image_path = stored_path
                        detail.update({"image": "ok", "image_path": stored_path, "image_error": None})
                        log.detail = json.dumps(detail, ensure_ascii=False)
                        retried += 1
        except Exception:
            delete_stored_image(media_dir, stored_path)
            raise
        if raced_with_edit:
            delete_stored_image(media_dir, stored_path)

    async with factory() as session:
        async with session.begin():
            run = await session.get(MigrationRun, run_id)
            logs = list((await session.execute(select(MigrationRecipeLog).where(
                MigrationRecipeLog.run_id == run_id,
                MigrationRecipeLog.outcome == "imported",
            ))).scalars())
            details = [json.loads(log.detail or "{}") for log in logs]
            run.images_ok = sum(detail.get("image") == "ok" for detail in details)
            run.images_failed = sum(detail.get("image") == "failed" for detail in details)
            failures = [
                {"source_post_id": log.source_post_id, "error": detail.get("image_error")}
                for log, detail in zip(logs, details)
                if detail.get("image") == "failed"
            ]
            result = {
                "run_id": run_id,
                "images_retried": retried,
                "images_ok": run.images_ok,
                "images_failed": run.images_failed,
                "failures": failures,
                "status": "completed" if not failures else "partial",
            }
    if report_path is not None:
        write_import_report(report_path, result)
    return result


async def verify_wp_run(
    factory: async_sessionmaker[AsyncSession], run_id: str, *, media_dir: Path | None = None,
) -> dict[str, Any]:
    """Verify WXR counts, imported fields, aliases, images, and resume behavior."""
    async with factory() as session:
        run = await session.get(MigrationRun, run_id)
        if run is None:
            raise ValueError("Migration run was not found.")
        logs = list((await session.execute(select(MigrationRecipeLog).where(
            MigrationRecipeLog.run_id == run_id,
        ))).scalars())
        checks: dict[str, Any] = {
            "run_completed": run.status == "completed",
            "run_has_no_errors": run.recipes_errors == 0,
            "log_count": len(logs),
            "recipes_total": run.recipes_total,
            "run_accounting": run.recipes_imported + run.recipes_skipped + run.recipes_errors == run.recipes_total,
        }
        source = Path(run.source_file) if run.source_file else None
        if source is None or not source.is_file():
            checks["source_file_available"] = False
            return {"run_id": run_id, "checks": checks, "passed": False}
        document, contents = parse_wxr(source)
        checks["source_file_available"] = True
        checks["file_hash_matches"] = hashlib.sha256(contents).hexdigest() == run.file_sha256
        checks["published_count_matches"] = len(document.recipes) == run.recipes_total
        logged = {str(log.source_post_id) for log in logs if log.source_post_id is not None}
        checks["every_published_post_logged"] = logged == {post.post_id for post in document.recipes}
        checks["one_log_per_published_post"] = len(logs) == len(document.recipes)

        alias_checks: list[bool] = []
        data_checks: list[bool] = []
        approved_exception_posts = {
            post.post_id: _approved_missing_ingredient_reason(post)
            for post in document.recipes
            if _approved_missing_ingredient_reason(post)
        }
        imported_ids = {str(log.source_post_id) for log in logs if log.outcome == "imported"}
        for post in document.recipes:
            recipe_id = (await session.execute(select(Recipe.id).where(
                Recipe.source_system == "wordpress", Recipe.source_id == post.post_id,
            ))).scalar_one_or_none()
            if recipe_id is None:
                alias_checks.append(False)
                data_checks.append(False)
                continue
            has_alias = (await session.execute(select(RecipeUrlAlias.id).where(
                RecipeUrlAlias.recipe_id == recipe_id, RecipeUrlAlias.path == post.legacy_path,
            ))).first() is not None
            category_count = (await session.execute(select(func.count()).select_from(
                recipe_categories
            ).where(recipe_categories.c.recipe_id == recipe_id))).scalar_one()
            ingredient_count = (await session.execute(
                select(func.count(RecipeItem.id)).join(
                    RecipeSection, RecipeSection.id == RecipeItem.section_id,
                ).where(RecipeSection.recipe_id == recipe_id, RecipeSection.kind == "ingredients")
            )).scalar_one()
            alias_checks.append(has_alias)
            data_checks.append(
                category_count > 0
                and (ingredient_count > 0 or post.post_id in approved_exception_posts)
            )
        category_map = {row.slug: row.id for row in (await session.execute(select(Category))).scalars()}
        for _source_id, slug, path in document.category_pages:
            alias = (await session.execute(select(RecipeUrlAlias).where(RecipeUrlAlias.path == path))).scalar_one_or_none()
            alias_checks.append(alias is not None and alias.category_id == category_map.get(slug))
        checks["every_recipe_and_category_has_alias"] = all(alias_checks)
        checks["every_recipe_has_category_and_raw_ingredients"] = all(data_checks)
        exception_audits = (await session.execute(
            select(MigrationRecipeLog.source_post_id, MigrationRecipeLog.detail)
            .join(MigrationRun, MigrationRun.id == MigrationRecipeLog.run_id)
            .where(
                MigrationRun.file_sha256 == run.file_sha256,
                MigrationRecipeLog.outcome == "imported",
                MigrationRecipeLog.source_post_id.in_(approved_exception_posts),
            )
        )).all() if approved_exception_posts else []
        audited_exceptions = set()
        for source_post_id, detail in exception_audits:
            try:
                exception_codes = json.loads(detail or "{}").get("approved_exceptions", [])
            except (TypeError, ValueError):
                exception_codes = []
            if MISSING_INGREDIENT_EXCEPTION in exception_codes:
                audited_exceptions.add(str(source_post_id))
        checks["approved_exceptions_logged"] = audited_exceptions == set(approved_exception_posts)

        expected_images = sum(bool(post.image_url) for post in document.recipes if post.post_id in imported_ids)
        image_details = [
            json.loads(log.detail) for log in logs
            if log.outcome == "imported" and log.detail
        ]
        logged_ok = sum(detail.get("image") == "ok" for detail in image_details)
        failed_details = [detail for detail in image_details if detail.get("image") == "failed"]
        logged_failed = len(failed_details)
        checks["image_accounting_matches"] = (
            run.images_ok == logged_ok and run.images_failed == logged_failed
            and logged_ok + logged_failed == expected_images
        )
        checks["stored_images_exist"] = True
        if media_dir is not None:
            from .recipes.images import resolve_stored_image
            for detail in image_details:
                if detail.get("image") == "ok":
                    checks["stored_images_exist"] &= resolve_stored_image(media_dir, detail.get("image_path", "")) is not None
        checks["image_failures"] = [
            {"source_post_id": log.source_post_id, "url": next(
                (post.image_url for post in document.recipes if post.post_id == log.source_post_id), None
            ), "error": detail.get("image_error")}
            for log, detail in zip(
                [log for log in logs if log.outcome == "imported" and log.detail], image_details,
            ) if detail.get("image") == "failed"
        ]
        same_hash_runs = list((await session.execute(select(MigrationRun).where(
            MigrationRun.file_sha256 == run.file_sha256,
            MigrationRun.id != run.id,
        ))).scalars())
        checks["second_run_all_skipped"] = any(
            other.recipes_imported == 0 and other.recipes_errors == 0
            and other.recipes_skipped == other.recipes_total
            for other in same_hash_runs
        ) or (run.recipes_imported == 0 and run.recipes_errors == 0
              and run.recipes_skipped == run.recipes_total and bool(same_hash_runs))
    required = [
        value for key, value in checks.items()
        if key not in {"log_count", "recipes_total", "image_failures"}
    ]
    passed = all(required)
    return {"run_id": run_id, "checks": checks, "passed": passed}
