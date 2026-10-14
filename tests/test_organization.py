"""v3 `categories` / `tags` + join tables — schema, FK, engine/charset, DL-4
seed, and DL-6/DL-7 deletion-rules behavior. Run against the ISOLATED test
MariaDB (task/6.3); the dev stack must remain untouched (plan shared contract).

Disposal convention (FIXED for this file — see the task handoff, item 1):
- Every test does ALL of its DB work inside one `async def run()` — one
  `asyncio.run` loop. The engine pool is process-global (`lru_cache`) and an
  Asyncmy connection is bound to the loop that opened it, so the pool MUST be
  drained from WITHIN that same live loop: each `run()` ends with
  `await get_engine().dispose()` in its `finally`, and the outer test is a bare
  `asyncio.run(run())` with NO second post-loop disposal. (The old helper ran
  `asyncio.run(get_engine().dispose())` on a *new* loop after the test's loop
  had already closed — the "Future attached to a different loop" risk this
  removes. The older v1/v2 test modules deliberately keep their own pattern and
  are NOT touched here.)
- Probe rows are cleaned up in `run()`'s `finally` (an async `await` on the same
  live loop), so cleanup still runs even if an assertion in the body failed.
- `re` is imported at module top; UUID validity is checked with
  `re.fullmatch(r"[0-9a-f]{8}-[0-9a-f-]{27}", id)`.
"""
from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import sqlalchemy
from sqlalchemy import text

from cookbook.db import get_engine, session_scope
from cookbook.models import Category, Recipe, RecipeItem, RecipeSection, Tag

# --- fixed literals (spec §3.4 + DL-4; the UUIDs are the locked seed rows) --
SEED_ROWS = [
    (1, "breakfast", "Breakfast", "412b94fd-8106-40b8-863b-46e2b1f1daff"),
    (2, "fermentation", "Fermentation", "26857e44-4786-4068-8faf-607d4a5a83a1"),
    (3, "appetizers", "Appetizers", "8fb7691b-6dd7-4cbc-a337-6a62d2a53b7f"),
    (4, "drinks", "Drinks", "ac5980f3-4d82-4f63-9ac7-73602ddbb682"),
    (5, "main-dishes", "Main Dishes", "3889e580-bb77-4a11-a87f-97c501f5ea87"),
    (6, "side-dishes", "Side Dishes", "d51b5466-d402-4157-a09f-6e63588913bc"),
    (7, "baking", "Baking", "58777180-feb6-42a5-a4b1-e6fb24e45bea"),
    (8, "desserts", "Desserts", "bf86f19e-3ce9-4159-a558-bba87672e822"),
]

# Locked UUIDs of two seeded categories, used in the membership / reassignment probes.
BREAKFAST_ID = "412b94fd-8106-40b8-863b-46e2b1f1daff"
FERMENTATION_ID = "26857e44-4786-4068-8faf-607d4a5a83a1"

# Unique-but-stable probe slugs: the cleanup path in `finally` deletes by slug,
# so a wrongly-accepted insert (if a constraint were missing) still leaves a
# deterministic row to clean up on the next re-run.
DUPLICATE_CATEGORY_SLUG = "org-dup-cat"
DUPLICATE_TAG_SLUG = "org-dup-tag"
MEMBER_CAT_A_SLUG = "org-mem-a"
MEMBER_CAT_B_SLUG = "org-mem-b"
MEMBER_TAG_SLUG = "org-mem-tag"
RECIPE_DELETE_TAG_SLUG = "org-del-tag"


async def _free_category_position() -> int:
    """`position` must be globally UNIQUE; pick max(existing)+1 so a probe is
    accepted whether the DB is fresh (seeds 1..8) or has stale probe rows.
    (Seeds are read-only in these tests, so this never collides with them.)"""
    n = await _count("categories")
    return (n + 1) if n else 1


def _is_uuid(s: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                             r"[0-9a-f]{4}-[0-9a-f]{12}", s, re.IGNORECASE))


async def _columns(table: str) -> list[tuple]:
    async with get_engine().connect() as conn:
        res = await conn.execute(
            text("SELECT column_name, column_type, is_nullable "
                 "FROM information_schema.columns "
                 "WHERE table_schema = DATABASE() AND table_name = :t"),
            {"t": table},
        )
        return [tuple(r) for r in res]


async def _indexes(table: str, non_unique: bool) -> dict[str, list[str]]:
    async with get_engine().connect() as conn:
        flag = 1 if non_unique else 0
        res = await conn.execute(
            text("SELECT index_name, column_name, seq_in_index "
                 "FROM information_schema.statistics "
                 "WHERE table_schema = DATABASE() AND table_name = :t "
                 "AND non_unique = :nu ORDER BY index_name, seq_in_index"),
            {"t": table, "nu": flag},
        )
        out: dict[str, list[str]] = {}
        for name, col, _seq in res:
            out.setdefault(name, []).append(col)
        return out


async def _fks(table: str) -> dict[str, tuple[str, str, str, str]]:
    """col -> (constraint_name, ref_table, ref_column, delete_rule)."""
    async with get_engine().connect() as conn:
        res = await conn.execute(
            text("SELECT kcu.constraint_name, kcu.column_name, "
                 "       kcu.referenced_table_name, kcu.referenced_column_name, "
                 "       rc.delete_rule "
                 "FROM information_schema.key_column_usage kcu "
                 "JOIN information_schema.referential_constraints rc "
                 "  ON rc.constraint_name = kcu.constraint_name "
                 " AND rc.constraint_schema = kcu.constraint_schema "
                 "WHERE kcu.table_schema = DATABASE() AND kcu.table_name = :t "
                 "ORDER BY kcu.column_name"),
            {"t": table},
        )
        return {r[1]: (r[0], r[2], r[3], r[4]) for r in res}


