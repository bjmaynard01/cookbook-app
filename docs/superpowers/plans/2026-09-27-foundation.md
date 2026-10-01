# Cookbook Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Promote the verified cookbook foundation (FastAPI app, SQLAlchemy/Alembic, Docker, dev compose) into the repo root as the committed first version, with a regression test suite and a container e2e gate — exactly the code that already passed the 2026-09-27 container verification runs.

**Architecture:** Single Docker image, `src/` package layout, async SQLAlchemy (`asyncmy`) against MariaDB, Alembic forward-only migrations run at container entrypoint. All 15 source files are copied byte-for-byte from `reference/foundation-verified/` (the versions proven by the real container runs recorded in `docs/superpowers/SESSION-HANDOFF-2026-09-27.md`). No new application code is written in this plan; the only new code is the test suite. Dev = two compose services (app + `db`), prod = the same image pointed at the owner's existing LAN MariaDB via `DB_HOST`.

**Tech Stack:** Python 3.12 (image) / 3.14 host, FastAPI, SQLAlchemy 2.0 async + asyncmy, Alembic, pydantic-settings, argon2-cffi, httpx, uv (0.12.19), Docker / Docker Compose, `mariadb:11`.

**Spec:** `docs/superpowers/specs/2026-09-20-cookbook-design.md` (approved 2026-09-20; decision log §13 binds DL-1..DL-4).

## Global Constraints

Verbatim from the spec — every task's requirements implicitly include this section.

- **D1:** "Stack: Python 3 + FastAPI + Jinja2 + HTMX, server-rendered, single process, single Docker image"
- **D2:** "MariaDB in both dev and prod (identical dialect + FULLTEXT); no SQLite anywhere"
- **D19:** "`/healthz` = liveness (process only); `/readyz` = readiness (MariaDB reachable + media storage usable)"
- **D20:** "Secrets via file-based inputs preferred (`SECRET_KEY_FILE`, `LLM_API_KEY_FILE`, `FIRST_ADMIN_PASSWORD_FILE`); plain env vars permitted for dev/fallback"
- **D22:** "Dev DB data may be disposable; dev media and test fixtures are independently preservable (dev media as a host bind mount, fixtures in the repo)"
- **D23:** "Alembic for forward-only schema migrations, run at image entrypoint"
- **D24:** "Single-node, intentionally boring operations: no HA, no LB, no CI/CD, no monitoring stack in v1"
- **§9.5 (health, verbatim):** "`GET /readyz` — **readiness**: 200 only if (a) a MariaDB round-trip succeeds (`SELECT 1`) and (b) `MEDIA_DIR` exists and is writable (touch + delete a temp file). … A failing `/readyz` is a loud log line, not a self-shutdown (D24)."
- **§9.6 (migrations, verbatim):** "Alembic, forward-only, applied at image entrypoint (`alembic upgrade head` then exec the server). A boot with a down-migration or a drift is a logged error and the entrypoint exits non-zero … the container visibly fails; D24 — the owner fixes the env, there's no silent fallback."

**Owner standing orders (binding, from the 2026-09-27 handoff):**

- **Storage contract:** "All container persistent storage must live on the host under `/mnt/container/<container_name>` (so: `/mnt/container/db` for MariaDB data, `/mnt/container/app` for media). No named/anonymous volumes." Do NOT "improve" to relative paths or named volumes.
- **Config contract:** "The app must be configurable — production will point it at the owner's existing LAN MariaDB. One code path; only config differs." Dev compose ships the `db` service; prod = same image + `DB_HOST=<LAN MariaDB>`.
- **Docs scope:** `README.md` stays a placeholder in this plan — real docs land with the data-model plan. Do not over-document now.

**Verified environment facts (re-verified 2026-09-28 on this box):**

- `uv 0.12.19`; no pip (PEP 668). Use `uv` for everything.
- Docker is reachable from the agent shell (`docker ps` works without `sudo`/`newgrp`). If it ever fails, the owner-side remedy is `sudo usermod -aG docker stephanie` + re-login — report it, don't hardcode "docker unavailable".
- Retained images exist: `cookbook:dev` (sha256:1a273aa35a57…315MB) and `mariadb:11`. Building from the repo (Task 5) is the real gate; the retained image exists only as a fallback.
- Nothing on host ports 8000/3306. An unrelated healthy container `foundcheck-db-1` (mariadb, no published ports) is running — **do not touch it**.
- `/mnt/container/db` and `/mnt/container/app` may hold disposable dev data from the 2026-09-27 verification run.
- `reference/foundation-verified/.env` is secret-guarded — never read it; `.env.example` carries identical dev values.

