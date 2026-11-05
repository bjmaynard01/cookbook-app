"""Anonymous public recipe reading routes."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates

from ..auth.sessions import LoadedSession
from ..db import session_scope
from .images import resolve_stored_image
from .browse_service import (
    CategoryBrowseData,
    CategorySummary,
    RecipeCardData,
    get_category_browse_data,
    get_home_browse_data,
    get_tag_browse_data,
)
from .service import get_recipe_by_slug

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")
TIME_FILTERS = (30, 60, 90)


def _safe_source_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        return value
    except ValueError:
        return None


def _ingredient_text(item) -> str:
    parts = [item.quantity_text, item.unit, item.item, item.preparation]
    rendered = " ".join(part.strip() for part in parts if part and part.strip())
    return rendered or item.raw_text


def _session_csrf(request: Request) -> str | None:
    loaded = getattr(request.state, "auth_session", None)
    if isinstance(loaded, LoadedSession):
        return loaded.session.csrf_token
    return None


def _card_context(card: RecipeCardData, media_dir: Path) -> dict:
    image = None
    if card.recipe.image_path:
        image = resolve_stored_image(media_dir, card.recipe.image_path)
    return {
        "recipe": card.recipe,
        "categories": card.categories,
        "tags": card.tags,
        "image_url": f"/media/{image.relative_path}" if image else None,
    }


def _category_context(summary: CategorySummary, media_dir: Path) -> dict:
    image = None
    if summary.thumbnail_path:
        image = resolve_stored_image(media_dir, summary.thumbnail_path)
    return {
        "category": summary.category,
        "recipe_count": summary.recipe_count,
        "image_url": f"/media/{image.relative_path}" if image else None,
    }


def _public_page(request: Request, template: str, context: dict):
    response = templates.TemplateResponse(
        request=request,
        name=template,
        context={**context, "session_csrf": _session_csrf(request)},
    )
    response.headers["Cache-Control"] = "public, max-age=300, s-maxage=600"
    return response


@router.get("/", name="public_home")
async def public_home(request: Request):
    async with session_scope() as db:
        data = await get_home_browse_data(db)
    settings = request.app.state.settings
    return _public_page(
        request,
        "public_home.html",
        {
            "featured": [_card_context(card, settings.media_dir) for card in data.featured],
            "categories": [
                _category_context(category, settings.media_dir)
                for category in data.categories
            ],
            "popular_tags": data.popular_tags,
        },
    )


@router.get("/c/{slug}", name="public_category")
async def public_category(
    request: Request,
    slug: str,
    max_minutes: int | None = None,
    tag: str | None = None,
):
    if max_minutes not in TIME_FILTERS:
        max_minutes = None
    async with session_scope() as db:
        data: CategoryBrowseData | None = await get_category_browse_data(
            db, slug, max_minutes=max_minutes, tag_slug=tag
        )
    if data is None:
        raise HTTPException(status_code=404, detail="Category not found")

    return _public_page(
        request,
        "public_listing.html",
        {
            "heading": data.category.name,
            "description": "Browse recipes in this category.",
            "recipes": [
                _card_context(card, request.app.state.settings.media_dir)
                for card in data.recipes
            ],
            "category": data.category,
            "tag": None,
            "available_tags": data.tags,
            "selected_tag": data.selected_tag,
            "max_minutes": data.max_minutes,
            "time_filters": TIME_FILTERS,
        },
    )


@router.get("/t/{slug}", name="public_tag")
async def public_tag(request: Request, slug: str):
    async with session_scope() as db:
        data = await get_tag_browse_data(db, slug)
    if data is None:
        raise HTTPException(status_code=404, detail="Tag not found")

    return _public_page(
        request,
        "public_listing.html",
        {
            "heading": data.tag.name,
            "description": "Recipes collected under this tag.",
            "recipes": [
                _card_context(card, request.app.state.settings.media_dir)
                for card in data.recipes
            ],
            "category": None,
            "tag": data.tag,
            "available_tags": (),
            "selected_tag": None,
            "max_minutes": None,
            "time_filters": (),
        },
    )


@router.get("/r/{slug}", name="public_recipe")
async def public_recipe(request: Request, slug: str):
    async with session_scope() as db:
        details = await get_recipe_by_slug(db, slug)
    if details is None:
        raise HTTPException(status_code=404, detail="Recipe not found")

    settings = request.app.state.settings
    image = None
    if details.recipe.image_path:
        image = await anyio.to_thread.run_sync(
            resolve_stored_image, settings.media_dir, details.recipe.image_path
        )
    ingredient_sections = [
        section for section in details.sections if section.section.kind == "ingredients"
    ]
    step_sections = [
        section for section in details.sections if section.section.kind == "steps"
    ]
    note_sections = [
        section for section in details.sections if section.section.kind == "note"
    ]
    source_url = None
    source_name = None
    if details.recipe.source_system and details.recipe.source_system != "wordpress":
        source_url = _safe_source_url(details.recipe.source_url)
        source_name = details.recipe.source_name or (
            urlsplit(source_url).hostname if source_url else None
        )

    response = templates.TemplateResponse(
        request=request,
        name="public_recipe.html",
        context={
            "details": details,
            "recipe": details.recipe,
            "ingredient_sections": ingredient_sections,
            "step_sections": step_sections,
            "note_sections": note_sections,
            "ingredient_text": _ingredient_text,
            "image_url": f"/media/{image.relative_path}" if image else None,
            "source_url": source_url,
            "source_name": source_name,
            "session_csrf": _session_csrf(request),
        },
    )
    response.headers["Cache-Control"] = "public, max-age=300, s-maxage=600"
    return response
