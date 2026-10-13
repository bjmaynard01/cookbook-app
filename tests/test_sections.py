"""v2 `recipe_sections` shape + behavior tests (DL-3, D4). Run against the
ISOLATED test MariaDB (task/6.2; the dev stack must remain untouched; see the
plan's shared contract).

Conventions carry over from `tests/test_recipes.py`:
- Every test does all of its DB work inside one `async def run()` (one
  `asyncio.run` loop), because Asyncmy connections are bound to the loop that
  opened them and the engine pool is process-global (`lru_cache`).
- Cleanup runs in `run()`'s own `finally` (async `await`, NOT a nested
  `asyncio.run` — same pattern as `test_recipe_source_idempotency_key`), so
  probe rows are removed even if an assertion in the body failed.
- `_dispose_engine()` runs in the outer `finally` so the pool is drained
  between the two fresh event loops (otherwise the next test's loop would
  reuse a connection created by the dead loop).
"""
from __future__ import annotations

import asyncio
import uuid

import sqlalchemy
from sqlalchemy import func, text

from cookbook.db import get_engine, session_scope
from cookbook.models import Recipe, RecipeSection

EXPECTED_COLUMNS = {"id", "recipe_id", "position", "heading", "kind"}
EXPECTED_TYPES = {
    "id": "varchar(36)",
    "recipe_id": "varchar(36)",
    "position": "int(11)",
    "heading": "varchar(200)",
    "kind": "enum('ingredients','steps','note')",
}


def _dispose_engine() -> None:
    """Drain the process-global engine pool between tests (see test_recipes.py)."""
    asyncio.run(get_engine().dispose())


async def _count_rows(table: str, where_sql: str = "", params: dict | None = None) -> int:
    """`SELECT COUNT(*)` helper kept in one place (table names are fixed literals
    in this file — safe to interpolate)."""
    sql = f"SELECT COUNT(*) FROM {table}"
    if where_sql:
        sql += f" WHERE {where_sql}"
    async with get_engine().connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar()


def test_sections_table_shape() -> None:
    """Task item 1: exact column set, MariaDB-reported types, and nullability."""

    async def run() -> None:
        engine = get_engine()
        async with engine.connect() as conn:
            res = await conn.execute(text(
                "SELECT column_name, column_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = 'recipe_sections'"))
            cols = {r[0]: {"type": r[1], "nullable": r[2]} for r in res}
            assert set(cols) == EXPECTED_COLUMNS, f"columns: {sorted(cols)}"
            for col, typ in EXPECTED_TYPES.items():
                assert cols[col]["type"] == typ, f"{col}: {cols[col]['type']!r} != {typ!r}"
            for col in ("id", "recipe_id", "position", "kind"):
                assert cols[col]["nullable"] == "NO", (
                    f"{col} should be NOT NULL, got {cols[col]!r}")
            assert cols["heading"]["nullable"] == "YES", (
                f"heading should be nullable, got {cols['heading']!r}")

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()


def test_sections_indexes() -> None:
    """Task items 2 + 3: PK contains ONLY `id`; the named
    `uq_sections_recipe_position == [recipe_id, position]`; AND the explicit
    non-unique FK-lookup index `ix_recipe_sections_recipe_id == [recipe_id]`.
    The explicit index is asserted *separately* from the ones we've already
    pinned (PK + composite UNIQUE) — it is `NON-UNIQUE`, so a query that only
    filters on `non_unique = 0` would never surface it (Codex gap #2)."""

    async def run() -> None:
        engine = get_engine()
        async with engine.connect() as conn:
            # unique indexes (for PK + composite UNIQUE assertions)
            ures = await conn.execute(text(
                "SELECT index_name, column_name, seq_in_index "
                "FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = 'recipe_sections' "
                "AND non_unique = 0 ORDER BY index_name, seq_in_index"))
            unique_indexes: dict[str, list[str]] = {}
            for name, col, _seq in ures:
                unique_indexes.setdefault(name, []).append(col)

            # non-unique index (the explicit FK-lookup index)
            nres = await conn.execute(text(
                "SELECT index_name, column_name, seq_in_index "
                "FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = 'recipe_sections' "
                "AND non_unique = 1 ORDER BY index_name, seq_in_index"))
            nonunique_indexes: dict[str, list[str]] = {}
            for name, col, _seq in nres:
                nonunique_indexes.setdefault(name, []).append(col)

            assert unique_indexes.get("PRIMARY") == ["id"], (
                f"PK should be exactly [id], got {unique_indexes.get('PRIMARY')!r}")
            assert unique_indexes.get("uq_sections_recipe_position") == ["recipe_id", "position"], (
                "named unique (recipe_id, position) index wrong: "
                f"{unique_indexes}")
            # explicit FK-lookup index: NON-UNIQUE, exactly [recipe_id]
            assert nonunique_indexes.get("ix_recipe_sections_recipe_id") == ["recipe_id"], (
                f"explicit FK index ix_recipe_sections_recipe_id should be "
                f"non-unique with exactly [recipe_id], got "
                f"{nonunique_indexes.get('ix_recipe_sections_recipe_id')!r}")

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()


