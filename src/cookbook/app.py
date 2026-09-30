"""Cookbook app — FastAPI application factory and health endpoints (spec §9.5)."""
from __future__ import annotations

import logging
import os
import tempfile
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

from . import __version__
from .config import get_settings
from .db import get_engine

log = logging.getLogger("cookbook")
_START = time.monotonic()


def create_app() -> FastAPI:
    settings = get_settings()  # fails fast on missing SECRET_KEY (D20)

    app = FastAPI(title=settings.app_name, version=__version__, docs_url=None, redoc_url=None)
    app.state.start_time = _START

    @app.get("/healthz")
    async def healthz() -> dict:
        # Liveness: process is up and answering. Deliberately does NOT touch
        # the database (spec D19 / 9.5).
        return {"status": "ok", "version": __version__, "uptime_seconds": int(time.monotonic() - _START)}

    @app.get("/readyz")
    async def readyz():
        # Readiness: can we actually serve traffic? Probe DB + media dir.
        checks: dict[str, str] = {}
        engine = get_engine()
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            checks["db"] = "ok"
        except Exception as exc:  # noqa: BLE001 - readiness reports, does not raise
            checks["db"] = f"error: {type(exc).__name__}"
        media = settings.media_dir
        try:
            media.mkdir(parents=True, exist_ok=True)
            # Spec 9.5: real write probe — touch + delete a temp file.
            fd, tmp = tempfile.mkstemp(prefix=".readyz-", dir=str(media))
            os.close(fd)
            os.unlink(tmp)
            checks["media"] = "ok"
        except OSError as exc:
            checks["media"] = f"error: {type(exc).__name__}"

        healthy = checks.get("db") == "ok" and checks.get("media") == "ok"
        body = {
            "status": "ready" if healthy else "not_ready",
            "version": __version__,
            "checks": checks,
        }
        if not healthy:
            # Spec 9.5/D24: a failing /readyz is a *loud log line*, not a
            # self-shutdown. The container keeps running so the failure is
            # observable; the proxy simply stops routing to it.
            log.warning("readyz: NOT READY %s", body)
            return JSONResponse(status_code=503, content=body)
        return body

    app.state.settings = settings
    return app
