"""v2 `recipe_items` shape + behavior tests (D4, DL-3). Run against the
ISOLATED test MariaDB (task/6.2; the dev stack must remain untouched; see the
plan's shared contract). Conventions are identical to `tests/test_sections.py`
(see that module docstring): one `asyncio.run` loop per test, cleanup in
`run()`'s own `finally`, and the engine disposed in the outer `finally`.
"""
from __future__ import annotations

import asyncio
import uuid

import sqlalchemy
from sqlalchemy import func, text

from cookbook.db import get_engine, session_scope
from cookbook.models import Recipe, RecipeItem, RecipeSection

EXPECTED_COLUMNS = {
    "id", "section_id", "position", "raw_text",
    "quantity_text", "unit", "item", "preparation",
}
EXPECTED_TYPES = {
    "id": "varchar(36)",
    "section_id": "varchar(36)",
    "position": "int(11)",
    "raw_text": "text",
    "quantity_text": "varchar(50)",
    "unit": "varchar(50)",
    "item": "varchar(300)",
    "preparation": "varchar(300)",
}


def _dispose_engine() -> None:
    """Drain the process-global engine pool between tests (see test_recipes.py)."""
    asyncio.run(get_engine().dispose())


async def _count_rows(table: str, where_sql: str = "", params: dict | None = None) -> int:
    """`SELECT COUNT(*)` helper (table names are fixed literals in this file)."""
    sql = f"SELECT COUNT(*) FROM {table}"
    if where_sql:
        sql += f" WHERE {where_sql}"
    async with get_engine().connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar()


def test_items_table_shape() -> None:
    """Task item 1: exact column set, MariaDB-reported types, nullability.
    `raw_text` is NOT NULL (D4 source of truth); the four parse columns are
    nullable (best-effort; only meaningful for `kind='ingredients'` lines)."""

    async def run() -> None:
        engine = get_engine()
        async with engine.connect() as conn:
            res = await conn.execute(text(
                "SELECT column_name, column_type, is_nullable "
                "FROM information_schema.columns "
                "WHERE table_schema = DATABASE() AND table_name = 'recipe_items'"))
            cols = {r[0]: {"type": r[1], "nullable": r[2]} for r in res}
            assert set(cols) == EXPECTED_COLUMNS, f"columns: {sorted(cols)}"
            for col, typ in EXPECTED_TYPES.items():
                assert cols[col]["type"] == typ, f"{col}: {cols[col]['type']!r} != {typ!r}"
            for col in ("id", "section_id", "position", "raw_text"):
                assert cols[col]["nullable"] == "NO", (
                    f"{col} should be NOT NULL, got {cols[col]!r}")
            for col in ("quantity_text", "unit", "item", "preparation"):
                assert cols[col]["nullable"] == "YES", (
                    f"{col} should be nullable, got {cols[col]!r}")

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()


def test_items_indexes() -> None:
    """Task items 2 + 3: PK contains ONLY `id`; the named
    `uq_items_section_position == [section_id, position]`; AND the explicit
    non-unique FK-lookup index `ix_recipe_items_section_id == [section_id]`.
    The explicit index is asserted *separately* from the ones we've already
    pinned — it is `NON-UNIQUE`, so a query that only filters on
    `non_unique = 0` would never surface it (Codex gap #2)."""

    async def run() -> None:
        engine = get_engine()
        async with engine.connect() as conn:
            # unique indexes (for PK + composite UNIQUE assertions)
            ures = await conn.execute(text(
                "SELECT index_name, column_name, seq_in_index "
                "FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = 'recipe_items' "
                "AND non_unique = 0 ORDER BY index_name, seq_in_index"))
            unique_indexes: dict[str, list[str]] = {}
            for name, col, _seq in ures:
                unique_indexes.setdefault(name, []).append(col)

            # non-unique index (the explicit FK-lookup index)
            nres = await conn.execute(text(
                "SELECT index_name, column_name, seq_in_index "
                "FROM information_schema.statistics "
                "WHERE table_schema = DATABASE() AND table_name = 'recipe_items' "
                "AND non_unique = 1 ORDER BY index_name, seq_in_index"))
            nonunique_indexes: dict[str, list[str]] = {}
            for name, col, _seq in nres:
                nonunique_indexes.setdefault(name, []).append(col)

            assert unique_indexes.get("PRIMARY") == ["id"], (
                f"PK should be exactly [id], got {unique_indexes.get('PRIMARY')!r}")
            assert unique_indexes.get("uq_items_section_position") == ["section_id", "position"], (
                "named unique (section_id, position) index wrong: "
                f"{unique_indexes}")
            # explicit FK-lookup index: NON-UNIQUE, exactly [section_id]
            assert nonunique_indexes.get("ix_recipe_items_section_id") == ["section_id"], (
                f"explicit FK index ix_recipe_items_section_id should be "
                f"non-unique with exactly [section_id], got "
                f"{nonunique_indexes.get('ix_recipe_items_section_id')!r}")

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()