def test_sections_fk() -> None:
    """Task item 4: `recipe_id` is a FK to `recipes.id` with ON DELETE CASCADE.
    Codex gap #3: also assert the constraint NAME and REFERENCED column —
    together these fully identify the relationship."""

    async def run() -> None:
        engine = get_engine()
        async with engine.connect() as conn:
            res = await conn.execute(text(
                "SELECT kcu.constraint_name, kcu.column_name, "
                "       kcu.referenced_table_name, kcu.referenced_column_name, "
                "       rc.delete_rule "
                "FROM information_schema.key_column_usage kcu "
                "JOIN information_schema.referential_constraints rc "
                "  ON rc.constraint_name = kcu.constraint_name "
                " AND rc.constraint_schema = kcu.constraint_schema "
                "WHERE kcu.table_schema = DATABASE() AND kcu.table_name = 'recipe_sections' "
                "ORDER BY kcu.column_name"))
            # col -> (constraint_name, ref_table, ref_column, delete_rule)
            by_col = {r[1]: (r[0], r[2], r[3], r[4]) for r in res}
            got = by_col.get("recipe_id")
            assert got == (
                "recfk_sections_recipe",   # constraint name (migration DDL)
                "recipes",                  # referenced table
                "id",                       # referenced column
                "CASCADE",                  # delete rule
            ), f"recipe_id FK: {got!r}"

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()


def test_sections_kind_valid_and_invalid() -> None:
    """Task items 5 + 6 (and part of 11). All three valid `kind` literals
    round-trip unchanged through the ORM (no id supplied → exercises the `_uuid`
    default). Then an **invalid** literal is rejected by strict-mode MariaDB;
    the plan asks us to inspect the *actual* error and assert that specific
    rejection (MariaDB/asyncmy surfaces 1265 "Invalid value for column 'kind'"
    as `DataError`, NOT `IntegrityError`) — we assert exactly that class."""
    rid = str(uuid.uuid4())

    async def run() -> None:
        try:
            # (1) one row per valid literal; id left to `_uuid`
            async with session_scope() as s:
                s.add(Recipe(id=rid, slug=f"kinds-{rid[:8]}", title="kind roundtrip"))
                for pos, kind in ((1, "ingredients"), (2, "steps"), (3, "note")):
                    s.add(RecipeSection(recipe_id=rid, position=pos,
                                        heading=f"sec-{pos}", kind=kind))
            async with session_scope() as s:
                rows = (await s.execute(
                    sqlalchemy.select(RecipeSection.position, RecipeSection.kind,
                                      RecipeSection.id).where(
                    RecipeSection.recipe_id == rid).
                    order_by(RecipeSection.position))).all()
            assert [(p, k) for p, k, _ in rows] == [
                (1, "ingredients"), (2, "steps"), (3, "note"),
            ], f"kind round-trip: {rows!r}"
            assert all(isinstance(i, str) and len(i) == 36
                       for _p, _k, i in rows), (
                "RecipeSection id default (_uuid) not applied: "
                f"{[(p, k, i) for p, k, i in rows]!r}")

            # (2) invalid literal → strict MariaDB must reject.
            bad_id = str(uuid.uuid4())
            try:
                async with get_engine().begin() as conn:
                    await conn.execute(text(
                        "INSERT INTO recipe_sections "
                        "(id, recipe_id, position, heading, kind) "
                        "VALUES (:id, :rid, 42, 'bad', 'not-a-kind')"),
                        {"id": bad_id, "rid": rid})
                # Reaching here means the DB wrongly accepted an invalid enum
                # literal — .begin() committed. (If the INSERT had succeeded,
                # the outer finally below will still remove the row.)
            except sqlalchemy.exc.DataError as exc:
                assert ("kind" in str(exc) or "1265" in str(exc)
                        or "Invalid value" in str(exc)), (
                    f"expected strict-mode invalid-enum rejection, got: {exc}")
            else:
                raise AssertionError(
                    "INSERT with kind='not-a-kind' was ACCEPTED — the ENUM/"
                    "strict-mode enforcement is not present in the migration DDL.")
        finally:
            async with get_engine().begin() as conn:
                await conn.execute(
                    text("DELETE FROM recipe_items WHERE section_id IN "
                         "(SELECT id FROM recipe_sections WHERE recipe_id = :rid)"),
                    {"rid": rid})
                await conn.execute(
                    text("DELETE FROM recipe_sections WHERE recipe_id = :rid"),
                    {"rid": rid})
                await conn.execute(
                    text("DELETE FROM recipes WHERE id = :rid"), {"rid": rid})

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()