## File structure (what lands where)

| Path | Responsibility | Source |
|---|---|---|
| `pyproject.toml` | deps (runtime + dev group) | promoted |
| `uv.lock` | reproducible lock (Dockerfile `uv sync --frozen` consumes it) | regenerated in-repo |
| `README.md` | one-line placeholder (real docs with data-model plan) | promoted |
| `.env.example` | dev env template (committed) | promoted |
| `.env` | local dev values (git-ignored, never committed) | copied from `.env.example` |
| `src/cookbook/__init__.py` | `__version__ = "0.1.0"` | promoted |
| `src/cookbook/config.py` | `Settings` + file-first secrets (D20) + `database_url` | promoted |
| `src/cookbook/db.py` | async engine + `session_scope()` | promoted |
| `src/cookbook/models.py` | `Base` + `User` (v0 only; rest arrives with data-model plan) | promoted |
| `src/cookbook/app.py` | `create_app()` + `/healthz` + `/readyz` | promoted |
| `alembic.ini` | no URL in file (injected in `env.py`) | promoted |
| `alembic/env.py` | async env, URL from `get_settings()` | promoted |
| `alembic/script.py.mako` | migration template | promoted |
| `alembic/versions/6d22befb6ec9_v0_users_table.py` | v0 `users` migration | promoted |
| `bin/docker-entrypoint.sh` | `alembic upgrade head` then `exec` (D23, D24) | promoted |
| `Dockerfile` | 2-stage uv build; `UV_PROJECT_ENVIRONMENT=/opt/venv`; HEALTHCHECK on `/readyz` | promoted |
| `docker-compose.yml` | dev-only `db` service + `app`; `/mnt/container/{db,app}` binds | promoted |
| `tests/test_config.py` | config/secret tests (NEW) | written here |
| `tests/test_app.py` | `/healthz` + `/readyz` tests (NEW) | written here |
| `tests/test_models.py` | `users` shape test (NEW) | written here |

Promoted-file integrity check (computed 2026-09-28 on `reference/foundation-verified/`):

```
b94dac86a452a01ca06f7c4b424036c8a52ab443947181c8fbbad057d9fef53e  ./pyproject.toml
0b064ccc4f29842b718ff13571b8ccfe589a2d9b1eafcd875ffbd3ef95ab31b3  ./README.md
0454abc6979d8d5c35cad4a9555b3fb96f0dbae68b801430d7ee91aa786f0163  ./.env.example
80d5a9a8d68e0559979af584cd25fb79c6df19e457bceb002faada127f53ef78  ./Dockerfile
7c51286d79149c184329b858d87f91892c580607bd16070bbe303276ef63a689  ./docker-compose.yml
10546d06184781e2782794118fb88749eb7833ccb4e46eac2cc54ba11c71df7f  ./alembic/env.py
1649f85b4170c583b60a15816ba6511b666883c872318e124b0713757011e1b0  ./alembic.ini
c20f6ab92d4ed36d77fbccb82bc410cd2ced42f2c162eebb43ddd45ae93dd90e  ./alembic/script.py.mako
92bd6f0a4d6f6fbe86dfc7d5cf98cad096453598aed398961a542831e27fdc15  ./alembic/versions/6d22befb6ec9_v0_users_table.py
cf6e5de6df1708dd0a264365dd117e1ff97d42508558b4d753f19f7c382640d0  ./bin/docker-entrypoint.sh
f0e55d866bdbc8e30d186736d0eddbca7ea5b06ca46f9a6305a9bba2e583ac52  ./src/cookbook/app.py
b833a6c96b8c73d4261ff9e64edd04cc49745693701c325a057941e3d7984e4a  ./src/cookbook/config.py
ce843030e26d597e8d97d423fd77ab009779bac4be2758edb5df9fbfda77590c  ./src/cookbook/db.py
91447944015cec709e8aa7655f7e9d64e1e4508e7023a57fe3746911c0fc6fed  ./src/cookbook/__init__.py
d6186799a665d75443774b713c050d83b1ae24849c7a0b282ae56bfae634e733  ./src/cookbook/models.py
```

---

### Task 1: Scaffold the repo — promote all verified files + lock file

**Files:**
- Modify: `.gitignore`
- Create: `pyproject.toml`, `uv.lock`, `README.md`, `.env.example`, `.env` (local only), `Dockerfile`, `docker-compose.yml`, `src/cookbook/*`, `alembic/*`, `bin/docker-entrypoint.sh`, `tests/` dir