def test_items_fk() -> None:
    """Task item 4: `section_id` is a FK to `recipe_sections.id` with ON DELETE
    CASCADE. Codex gap #3: also assert the constraint NAME and REFERENCED
    column — together these fully identify the relationship."""

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
                "WHERE kcu.table_schema = DATABASE() AND kcu.table_name = 'recipe_items' "
                "ORDER BY kcu.column_name"))
            # col -> (constraint_name, ref_table, ref_column, delete_rule)
            by_col = {r[1]: (r[0], r[2], r[3], r[4]) for r in res}
            got = by_col.get("section_id")
            assert got == (
                "recfk_items_section",   # constraint name (migration DDL)
                "recipe_sections",        # referenced table
                "id",                     # referenced column
                "CASCADE",                # delete rule
            ), f"section_id FK: {got!r}"

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()


def test_items_raw_text_and_parse_fields() -> None:
    """Task item 10 (+ part of 11). `raw_text` round-trips verbatim, including
    Unicode (French accents, CJK, emoji) and free-form quantities. When the
    parse fields are omitted they stay NULL; when supplied they round-trip back.
    Both use the ORM (ids are left to `_uuid` for the items)."""
    rid = str(uuid.uuid4())
    sid = str(uuid.uuid4())
    unicode_raw = "1 1/2 tasses de sucre brun, finement haché 🍚"
    cjk_raw = "2 大 酱油（薄口）"
    ascii_raw = "2 tbsp butter, salted"

    async def run() -> None:
        try:
            # NB: RecipeItem and RecipeSection have no ORM relationship — the
            # Unit of Work does NOT know it should order them parent-before-
            # child (it uses the explicit `section_id` literal). So we flush
            # after adding the section, before adding the items, to guarantee
            # the FK target exists first.
            async with session_scope() as s:
                s.add(Recipe(id=rid, slug=f"rtitems-{rid[:8]}", title="Item rt"))
                s.add(RecipeSection(id=sid, recipe_id=rid, position=1,
                                    kind="ingredients", heading="Base"))
                await s.flush()  # persist the FK parent before its children
                # no explicit id on the items — exercise RecipeItem `_uuid`
                s.add(RecipeItem(section_id=sid, position=1, raw_text=unicode_raw))
                s.add(RecipeItem(section_id=sid, position=2, raw_text=cjk_raw))
                s.add(RecipeItem(section_id=sid, position=3, raw_text=ascii_raw,
                                 quantity_text="2", unit="tbsp",
                                 item="butter, salted", preparation=None))

            async with session_scope() as s:
                # select(Entity) returns ORM instances — use .scalars()
                # (session.execute() alone would yield Row tuples).
                rows = (await s.execute(
                    sqlalchemy.select(RecipeItem).where(
                    RecipeItem.section_id == sid).
                    order_by(RecipeItem.position))).scalars().all()

            assert len(rows) == 3, f"expected 3 items, got {len(rows)}"
            # raw_text is the source of truth — read back byte-for-byte (D4)
            assert [r.raw_text for r in rows] == [unicode_raw, cjk_raw, ascii_raw], (
                f"raw_text round-trip failed: {[r.raw_text for r in rows]}")
            # omitted parse fields stay NULL (row 1)
            assert rows[0].quantity_text is None and rows[0].unit is None
            assert rows[0].item is None and rows[0].preparation is None
            # supplied parse fields round-trip (row 3)
            assert rows[2].quantity_text == "2"
            assert rows[2].unit == "tbsp"
            assert rows[2].item == "butter, salted"
            assert rows[2].preparation is None  # explicitly None
            # `_uuid` default applied to every item id (task item 11)
            assert all(isinstance(r.id, str) and len(r.id) == 36 for r in rows), (
                f"RecipeItem id default (_uuid) not applied: "
                f"{[(r.id) for r in rows]!r}")
        finally:
            async with get_engine().begin() as conn:
                await conn.execute(
                    text("DELETE FROM recipe_items WHERE section_id = :sid"),
                    {"sid": sid})
                await conn.execute(
                    text("DELETE FROM recipe_sections WHERE id = :sid"), {"sid": sid})
                await conn.execute(
                    text("DELETE FROM recipes WHERE id = :id"), {"id": rid})

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()