async def _count(table: str, where_sql: str = "", params: dict | None = None) -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if where_sql:
        sql += f" WHERE {where_sql}"
    async with get_engine().connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar()


async def _exec_insight(sql: str, params: dict) -> int:
    async with get_engine().begin() as conn:
        return (await conn.execute(text(sql), params)).rowcount


async def _insert_rc(recipe_id: str, category_id: str) -> None:
    await _exec_insight(
        "INSERT INTO recipe_categories (recipe_id, category_id) VALUES (:r, :c)",
        {"r": recipe_id, "c": category_id})


async def _insert_rt(recipe_id: str, tag_id: str) -> None:
    await _exec_insight(
        "INSERT INTO recipe_tags (recipe_id, tag_id) VALUES (:r, :t)",
        {"r": recipe_id, "t": tag_id})


async def _delete_probe(
        conn, recipe_ids: list[str], category_ids: list[str], tag_ids: list[str]
) -> None:
    """Remove every probe row in FK-safe order (children, then parents).

    A probe tag/category is referenced only by probe recipes created here — we
    never create a membership that points at a SEED category, and we never
    delete a seed category — so deleting the probe recipes (which CASCADEs their
    join rows) and then the probe tag/category parents is always safe and
    idempotent. The explicit join deletes are belt-and-suspenders."""
    for r in recipe_ids:
        await conn.execute(
            text("DELETE FROM recipe_categories WHERE recipe_id = :r"), {"r": r})
        await conn.execute(text("DELETE FROM recipe_tags WHERE recipe_id = :r"), {"r": r})
    for r in recipe_ids:
        await conn.execute(text("DELETE FROM recipes WHERE id = :r"), {"r": r})
    for c in category_ids:
        await conn.execute(text("DELETE FROM categories WHERE id = :c"), {"c": c})
    for t in tag_ids:
        await conn.execute(text("DELETE FROM tags WHERE id = :t"), {"t": t})


async def _clean_probe(recipe_ids: list[str], category_ids: list[str],
                       tag_ids: list[str],
                       category_slugs: list[str], tag_slugs: list[str]) -> None:
    """`finally` cleanup that is safe even if the test body only partially
    committed: delete any lingering join/recipe/category/tag probe rows
    (matching by per-run ids where we have them, and by the stable probe slugs
    so a wrongly-accepted insert still leaves a deterministic row to remove),
    in FK-safe order. The nested try/finally GUARANTEES the engine disposal
    runs even if one of the cleanup DELETEs raises — the disposal lives INSIDE
    this `async def` (the same live loop the test ran in), so no second
    `asyncio.run` is needed to reach it."""
    try:
        async with get_engine().begin() as conn:
            await _delete_probe(conn, recipe_ids, category_ids, tag_ids)
            for slug in category_slugs:
                await conn.execute(text("DELETE FROM categories WHERE slug = :s"),
                                   {"s": slug})
            for slug in tag_slugs:
                await conn.execute(text("DELETE FROM tags WHERE slug = :s"),
                                   {"s": slug})
    finally:
        await get_engine().dispose()  # MUST run even if the DELETEs above threw


async def _seed_rows() -> list[tuple[str, str, str, int]]:
    """(id, slug, name, position) rows, ordered by position."""
    async with get_engine().connect() as conn:
        res = await conn.execute(
            text("SELECT id, slug, name, position FROM categories ORDER BY position"))
        return [tuple(r) for r in res]


async def _delete_probe_rows(probe_rids: list[str]) -> None:
    """Delete a set of probe recipes (plus, via CASCADE, their join rows);
    used after the body to prove the delete actually removed them."""
    async with get_engine().begin() as conn:
        for r in probe_rids:
            await conn.execute(text("DELETE FROM recipes WHERE id = :r"), {"r": r})


async def _assert_not_null_cols(cols: list[tuple]) -> None:
    for c in cols:
        assert c[2] == "NO", f"{c[0]} should be NOT NULL, got {c[2]}"


# ============================================================================
# SCHEMA (pure SQL — fail with "table doesn't exist" on a baseline w/o v3)
# ============================================================================

def test_categories_columns() -> None:
    """Exact column set, MariaDB-reported types, and ALL NOT NULL."""

    async def run() -> None:
        try:
            cols = await _columns("categories")
            names = {c[0] for c in cols}
            assert names == {"id", "slug", "name", "position"}, f"columns: {sorted(names)}"
            types = {c[0]: c[1] for c in cols}
            assert types == {
                "id": "varchar(36)", "slug": "varchar(200)",
                "name": "varchar(100)", "position": "int(11)",
            }, f"types: {types}"
            await _assert_not_null_cols(cols)
        finally:
            await get_engine().dispose()  # drain the pool on THIS loop

    asyncio.run(run())


