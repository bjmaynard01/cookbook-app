"""v0 `users` regression tests — run INSIDE the app container against the dev db
(compose network; the db publishes no host port by design).

Requires: dev stack up (`docker compose up -d`), migrations applied (the app
entrypoint does this at boot).

Task 4 findings (2026-10-01, from `task/4-user-model-tests` on `main` bf4669d):

1. `users.id` is `varchar(36)` on the live server — `sa.String(36)` renders as
   VARCHAR under the MySQL-family dialect. Spec §2/§3.5 says `CHAR(36) PK`.
   CHAR(36)/VARCHAR(36) are functionally equivalent here (36-char UUID, no
   CHAR padding semantics in play), so this test accepts either keyword and
   pins the length. Flagged as a spec-vs-migration wording mismatch for the
   owner; the test guards what v0 actually produced.

2. `cookbook.db.session_scope` is an `async def` ... `yield` coroutine that was
   never decorated with `@asynccontextmanager`, so `async with session_scope()`
   raises `TypeError: 'async_generator' object does not support the
   asynchronous context manager protocol` (verified at runtime in the app
   container). The defect is in the promoted code (byte-identical to
   `reference/foundation-verified/`, hash `ce843030…`). Per Task 4's scope
   (tests only — no app-code changes), this test drives the generator with a
   minimal local shim (`_SessionScope`) that implements the commit-on-clean-exit
   / rollback-on-exception contract `session_scope`'s own docstring
   documents. The upstream fix is a one-line `@asynccontextmanager` decorator;
   flagged for owner approval. Do NOT silently patch `db.py` in this task.

3. AsyncMy connections are bound to the event loop that created them, and the
   engine pool is process-global (`lru_cache`), so each test disposes the
   engine (drains the pool) before returning — otherwise the next test's
   fresh `asyncio.run()` loop reuses a pooled connection created by the
   dead loop and fails with "Future attached to a different loop"
   (observed in-container 2026-10-01; the plan's draft test has this
   latent issue too — it passes today only by test order and by the first
   test's connection returning the pool clean enough to be re-pinged).
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid

from sqlalchemy import text

from cookbook.db import get_engine, session_scope
from cookbook.models import User

EXPECTED_COLUMNS = {
    "id", "username", "password_hash", "display_name",
    "role", "is_active", "created_at", "updated_at",
}


def _dispose_engine() -> None:
    """Drain the process-global engine pool between tests.

    AsyncMy connections belong to the event loop that created them, and
    each test runs under its own ``asyncio.run()`` loop. Without this, the
    next test would check out a pooled connection from the previous (now
    closed) loop and fail with ``RuntimeError: ... attached to a different
    loop``.
    """
    asyncio.run(get_engine().dispose())


class _SessionScope:
    """Drives `session_scope()`'s async-generator with the
    `@asynccontextmanager` protocol it is missing.

    Semantics are exactly as documented in `cookbook.db.session_scope`:
    yield the session; commit on clean exit; rollback on exception.
    Kept here (not in `db.py`) so Task 4's diff stays scoped to the test file;
    the one-line upstream fix is flagged separately for owner approval.
    """

    def __init__(self) -> None:
        self._gen = session_scope()
        self._session = None

    async def __aenter__(self):
        self._session = await self._gen.__anext__()
        return self._session

    async def __aexit__(self, exc_type, exc, tb):
        if exc_type is None:
            # resume past the yield -> commit() on the happy path
            with contextlib.suppress(StopAsyncIteration):
                await self._gen.__anext__()
        else:
            # push the exception into the generator at its yield ->
            # the in-generator except-block rolls back and re-raises
            with contextlib.suppress(Exception, StopAsyncIteration):
                await self._gen.athrow(exc)
        return False


def test_users_table_shape() -> None:
    """The v0 migration produced the exact v0 `users` shape (spec §3.5 —
    the sessions/lockout columns belong to the auth plan, not v0):
    8 columns, expected types + nullability, `id` PK, UNIQUE `username`.

    Uses `get_engine()` directly (read-only); no rows are written here.
    The `users` table started at 0 rows (verified in dev) and this test
    leaves it exactly as it found it.
    """

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
            # char(36)/varchar(36) are equivalent on MySQL-family dialects;
            # the spec says CHAR(36), the migration emits VARCHAR(36)
            assert cols["id"]["type"] in ("char(36)", "varchar(36)")
            assert cols["id"]["nullable"] == "NO"
            assert cols["username"]["type"] == "varchar(60)"
            assert cols["username"]["nullable"] == "NO"
            assert cols["password_hash"]["type"] == "varchar(255)"
            assert cols["password_hash"]["nullable"] == "NO"
            assert cols["display_name"]["type"] == "varchar(100)"
            assert cols["display_name"]["nullable"] == "YES"
            assert cols["role"]["type"] == "varchar(10)"
            assert cols["role"]["nullable"] == "NO"
            assert cols["is_active"]["nullable"] == "NO"
            assert cols["created_at"]["type"] == "datetime"
            assert cols["created_at"]["nullable"] == "NO"
            assert cols["updated_at"]["type"] == "datetime"
            assert cols["updated_at"]["nullable"] == "NO"
            res2 = await conn.execute(text(
                "SELECT column_name FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = 'users' "
                "AND non_unique = 0"
            ))
            unique_cols = {row[0] for row in res2}
            assert "id" in unique_cols, f"PK missing: {unique_cols}"
            assert "username" in unique_cols, f"UNIQUE(username) missing: {unique_cols}"

    asyncio.run(run())
    _dispose_engine()


def test_user_roundtrip() -> None:
    """INSERT + read back through `session_scope`'s commit-on-exit contract:
    `role` defaults to `editor`, `is_active` to `True`, and the server-side
    `now()` defaults fill `created_at`/`updated_at`.

    Hygiene: a UUID-derived `username` and a `finally`-block cleanup that
    deletes the test row by its exact `(id, username)` pair and verifies it
    is gone — so the dev `users` table (0 rows at test time) is left exactly
    as it was, even if an assertion in the body fails. Existing user
    records are never touched by this test.
    """

    async def run() -> None:
        uid = str(uuid.uuid4())
        username = f"t4_roundtrip_{uid[:12]}"
        inserted: bool = False
        try:
            async with _SessionScope() as session:
                session.add(User(
                    id=uid,
                    username=username,
                    password_hash="x" * 60,
                ))
            inserted = True  # committed inside the scope's clean exit

            async with _SessionScope() as session:
                u = await session.get(User, uid)
                assert u is not None, "row not found after commit"
                assert u.username == username
                assert u.role == "editor"
                assert u.is_active is True
                assert u.created_at is not None
                assert u.updated_at is not None
                # display_name was left NULL
                assert u.display_name is None
        finally:
            engine = get_engine()
            if inserted:
                async with engine.begin() as conn:
                    rc = (await conn.execute(
                        text("DELETE FROM users WHERE id=:id AND username=:username"),
                        {"id": uid, "username": username},
                    )).rowcount
                    assert rc == 1, (
                        f"cleanup: expected exactly 1 row for ({uid}, {username}), "
                        f"deleted {rc}"
                    )
            # verify (unconditionally) that our row really is gone
            async with engine.connect() as conn:
                remaining = (await conn.execute(
                    text("SELECT COUNT(*) FROM users WHERE id=:id"), {"id": uid}
                )).scalar()
                assert remaining == 0, f"test row {uid} still present after cleanup"

    asyncio.run(run())
    _dispose_engine()