**Interfaces:**
- Consumes: `reference/foundation-verified/` (verified source tree per the handoff)
- Produces: a complete repo layout plus working `.venv` and `uv.lock`; later tasks consume `cookbook.config.get_settings()`, `cookbook.db.get_engine()`, `cookbook.app.create_app()`, `cookbook.models.User`

- [ ] **Step 1: Promote every foundation file from the reference tree**

All from the repo root (`/home/stephanie/Projects/cookbook-app`):

```bash
mkdir -p src/cookbook alembic/versions bin tests
cp reference/foundation-verified/pyproject.toml reference/foundation-verified/README.md reference/foundation-verified/.env.example .
cp reference/foundation-verified/Dockerfile reference/foundation-verified/docker-compose.yml .
cp reference/foundation-verified/alembic.ini .
cp reference/foundation-verified/alembic/env.py reference/foundation-verified/alembic/script.py.mako alembic/
cp reference/foundation-verified/alembic/versions/6d22befb6ec9_v0_users_table.py alembic/versions/
cp reference/foundation-verified/bin/docker-entrypoint.sh bin/
cp reference/foundation-verified/src/cookbook/__init__.py reference/foundation-verified/src/cookbook/config.py reference/foundation-verified/src/cookbook/db.py reference/foundation-verified/src/cookbook/models.py reference/foundation-verified/src/cookbook/app.py src/cookbook/
```

Do NOT copy `reference/foundation-verified/.env` or anything under `__pycache__/`.

- [ ] **Step 2: Verify byte-for-byte promotion**

```bash
for f in pyproject.toml README.md .env.example Dockerfile docker-compose.yml alembic/env.py alembic.ini alembic/script.py.mako alembic/versions/6d22befb6ec9_v0_users_table.py bin/docker-entrypoint.sh src/cookbook/app.py src/cookbook/config.py src/cookbook/db.py src/cookbook/__init__.py src/cookbook/models.py; do
  h=$(sha256sum "$f" | cut -d' ' -f1)
  ref=$(sha256sum "reference/foundation-verified/$f" | cut -d' ' -f1)
  [ "$h" = "$ref" ] && echo "OK  $f" || echo "MISMATCH  $f"
done
```

Expected: 15 lines, all `OK` (hashes must agree with the integrity table above).

- [ ] **Step 3: Finalize `.gitignore`** (replaces the working-tree version)

```gitignore
# Python — spikes use throwaway scripts on the system python
__pycache__/
*.pyc

# WordPress source export: large binary-ish input, not part of the app
wp-exports/

# Local env (dev values); .env.example is the committed template
.env

# Local venv (uv); uv.lock IS committed — the Dockerfile builds with --frozen
.venv/

# Reference tree (verified source copies, not part of the app)
reference/

# Local media scratch dir if the app is run off-Docker in dev
media/
```

- [ ] **Step 4: Create the local `.env` from the committed template**

```bash
cp .env.example .env
git check-ignore -v .env
```

Expected: `git check-ignore` reports `.env` matched by `.gitignore` (line for `.env`). Never `cat reference/foundation-verified/.env` (secret-guarded); `.env.example` carries the identical dev values.

- [ ] **Step 5: Create the venv, lock, and install**

```bash
uv venv .venv
uv lock
uv sync
```

Expected: `uv lock` prints the resolution result and writes `uv.lock` (committed); `uv sync` creates/populates `.venv` with the project + dev group. Both commands were exercised in the 2026-09-27 verification runs (the same `uv sync --frozen` runs inside the verified image build).

- [ ] **Step 6: Verify the runtime + dev dependencies import**

```bash
.venv/bin/python -c "import fastapi, sqlalchemy, alembic, asyncmy, pydantic, pydantic_settings, httpx, argon2, pytest, anyio; print('deps ok')"
```

Expected: `deps ok`

- [ ] **Step 7: Commit**

```bash
git add .gitignore pyproject.toml uv.lock README.md .env.example Dockerfile docker-compose.yml alembic.ini alembic bin src
git commit -m "feat: promote verified cookbook foundation to repo root (app, alembic v0 users, docker, dev compose)"
```

