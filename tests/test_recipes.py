"""v1 `recipes` shape tests — run against the ISOLATED test MariaDB
(task/6.1; the dev stack must remain untouched; see plan's shared contract).

AsyncMy connections are bound to the event loop that created them, and the
engine pool is process-global (`lru_cache` in `cookbook.db`). So each test does
all of its DB work inside a single `async def run()` (one `asyncio.run` loop),
and disposes the engine in a `finally` — BEFORE the next test's fresh loop can
re-checkout a pooled connection created by the now-dead loop. This is the same
pattern as `tests/test_models.py`.
"""
from __future__ import annotations

import asyncio
import uuid

import sqlalchemy
from sqlalchemy import text

from cookbook.db import get_engine, session_scope
from cookbook.models import Recipe

EXPECTED_COLUMNS = {
    "id", "slug", "title", "description",
    "servings_text", "servings_number",
    "prep_minutes", "cook_minutes", "total_minutes",
    "home_featured", "home_position",
    "source_system", "source_id", "source_name", "source_url",
    "extraction_method", "imported_at", "published_date",
    "image_path", "created_by", "last_modified_by",
    "created_at", "updated_at",
}
EXPECTED_TYPES = {
    "id": "varchar(36)", "slug": "varchar(200)", "title": "varchar(300)",
    "description": "text",
    "servings_text": "varchar(100)", "servings_number": "decimal(10,2)",
    "prep_minutes": "int(11)", "cook_minutes": "int(11)", "total_minutes": "int(11)",
    "home_featured": "tinyint(1)", "home_position": "int(11)",
    "source_system": "varchar(20)", "source_id": "varchar(64)",
    "source_name": "varchar(300)", "source_url": "varchar(2000)",
    "extraction_method": "varchar(20)",
    "imported_at": "datetime", "published_date": "date",
    "image_path": "varchar(500)",
    "created_by": "varchar(36)", "last_modified_by": "varchar(36)",
    "created_at": "datetime", "updated_at": "datetime",
}


def _dispose_engine() -> None:
    """Drain the process-global engine pool between tests (see module docstring)."""
    asyncio.run(get_engine().dispose())


def test_recipes_table_shape() -> None:
    """Exact v1 column set + MariaDB-reported types + nullability, and the three
    unique index shapes (complete ordered column lists — the Task 4 pinning style).
    Read-only; leaves the table exactly as it found it."""

    async def run() -> None:
        engine = get_engine()
        async with engine.connect() as conn:
            res = await conn.execute(text(
                "SELECT column_name, column_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = 'recipes'"))
            cols = {r[0]: {"type": r[1], "nullable": r[2]} for r in res}
            assert set(cols) == EXPECTED_COLUMNS, f"columns: {set(cols)}"
            for col, typ in EXPECTED_TYPES.items():
                assert cols[col]["type"] == typ, f"{col}: {cols[col]['type']} != {typ}"
            assert cols["home_featured"]["nullable"] == "NO"
            assert cols["title"]["nullable"] == "NO"
            assert cols["slug"]["nullable"] == "NO"

            res2 = await conn.execute(text(
                "SELECT index_name, column_name, seq_in_index "
                "FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = 'recipes' "
                "AND non_unique = 0 "
                "ORDER BY index_name, seq_in_index"))
            indexes: dict[str, list[str]] = {}
            for name, col, _seq in res2:
                indexes.setdefault(name, []).append(col)
            assert indexes.get("PRIMARY") == ["id"], f"PRIMARY: {indexes.get('PRIMARY')!r}"
            assert indexes.get("uq_recipes_slug") == ["slug"], f"slug uniq: {indexes}"
            assert indexes.get("uq_recipes_source") == ["source_system", "source_id"], (
                f"source uniq: {indexes}")

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()


def test_recipes_fk_targets() -> None:
    """`created_by` / `last_modified_by` are FKs to `users.id` with ON DELETE
    SET NULL (D10 attribution only). Read-only."""

    async def run() -> None:
        engine = get_engine()
        async with engine.connect() as conn:
            res = await conn.execute(text(
                "SELECT kcu.column_name, kcu.referenced_table_name, rc.delete_rule "
                "FROM information_schema.key_column_usage kcu "
                "JOIN information_schema.referential_constraints rc "
                "  ON rc.constraint_name = kcu.constraint_name "
                " AND rc.constraint_schema = kcu.constraint_schema "
                "WHERE kcu.table_schema = DATABASE() AND kcu.table_name = 'recipes' "
                "ORDER BY kcu.column_name"))
            by_col = {r[0]: (r[2], r[1]) for r in res}  # col -> (delete_rule, ref_table)
            assert by_col.get("created_by") == ("SET NULL", "users"), by_col.get("created_by")
            assert by_col.get("last_modified_by") == ("SET NULL", "users"), by_col.get("last_modified_by")

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()