def test_items_position_unique_and_shared() -> None:
    """Task item 8 (+ 9). A duplicate `(section_id, position)` within ONE
    section is rejected by `uq_items_section_position`; the SAME `position`
    under DIFFERENT sections is allowed (the key is on the pair)."""
    rid = str(uuid.uuid4())
    sec_a = str(uuid.uuid4())
    sec_b = str(uuid.uuid4())

    async def run() -> None:
        try:
            async with session_scope() as s:
                s.add(Recipe(id=rid, slug=f"itemdup-{rid[:8]}", title="Dup item"))
                s.add(RecipeSection(id=sec_a, recipe_id=rid,
                                    position=1, kind="steps", heading="A"))
                s.add(RecipeSection(id=sec_b, recipe_id=rid,
                                    position=2, kind="steps", heading="B"))
                await s.flush()  # both FK parents present before items
                # pos=1 in sec_a (committed)
                s.add(RecipeItem(id=str(uuid.uuid4()), section_id=sec_a,
                                 position=1, raw_text="a1"))

            # (1) duplicate pos=1 in sec_a → rejected
            dup_add = False
            try:
                async with get_engine().begin() as conn:
                    await conn.execute(text(
                        "INSERT INTO recipe_items "
                        "(id, section_id, position, raw_text) "
                        "VALUES (:id, :sid, 1, 'a1-dup')"),
                        {"id": str(uuid.uuid4()), "sid": sec_a})
                dup_add = True  # .begin() committed; must NOT happen
            except sqlalchemy.exc.IntegrityError as exc:
                assert ("uq_items_section_position" in str(exc)
                        or "Duplicate" in str(exc)), (
                    f"expected unique (section_id, position) rejection, got: {exc}")
            assert not dup_add, "duplicate (section_id, position) was ACCEPTED"

            # (2) the SAME position (1) in a DIFFERENT section (sec_b) → allowed
            async with session_scope() as s:
                s.add(RecipeItem(id=str(uuid.uuid4()), section_id=sec_b,
                                 position=1, raw_text="b1"))
                a_items = (await s.execute(sqlalchemy.select(
                    func.count(RecipeItem.id)).where(
                    RecipeItem.section_id == sec_a))).scalar()
                b_items = (await s.execute(sqlalchemy.select(
                    func.count(RecipeItem.id)).where(
                    RecipeItem.section_id == sec_b))).scalar()
            assert a_items == 1, f"sec_a should have exactly 1 item, got {a_items}"
            assert b_items == 1, f"sec_b should have exactly 1 item, got {b_items}"
        finally:
            async with get_engine().begin() as conn:
                await conn.execute(
                    text("DELETE FROM recipe_items WHERE section_id IN "
                         "(:a, :b)"), {"a": sec_a, "b": sec_b})
                await conn.execute(
                    text("DELETE FROM recipe_sections WHERE id IN (:a, :b)"),
                    {"a": sec_a, "b": sec_b})
                await conn.execute(
                    text("DELETE FROM recipes WHERE id = :id"), {"id": rid})

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()


