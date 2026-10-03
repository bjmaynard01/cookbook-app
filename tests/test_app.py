"""`/healthz` + `/readyz` endpoint tests (spec §9.5 / D19 / D24).

Design — reliable, hermetic, **no database required**:

* ``/readyz`` calls ``get_engine()`` internally (a real ``mysql+asyncmy`` engine).
  Rather than pointing it at a live MariaDB (the dev compose publishes no host
  port — see ``references/environment.md``), we monkeypatch
  ``cookbook.app.get_engine`` with a tiny fake engine. That makes both the
  healthy (200) and failing (503) branches fully deterministic on any machine.
* ``get_settings`` is ``@lru_cache``-decorated and reads the real relative
  ``.env``. We follow the same hermetic pattern as ``tests/test_config.py``:
  chdir out of the repo (so ``env_file=".env"`` finds nothing), strip inherited
  config env vars, set a controlled minimal env, and ``cache_clear()`` before
  app creation.
* The 503 case must (a) return useful per-check results, (b) emit the loud
  ``readyz: NOT READY`` warning log, and (c) leave the app responsive — a
  follow-up ``/healthz`` still returns 200 (no self-shutdown, spec D24).

Run: ``.venv/bin/pytest tests/test_app.py -v``  (from the repo root, no DB).
"""
from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from cookbook import __version__
from cookbook import app as app_mod
from cookbook import db as db_mod

# Env var prefixes the app reads; strip all of them (and the D20 ``*_FILE``
# secret variants) so no inherited value or the real ``.env`` leaks in.
_STRIP_PREFIXES = (
    "DB_", "SECRET_", "APP_", "DEBUG", "MEDIA_",
    "ALLOWED_", "SESSION_TTL", "UPLOAD_",
)
_STRIP_EXACT = {"SECRET_KEY_FILE", "DB_PASSWORD_FILE"}


# ---------------------------------------------------------------------------
# Fake async engine — stands in for get_engine()'s real MySQL engine.
# ---------------------------------------------------------------------------
class _FakeConn:
    """The object an ``async with engine.connect()`` yields on success."""

    async def execute(self, *_args, **_kwargs):
        # ``SELECT 1`` succeeds: readiness sees a reachable dependency.
        return None


class _FakeConnCtx:
    """Async context manager returned by ``fake_engine.connect()``."""

    def __init__(self, fail: bool) -> None:
        self._fail = fail

    async def __aenter__(self):
        if self._fail:
            # Deterministic "dependency failed" — no real socket, no DB.
            raise ConnectionRefusedError("simulated db outage (test)")
        return _FakeConn()

    async def __aexit__(self, *_exc_info):
        return False


class _FakeEngine:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    def connect(self):  # returns an async context manager (as the real one does)
        return _FakeConnCtx(self._fail)


def _install_fake_engine(monkeypatch: pytest.MonkeyPatch, fail: bool) -> None:
    """Point ``cookbook.app.get_engine`` at a fake so ``/readyz`` is controlled.

    ``readyz`` looks up ``get_engine`` in the app module globals on each call,
    so patching the module attribute is enough (the real engine is never built).
    """
    monkeypatch.setattr(app_mod, "get_engine", lambda: _FakeEngine(fail=fail))


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> Iterator[TestClient]:
    # --- hermetic settings (same isolation as tests/test_config.py) ---
    import os

    for var in list(os.environ):
        if var.startswith(_STRIP_PREFIXES) or var in _STRIP_EXACT:
            monkeypatch.delenv(var, raising=False)

    # chdir out of the repo so the relative env_file=".env" resolves to
    # tmp_path/.env (absent) rather than the developer's real .env.
    monkeypatch.chdir(tmp_path)

    # Controlled, minimal configuration — just enough for get_settings() to
    # validate (SECRET_KEY required) and for database_url to be well-formed.
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-not-a-real-secret")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_PORT", "3306")
    monkeypatch.setenv("DB_NAME", "cookbook")
    monkeypatch.setenv("DB_USER", "cookbook")
    monkeypatch.setenv("DB_PASSWORD", "test-password")

    # Reset the @lru_cache so this test sees *its* env, not a prior test's.
    app_mod.get_settings.cache_clear()
    db_mod.get_engine.cache_clear()
    db_mod.get_session.cache_clear()

    app = app_mod.create_app()  # validates SECRET_KEY (D20); does not touch DB
    yield TestClient(app)

    # --- teardown: leave no poisoned settings cache behind ----------------
    # ``create_app()`` called ``get_settings()`` against THIS fixture's env
    # (DB_HOST=127.0.0.1), populating the @lru_cache. ``monkeypatch`` restores
    # the real env vars on teardown but NOT the cache — so without this the
    # cached Settings(db_host=127.0.0.1) survives and a later module
    # (``test_models.py``) that builds an engine via ``get_engine()→
    # get_settings()`` inherits it and dials 127.0.0.1 instead of the compose
    # database host. Clearing here mirrors test_config.py's autouse fixture.
    app_mod.get_settings.cache_clear()
    db_mod.get_engine.cache_clear()
    db_mod.get_session.cache_clear()


# ---------------------------------------------------------------------------
# /healthz — liveness
# ---------------------------------------------------------------------------
def test_healthz_liveness(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # /healthz must not require a database at all: point the engine at a fake
    # that would fail if consulted, to prove /healthz never consults it.
    _install_fake_engine(monkeypatch, fail=True)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == "0.1.0" == __version__
    assert isinstance(body["uptime_seconds"], int)
    assert body["uptime_seconds"] >= 0


# ---------------------------------------------------------------------------
# /readyz — readiness: healthy
# ---------------------------------------------------------------------------
def test_readyz_healthy(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_engine(monkeypatch, fail=False)  # db reports ok

    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["version"] == "0.1.0"
    assert body["checks"] == {"db": "ok", "media": "ok"}


# ---------------------------------------------------------------------------
# /readyz — readiness: dependency failed -> 503 + loud log, still responsive
# ---------------------------------------------------------------------------
def test_readyz_dependency_failure(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_fake_engine(monkeypatch, fail=True)  # db reports error

    with caplog.at_level(logging.WARNING, logger="cookbook"):
        r = client.get("/readyz")

    # (a) useful check results: db failed, media still fine.
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "not_ready"
    assert body["version"] == "0.1.0"
    assert body["checks"]["db"].startswith("error:")
    assert body["checks"]["db"] == "error: ConnectionRefusedError"
    assert body["checks"]["media"] == "ok"

    # (b) the loud log line (spec §9.5 / D24), named after the failing check.
    assert "readyz: NOT READY" in caplog.text

    # (c) the app remains responsive — no self-shutdown; a liveness probe
    # immediately after the failure still succeeds.
    follow = client.get("/healthz")
    assert follow.status_code == 200
    assert follow.json()["status"] == "ok"
