from __future__ import annotations

import asyncio

import httpx

from cookbook.app import create_app
from cookbook.config import get_settings


def test_image_upload_request_requires_bounded_content_length(monkeypatch) -> None:
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "upload-limit-test-secret")
    monkeypatch.setenv("UPLOAD_MAX_BYTES", "8")
    get_settings.cache_clear()

    async def run() -> None:
        async def body_without_length():
            yield b"bounded test body"

        app = create_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            too_large = await client.post(
                "/recipes/recipe-id/image",
                content=b"x" * (8 + 64 * 1024 + 1),
                headers={"content-type": "multipart/form-data; boundary=test"},
            )
            no_length = await client.post(
                "/recipes/recipe-id/image",
                content=body_without_length(),
                headers={"content-type": "multipart/form-data; boundary=test"},
            )
        assert too_large.status_code == 413
        assert no_length.status_code == 411
        assert too_large.headers["cache-control"] == "private, no-store"

    try:
        asyncio.run(run())
    finally:
        get_settings.cache_clear()
