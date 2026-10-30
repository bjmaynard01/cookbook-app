from __future__ import annotations

import asyncio

import httpx

from cookbook.app import create_app
from cookbook.config import get_settings


def test_signed_out_root_redirects_to_login(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "root-route-test-secret")
    get_settings.cache_clear()

    async def run() -> None:
        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/")
        assert response.status_code == 302
        assert response.headers["location"] == "/login"
        assert response.headers["cache-control"] == "private, no-store"

    try:
        asyncio.run(run())
    finally:
        get_settings.cache_clear()
