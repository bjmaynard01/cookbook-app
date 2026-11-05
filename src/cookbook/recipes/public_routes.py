"""Anonymous public recipe reading routes."""
from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import anyio
from fastapi import APIRouter, HTTPException, Request
from fastapi.templating import Jinja2Templates

from ..db import session_scope
from .images import resolve_stored_image
from .service import get_recipe_by_slug

router = APIRouter()
templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")


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
        },
    )
    response.headers["Cache-Control"] = "public, max-age=300, s-maxage=600"
    return response