Verify: `git status --short` shows a clean tree (only `tests/` untracked if Step 3's `mkdir tests` ran empty, and `reference/` now ignored); `git show --stat HEAD` lists 16 files.

---

### Task 2: Config + file-first secrets (TDD)

**Files:**
- Test: `tests/test_config.py`
- Consumes (Task 1): `cookbook.config.get_settings()`, `cookbook.config._from_secret_file()`, `cookbook.config.Settings`

**Interfaces:**
- Consumes: `Settings` fields per `reference/foundation-verified/src/cookbook/config.py`: `secret_key` (alias `SECRET_KEY`), `db_host`/`db_port`/`db_name`/`db_user`/`db_password` (aliases `DB_*`), `media_dir` (alias `MEDIA_DIR`, default `Path("/media")`), `session_ttl_hours` (alias `SESSION_TTL_HOURS`, default 12); `Settings.database_url` property; `get_settings()` is `@lru_cache`-d and raises `RuntimeError` when no `SECRET_KEY` is resolved.
- Produces: a green `tests/test_config.py` guarding D20 semantics — later tasks rely on `get_settings()` working under plain env vars.

- [ ] **Step 1: Write the failing tests**

`tests/test_config.py`:

```python
from __future__ import annotations

import os
from pathlib import Path

import pytest

from cookbook import config as config_mod
from cookbook.config import Settings, _from_secret_file


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Clean env for each test: strip inherited DB_/SECRET_/... vars, and chdir
    OUT of the repo root so Settings (env_file='.env', relative) cannot pick up
    the local .env. Env vars are the sole config source in these tests."""
    for var in list(os.environ):
        if var.startswith(("DB_", "SECRET_", "APP_", "DEBUG", "MEDIA_", "ALLOWED_", "UPLOAD_")):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    config_mod.get_settings.cache_clear()
    yield
    config_mod.get_settings.cache_clear()


def test_database_url_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "devsecretkey")
    monkeypatch.setenv("DB_HOST", "db")
    monkeypatch.setenv("DB_PORT", "3306")
    monkeypatch.setenv("DB_NAME", "cookbook")
    monkeypatch.setenv("DB_USER", "cookbook")
    monkeypatch.setenv("DB_PASSWORD", "devpassword")
    s = Settings()
    assert s.database_url == (
        "mysql+asyncmy://cookbook:devpassword@db:3306/cookbook?charset=utf8mb4"
    )


def test_database_url_without_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "k")
    monkeypatch.setenv("DB_USER", "cookbook")
    s = Settings()
    assert s.database_url == "mysql+asyncmy://cookbook@127.0.0.1:3306/cookbook?charset=utf8mb4"


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SECRET_KEY", "k")
    s = Settings()
    assert s.db_port == 3306
    assert s.db_name == "cookbook"
    assert s.db_user == "cookbook"
    assert s.media_dir == Path("/media")
    assert s.session_ttl_hours == 12


def test_from_secret_file_first_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    f = tmp_path / "secret"
    f.write_text("filesecret\n\n", encoding="utf-8")
    monkeypatch.setenv("SECRET_KEY_FILE", str(f))
    monkeypatch.setenv("SECRET_KEY", "plainsecret")
    assert _from_secret_file("SECRET_KEY") == "filesecret"


def test_from_secret_file_unreadable_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PASSWORD_FILE", str(tmp_path / "missing"))
    with pytest.raises(RuntimeError, match="DB_PASSWORD_FILE"):
        _from_secret_file("DB_PASSWORD")


def test_get_settings_prefers_file_over_plain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    f = tmp_path / "secret"
    f.write_text("fromfile\n", encoding="utf-8")
    monkeypatch.setenv("SECRET_KEY_FILE", str(f))
    monkeypatch.setenv("SECRET_KEY", "plain")
    s = config_mod.get_settings()
    assert s.secret_key == "fromfile"


def test_get_settings_refuses_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    """No SECRET_KEY and no SECRET_KEY_FILE anywhere -> RuntimeError at boot (D20):
    the app fails loudly instead of starting with a default session key."""
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.delenv("SECRET_KEY_FILE", raising=False)
    config_mod.get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        config_mod.get_settings()
```

Run: `.venv/bin/pytest tests/test_config.py -v`

Expected: 7 passed, 0 failed — all green against the promoted code. This suite is a regression guard on the D20 contract (file over plain, first line wins, unreadable file = loud RuntimeError, no secret at all = refuse to start), not a RED-GREEN cycle: the implementation is already verified. If any assertion FAILS, the promoted `config.py` diverged from the verified reference — stop and diff it against `reference/foundation-verified/src/cookbook/config.py` (hash `b833a6c9…` above) instead of "fixing" either side.

- [ ] **Step 2: Run the tests**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: 7 passed, 0 failed (all green against the promoted code).

- [ ] **Step 3: Commit**

```bash
git add tests/test_config.py
git commit -m "test: config + D20 file-first secret behavior (regression guard)"
```

---

### Task 3: App health endpoints `/healthz` + `/readyz` (TDD)

**Files:**
- Test: `tests/test_app.py`
- Consumes (Task 1): `cookbook.app.create_app()`, `cookbook.db.get_engine()`, `cookbook.config.get_settings()`

**Interfaces:**
- Consumes: `create_app() -> FastAPI`; `GET /healthz` → 200 `{"status": "ok", "version": str, "uptime_seconds": int}` (no DB); `GET /readyz` → 200 `{"status": "ready", "version": str, "checks": {"db": "ok", "media": "ok"}}` when healthy, else 503 `{"status": "not_ready", "checks": {...}}` + a `log.warning("readyz: NOT READY ...")` line (spec §9.5/D24: loud log, container keeps running).
- Produces: a green `tests/test_app.py` — **deterministic, no database required**. The `readyz` dependency is driven by monkeypatching `cookbook.app.get_engine` with a fake engine (both the 200 and the 503 branch are covered on any machine; the live in-container negative case remains the Task 5 exercise).

- [ ] **Step 1: Write the tests**

`tests/test_app.py` (final form — hermetic, `3 passed`):

```python
"""`/healthz` + `/readyz` endpoint tests (spec §9.5 / D19 / D24).

Design — reliable, hermetic, **no database required**:

* ``/readyz`` calls ``get_engine()`` internally (a real ``mysql+asyncmy``
  engine). Rather than pointing it at a live MariaDB (the dev compose
  publishes no host port), we monkeypatch ``cookbook.app.get_engine`` with a
  tiny fake engine. That makes both the healthy (200) and failing (503)
  branches fully deterministic on any machine.
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


# --- Fake async engine — stands in for get_engine()'s real MySQL engine. ---
class _FakeConn:
    async def execute(self, *_args, **_kwargs):
        # ``SELECT 1`` succeeds: readiness sees a reachable dependency.
        return None


class _FakeConnCtx:
    def __init__(self, fail: bool) -> None:
        self._fail = fail

    async def __aenter__(self):
        if self._fail:
            raise ConnectionRefusedError("simulated db outage (test)")
        return _FakeConn()

    async def __aexit__(self, *_exc_info):
        return False


class _FakeEngine:
    def __init__(self, fail: bool = False) -> None:
        self._fail = fail

    def connect(self):  # async context manager, as the real engine does
        return _FakeConnCtx(self._fail)


def _install_fake_engine(monkeypatch: pytest.MonkeyPatch, fail: bool) -> None:
    """``readyz`` looks up ``get_engine`` in app-module globals on each call,
    so patching the module attribute is enough (the real engine never builds)."""
    monkeypatch.setattr(app_mod, "get_engine", lambda: _FakeEngine(fail=fail))


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path) -> TestClient:
    import os
    for var in list(os.environ):
        if var.startswith(_STRIP_PREFIXES) or var in _STRIP_EXACT:
            monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)  # relative .env cannot reach the repo's
    monkeypatch.setenv("SECRET_KEY", "test-secret-key-not-a-real-secret")
    monkeypatch.setenv("MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("DB_HOST", "127.0.0.1")
    monkeypatch.setenv("DB_PORT", "3306")
    monkeypatch.setenv("DB_NAME", "cookbook")
    monkeypatch.setenv("DB_USER", "cookbook")
    monkeypatch.setenv("DB_PASSWORD", "test-password")
    app_mod.get_settings.cache_clear()
    db_mod.get_engine.cache_clear()
    db_mod.get_session.cache_clear()
    app = app_mod.create_app()  # validates SECRET_KEY (D20); does not touch DB
    return TestClient(app)


def test_healthz_liveness(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    # A failing fake engine would blow up if /healthz consulted it — so its
    # 200 proves /healthz never touches the database.
    _install_fake_engine(monkeypatch, fail=True)
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["version"] == "0.1.0" == __version__
    assert isinstance(body["uptime_seconds"], int)
    assert body["uptime_seconds"] >= 0


def test_readyz_healthy(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_engine(monkeypatch, fail=False)  # db reports ok
    r = client.get("/readyz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ready"
    assert body["version"] == "0.1.0"
    assert body["checks"] == {"db": "ok", "media": "ok"}


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

    # (b) the loud log line (spec §9.5 / D24).
    assert "readyz: NOT READY" in caplog.text

    # (c) app remains responsive — no self-shutdown.
    follow = client.get("/healthz")
    assert follow.status_code == 200
    assert follow.json()["status"] == "ok"
```

- [x] **Step 2: Run (deterministic green, no DB required)**

Run: `.venv/bin/pytest tests/test_app.py -v`
Expected: **3 passed** — all deterministic because `readyz`'s dependency is a fake injected engine (200 when its `connect()` succeeds, 503 when it raises); `/healthz` is proven DB-free by running with a failing fake in place. No MariaDB, network, or container state is involved.

- [x] **Step 3: Commit**

```bash
git add tests/test_app.py
git commit -m "test: /healthz + /readyz healthy and 503 shapes (spec §9.5)"
```

---

### Task 4: v0 model shape regression test (run inside the dev containers)

**Files:**
- Test: `tests/test_models.py`
- Consumes (Task 1): `cookbook.models.Base`, `cookbook.models.User`, `cookbook.db.get_engine()`, `cookbook.db.session_scope()`

**Interfaces:**
- Consumes: `User` columns per `reference/foundation-verified/src/cookbook/models.py`: `id` CHAR(36) PK, `username` VARCHAR(60) UNIQUE NOT NULL, `password_hash` VARCHAR(255) NOT NULL, `display_name` VARCHAR(100) NULL, `role` VARCHAR(10) NOT NULL (default `editor`), `is_active` BOOLEAN NOT NULL (default True), `created_at`/`updated_at` DATETIME `server_default=func.now()`.
- Produces: a green `tests/test_models.py` executed against the dev `db` container's `cookbook` schema — independently proving the v0 migration produced the exact spec §3.5 shape.

**Execution location:** the dev `db` container publishes NO host port (compose is deliberate — host reachability to the dev DB is not part of the contract). Therefore these tests run **inside the `app` container**, where `DB_HOST=db` + the compose network already connect to it. `uv.lock` pins `pytest`, `anyio`, `pytest-asyncio`, `respx` (the dev group in `pyproject.toml`), which is why the image ships them.

- [ ] **Step 1: Write the tests**

`tests/test_models.py`:

```python
"""v0 `users` regression tests — run INSIDE the app container against the dev db
(compose network; the db publishes no host port by design).

Requires: dev stack up (`docker compose up -d`), migrations applied (the app
entrypoint does this at boot).
"""
from __future__ import annotations

import asyncio
import uuid

from sqlalchemy import text

from cookbook.db import get_engine, session_scope
from cookbook.models import User

EXPECTED_COLUMNS = {
    "id", "username", "password_hash", "display_name",
    "role", "is_active", "created_at", "updated_at",
}


def test_users_table_shape() -> None:
    """The v0 migration produced the exact spec §3.5 `users` shape (the
    session/lockout columns belong to the auth plan, not v0)."""

    async def run() -> None:
        engine = get_engine()
        async with engine.connect() as conn:
            res = await conn.execute(text(
                "SELECT column_name, column_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = 'users'"
            ))
            cols = {row[0]: {"type": row[1], "nullable": row[2]} for row in res}
            assert set(cols) == EXPECTED_COLUMNS, f"columns: {set(cols)}"
            assert cols["id"]["type"] == "char(36)"
            assert cols["username"]["type"] == "varchar(60)"
            assert cols["username"]["nullable"] == "NO"
            assert cols["password_hash"]["nullable"] == "NO"
            assert cols["display_name"]["nullable"] == "YES"
            assert cols["role"]["type"] == "varchar(10)"
            assert cols["role"]["nullable"] == "NO"
            res2 = await conn.execute(text(
                "SELECT column_name FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = 'users' "
                "AND non_unique = 0"
            ))
            unique_cols = {row[0] for row in res2}
            assert "id" in unique_cols and "username" in unique_cols

    asyncio.run(run())


def test_user_roundtrip() -> None:
    """INSERT + read back through session_scope: server defaults fill
    created_at/updated_at; role defaults to 'editor'; then clean up so the
    dev DB stays tidy (it is disposable per D22, but tidy anyway)."""

    async def run() -> None:
        uid = str(uuid.uuid4())
        async with session_scope() as session:
            session.add(User(
                id=uid,
                username=f"roundtrip_{uid[:8]}",
                password_hash="x" * 60,
            ))
        async with session_scope() as session:
            u = await session.get(User, uid)
            assert u is not None
            assert u.role == "editor"
            assert u.is_active is True
            assert u.created_at is not None
        async with session_scope() as session:
            await session.execute(text("DELETE FROM users WHERE id = :id"), {"id": uid})

    asyncio.run(run())
```

Notes: `session_scope()` is an async context manager (verified in `reference/foundation-verified/src/cookbook/db.py`) — the outer `async with` exits, then `session.commit()` runs, so the row is durable before the second `session_scope` reads it. `await session.get(User, uid)` is the async ORM API. Column types are asserted as MariaDB reports them (`char(36)`, `varchar(60)`) — the migration file (hash `92bd6f0a…`) is the contract.

- [ ] **Step 2: Bring up the dev stack and run the tests inside the app container**

```bash
docker compose up -d
# wait for healthy (service_healthy gates the app start; allow the healthcheck loop)
for i in $(seq 1 30); do
  docker compose ps --format '{{.Name}} {{.Status}}' | grep -E '^db .*healthy' && break
  sleep 2
done
docker cp tests app:/tests
docker exec app python -m pytest /tests/test_models.py -v
```

Expected: `db ... healthy`; `2 passed`. (If `users` table is absent — i.e. the entrypoint migration somehow didn't run — the shape test fails with `columns: set()`, which points at the entrypoint; check `docker compose logs app | grep -E '\[entrypoint\]'` first.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_models.py
git commit -m "test: v0 users table shape + roundtrip (spec §3.5, in-container)"
```

---

### Task 5: Docker e2e — build, boot migrations, health, negative readyz (the gate)

**Files:**
- Consumes: everything from Tasks 1–4 (repo-root layout, `uv.lock`, compose, entrypoint)

**Interfaces:**
- Produces: a freshly built `cookbook:dev` image from the repo; a running `db`+`app` dev stack with `users` table migrated at boot; proven 200 shapes for `/healthz` and `/readyz`; proven 503 + loud log line for the media negative case; proven prod-pointer config path (`DB_HOST=<host>`). This task is the verification gate for the whole foundation.

- [ ] **Step 1: Pre-flight (loud, per the cookbook-app skill's docker note)**

```bash
docker ps >/dev/null 2>&1 && echo "docker OK" || echo "docker UNREACHABLE from this shell (owner: sudo usermod -aG docker stephanie; re-login)"
ss -ltn 2>/dev/null | grep -E ':(3306|8000)' && echo "WARN: something already on 3306/8000" || echo "ports 3306/8000 free"
docker ps --format '{{.Names}}' | grep -E '^(db|app)$' && echo "dev containers already exist (from a prior run)" || echo "no dev containers"
```

Expected: `docker OK`, `ports 3306/8000 free` (or the known state noted), and a decision: if `db`/`app` containers from a prior run exist, `docker compose down` first (their data under `/mnt/container/{db,app}` is disposable per D22 — confirmed by the handoff). The unrelated `foundcheck-db-1` must be left untouched.

- [ ] **Step 2: Build the image from the repo (real gate, not the retained image)**

```bash
docker compose build
docker images | grep cookbook
```

Expected: a new `cookbook:dev` digest (will not match the retained sha256:1a273aa35a57… unless inputs are byte-identical — that's fine; the retained image is a fallback, the build is the gate). The build's `uv sync --frozen` proves `uv.lock` is complete.

- [ ] **Step 3: Boot the dev stack**

```bash
docker compose up -d
docker compose ps
```

Expected: `db` reaches `healthy`; `app` starts after (depends_on service_healthy). If `app` is `restarting`, the crash loop signature is `exit 127` (missing alembic/uvicorn — the `UV_PROJECT_ENVIRONMENT` fix) or a failed `alembic upgrade head` — capture with `docker compose logs app | tail -40` before touching anything else.

- [ ] **Step 4: Positive health — the exact verified 200 shapes**

```bash
curl -fsS http://127.0.0.1:8000/healthz | python3 -m json.tool
curl -fsS http://127.0.0.1:8000/readyz | python3 -m json.tool
```

Expected (per the 2026-09-27 verification runs):
- `/healthz` → 200 `{"status": "ok", "version": "0.1.0", "uptime_seconds": <int>}`
- `/readyz` → 200 `{"status": "ready", "version": "0.1.0", "checks": {"db": "ok", "media": "ok"}}`

- [ ] **Step 5: Verify the entrypoint actually ran migrations (D23)**

```bash
docker exec -i db mariadb -h 127.0.0.1 -ucookbook -pdevpassword cookbook -e "SHOW TABLES; SELECT version_num FROM alembic_version;"
docker compose logs app | grep -E '\[entrypoint\]'
```

Expected: `alembic_version` + `users` tables exist; `version_num` = `6d22befb6ec9`; entrypoint log lines `[entrypoint] applying database migrations (alembic upgrade head)` and `[entrypoint] migrations OK; exec: ...`.

- [ ] **Step 6: Negative readyz — media read-only (spec §9.5: 503 + loud log + container keeps running)**

```bash
chmod o-w /mnt/container/app   # host bind mount == the container's /media (owner directive: host-side storage)
curl -s -o /tmp/readyz_neg.json -w '%{http_code}\n' http://127.0.0.1:8000/readyz
cat /tmp/readyz_neg.json
docker compose logs app 2>&1 | grep -E 'readyz: NOT READY' | tail -1
docker compose ps --format '{{.Names}}\t{{.Status}}' | grep app
chmod u+w /mnt/container/app   # restore
curl -fsS http://127.0.0.1:8000/readyz >/dev/null && echo "readyz back to 200"
```

Expected:
- HTTP `503`
- body `{"status": "not_ready", "version": "0.1.0", "checks": {"db": "ok", "media": "error: PermissionError"}}` (the 2026-09-27 verified negative case; `db` stays `ok` — the probe is per-check)
- a `WARNING:cookbook:readyz: NOT READY {...}` line in the app log
- the `app` container still `Up` (no self-shutdown, D24)
- after `chmod u+w`, `/readyz` returns to 200

Note: if the app runtime user cannot write the media dir for a reason other than permissions (e.g. SELinux label), the error string will differ; the assertion is `status == not_ready`, `checks.db == ok`, `checks.media` starts with `error:`, plus the log line + container staying up.

- [ ] **Step 7: Config path proof — the prod mechanism (DB_HOST = arbitrary host)**

The positive case already proved it: `app` reaches `db` by compose hostname (`DB_HOST=db` in `docker-compose.yml`), which is the identical code path prod uses with `DB_HOST=<LAN MariaDB>` — one code path, only the `.env` value differs (owner config contract; 2026-09-27 cross-container `extdb` proof recorded in the handoff). Record it in the session notes rather than re-proving with a second scratch network:

```bash
docker compose config --services
```

Expected: two services, `db` + `app`; `app.environment.DB_HOST=db`. No further action.

- [ ] **Step 8: Tear down (dev data disposable, D22)**

```bash
docker compose down
docker rmi -f cookbook:dev 2>/dev/null || true   # drop the build under test only if owner approves; otherwise keep both digests
```

Keep the stack down unless the owner wants it running. The `/mnt/container/{db,app}` dirs may remain (disposable data) or be removed at owner's call.

- [ ] **Step 9: Commit the session gate record**

```bash
mkdir -p docs/superpowers/notes
```

Append a dated entry to `docs/superpowers/notes/2026-09-28-foundation-e2e.md` (new file) with: build digest, `docker compose ps` output, both health bodies, the negative body + log line, `SHOW TABLES` + `version_num`, and the `DB_HOST=db` note. Commit it:

```bash
git add docs/superpowers/notes/2026-09-28-foundation-e2e.md
git commit -m "docs: record 2026-09-28 foundation e2e gate results"
```

---

## Self-Review (per the writing-plans skill)

1. **Spec coverage (foundation scope):** D1 (single image — Task 1 Dockerfile + Task 5 build), D2 (MariaDB dev+prod, no SQLite — compose + config URL + Task 5 Step 7), D19 (health split — Task 3), D20 (file-first secrets — Task 2), D22 (disposable dev DB / preservable media — compose binds `/mnt/container/{db,app}`, Task 5 Step 8), D23 (Alembic forward-only at entrypoint — Task 5 Steps 5), D24 (boring ops: no extra services, loud failure, no self-shutdown — Task 5 Step 6), §9.5 (both endpoints + failure mode — Tasks 3 + 5), §9.6 (entrypoint migration failure = visible non-zero exit — code in the promoted entrypoint; the happy path is gated, the failure path is the same `set -euo pipefail` line verified in the runs). Foundation-only items: `users` table is the v0 migration per the handoff's "rest of models belong in the data-model plan" — deliberately not expanded here. **No gaps in the foundation scope.**
2. **Placeholder scan:** none — every step carries a real command; the one judgment call (Step 6's `error:` string) is documented with its exact assertion.
3. **Type consistency:** `get_settings()`, `get_engine()`, `session_scope()`, `Settings.database_url`, `User` fields — all used exactly as defined in the promoted reference files (hash-verified in Task 1 Step 2).

---

## Out of scope for this plan (lands in later plans, per the handoff)

- All models beyond `users` (data-model plan; DL-3 `section[]` body shape binds it)
- Auth (§4), public read (§5), ingest (§2), WP importer (§3 + spike)
- Real `README.md` (data-model plan)
- Backup/restore CLI (§9.7), `/setup` (§6.4)

---