def test_recipe_defaults_roundtrip() -> None:
    """INSERT with only required fields: server defaults fill `created_at` /
    `updated_at`, `home_featured` defaults FALSE. Read back through the real
    `session_scope()`, then clean up and re-verify the row is gone — even if an
    assertion in the body fails."""
    rid = str(uuid.uuid4())
    inserted = False

    async def run() -> None:
        nonlocal inserted
        try:
            async with session_scope() as s:
                s.add(Recipe(id=rid, slug=f"rt-{rid[:8]}", title="Roundtrip title"))
            inserted = True  # committed on clean scope exit

            async with session_scope() as s:
                r = await s.get(Recipe, rid)
                assert r is not None, "row not found after commit"
                assert r.home_featured is False
                assert r.created_at is not None and r.updated_at is not None
                assert r.servings_number is None and r.prep_minutes is None
        finally:
            engine = get_engine()
            if inserted:
                async with engine.begin() as conn:
                    rc = (await conn.execute(
                        text("DELETE FROM recipes WHERE id = :id"), {"id": rid})).rowcount
                    assert rc == 1, f"cleanup: expected 1 row, deleted {rc}"
            async with engine.connect() as conn:
                remaining = (await conn.execute(
                    text("SELECT COUNT(*) FROM recipes WHERE id = :id"), {"id": rid})).scalar()
                assert remaining == 0, "test row still present after cleanup"

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()


def test_recipe_source_idempotency_key() -> None:
    """The importer idempotency key `UNIQUE (source_system, source_id)` (D5/D8):
    a second recipe with the same `(wordpress, 1234)` is rejected, while two
    DISTINCT `web` recipes with `source_id IS NULL` are both allowed (MariaDB
    multiple-NULL semantics). All test rows are cleaned up and verified gone,
    even when an assertion in the body fails (same pattern as
    `test_recipe_defaults_roundtrip`)."""
    rid1, rid2, rid3, rid4 = (str(uuid.uuid4()) for _ in range(4))
    committed: list[str] = []  # ids actually reached the DB; cleanup gate

    async def try_add(r: Recipe) -> bool:
        """Return True if committed; False if rejected by the DB (IntegrityError)."""
        try:
            async with session_scope() as s:
                s.add(r)
        except sqlalchemy.exc.IntegrityError as exc:
            assert "uq_recipes_source" in str(exc) or "Duplicate" in str(exc), str(exc)
            return False
        return True

    async def cleanup() -> None:
        """Delete this test's rows and verify they are gone.

        Explicit named params (not IN-clause tuple expansion — asyncmy rejects
        the expanded `IN (%s)` binding with "Illegal parameter data types"
        under SQLAlchemy 2.0 insertmanyvalues); see the `IN (:a,:b,:c)` style
        above in this file."""
        if committed:
            ph = ",".join(f":r{i}" for i in range(len(committed)))
            params = {f"r{i}": rid for i, rid in enumerate(committed)}
            async with get_engine().begin() as conn:
                rc = (await conn.execute(
                    text(f"DELETE FROM recipes WHERE id IN ({ph})"),
                    params)).rowcount
                assert rc == len(committed), (
                    f"cleanup: expected {len(committed)} rows, deleted {rc}")
        async with get_engine().connect() as conn:
            remaining = (await conn.execute(
                text("SELECT COUNT(*) FROM recipes WHERE id IN (:a,:b,:c,:d)"),
                {"a": rid1, "b": rid2, "c": rid3, "d": rid4})).scalar()
            assert remaining == 0, "probe rows still present after cleanup"

    async def run() -> None:
        nonlocal committed
        try:
            async with session_scope() as s:
                s.add(Recipe(id=rid1, slug=f"src-{rid1[:8]}", title="S1",
                             source_system="wordpress", source_id="1234"))
                s.add(Recipe(id=rid2, slug=f"src2-{rid2[:8]}", title="S2",
                             source_system="wordpress", source_id="5678"))
                # two DISTINCT web recipes, both with source_id NULL — both
                # must be accepted by the same (source_system, source_id) key
                # (MariaDB unique indexes treat NULLs as distinct)
                s.add(Recipe(id=rid3, slug=f"web-{rid3[:8]}", title="Web A",
                             source_system="web", source_id=None))
                s.add(Recipe(id=rid4, slug=f"web2-{rid4[:8]}", title="Web B",
                             source_system="web", source_id=None))
            committed += [rid1, rid2, rid3, rid4]  # committed on clean scope exit

            # both NULL-source web rows must be present and distinct
            async with session_scope() as s:
                present = (await s.execute(
                    sqlalchemy.select(Recipe.id, Recipe.source_system,
                                      Recipe.source_id).where(
                        (Recipe.id == rid3) | (Recipe.id == rid4)))).all()
            got = {row[0] for row in present}
            assert got == {rid3, rid4}, f"web rows: {got!r}"
            assert all(sys == "web" and sid is None
                       for _id, sys, sid in present), (
                f"web rows not (web, NULL source_id): {present!r}")

            # duplicate (source_system, source_id) must be rejected by the DB;
            # if it is (wrongly) accepted, track it so cleanup still removes it
            dup_id = str(uuid.uuid4())
            dup_added = await try_add(Recipe(
                id=dup_id, slug=f"dup-{dup_id[:8]}",
                title="Duplicate source", source_system="wordpress", source_id="1234"))
            if dup_added:
                committed.append(dup_id)
            assert dup_added is False, "duplicate (source_system, source_id) was ACCEPTED"
        finally:
            await cleanup()

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()
