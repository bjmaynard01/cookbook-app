from __future__ import annotations

import asyncio

import httpx

from cookbook.app import create_app
from cookbook.config import get_settings


def test_signed_out_root_renders_public_home(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "root-route-test-secret")
    get_settings.cache_clear()

    async def run() -> None:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/")
        assert response.status_code == 200
        assert "What would you like to cook?" in response.text
        assert response.text.count('aria-label="Primary navigation"') == 1
        assert 'href="/#categories-title">Browse</a>' in response.text
        assert 'href="/search">Search</a>' in response.text
        assert 'href="/login">Editor sign in</a>' in response.text
        assert 'href="/edit">Recipes</a>' not in response.text
        assert 'href="/recipes/new">New recipe</a>' not in response.text
        assert response.headers["cache-control"] == "public, max-age=300, s-maxage=600"

    try:
        asyncio.run(run())
    finally:
        get_settings.cache_clear()