def test_tags_columns() -> None:
    """Exact column set (NO position — ordering rule 6/types locked), types,
    and ALL NOT NULL on the three columns."""

    async def run() -> None:
        try:
            cols = await _columns("tags")
            names = {c[0] for c in cols}
            assert names == {"id", "slug", "name"}, (
                f"tags columns: {sorted(names)} — 'position' must NOT exist")
            types = {c[0]: c[1] for c in cols}
            assert types == {
                "id": "varchar(36)", "slug": "varchar(200)", "name": "varchar(100)",
            }, f"types: {types}"
            await _assert_not_null_cols(cols)
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_recipe_categories_columns() -> None:
    """Join table: exactly two NOT NULL varchar(36) columns."""

    async def run() -> None:
        try:
            cols = await _columns("recipe_categories")
            assert {c[0] for c in cols} == {"recipe_id", "category_id"}, (
                f"recipe_categories columns: {sorted(c[0] for c in cols)}")
            for c in cols:
                assert c[1] == "varchar(36)", f"{c[0]} type: {c[1]!r}"
            await _assert_not_null_cols(cols)
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_recipe_tags_columns() -> None:
    """Join table: exactly two NOT NULL varchar(36) columns."""

    async def run() -> None:
        try:
            cols = await _columns("recipe_tags")
            assert {c[0] for c in cols} == {"recipe_id", "tag_id"}, (
                f"recipe_tags columns: {sorted(c[0] for c in cols)}")
            for c in cols:
                assert c[1] == "varchar(36)", f"{c[0]} type: {c[1]!r}"
            await _assert_not_null_cols(cols)
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_categories_indexes() -> None:
    """PK is exactly [id]; uq slug is [slug]; uq position is [position]."""

    async def run() -> None:
        try:
            unique = await _indexes("categories", non_unique=False)
            assert unique.get("PRIMARY") == ["id"], f"PRIMARY: {unique.get('PRIMARY')!r}"
            assert unique.get("uq_categories_slug") == ["slug"], (
                f"uq_categories_slug: {unique.get('uq_categories_slug')!r}")
            assert unique.get("uq_categories_position") == ["position"], (
                f"uq_categories_position: {unique.get('uq_categories_position')!r}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_tags_indexes() -> None:
    """PK is exactly [id]; uq slug is [slug]; NO position-derived index."""

    async def run() -> None:
        try:
            unique = await _indexes("tags", non_unique=False)
            assert unique.get("PRIMARY") == ["id"], f"PRIMARY: {unique.get('PRIMARY')!r}"
            assert unique.get("uq_tags_slug") == ["slug"], (
                f"uq_tags_slug: {unique.get('uq_tags_slug')!r}")
            assert not any("position" in k for k in unique), (
                f"tags should have no position-derived unique index: {list(unique)}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_join_primary_keys() -> None:
    """recipe_categories PK == [recipe_id, category_id];
       recipe_tags     PK == [recipe_id, tag_id]  (that order)."""

    async def run() -> None:
        try:
            rc = await _indexes("recipe_categories", False)
            rt = await _indexes("recipe_tags", False)
            assert rc.get("PRIMARY") == ["recipe_id", "category_id"], (
                f"recipe_categories PK: {rc.get('PRIMARY')!r}")
            assert rt.get("PRIMARY") == ["recipe_id", "tag_id"], (
                f"recipe_tags PK: {rt.get('PRIMARY')!r}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_join_secondary_fk_indexes() -> None:
    """category_id and tag_id each have a NON-UNIQUE FK-lookup index, in that
    exact column list — while recipe_id (the PK-leading col) already has the
    PK index and NO redundant secondary index (v2 pattern confirmed live)."""

    async def run() -> None:
        try:
            rc_nu = await _indexes("recipe_categories", non_unique=True)
            rt_nu = await _indexes("recipe_tags", non_unique=True)
            assert rc_nu.get("ix_recipe_categories_category_id") == ["category_id"], (
                f"ix_recipe_categories_category_id: "
                f"{rc_nu.get('ix_recipe_categories_category_id')!r}")
            assert rt_nu.get("ix_recipe_tags_tag_id") == ["tag_id"], (
                f"ix_recipe_tags_tag_id: {rt_nu.get('ix_recipe_tags_tag_id')!r}")
            assert "ix_recipe_categories_recipe_id" not in rc_nu, (
                f"no redundant recipe_id index on recipe_categories: {list(rc_nu)}")
            assert "ix_recipe_tags_recipe_id" not in rt_nu, (
                f"no redundant recipe_id index on recipe_tags: {list(rt_nu)}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_join_fks() -> None:
    """All four FKs with exact names, source columns, target table/column, and
    delete rule:
    - recfk_recipe_categories_recipe   -> recipes.id    ON DELETE CASCADE
    - recfk_recipe_categories_category -> categories.id ON DELETE RESTRICT (DL-6)
    - recfk_recipe_tags_recipe         -> recipes.id    ON DELETE CASCADE
    - recfk_recipe_tags_tag            -> tags.id       ON DELETE CASCADE (DL-7)
    """

    async def run() -> None:
        try:
            rc = await _fks("recipe_categories")
            assert rc.get("recipe_id") == (
                "recfk_recipe_categories_recipe", "recipes", "id", "CASCADE"), (
                f"recipe_categories.recipe_id FK: {rc.get('recipe_id')!r}")
            assert rc.get("category_id") == (
                "recfk_recipe_categories_category", "categories", "id", "RESTRICT"), (
                f"recipe_categories.category_id FK: {rc.get('category_id')!r}")

            rt = await _fks("recipe_tags")
            assert rt.get("recipe_id") == (
                "recfk_recipe_tags_recipe", "recipes", "id", "CASCADE"), (
                f"recipe_tags.recipe_id FK: {rt.get('recipe_id')!r}")
            assert rt.get("tag_id") == (
                "recfk_recipe_tags_tag", "tags", "id", "CASCADE"), (
                f"recipe_tags.tag_id FK: {rt.get('tag_id')!r}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_engine_charset_all_four() -> None:
    """All four tables are InnoDB with the utf8mb4 charset.

    `information_schema.TABLES` reports the charset via `TABLE_COLLATION`,
    whose prefix is the charset name. The exact collation is server-configured
    (e.g. utf8mb4_uca1400_ai_ci here), so we assert the utf8mb4 *charset*
    prefix — the spec's "InnoDB and utf8mb4" requirement — not a collation.
    """

    async def run() -> None:
        try:
            tables = ("categories", "tags", "recipe_categories", "recipe_tags")
            info: dict[str, tuple[str, str]] = {}
            async with get_engine().connect() as conn:
                for t in tables:
                    r = await conn.execute(
                        text("SELECT ENGINE, TABLE_COLLATION "
                             "FROM information_schema.TABLES "
                             "WHERE table_schema = DATABASE() AND table_name = :t"),
                        {"t": t},
                    )
                    row = r.one()
                    info[t] = (row[0], row[1])
            for t in tables:
                engine, coll = info[t]
                assert engine == "InnoDB", f"{t} engine: {engine!r}"
                assert isinstance(coll, str) and coll.startswith("utf8mb4"), (
                    f"{t} collation {coll!r} must be a utf8mb4-charset collation")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


# ============================================================================
# DL-4 SEED (pure SQL; these assert the migrated data, not the schema)
# ============================================================================

def test_seed_rows_locked() -> None:
    """Exactly the 8 locked UUIDs, slugs, names, and positions (1..8, dense).
    No "Others" row; no gap; no duplicates."""

    async def run() -> None:
        try:
            rows = await _seed_rows()  # (id, slug, name, position), by position
            assert len(rows) == 8, f"expected 8 seed categories, got {len(rows)}"
            got = [(p, s, n, i) for i, s, n, p in rows]
            expected = SEED_ROWS
            assert got == expected, (
                f"seed rows mismatch.\n got:      {got}\n expected: {expected}")
            positions = sorted(p for p, _, _, _ in got)
            assert positions == list(range(1, 9)), f"positions not dense 1..8: {positions}"
            slug_to_id = {s: i for i, s, _n, _p in rows}
            for _p, slug, _name, locked_id in SEED_ROWS:
                assert slug_to_id.get(slug) == locked_id, (
                    f"slug {slug!r} has id {slug_to_id.get(slug)!r}, expected {locked_id}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_no_others_seed_and_no_tag_seeds() -> None:
    """No "Others" category is pre-seeded (the importer owns it), and the tags
    table has ZERO rows (no tag seeds)."""

    async def run() -> None:
        try:
            cnt = await _count("categories", "slug = 'others'")
            assert cnt == 0, f"unexpected 'others' category row: {cnt}"
            tcnt = await _count("tags")
            assert tcnt == 0, f"expected 0 tag rows (no seeds), got {tcnt}"
        finally:
            await get_engine().dispose()

    asyncio.run(run())


# ============================================================================
# BEHAVIOR (ORM round-trips, duplicates, and DL-6/DL-7 deletion rules)
# ============================================================================

def test_category_and_tag_roundtrip_uuid_defaults() -> None:
    """Category and Tag ORM round-trips (the joins are plain SQL — there is no
    relationship()/backref to lean on here, per the v1 scope). Values persist
    unchanged and the `_uuid` default produces a valid 36-char id when none is
    supplied. A Category is inserted with an explicit id (which must stick) and
    a Tag with NO id (default applied) — both are then read back and cleaned up
    in one `finally` on the same live loop."""

    async def run() -> None:
        cat_id = str(uuid.uuid4())
        try:
            async with session_scope() as s:
                s.add(Category(id=cat_id, slug="org-probe-a",
                               name="Org Probe A", position=11))
                s.add(Tag(id=str(uuid.uuid4()), slug="org-tag-a", name="Org Tag A"))
                # No explicit id on these two — rely on the `_uuid` default.
                s.add(Category(slug="org-probe-b", name="Org Probe B", position=12))
                s.add(Tag(slug="org-tag-b", name="Org Tag B"))

            async with session_scope() as s:
                ca = (await s.execute(
                    sqlalchemy.select(Category).where(Category.slug == "org-probe-a"))
                    ).scalars().first()
                cb = (await s.execute(
                    sqlalchemy.select(Category).where(Category.slug == "org-probe-b"))
                    ).scalars().first()
                ta = (await s.execute(
                    sqlalchemy.select(Tag).where(Tag.slug == "org-tag-a"))).scalars().first()
                tb = (await s.execute(
                    sqlalchemy.select(Tag).where(Tag.slug == "org-tag-b"))).scalars().first()

            assert ca is not None and ca.id == cat_id, (
                f"explicit category id should round-trip, got {ca!r}")
            assert ca.name == "Org Probe A" and ca.position == 11
            assert cb is not None and _is_uuid(cb.id), (
                f"org-probe-b id (_uuid default) must be a valid UUID, got {cb!r}")
            assert ta is not None and _is_uuid(ta.id) and ta.name == "Org Tag A"
            assert tb is not None and _is_uuid(tb.id) and tb.name == "Org Tag B"
        finally:
            # Slug-based cleanup of all four probe rows (id-independent — the
            # `_uuid` defaults are unknown until read back, so we match by the
            # stable slugs we control). The nested try/finally inside
            # `_clean_probe` guarantees `await get_engine().dispose()` still
            # runs even if one of the cleanup DELETEs raises (disposal happens
            # INSIDE the same live loop — no second `asyncio.run` needed).
            await _clean_probe(
                recipe_ids=[],
                category_ids=[], tag_ids=[],
                category_slugs=["org-probe-a", "org-probe-b"],
                tag_slugs=["org-tag-a", "org-tag-b"],
            )

    asyncio.run(run())


def test_duplicate_slugs_and_category_position_rejected() -> None:
    """Duplicate category and duplicate tag slugs are both rejected
    (`uq_categories_slug` / `uq_tags_slug`); a duplicate category position is
    rejected (`uq_categories_position`). The specific constraint marker is
    checked right beside the expected `IntegrityError`, so an unrelated
    failure (a non-IntegrityError raise, or a different DB error) can never be
    masked as a 'duplicate' rejection. Probes use positions well clear of the
    locked seeds (1..8); cleanup matches the stable probe slugs below (not a
    per-run UUID), so a wrongly-accepted insert leaves a deterministic row
    that the next re-run's `finally` still removes."""

    async def _try_insert(row, marker: str) -> None:
        """Insert `row` in one session; assert the DB rejected it with an
        `IntegrityError` naming `marker` (mirrors the `try_add` convention in
        tests/test_recipes.py)."""
        try:
            async with session_scope() as s:
                s.add(row)
        except sqlalchemy.exc.IntegrityError as exc:
            assert marker in str(exc) or "1062" in str(exc) or "Duplicate" in str(exc), (
                f"expected {marker} violation, got: {exc}")
            return
        raise AssertionError(f"expected {marker} violation, but the insert succeeded")

    async def run() -> None:
        pos = await _free_category_position()
        probe_cat = Category(slug=DUPLICATE_CATEGORY_SLUG, name="Org Dup Cat",
                             position=pos)
        probe_tag = Tag(slug=DUPLICATE_TAG_SLUG, name="Org Dup Tag")
        try:
            # baseline probe category + probe tag, committed so the duplicate
            # checks below each reference a real, existing row.
            async with session_scope() as s:
                s.add(probe_cat)
                s.add(probe_tag)

            # (1) duplicate category slug (different id, fresh position) -> reject
            await _try_insert(
                Category(slug=DUPLICATE_CATEGORY_SLUG, name="Org Dup Cat 2",
                         position=pos + 1),
                "uq_categories_slug")

            # (2) duplicate category position (unique slug, position already
            #     taken by the baseline probe) -> reject
            await _try_insert(
                Category(slug="org-dup-cat-pos", name="Org Dup Pos", position=pos),
                "uq_categories_position")

            # (3) duplicate tag slug (different id) -> reject
            await _try_insert(
                Tag(slug=DUPLICATE_TAG_SLUG, name="Org Dup Tag 2"),
                "uq_tags_slug")
        finally:
            async with get_engine().begin() as conn:
                await conn.execute(
                    text("DELETE FROM categories WHERE slug = :slug"),
                    {"slug": DUPLICATE_CATEGORY_SLUG})
                await conn.execute(
                    text("DELETE FROM categories WHERE slug = 'org-dup-cat-pos'"))
                await conn.execute(
                    text("DELETE FROM tags WHERE slug = :slug"),
                    {"slug": DUPLICATE_TAG_SLUG})
            await get_engine().dispose()

    asyncio.run(run())


def test_in_use_category_deletion_blocked_membership_preserved() -> None:
    """DL-6: an in-use category cannot be deleted (ON DELETE RESTRICT on
    recipe_categories.category_id). While a probe recipe is a member of the
    SEED `breakfast` category, `DELETE FROM categories` for breakfast must be
    rejected, the seed row must still exist, and BOTH of the recipe's
    memberships (breakfast + a second probe category) must be preserved.

    We deliberately do NOT delete the seed category here — only attempt and
    prove the block, then clean up our probe recipe and probe category.
    """
    rid = str(uuid.uuid4())
    probe_cat_id = str(uuid.uuid4())

    async def run() -> None:
        pos = await _free_category_position()
        try:
            async with session_scope() as s:
                s.add(Recipe(id=rid, slug=f"incat-{rid[:8]}", title="In-use cat"))
                # second (probe) category so we can prove the recipe's OTHER
                # membership is preserved when the in-use delete is blocked.
                s.add(Category(id=probe_cat_id, slug="org-incat",
                               name="Org InCat", position=pos))
            await _insert_rc(rid, BREAKFAST_ID)
            await _insert_rc(rid, probe_cat_id)  # recipe in TWO distinct categories

            # baseline: two memberships for this recipe (breakfast + probe cat)
            assert await _count("recipe_categories", "recipe_id = :r", {"r": rid}) == 2

            # attempt to delete the SEED breakfast while it is in use -> blocked
            try:
                await _exec_insight(
                    "DELETE FROM categories WHERE id = :c", {"c": BREAKFAST_ID})
            except sqlalchemy.exc.IntegrityError as exc:
                assert ("1451" in str(exc) or "restrict" in str(exc).lower()
                        or "recfk_recipe_categories_category" in str(exc)), (
                    f"expected a RESTRICT block, got: {exc}")
            else:
                raise AssertionError(
                    "expected the SEED breakfast delete to be blocked by RESTRICT, "
                    "but it succeeded")

            # membership preserved: seed still present + both memberships intact
            assert await _count("categories", "id = :c", {"c": BREAKFAST_ID}) == 1, (
                "seed breakfast category must still exist after the blocked delete")
            assert await _count(
                "recipe_categories",
                "recipe_id = :r AND category_id = :c",
                {"r": rid, "c": BREAKFAST_ID}) == 1, (
                "membership to the in-use category must be preserved (not dropped)")
            assert await _count(
                "recipe_categories",
                "recipe_id = :r AND category_id = :c",
                {"r": rid, "c": probe_cat_id}) == 1, (
                "the recipe's OTHER membership must be preserved too")
            assert await _count("recipes", "id = :r", {"r": rid}) == 1, (
                "the recipe must survive the blocked category delete")
        finally:
            await _clean_probe(
                recipe_ids=[rid], category_ids=[probe_cat_id], tag_ids=[],
                category_slugs=["org-incat"], tag_slugs=[])

    asyncio.run(run())


def test_in_use_category_unassigned_is_deletable() -> None:
    """DL-6 positive path: a category a recipe is NOT a member of can be
    deleted. And the in-use block is *released* by deliberately REMOVING the
    membership: a probe category that a probe recipe belongs to first blocks a
    delete, then after we delete the membership row the same delete succeeds.
    (We only ever delete PROBE categories here — never a seed.)"""
    rid = str(uuid.uuid4())
    in_use_cat = str(uuid.uuid4())      # the one the recipe is a member of
    free_cat = str(uuid.uuid4())        # the one it is NOT a member of
    p_in = 41
    p_free = 42

    async def run() -> None:
        try:
            async with session_scope() as s:
                s.add(Recipe(id=rid, slug=f"del-{rid[:8]}", title="Del probe"))
                s.add(Category(id=in_use_cat, slug="org-inuse", name="Org InUse",
                               position=p_in))
                s.add(Category(id=free_cat, slug="org-free", name="Org Free",
                               position=p_free))
            await _insert_rc(rid, in_use_cat)

            # in-use: delete blocked
            try:
                await _exec_insight(
                    "DELETE FROM categories WHERE id = :c", {"c": in_use_cat})
            except sqlalchemy.exc.IntegrityError as exc:
                assert ("1451" in str(exc) or "recfk_recipe_categories_category" in str(exc)
                        or "restrict" in str(exc).lower()), f"block error: {exc}"
            else:
                raise AssertionError(
                    "expected the in-use category delete to be blocked by RESTRICT, "
                    "but it succeeded")
            assert await _count("categories", "id = :c", {"c": in_use_cat}) == 1

            # deliberately REMOVED the membership: the same delete now succeeds
            await _exec_insight(
                "DELETE FROM recipe_categories WHERE recipe_id = :r AND category_id = :c",
                {"r": rid, "c": in_use_cat})
            rc = await _exec_insight(
                "DELETE FROM categories WHERE id = :c", {"c": in_use_cat})
            assert rc == 1, f"expected to delete exactly 1 now-free category, deleted {rc}"
            assert await _count("categories", "id = :c", {"c": in_use_cat}) == 0, (
                "in-use category should be gone after membership removal")

            # the unrelated FREE category (recipe not a member) also deletes cleanly
            rc_free = await _exec_insight(
                "DELETE FROM categories WHERE id = :c", {"c": free_cat})
            assert rc_free == 1, f"expected to delete the free category, deleted {rc_free}"
        finally:
            await _clean_probe(
                recipe_ids=[rid], category_ids=[in_use_cat, free_cat], tag_ids=[],
                category_slugs=["org-inuse", "org-free"], tag_slugs=[])

    asyncio.run(run())


def test_membership_duplicates_rejected_distinct_allowed() -> None:
    """D12 join semantics, for BOTH joins: the SAME (recipe, category) AND
    (recipe, tag) memberships are each rejected by their composite PK, while
    the same recipe joining two DISTINCT categories AND two DISTINCT tags is
    allowed (that is the whole point of a many-to-many)."""
    rid = str(uuid.uuid4())
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    t1 = str(uuid.uuid4())
    t2 = str(uuid.uuid4())

    async def run() -> None:
        pos = await _free_category_position()
        try:
            async with session_scope() as s:
                s.add(Recipe(id=rid, slug=f"mem-{rid[:8]}", title="Mem"))
                s.add(Category(id=a, slug=MEMBER_CAT_A_SLUG, name="Mem A", position=pos))
                s.add(Category(id=b, slug=MEMBER_CAT_B_SLUG, name="Mem B", position=pos + 1))
                s.add(Tag(id=t1, slug="org-mem-tag1", name="Mem Tag 1"))
                s.add(Tag(id=t2, slug="org-mem-tag2", name="Mem Tag 2"))

            # --- recipe_categories: distinct -> both accepted ---
            await _insert_rc(rid, a)
            await _insert_rc(rid, b)
            assert await _count("recipe_categories", "recipe_id = :r", {"r": rid}) == 2, (
                "a recipe must be allowed in two DISTINCT categories (m2m)")
            # the same (rid, a) again -> composite PK reject
            try:
                await _insert_rc(rid, a)
            except sqlalchemy.exc.IntegrityError as exc:
                assert ("PRIMARY" in str(exc) or "1062" in str(exc) or
                        "Duplicate" in str(exc)), f"dup category membership: {exc}"
            else:
                raise AssertionError("duplicate category membership should be rejected")
            assert await _count("recipe_categories", "recipe_id = :r", {"r": rid}) == 2, (
                "the duplicate category membership must NOT have been added")

            # --- recipe_tags: distinct -> both accepted ---
            await _insert_rt(rid, t1)
            await _insert_rt(rid, t2)
            assert await _count("recipe_tags", "recipe_id = :r", {"r": rid}) == 2, (
                "a recipe must be allowed with two DISTINCT tags (m2m)")
            # the same (rid, t1) again -> composite PK reject
            try:
                await _insert_rt(rid, t1)
            except sqlalchemy.exc.IntegrityError as exc:
                assert ("PRIMARY" in str(exc) or "1062" in str(exc) or
                        "Duplicate" in str(exc)), f"dup tag membership: {exc}"
            else:
                raise AssertionError("duplicate tag membership should be rejected")
            assert await _count("recipe_tags", "recipe_id = :r", {"r": rid}) == 2, (
                "the duplicate tag membership must NOT have been added")
        finally:
            await _clean_probe(
                recipe_ids=[rid], category_ids=[a, b], tag_ids=[t1, t2],
                category_slugs=[MEMBER_CAT_A_SLUG, MEMBER_CAT_B_SLUG],
                tag_slugs=["org-mem-tag1", "org-mem-tag2"])

    asyncio.run(run())


def test_recipe_delete_removes_both_membership_kinds_preserves_parents() -> None:
    """DL-6 + DL-7 (recipe-side CASCADE on BOTH joins): deleting a recipe
    removes its `recipe_categories` AND `recipe_tags` rows, while the
    categories/tags themselves and every unrelated recipe's memberships
    survive (the FK CASCADE is on the recipe side of each join, never the
    parents)."""
    rid = str(uuid.uuid4())
    other_rid = str(uuid.uuid4())
    a = str(uuid.uuid4())
    b = str(uuid.uuid4())
    t1 = str(uuid.uuid4())
    t2 = str(uuid.uuid4())

    async def run() -> None:
        pos = await _free_category_position()
        try:
            async with session_scope() as s:
                s.add(Recipe(id=rid, slug=f"rc-{rid[:8]}", title="RC target"))
                s.add(Recipe(id=other_rid, slug=f"rc-other-{other_rid[:8]}",
                             title="RC other"))
                s.add(Category(id=a, slug="org-rc-a", name="RC A", position=pos))
                s.add(Category(id=b, slug="org-rc-b", name="RC B", position=pos + 1))
                s.add(Tag(id=t1, slug="org-rc-t1", name="RC T1"))
                s.add(Tag(id=t2, slug="org-rc-t2", name="RC T2"))
            # target recipe: BOTH kinds of memberships present
            await _insert_rc(rid, a)
            await _insert_rt(rid, t1)
            # other recipe: also both kinds, so we can prove its memberships
            # survive the target's delete (only the target's rows CASCADE).
            await _insert_rc(other_rid, b)
            await _insert_rt(other_rid, t2)

            assert await _count("recipe_categories", "recipe_id = :r", {"r": rid}) == 1
            assert await _count("recipe_tags", "recipe_id = :r", {"r": rid}) == 1
            assert await _count("recipe_categories", "recipe_id = :r", {"r": other_rid}) == 1
            assert await _count("recipe_tags", "recipe_id = :r", {"r": other_rid}) == 1

            # delete the target recipe -> both of its join rows CASCADE away
            await _delete_probe_rows([rid])

            assert await _count("recipes", "id = :r", {"r": rid}) == 0, (
                "the target recipe should be gone")
            assert await _count(
                "recipe_categories", "recipe_id = :r", {"r": rid}) == 0, (
                "the target's category memberships must CASCADE with it")
            assert await _count(
                "recipe_tags", "recipe_id = :r", {"r": rid}) == 0, (
                "the target's tag memberships must CASCADE with it")

            # parents PRESERVED (CASCADE is on recipe_id, not the parent side)
            assert await _count("categories", "id = :c", {"c": a}) == 1, (
                "category parent must survive the recipe delete")
            assert await _count("categories", "id = :c", {"c": b}) == 1, (
                "the unrelated recipe's category must survive")
            assert await _count("tags", "id = :t", {"t": t1}) == 1, (
                "tag parent must survive the recipe delete")
            assert await _count("tags", "id = :t", {"t": t2}) == 1, (
                "the unrelated recipe's tag must survive")

            # the unrelated recipe AND its memberships are untouched
            assert await _count("recipes", "id = :r", {"r": other_rid}) == 1, (
                "the unrelated recipe must survive")
            assert await _count(
                "recipe_categories", "recipe_id = :r", {"r": other_rid}) == 1, (
                "the unrelated recipe's category membership must survive")
            assert await _count(
                "recipe_tags", "recipe_id = :r", {"r": other_rid}) == 1, (
                "the unrelated recipe's tag membership must survive")
        finally:
            await _clean_probe(
                recipe_ids=[other_rid], category_ids=[a, b], tag_ids=[t1, t2],
                category_slugs=["org-rc-a", "org-rc-b"],
                tag_slugs=["org-rc-t1", "org-rc-t2"])

    asyncio.run(run())


def test_tag_delete_removes_only_its_memberships() -> None:
    """DL-7: deleting an in-use tag is ALLOWED and removes only its
    `recipe_tags` rows. The recipes that used the tag, their sections/items,
    their category memberships, and every other tag/membership all survive.
    (This is the deliberate contrast with the category RESTRICT rule above.)
    We use a PROBE tag — never a seed — because seeds are categories only."""
    rid = str(uuid.uuid4())
    other_rid = str(uuid.uuid4())
    tag = str(uuid.uuid4())
    other_tag = str(uuid.uuid4())
    cat = str(uuid.uuid4())

    async def run() -> None:
        pos = await _free_category_position()
        try:
            # ORM parents (recipe + section + item rows) flushed/committed in
            # ONE session before the dependent raw-SQL membership inserts —
            # the membership FKs point at committed parent rows.
            async with session_scope() as s:
                s.add(Recipe(id=rid, slug=f"td-{rid[:8]}", title="TD target"))
                s.add(Recipe(id=other_rid, slug=f"td-other-{other_rid[:8]}",
                             title="TD other"))
                sec = RecipeSection(recipe_id=rid, position=1, kind="steps",
                                    heading="Steps")
                s.add(sec)
                # flush the section FIRST so its `id` (the ORM `_uuid` default)
                # is populated, then the dependent item row can reference it —
                # the item's `section_id` FK points at the flushed parent.
                await s.flush()
                s.add(RecipeItem(section_id=sec.id, position=1, raw_text="do the thing"))
                s.add(Tag(id=tag, slug=RECIPE_DELETE_TAG_SLUG, name="TD Tag"))
                s.add(Tag(id=other_tag, slug="org-td-other-tag", name="TD Other Tag"))
                s.add(Category(id=cat, slug="org-td-cat", name="TD Cat",
                               position=pos))
            await _insert_rt(rid, tag)                 # membership to delete WITH the tag
            await _insert_rt(other_rid, tag)           # second recipe shares the tag
            await _insert_rt(other_rid, other_tag)     # a DIFFERENT tag of other_rid
            await _insert_rc(rid, cat)                 # target's category membership (survives)
            await _insert_rc(other_rid, cat)           # other's category membership (survives)

            # baseline: two recipes, one section, one item, memberships
            assert await _count("recipe_tags", "recipe_id = :r", {"r": rid}) == 1
            assert await _count("recipe_tags", "recipe_id = :r", {"r": other_rid}) == 2
            assert await _count("recipe_sections", "recipe_id = :r", {"r": rid}) == 1
            assert await _count("recipe_items", "section_id = :s", {"s": sec.id}) == 1
            assert await _count("recipe_categories", "recipe_id = :r", {"r": rid}) == 1

            # DELETE the PROBE tag -> its join rows CASCADE away
            async with get_engine().begin() as conn:
                rc = (await conn.execute(
                    text("DELETE FROM tags WHERE id = :t"), {"t": tag})).rowcount
            assert rc == 1, f"expected to delete exactly the probe tag, deleted {rc}"
            assert await _count("tags", "id = :t", {"t": tag}) == 0, (
                "the probe tag should be gone")
            assert await _count(
                "recipe_tags", "tag_id = :t", {"t": tag}) == 0, (
                "ALL memberships to the deleted tag must be gone (CASCADE)")

            # recipes PRESERVED (both of them)
            assert await _count("recipes", "id = :r", {"r": rid}) == 1, (
                "the target recipe must survive the tag delete")
            assert await _count("recipes", "id = :r", {"r": other_rid}) == 1, (
                "the other recipe must survive the tag delete")
            # the target's SECTION + ITEM survive (body is not a join)
            assert await _count("recipe_sections", "recipe_id = :r", {"r": rid}) == 1, (
                "the target's section must survive the tag delete")
            assert await _count("recipe_items", "section_id = :s", {"s": sec.id}) == 1, (
                "the target's item must survive the tag delete")
            # both recipes' CATEGORY memberships survive the tag delete
            assert await _count(
                "recipe_categories", "recipe_id = :r AND category_id = :c",
                {"r": rid, "c": cat}) == 1, (
                "the target's category membership must survive the tag delete")
            assert await _count(
                "recipe_categories", "recipe_id = :r AND category_id = :c",
                {"r": other_rid, "c": cat}) == 1, (
                "the other recipe's category membership must survive the tag delete")
            # the OTHER tag survives, and the other recipe's membership to it
            assert await _count("tags", "id = :t", {"t": other_tag}) == 1, (
                "the other tag must survive")
            assert await _count(
                "recipe_tags", "recipe_id = :r AND tag_id = :t",
                {"r": other_rid, "t": other_tag}) == 1, (
                "the other recipe's other-tag membership must survive")
            # other_rid now has exactly ONE tag left (its other_tag)
            assert await _count("recipe_tags", "recipe_id = :r", {"r": other_rid}) == 1, (
                "other recipe keeps its one remaining tag membership")
        finally:
            await _clean_probe(
                recipe_ids=[rid, other_rid],
                category_ids=[cat], tag_ids=[other_tag],
                category_slugs=["org-td-cat"],
                tag_slugs=[RECIPE_DELETE_TAG_SLUG, "org-td-other-tag"])

    asyncio.run(run())


def test_tag_rename_preserves_id_and_memberships() -> None:
    """DL-7: renaming a tag (slug/name) is a plain UPDATE — the PK `id` is
    immutable and every `recipe_tags` membership row still points at it, so
    the rename must not disturb a single membership."""
    rid = str(uuid.uuid4())
    tag = str(uuid.uuid4())

    async def run() -> None:
        try:
            async with session_scope() as s:
                s.add(Recipe(id=rid, slug=f"tr-{rid[:8]}", title="TR target"))
                s.add(Tag(id=tag, slug="org-tr-tag-old", name="Old Name"))
            await _insert_rt(rid, tag)
            assert await _count("recipe_tags", "recipe_id = :r AND tag_id = :t",
                               {"r": rid, "t": tag}) == 1
            assert await _count("tags", "id = :t", {"t": tag}) == 1

            # in the real flow an app-side UPDATE would rename; here we assert
            # the DB layer accepts a rename on a row the membership points at.
            async with get_engine().begin() as conn:
                await conn.execute(
                    text("UPDATE tags SET slug = :s, name = :n WHERE id = :t"),
                    {"s": "org-tr-tag-new", "n": "New Name", "t": tag})

            async with get_engine().connect() as conn:
                row = (await conn.execute(
                    text("SELECT id, slug, name FROM tags WHERE id = :t"),
                    {"t": tag})).one()

            assert row[0] == tag, (
                f"the tag's immutable id must be UNCHANGED by the rename, got {row[0]!r}")
            assert row[1] == "org-tr-tag-new" and row[2] == "New Name", (
                f"rename should update slug+name, got {row!r}")
            # the single membership row is fully intact after the rename
            assert await _count("recipe_tags", "recipe_id = :r AND tag_id = :t",
                               {"r": rid, "t": tag}) == 1, (
                "the tag's membership must survive the rename (unaffected by id)")
            assert await _count("recipes", "id = :r", {"r": rid}) == 1, (
                "the recipe must survive the tag rename")
        finally:
            await _clean_probe(
                recipe_ids=[rid], category_ids=[], tag_ids=[tag],
                category_slugs=[],
                tag_slugs=["org-tr-tag-old", "org-tr-tag-new"])

    asyncio.run(run())


def test_reupgrade_preserves_exact_seeds() -> None:
    """Re-running `alembic upgrade head` is a no-op on an already-migrated DB:
    the migration chain has exactly one head, so re-applying must not raise
    (no re-INSERT of the locked 8 seed rows) and must not change the exact
    8 locked seed categories — proof the head is idempotent and the seed
    state is stable under re-upgrade. The subprocess inherits this process's
    environment (the same CT env vars the other tests connect with)."""

    async def run() -> None:
        try:
            # capture the exact seed rows before re-upgrade
            rows_before = await _seed_rows()
            # re-apply: must be a clean no-op (head already applied)
            result = subprocess.run(
                [sys.executable, "-m", "alembic", "upgrade", "head"],
                cwd=str(Path(__file__).resolve().parent.parent),
                env=dict(os.environ), capture_output=True, text=True, timeout=60)
            assert result.returncode == 0, (
                f"re-upgrade must succeed, got rc={result.returncode}\n"
                f"stdout: {result.stdout}\nstderr: {result.stderr}")

            # rows must be byte-for-byte unchanged after the re-upgrade
            rows_after = await _seed_rows()
            before = sorted((p, s, n, i) for i, s, n, p in rows_before)
            after = sorted((p, s, n, i) for i, s, n, p in rows_after)
            assert before == after, (
                "re-upgrade must not change the locked seed rows.\n"
                f" before: {before}\n after: {after}")
            # and still exactly the 8 locked rows
            assert len(rows_after) == 8, (
                f"after re-upgrade there must be exactly 8 seed rows, got {len(rows_after)}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())