def test_section_delete_cascades_items_only() -> None:
    """Task item 12. Deleting ONE section drops ITS items (CASCADE) but leaves
    the sibling section and the sibling's items intact — the cascade targets
    the parent section, not its siblings."""
    rid = str(uuid.uuid4())
    sec_a = str(uuid.uuid4())
    sec_b = str(uuid.uuid4())

    async def run() -> None:
        try:
            async with session_scope() as s:
                s.add(Recipe(id=rid, slug=f"secdel-{rid[:8]}", title="C"))
                s.add(RecipeSection(id=sec_a, recipe_id=rid,
                                    position=1, kind="ingredients", heading="A"))
                s.add(RecipeSection(id=sec_b, recipe_id=rid,
                                     position=2, kind="steps", heading="B"))
                await s.flush()  # persist both FK parents before their items
                s.add(RecipeItem(id=str(uuid.uuid4()), section_id=sec_a,
                                 position=1, raw_text="a1"))
                s.add(RecipeItem(id=str(uuid.uuid4()), section_id=sec_a,
                                 position=2, raw_text="a2"))
                s.add(RecipeItem(id=str(uuid.uuid4()), section_id=sec_b,
                                 position=1, raw_text="b1"))

            async with get_engine().begin() as conn:
                rc = (await conn.execute(
                    text("DELETE FROM recipe_sections WHERE id = :id"),
                    {"id": sec_a})).rowcount
            assert rc == 1, f"expected to delete 1 section, deleted {rc}"

            a_items = await _count_rows("recipe_items", "section_id = :s", {"s": sec_a})
            b_items = await _count_rows("recipe_items", "section_id = :s", {"s": sec_b})
            b_section = await _count_rows("recipe_sections", "id = :s", {"s": sec_b})
            assert a_items == 0, f"A's items should be 0 after cascade, got {a_items}"
            assert b_items == 1, f"B's items must be unchanged (1), got {b_items}"
            assert b_section == 1, f"B section must survive A's delete, got {b_section}"
        finally:
            async with get_engine().begin() as conn:
                await conn.execute(
                    text("DELETE FROM recipe_items WHERE section_id IN "
                         "(:a, :b)"), {"a": sec_a, "b": sec_b})
                await conn.execute(
                    text("DELETE FROM recipe_sections WHERE id IN (:a, :b)"),
                    {"a": sec_a, "b": sec_b})
                await conn.execute(
                    text("DELETE FROM recipes WHERE id = :id"), {"id": rid})

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()


def test_recipe_delete_cascades_items() -> None:
    """Task item 13 (full chain). Deleting a recipe cascades through its
    sections AND their items; an UNRELATED recipe's section + item survive."""
    probe_rid = str(uuid.uuid4())
    other_rid = str(uuid.uuid4())
    probe_sec = str(uuid.uuid4())
    other_sec = str(uuid.uuid4())

    async def run() -> None:
        try:
            async with session_scope() as s:
                s.add(Recipe(id=probe_rid, slug=f"probe-{probe_rid[:8]}", title="P"))
                s.add(Recipe(id=other_rid, slug=f"other-{other_rid[:8]}", title="O"))
                s.add(RecipeSection(id=probe_sec, recipe_id=probe_rid,
                                    position=1, kind="note", heading=None))
                s.add(RecipeSection(id=other_sec, recipe_id=other_rid,
                                    position=1, kind="note", heading=None))
                await s.flush()  # persist both FK parents before their items
                s.add(RecipeItem(id=str(uuid.uuid4()), section_id=probe_sec,
                                 position=1, raw_text="p1"))
                s.add(RecipeItem(id=str(uuid.uuid4()), section_id=probe_sec,
                                 position=2, raw_text="p2"))
                s.add(RecipeItem(id=str(uuid.uuid4()), section_id=other_sec,
                                 position=1, raw_text="o1"))

            async with get_engine().begin() as conn:
                rc = (await conn.execute(
                    text("DELETE FROM recipes WHERE id = :id"),
                    {"id": probe_rid})).rowcount
            assert rc == 1, f"expected to delete exactly 1 recipe, deleted {rc}"

            # deep cascade: both levels for the probe recipe must be gone
            probe_sections = await _count_rows(
                "recipe_sections", "recipe_id = :r", {"r": probe_rid})
            probe_items = await _count_rows(
                "recipe_items", "section_id = :s", {"s": probe_sec})
            # unrelated recipe's section + item must survive
            other_sections = await _count_rows(
                "recipe_sections", "recipe_id = :r", {"r": other_rid})
            other_items = await _count_rows(
                "recipe_items", "section_id = :s", {"s": other_sec})

            assert probe_sections == 0, (
                f"probe sections should be 0 (cascade), got {probe_sections}")
            assert probe_items == 0, (
                f"probe items should be 0 (cascade through section), got {probe_items}")
            assert other_sections == 1, (
                f"other recipe's section must survive, got {other_sections}")
            assert other_items == 1, (
                f"other recipe's item must survive, got {other_items}")
        finally:
            async with get_engine().begin() as conn:
                await conn.execute(
                    text("DELETE FROM recipe_items WHERE section_id IN "
                         "(:a, :b)"), {"a": probe_sec, "b": other_sec})
                await conn.execute(
                    text("DELETE FROM recipe_sections WHERE id IN (:a, :b)"),
                    {"a": probe_sec, "b": other_sec})
                await conn.execute(
                    text("DELETE FROM recipes WHERE id IN (:a, :b)"),
                    {"a": probe_rid, "b": other_rid})

    try:
        asyncio.run(run())
    finally:
        _dispose_engine()