def test_sections_position_unique_and_shared() -> None:
    """Task items 7 + 9. A duplicate `(recipe_id, position)` for the SAME
    recipe is rejected by `uq_sections_recipe_position`; the SAME `position`
    under DIFFERENT recipes is allowed (the key is on the pair, not position)."""
    rid1 = str(uuid.uuid4())
    rid2 = str(uuid.uuid4())

    async def run() -> None:
        try:
            # (1) same recipe: pos=1 once (committed), then a second pos=1 → reject
            async with session_scope() as s:
                s.add(Recipe(id=rid1, slug=f"dup-{rid1[:8]}", title="Dup"))
                s.add(RecipeSection(id=str(uuid.uuid4()), recipe_id=rid1,
                                    position=1, kind="note", heading="first"))
            dup_id = str(uuid.uuid4())
            dup_accepted = False
            try:
                async with get_engine().begin() as conn:
                    await conn.execute(text(
                        "INSERT INTO recipe_sections "
                        "(id, recipe_id, position, heading, kind) "
                        "VALUES (:id, :rid, 1, 'dup', 'note')"),
                        {"id": dup_id, "rid": rid1})
                dup_accepted = True  # .begin() committed; must NOT happen
            except sqlalchemy.exc.IntegrityError as exc:
                assert ("uq_sections_recipe_position" in str(exc)
                        or "Duplicate" in str(exc)), (
                    f"expected unique (recipe_id, position) rejection, got: {exc}")
            assert not dup_accepted, (
                "duplicate (recipe_id, position) within one recipe was ACCEPTED")

            # (2) same position=7 across two DIFFERENT recipes → both allowed
            async with session_scope() as s:
                s.add(Recipe(id=rid2, slug=f"same-{rid2[:8]}", title="Same pos"))
                s.add(RecipeSection(id=str(uuid.uuid4()), recipe_id=rid2,
                                    position=1, kind="note", heading="x"))
                # position=1 already used by rid1's row — but rid2 is a different
                # recipe, so this MUST be accepted (proves the pair is the key).
                n = (await s.execute(
                    sqlalchemy.select(func.count(RecipeSection.id)).where(
                    RecipeSection.recipe_id.in_([rid1, rid2])))).scalar()
            assert n == 2, f"expected 2 sections (one per recipe, both pos=1), got {n}"
        finally:
            async with get_engine().begin() as conn:
                await conn.execute(
                    text("DELETE FROM recipe_items WHERE section_id IN "
                         "(SELECT id FROM recipe_sections WHERE recipe_id IN "
                         "(:a, :b))"), {"a": rid1, "b": rid2})
                await conn.execute(
                    text("DELETE FROM recipe_sections WHERE recipe_id IN "
                         "(:a, :b)"), {"a": rid1, "b": rid2})
                await conn.execute(
                    text("DELETE FROM recipes WHERE id IN (:a, :b)"),
                    {"a": rid1, "b": rid2})

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()


def test_sections_recipe_delete_cascades() -> None:
    """Task item 13 (sections level). Deleting a recipe drops ALL its sections
    (ON DELETE CASCADE), leaving unrelated recipes' sections intact."""
    probe_rid = str(uuid.uuid4())
    other_rid = str(uuid.uuid4())

    async def run() -> None:
        try:
            async with session_scope() as s:
                s.add(Recipe(id=probe_rid, slug=f"probe-{probe_rid[:8]}", title="P"))
                s.add(Recipe(id=other_rid, slug=f"other-{other_rid[:8]}", title="O"))
                s.add(RecipeSection(id=str(uuid.uuid4()), recipe_id=probe_rid,
                                    position=1, kind="note", heading="a"))
                s.add(RecipeSection(id=str(uuid.uuid4()), recipe_id=probe_rid,
                                    position=2, kind="note", heading="b"))
                s.add(RecipeSection(id=str(uuid.uuid4()), recipe_id=other_rid,
                                    position=1, kind="note", heading="z"))

            async with get_engine().begin() as conn:
                rc = (await conn.execute(
                    text("DELETE FROM recipes WHERE id = :id"),
                    {"id": probe_rid})).rowcount
            assert rc == 1, f"expected to delete exactly 1 recipe, deleted {rc}"

            probe_sections = await _count_rows(
                "recipe_sections", "recipe_id = :r", {"r": probe_rid})
            other_sections = await _count_rows(
                "recipe_sections", "recipe_id = :r", {"r": other_rid})
            assert probe_sections == 0, (
                f"probe sections should all be gone (cascade), got {probe_sections}")
            assert other_sections == 1, (
                f"other recipe's section must survive, got {other_sections}")
        finally:
            async with get_engine().begin() as conn:
                await conn.execute(
                    text("DELETE FROM recipe_sections WHERE recipe_id IN "
                         "(:a, :b)"), {"a": probe_rid, "b": other_rid})
                await conn.execute(
                    text("DELETE FROM recipes WHERE id IN (:a, :b)"),
                    {"a": probe_rid, "b": other_rid})

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()
