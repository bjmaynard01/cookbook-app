"""Exact legacy URL redirects backed by stored alias rows."""
from __future__ import annotations

from sqlalchemy import select
from starlette.responses import RedirectResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from ..db import session_scope
from ..models import Category, Recipe, RecipeUrlAlias


class LegacyAliasMiddleware:
    """301 only exact GET path/query matches recorded in recipe_url_aliases."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "GET":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        # Liveness and readiness remain useful when the database is down;
        # neither endpoint can be a stored content alias.
        if path in {"/healthz", "/readyz", "/media"} or path.startswith("/media/"):
            await self.app(scope, receive, send)
            return
        query_bytes = scope.get("query_string", b"")
        exact_path = path + ("?" + query_bytes.decode("latin-1") if query_bytes else "")
        if len(exact_path) > 255:
            await self.app(scope, receive, send)
            return

        target = None
        async with session_scope() as db:
            row = (
                await db.execute(
                    select(RecipeUrlAlias.recipe_id, Recipe.slug, Category.slug)
                    .outerjoin(Recipe, Recipe.id == RecipeUrlAlias.recipe_id)
                    .outerjoin(Category, Category.id == RecipeUrlAlias.category_id)
                    .where(RecipeUrlAlias.path.collate("utf8mb4_bin") == exact_path)
                    .limit(1)
                )
            ).first()
            if row is not None:
                _, recipe_slug, category_slug = row
                if recipe_slug:
                    target = f"/r/{recipe_slug}"
                elif category_slug:
                    target = f"/c/{category_slug}"

        if target is None:
            await self.app(scope, receive, send)
            return

        response = RedirectResponse(target, status_code=301)
        response.headers["Cache-Control"] = "public, max-age=300, s-maxage=600"
        await response(scope, receive, send)
