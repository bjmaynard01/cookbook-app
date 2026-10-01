"""v0 `users` regression tests — run INSIDE the app container against the dev db
(compose network; the db publishes no host port by design).

Requires: dev stack up (`docker compose up -d`), migrations applied (the app
entrypoint does this at boot).

Follow-up (2026-09-27 → 2026-10-01): the owner resolved the id-type wording —
UUID ids and their FKs are `VARCHAR(36)` (spec + plan updated in f2a5750,
Decision Log DL-5) — so this file pins exactly `varchar(36)` for `users.id`.

`session_scope` in `cookbook.db` was originally written as a bare
`async def ... yield` without `@asynccontextmanager`, so
`async with session_scope()` raised `TypeError: 'async_generator' object does
not support the asynchronous context manager protocol` (first reproduced with
a local shim in the original Task 4 pass — which masked the real defect).
Owner decision 2026-10-01: fix `db.py` (add `@asynccontextmanager`) and use
the real API in the roundtrip test; this test is that regression guard.

AsyncMy connections are bound to the event loop that created them, and the
engine pool is process-global (`lru_cache`), so each test disposes the engine
in a `finally` — BEFORE the next test's fresh `asyncio.run()` loop can
re-checkout a pooled connection created by the dead loop
("Future attached to a different loop"). The `finally` also means disposal
runs even when an assertion in the test body fails.
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


def _dispose_engine() -> None:
    """Drain the process-global engine pool between tests.

    Called from a `finally`, so it runs even when a test assertion fails —
    see the module docstring for why skipping it breaks the next test.
    """
    asyncio.run(get_engine().dispose())


def test_users_table_shape() -> None:
    """The v0 migration produced the exact v0 `users` shape (spec §3.5 —
    the sessions/lockout columns belong to the auth plan, not v0):
    8 columns, expected types + nullability, `id` is the primary key,
    `username` carries its own separate UNIQUE constraint.

    Uses `get_engine()` directly (read-only); no rows are written here.
    The `users` table started at 0 rows (verified in dev) and this test
    leaves it exactly as it found it.

    `id` type is pinned to exactly `varchar(36)` — owner decision DL-5
    (f2a5750), no CHAR/VARCHAR equivalence.
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
            assert cols["id"]["type"] == "varchar(36)", f"id type: {cols['id']['type']!r}"
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

            # Index-level assertions: (column, index_name) for every
            # non-unique (PK/UNIQUE) index on the table.
            res2 = await conn.execute(text(
                "SELECT column_name, index_name FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = 'users' "
                "AND non_unique = 0"
            ))
            col_to_index = {row[0]: row[1] for row in res2}

            # `id` is the primary key — under MariaDB `information_schema`,
            # the PK index on InnoDB is named `PRIMARY`.
            assert col_to_index.get("id") == "PRIMARY", (
                f"id is not the primary key: {col_to_index}"
            )
            # `username` has its own UNIQUE constraint: a non-unique index
            # that is NOT the primary key (its own named unique index).
            username_index = col_to_index.get("username")
            assert username_index is not None, (
                f"no non-unique index on username: {col_to_index}"
            )
            assert username_index != "PRIMARY", (
                f"username's unique index is unexpectedly the PK: {col_to_index}"
            )

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()


def test_user_roundtrip() -> None:
    """INSERT + read back through the real `session_scope()` API — the exact
    `async with session_scope()` usage the app code relies on (regression
    guard against the missing `@asynccontextmanager` bug): on clean exit the
    scope commits; on exception it rolls back.

    Expected defaults survive the roundtrip: `role` = `editor`,
    `is_active` = True, server-side `now()` fills `created_at`/`updated_at`,
    `display_name` stays NULL.

    Hygiene: a UUID-derived `username`; `finally`-block cleanup deletes the
    test row by its exact `(id, username)` pair and verifies it is gone —
    so the dev `users` table (0 rows at test time) is left exactly as it was
    EVEN IF AN ASSERTION IN THE BODY FAILS. Existing user records are never
    touched. The engine is disposed in `finally` too (see module docstring).
    """

    async def run() -> None:
        uid = str(uuid.uuid4())
        username = f"t4_roundtrip_{uid[:12]}"
        inserted = False
        try:
            async with session_scope() as session:
                session.add(User(
                    id=uid,
                    username=username,
                    password_hash="x" * 60,
                ))
            inserted = True  # committed inside the scope's clean exit

            async with session_scope() as session:
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

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()
