"""Authenticated, server-rendered recipe editor routes."""
from __future__ import annotations

import asyncio
import re
import logging
from datetime import UTC, datetime
from functools import partial
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import anyio
import httpx
from fastapi import APIRouter, Depends, File, Request, UploadFile
from sqlalchemy import or_, select
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates

from ..auth.roles import require_csrf, require_role
from ..auth.sessions import LoadedSession
from ..config import get_settings
from ..db import session_scope
from ..models import (
    Category,
    Recipe,
    Tag,
    User,
    recipe_categories,
    recipe_tags,
)
from .drafts import (
    DuplicateRecipeDraft,
    RecipeDraft,
    RecipeIngredientDraft,
    RecipeProvenanceDraft,
    RecipeSectionDraft,
    RecipeStepDraft,
)
from .errors import (
    InvalidRecipeInput,
    RecipeAliasConflict,
    RecipeHasAliases,
    RecipeNotFound,
    UnknownCategories,
)
from .ingest import FetchError, fetch_page, fetch_public_bytes
from .llm import parse_recipe_page_with_fallback
from .images import InvalidRecipeImage, delete_stored_image, resolve_stored_image, store_image
from .service import (
    create_recipe,
    delete_recipe,
    get_recipe,
    set_recipe_image,
    update_recipe,
)
from .validation import RecipeValidationError, validate_for_save

router = APIRouter()
log = logging.getLogger("cookbook.recipes.images")
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")
_editor = require_role("editor")
INGEST_TIMEOUT_SECONDS = 30.0
_SECTION_FIELD = re.compile(r"^sections\[(\d+)\]\[(heading|kind)\]$")
_LINE_FIELD = re.compile(
    r"^sections\[(\d+)\]\[lines\]\[(\d+)\]"
    r"\[(raw_text|body|quantity_text|unit|item|preparation)\]$"
)
_SORTS = {
    "updated": (Recipe.updated_at.desc(), Recipe.id),
    "title": (Recipe.title.asc(), Recipe.id),
    "time": (Recipe.total_minutes.asc(), Recipe.title.asc(), Recipe.id),
}


def _csrf_token(request: Request) -> str:
    loaded = getattr(request.state, "auth_session", None)
    if isinstance(loaded, LoadedSession):
        return loaded.session.csrf_token
    return ""


def _normalize_source_url(value: str) -> str:
    return str(httpx.URL(value.strip()).copy_with(fragment=None))


def _source_name(url: str, site_name: str | None = None) -> str | None:
    if site_name and site_name.strip():
        return site_name.strip()[:300]
    try:
        return httpx.URL(url).host or None
    except (httpx.InvalidURL, TypeError, ValueError):
        return None


def _blank_ingest_draft(source_url: str, source_name: str | None) -> RecipeDraft:
    return RecipeDraft(
        title="",
        sections=[
            RecipeSectionDraft(
                heading="Ingredients", kind="ingredients", lines=[RecipeIngredientDraft(raw_text="")]
            ),
            RecipeSectionDraft(
                heading="Steps", kind="steps", lines=[RecipeStepDraft(body="")]
            ),
        ],
        provenance=RecipeProvenanceDraft(
            source_url=source_url or None,
            source_name=source_name,
        ),
    )


def _ingest_state(draft: RecipeDraft) -> str:
    ingredient_lines = [
        line.raw_text.strip()
        for section in draft.sections
        if section.kind == "ingredients"
        for line in section.lines
        if isinstance(line, RecipeIngredientDraft) and line.raw_text.strip()
    ]
    step_lines = [
        line.body.strip()
        for section in draft.sections
        if section.kind == "steps"
        for line in section.lines
        if isinstance(line, RecipeStepDraft) and line.body.strip()
    ]
    has_content = bool(
        draft.title.strip()
        or (draft.description or "").strip()
        or ingredient_lines
        or step_lines
        or any(name.strip() for name in draft.tags)
        or any(name.strip() for name in draft.suggested_categories)
        or draft.servings_text
        or draft.servings_number is not None
        or draft.servings_unit
        or draft.prep_minutes is not None
        or draft.cook_minutes is not None
        or draft.total_minutes is not None
        or draft.hero_image
    )
    if not has_content:
        return "nothing-found"
    if draft.title.strip() and ingredient_lines and step_lines and not draft.parse_notes:
        return "full"
    return "partial"


async def _categories() -> list[Category]:
    async with session_scope() as db:
        return list(
            (
                await db.execute(select(Category).order_by(Category.position))
            ).scalars().all()
        )


def _form_context(
    request: Request,
    *,
    draft: RecipeDraft,
    categories: list[Category],
    selected_category_ids: set[str],
    csrf_token: str,
    recipe_id: str | None = None,
    slug: str = "",
    errors: dict[str, str] | None = None,
    form_values: dict[str, str] | None = None,
    message: str | None = None,
    image_path: str | None = None,
    image_message: str | None = None,
    ingest_state: str | None = None,
    ingest_message: str | None = None,
    status_code: int = 200,
) -> Response:
    form_values = form_values or {}
    allowed_ingest_states = {"fetch-failed", "nothing-found", "partial", "full"}
    if ingest_state is None:
        ingest_state = form_values.get("ingest_state")
    if ingest_state not in allowed_ingest_states:
        ingest_state = None
    if ingest_message is None:
        ingest_message = form_values.get("ingest_message")
    response = templates.TemplateResponse(
        request,
        "recipe_form.html",
        {
            "draft": draft,
            "categories": categories,
            "selected_category_ids": selected_category_ids,
            "csrf_token": csrf_token,
            "recipe_id": recipe_id,
            "slug": slug,
            "errors": errors or {},
            "form_values": form_values,
            "message": message,
            "image_path": image_path,
            "image_message": image_message,
            "ingest_state": ingest_state,
            "ingest_message": ingest_message,
            "upload_max_bytes": get_settings().upload_max_bytes,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _draft_from_form(fields: Any) -> tuple[RecipeDraft, dict[str, str]]:
    sections: dict[int, dict[str, Any]] = {}
    for key, value in fields.multi_items():
        if not isinstance(value, str):
            continue
        section_match = _SECTION_FIELD.fullmatch(key)
        if section_match:
            index = int(section_match.group(1))
            sections.setdefault(index, {})[section_match.group(2)] = value
            continue
        line_match = _LINE_FIELD.fullmatch(key)
        if line_match:
            section_index, line_index = map(int, line_match.group(1, 2))
            line = sections.setdefault(section_index, {}).setdefault("lines", {})
            line.setdefault(line_index, {})[line_match.group(3)] = value

    draft_sections: list[RecipeSectionDraft] = []
    for section_index in sorted(sections):
        section = sections[section_index]
        kind = section.get("kind", "ingredients")
        lines = section.get("lines", {})
        if kind == "ingredients":
            parsed_lines = [
                RecipeIngredientDraft(
                    raw_text=line.get("raw_text", ""),
                    quantity_text=line.get("quantity_text") or None,
                    unit=line.get("unit") or None,
                    item=line.get("item") or None,
                    preparation=line.get("preparation") or None,
                )
                for _, line in sorted(lines.items())
            ]
        else:
            parsed_lines = [
                RecipeStepDraft(body=line.get("body", ""))
                for _, line in sorted(lines.items())
            ]
        draft_sections.append(
            RecipeSectionDraft(
                heading=section.get("heading", ""),
                kind=kind,
                lines=parsed_lines,
            )
        )

    field_errors: dict[str, str] = {}

    def _optional_int(name: str) -> int | None:
        raw = str(fields.get(name, "")).strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            field_errors[name] = "Enter a whole number of minutes."
            return None

    raw_servings = str(fields.get("servings_number", "")).strip()
    servings_number = None
    if raw_servings:
        try:
            servings_number = Decimal(raw_servings)
        except InvalidOperation:
            field_errors["servings_number"] = "Enter a valid servings quantity."
    draft = RecipeDraft(
        title=str(fields.get("title", "")),
        description=str(fields.get("description", "")).strip() or None,
        sections=draft_sections,
        tags=[line.strip() for line in str(fields.get("tags", "")).splitlines() if line.strip()],
        servings_text=str(fields.get("servings_text", "")).strip() or None,
        servings_number=servings_number,
        prep_minutes=_optional_int("prep_minutes"),
        cook_minutes=_optional_int("cook_minutes"),
        total_minutes=_optional_int("total_minutes"),
        hero_image=str(fields.get("hero_image", "")).strip() or None,
        parse_notes=[
            value.strip()
            for value in fields.getlist("parse_notes")
            if isinstance(value, str) and value.strip()
        ],
        provenance=_provenance_from_form(fields),
    )
    return draft, field_errors


def _provenance_from_form(fields: Any) -> RecipeProvenanceDraft | None:
    source_url = str(fields.get("source_url", "")).strip() or None
    source_name = str(fields.get("source_name", "")).strip() or None
    method = str(fields.get("extraction_method", "")).strip().casefold()
    if method not in {"jsonld", "microdata", "llm", "manual", "wxr"}:
        method = "manual" if source_url else "manual"
    source_system = str(fields.get("source_system", "")).strip() or None
    source_id = str(fields.get("source_id", "")).strip() or None
    if (
        source_url is None
        and source_name is None
        and source_system is None
        and source_id is None
        and not fields.get("extraction_method")
    ):
        return None
    raw_imported_at = str(fields.get("imported_at", "")).strip()
    try:
        imported_at = datetime.fromisoformat(raw_imported_at) if raw_imported_at else None
    except ValueError:
        imported_at = None
    if imported_at is None and source_url:
        imported_at = datetime.now(UTC)
    return RecipeProvenanceDraft(
        source_system=source_system,
        source_id=source_id,
        source_url=source_url,
        source_name=source_name,
        extraction_method=method,
        imported_at=imported_at,
    )


def _simple_form_values(fields: Any) -> dict[str, str]:
    return {
        key: value
        for key, value in fields.multi_items()
        if isinstance(value, str) and "[" not in key
    }


def _draft_from_recipe(details: Any) -> RecipeDraft:
    sections: list[RecipeSectionDraft] = []
    for section_details in details.sections:
        section = section_details.section
        if section.kind == "ingredients":
            lines = [
                RecipeIngredientDraft(
                    raw_text=item.raw_text,
                    quantity_text=item.quantity_text,
                    unit=item.unit,
                    item=item.item,
                    preparation=item.preparation,
                )
                for item in section_details.items
            ]
        else:
            lines = [RecipeStepDraft(body=item.raw_text) for item in section_details.items]
        sections.append(
            RecipeSectionDraft(heading=section.heading, kind=section.kind, lines=lines)
        )
    recipe = details.recipe
    return RecipeDraft(
        title=recipe.title,
        description=recipe.description,
        sections=sections,
        tags=[tag.name for tag in details.tags],
        servings_text=recipe.servings_text,
        servings_number=recipe.servings_number,
        prep_minutes=recipe.prep_minutes,
        cook_minutes=recipe.cook_minutes,
        total_minutes=recipe.total_minutes,
        provenance=(
            RecipeProvenanceDraft(
                source_system=recipe.source_system,
                source_id=recipe.source_id,
                source_url=recipe.source_url,
                source_name=recipe.source_name,
                extraction_method=recipe.extraction_method,
                imported_at=recipe.imported_at,
            )
            if any(
                value is not None
                for value in (
                    recipe.source_system,
                    recipe.source_id,
                    recipe.source_url,
                    recipe.source_name,
                    recipe.extraction_method,
                    recipe.imported_at,
                )
            )
            else None
        ),
    )


async def _save_from_form(
    request: Request,
    fields: Any,
    *,
    recipe_id: str | None,
    editor: User,
) -> Response:
    draft, form_errors = _draft_from_form(fields)
    if form_errors:
        categories = await _categories()
        return _form_context(
            request,
            draft=draft,
            categories=categories,
            selected_category_ids=set(fields.getlist("category_ids")),
            csrf_token=_csrf_token(request),
            recipe_id=recipe_id,
            slug=str(fields.get("slug", "")),
            errors=form_errors,
            form_values=_simple_form_values(fields),
            status_code=400,
        )
        return response

    selected_ids = list(dict.fromkeys(fields.getlist("category_ids")))
    async with session_scope() as db:
        selected_categories = list(
            (
                await db.execute(
                    select(Category).where(Category.id.in_(selected_ids))
                )
            ).scalars().all()
        )
        try:
            save_data = validate_for_save(
                draft, category_names=[category.name for category in selected_categories]
            )
            if recipe_id is None:
                recipe = await create_recipe(
                    db,
                    save_data,
                    category_ids=selected_ids,
                    actor_user_id=editor.id,
                    slug=str(fields.get("slug", "")) or None,
                )
            else:
                recipe = await update_recipe(
                    db,
                    recipe_id,
                    save_data,
                    category_ids=selected_ids,
                    actor_user_id=editor.id,
                    slug=str(fields.get("slug", "")) or None,
                )
        except RecipeValidationError as exc:
            categories = list(
                (
                    await db.execute(select(Category).order_by(Category.position))
                ).scalars().all()
            )
            return _form_context(
                request,
                draft=draft,
                categories=categories,
                selected_category_ids=set(selected_ids),
                csrf_token=_csrf_token(request),
                recipe_id=recipe_id,
                slug=str(fields.get("slug", "")),
                errors=exc.field_errors,
                form_values=_simple_form_values(fields),
                status_code=400,
            )
        except InvalidRecipeInput as exc:
            categories = list(
                (
                    await db.execute(select(Category).order_by(Category.position))
                ).scalars().all()
            )
            return _form_context(
                request,
                draft=draft,
                categories=categories,
                selected_category_ids=set(selected_ids),
                csrf_token=_csrf_token(request),
                recipe_id=recipe_id,
                slug=str(fields.get("slug", "")),
                errors=exc.field_errors,
                form_values=_simple_form_values(fields),
                status_code=400,
            )
        except UnknownCategories as exc:
            categories = list(
                (
                    await db.execute(select(Category).order_by(Category.position))
                ).scalars().all()
            )
            return _form_context(
                request,
                draft=draft,
                categories=categories,
                selected_category_ids=set(selected_ids),
                csrf_token=_csrf_token(request),
                recipe_id=recipe_id,
                slug=str(fields.get("slug", "")),
                errors={"categories": f"Unknown category id(s): {', '.join(exc.unknown_ids)}"},
                form_values=_simple_form_values(fields),
                status_code=400,
            )
        except RecipeAliasConflict:
            categories = list(
                (
                    await db.execute(select(Category).order_by(Category.position))
                ).scalars().all()
            )
            return _form_context(
                request,
                draft=draft,
                categories=categories,
                selected_category_ids=set(selected_ids),
                csrf_token=_csrf_token(request),
                recipe_id=recipe_id,
                slug=str(fields.get("slug", "")),
                errors={"slug": "That URL is already reserved."},
                form_values=_simple_form_values(fields),
                status_code=409,
            )
        except RecipeNotFound as exc:
            raise HTTPException(status_code=404, detail="Recipe not found") from exc
        saved_id = recipe.id
    image_failed = False
    if recipe_id is None and draft.hero_image:
        image_failed = not await _save_candidate_image(
            request,
            saved_id,
            draft.hero_image,
        )
    location = f"/recipes/{saved_id}/edit?saved=1"
    if image_failed:
        location += "&image=download_failed"
    return RedirectResponse(location, status_code=303)


@router.get("/edit")
async def editor_dashboard(
    request: Request,
    editor: User = Depends(_editor),
    q: str = "",
    category_id: str = "",
    tag_id: str = "",
    sort: str = "updated",
):
    query = select(Recipe).distinct()
    clean_query = q.strip()
    if clean_query:
        pattern = f"%{clean_query}%"
        query = query.where(
            or_(
                Recipe.title.ilike(pattern),
                Recipe.description.ilike(pattern),
                Recipe.source_name.ilike(pattern),
            )
        )
    if category_id:
        query = query.where(
            Recipe.id.in_(
                select(recipe_categories.c.recipe_id).where(
                    recipe_categories.c.category_id == category_id
                )
            )
        )
    if tag_id:
        query = query.where(
            Recipe.id.in_(
                select(recipe_tags.c.recipe_id).where(recipe_tags.c.tag_id == tag_id)
            )
        )
    selected_sort = sort if sort in _SORTS else "updated"
    query = query.order_by(*_SORTS[selected_sort])

    async with session_scope() as db:
        recipes = list((await db.execute(query)).scalars().all())
        categories = list(
            (
                await db.execute(select(Category).order_by(Category.position))
            ).scalars().all()
        )
        tags = list((await db.execute(select(Tag).order_by(Tag.name))).scalars().all())
        category_names: dict[str, list[str]] = {recipe.id: [] for recipe in recipes}
        if recipes:
            rows = await db.execute(
                select(recipe_categories.c.recipe_id, Category.name)
                .join(Category, Category.id == recipe_categories.c.category_id)
                .where(recipe_categories.c.recipe_id.in_(category_names))
                .order_by(Category.position)
            )
            for recipe_id, name in rows:
                category_names[recipe_id].append(name)

    response = templates.TemplateResponse(
        request,
        "recipe_dashboard.html",
        {
            "recipes": recipes,
            "category_names": category_names,
            "categories": categories,
            "tags": tags,
            "q": clean_query,
            "category_id": category_id,
            "tag_id": tag_id,
            "sort": selected_sort,
            "csrf_token": _csrf_token(request),
            "editor": editor,
        },
    )
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.post("/recipes/ingest", dependencies=[Depends(require_csrf)])
async def ingest_recipe_form(
    request: Request,
    editor: User = Depends(_editor),
):
    fields = await request.form()
    submitted_url = str(fields.get("url", "")).strip()
    try:
        source_url = _normalize_source_url(submitted_url)
    except (httpx.InvalidURL, TypeError, ValueError):
        source_url = submitted_url
    source_name = _source_name(source_url)
    draft = _blank_ingest_draft(source_url, source_name)
    ingest_state = "fetch-failed"
    ingest_message = "The recipe source could not be fetched. You can enter the recipe details manually."

    try:
        async with asyncio.timeout(INGEST_TIMEOUT_SECONDS):
            async with request.app.state.ingest_semaphore:
                page = await fetch_page(source_url)
                parsed = await parse_recipe_page_with_fallback(page)
        source_name = _source_name(source_url, page.site_name)
        provenance = RecipeProvenanceDraft(
            source_url=source_url,
            source_name=source_name,
            extraction_method=(
                parsed.provenance.extraction_method if parsed.provenance else None
            ),
            imported_at=datetime.now(UTC),
        )
        draft = parsed.model_copy(
            update={
                "provenance": provenance,
                "hero_image": parsed.hero_image or page.og_image,
            }
        )
        ingest_state = _ingest_state(draft)
        ingest_message = None
        if ingest_state == "nothing-found":
            ingest_message = "No recipe fields were found. You can enter the details manually."
        elif ingest_state == "partial":
            ingest_message = "Some recipe fields were found. Review the details and fill in anything missing."
        elif ingest_state == "full":
            ingest_message = "Recipe details were found. Review them before saving."

        async with session_scope() as db:
            duplicate = (
                await db.execute(
                    select(Recipe.id, Recipe.title)
                    .where(Recipe.source_url == source_url)
                    .limit(1)
                )
            ).first()
        if duplicate is not None:
            draft = draft.model_copy(
                update={
                    "already_exists": DuplicateRecipeDraft(
                        id=duplicate.id,
                        title=duplicate.title,
                    )
                }
            )
    except FetchError as exc:
        ingest_message = f"{exc} You can enter the recipe details manually."
    except TimeoutError:
        ingest_message = (
            "Fetching and parsing the recipe source exceeded the time limit. "
            "You can enter the recipe details manually."
        )
    except Exception as exc:
        logging.getLogger("cookbook.recipes.ingest").warning(
            "recipe ingest failed: %s", type(exc).__name__
        )
        ingest_message = (
            "The recipe source could not be processed. You can enter the recipe details manually."
        )

    categories = await _categories()
    suggested = {name.strip().casefold() for name in draft.suggested_categories if name.strip()}
    selected_category_ids = {
        category.id for category in categories if category.name.strip().casefold() in suggested
    }
    return _form_context(
        request,
        draft=draft,
        categories=categories,
        selected_category_ids=selected_category_ids,
        csrf_token=_csrf_token(request),
        ingest_state=ingest_state,
        ingest_message=ingest_message,
    )


@router.get("/recipes/new")
async def new_recipe_form(request: Request, editor: User = Depends(_editor)):
    categories = await _categories()
    draft = RecipeDraft(
        title="",
        sections=[
            RecipeSectionDraft(
                heading="Ingredients", kind="ingredients", lines=[RecipeIngredientDraft(raw_text="")]
            ),
            RecipeSectionDraft(
                heading="Steps", kind="steps", lines=[RecipeStepDraft(body="")]
            ),
        ],
    )
    return _form_context(
        request,
        draft=draft,
        categories=categories,
        selected_category_ids=set(),
        csrf_token=_csrf_token(request),
    )


@router.post("/recipes", dependencies=[Depends(require_csrf)])
async def create_recipe_form(request: Request, editor: User = Depends(_editor)):
    fields = await request.form()
    return await _save_from_form(request, fields, recipe_id=None, editor=editor)


@router.get("/recipes/{recipe_id}/edit")
async def edit_recipe_form(
    request: Request,
    recipe_id: str,
    editor: User = Depends(_editor),
):
    async with session_scope() as db:
        details = await get_recipe(db, recipe_id)
        if details is None:
            raise HTTPException(status_code=404, detail="Recipe not found")
        draft = _draft_from_recipe(details)
        categories = list(
            (
                await db.execute(select(Category).order_by(Category.position))
            ).scalars().all()
        )
        selected_category_ids = {category.id for category in details.categories}
        slug = details.recipe.slug
        image_path = details.recipe.image_path
        recipe_id = details.recipe.id
    image_state = request.query_params.get("image")
    image_messages = {
        "saved": "Recipe image saved.",
        "download_failed": "The recipe was saved, but its source image could not be downloaded.",
        "removed": "Recipe image removed.",
        "invalid": "Upload a valid JPEG, PNG, or WebP image within the size limit.",
        "storage_error": "The image could not be stored. Your existing image is unchanged.",
    }
    return _form_context(
        request,
        draft=draft,
        categories=categories,
        selected_category_ids=selected_category_ids,
        csrf_token=_csrf_token(request),
        recipe_id=recipe_id,
        slug=slug,
        message="Recipe saved." if request.query_params.get("saved") == "1" else None,
        image_path=image_path,
        image_message=image_messages.get(image_state),
    )


@router.post("/recipes/{recipe_id}", dependencies=[Depends(require_csrf)])
async def update_recipe_form(
    request: Request,
    recipe_id: str,
    editor: User = Depends(_editor),
):
    fields = await request.form()
    return await _save_from_form(request, fields, recipe_id=recipe_id, editor=editor)


@router.post("/recipes/{recipe_id}/delete", dependencies=[Depends(require_csrf)])
async def delete_recipe_form(
    request: Request,
    recipe_id: str,
    editor: User = Depends(_editor),
):
    image_path: str | None = None
    try:
        async with session_scope() as db:
            recipe = await db.get(Recipe, recipe_id)
            if recipe is None:
                raise RecipeNotFound(recipe_id)
            image_path = recipe.image_path
            await delete_recipe(db, recipe_id)
    except RecipeNotFound as exc:
        raise HTTPException(status_code=404, detail="Recipe not found") from exc
    except RecipeHasAliases:
        async with session_scope() as db:
            details = await get_recipe(db, recipe_id)
            if details is None:
                raise HTTPException(status_code=404, detail="Recipe not found")
            categories = list(
                (
                    await db.execute(select(Category).order_by(Category.position))
                ).scalars().all()
            )
            draft = _draft_from_recipe(details)
            selected_category_ids = {category.id for category in details.categories}
            slug = details.recipe.slug
        return _form_context(
            request,
            draft=draft,
            categories=categories,
            selected_category_ids=selected_category_ids,
            csrf_token=_csrf_token(request),
            recipe_id=recipe_id,
            slug=slug,
            image_path=details.recipe.image_path,
            errors={"delete": "This recipe has saved URL aliases and cannot be deleted."},
            status_code=409,
        )
    await _remove_image_best_effort(image_path)
    return RedirectResponse("/edit?deleted=1", status_code=303)


async def _remove_image_best_effort(relative_path: str | None) -> None:
    if not relative_path:
        return
    try:
        await anyio.to_thread.run_sync(
            delete_stored_image, get_settings().media_dir, relative_path
        )
    except OSError as exc:
        log.warning("recipe image cleanup failed: %s", type(exc).__name__)


async def _save_candidate_image(
    request: Request,
    recipe_id: str,
    image_url: str,
) -> bool:
    settings = request.app.state.settings
    if len(image_url) > 2000:
        log.warning("source image candidate rejected: URL too long")
        return False
    stored = None
    try:
        content, _media_type = await fetch_public_bytes(
            image_url,
            max_bytes=min(settings.upload_max_bytes, 10 * 1024 * 1024),
        )
        stored = await anyio.to_thread.run_sync(
            partial(
                store_image,
                content,
                settings.media_dir,
                max_bytes=settings.upload_max_bytes,
            )
        )
        async with session_scope() as db:
            await set_recipe_image(db, recipe_id, stored.relative_path)
        return True
    except Exception as exc:
        if stored is not None:
            await _remove_image_best_effort(stored.relative_path)
        log.warning("source image save failed: %s", type(exc).__name__)
        return False


@router.post("/recipes/{recipe_id}/image", dependencies=[Depends(require_csrf)])
async def upload_recipe_image(
    request: Request,
    recipe_id: str,
    image: UploadFile = File(...),
    editor: User = Depends(_editor),
):
    settings = request.app.state.settings
    if image.size is not None and image.size > settings.upload_max_bytes:
        await image.close()
        raise HTTPException(status_code=413, detail="Image upload is too large")
    content = await image.read(settings.upload_max_bytes + 1)
    await image.close()
    if len(content) > settings.upload_max_bytes:
        raise HTTPException(status_code=413, detail="Image upload is too large")

    try:
        stored = await anyio.to_thread.run_sync(
            partial(
                store_image,
                content,
                settings.media_dir,
                max_bytes=settings.upload_max_bytes,
            )
        )
    except InvalidRecipeImage:
        return RedirectResponse(
            f"/recipes/{recipe_id}/edit?image=invalid", status_code=303
        )
    except OSError as exc:
        log.warning("recipe image write failed: %s", type(exc).__name__)
        return RedirectResponse(
            f"/recipes/{recipe_id}/edit?image=storage_error", status_code=303
        )

    try:
        async with session_scope() as db:
            previous_path = await set_recipe_image(db, recipe_id, stored.relative_path)
    except RecipeNotFound as exc:
        await _remove_image_best_effort(stored.relative_path)
        raise HTTPException(status_code=404, detail="Recipe not found") from exc
    except Exception:
        await _remove_image_best_effort(stored.relative_path)
        raise

    if previous_path:
        await _remove_image_best_effort(previous_path)
    return RedirectResponse(
        f"/recipes/{recipe_id}/edit?image=saved", status_code=303
    )


@router.post("/recipes/{recipe_id}/image/remove", dependencies=[Depends(require_csrf)])
async def remove_recipe_image(
    request: Request,
    recipe_id: str,
    editor: User = Depends(_editor),
):
    try:
        async with session_scope() as db:
            previous_path = await set_recipe_image(db, recipe_id, None)
    except RecipeNotFound as exc:
        raise HTTPException(status_code=404, detail="Recipe not found") from exc
    await _remove_image_best_effort(previous_path)
    return RedirectResponse(
        f"/recipes/{recipe_id}/edit?image=removed", status_code=303
    )


@router.get("/media/{image_path:path}")
async def serve_recipe_image(request: Request, image_path: str):
    settings = request.app.state.settings
    image = await anyio.to_thread.run_sync(
        resolve_stored_image, settings.media_dir, image_path
    )
    if image is None:
        raise HTTPException(status_code=404, detail="Image not found")
    return FileResponse(
        image.path,
        media_type=image.media_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        },
    )
