"""v4 `recipe_url_aliases` (spec section 3.5, D6/D17) — schema, named CHECK,
FK rules, explicit index shapes, engine/charset, and exactly-one-target
behavior (DL-6). Run against the ISOLATED test MariaDB (task/6.4); the dev
stack and dev `cookbook` schema must remain untouched (plan shared contract).

All database access here goes through the app's async engine — the same
connection the migrations use. (The dev CLI cannot authenticate on the unix
socket in this container, so `docker exec mariadb` is intentionally not used.)

Async disposal convention: the engine pool is process-global (`lru_cache`) and
an asyncmy connection is bound to the event loop that opened it, so the pool
MUST be drained from WITHIN the same live loop. Every test wraps ALL of its DB
work in one `async def run()` (one `asyncio.run` loop) and disposes the pool
EXACTLY ONCE at the very end of that loop — a single `finally` that runs after
cleanup, so it still drains the pool even if an assertion or a cleanup DELETE
raises. The read/DML helpers therefore do NOT dispose; the one outer `finally`
is sufficient (and avoids the "Future attached to a different loop" risk that
a second `asyncio.run(dispose())` on a fresh loop would introduce).
"""
from __future__ import annotations

import asyncio
import uuid

import sqlalchemy
from sqlalchemy import text

from cookbook.db import get_engine, session_scope
from cookbook.models import Category, Recipe, RecipeUrlAlias

# Category targets for alias round-trips / deletion probes use PROBE categories
# (created + deleted here), never the locked v3 DL-4 seed categories — so the
# seed state is guaranteed untouched.

TABLE = "recipe_url_aliases"

# Named constraints asserted alongside the SQL error code. The code comes from
# the DBAPI error, independent of MariaDB's (whitespace/parenthesis) text form.
CHK_NAME = "chk_alias_exactly_one"
UQ_NAME = "uq_aliases_path"

# MariaDB raises these as distinct DBAPI classes; asserting the exact class
# (never a bare `Exception`) + the SQL error code + the named-constraint marker
# is the contract. These class + code pairs are OBSERVED against MariaDB 11 with
# the v4 DDL (confirmed empirically in a dedicated probe, not assumed):
CHK_ERR = sqlalchemy.exc.OperationalError   # 4025  (chk_alias_exactly_one) — note: CHECK
# is a DBAPI OperationalError in this driver, NOT an IntegrityError.
FK_ERR = sqlalchemy.exc.IntegrityError      # 1452  (missing referent)
UNIQ_ERR = sqlalchemy.exc.IntegrityError    # 1062  (uq_aliases_path)
RESTRICT_ERR = sqlalchemy.exc.IntegrityError  # 1451 (RESTRICT delete of a referenced target)


def _is_uuid(s: object) -> bool:
    return bool(str(s) and len(str(s)) == 36 and str(s).count("-") == 4)


# ---------------------------------------------------------------------------
# Read / DML helpers — these do NOT dispose the pool. Each test drains the pool
# exactly once, in its own outer `async def run()` `finally`, on the live loop.
# ---------------------------------------------------------------------------

async def _column_names() -> list[str]:
    async with get_engine().connect() as conn:
        r = await conn.execute(
            text("SELECT column_name FROM information_schema.columns "
                 "WHERE table_schema = DATABASE() AND table_name = :t"),
            {"t": TABLE})
        return [row[0] for row in r]


async def _column_info(name: str) -> tuple | None:
    async with get_engine().connect() as conn:
        r = await conn.execute(
            text("SELECT column_type, is_nullable, column_default "
                 "FROM information_schema.columns "
                 "WHERE table_schema = DATABASE() AND table_name = :t "
                 "AND column_name = :c"),
            {"t": TABLE, "c": name})
        row = r.first()
    return tuple(row) if row else None


async def _indexes(non_unique: bool) -> dict[str, list[str]]:
    async with get_engine().connect() as conn:
        flag = 1 if non_unique else 0
        r = await conn.execute(
            text("SELECT index_name, column_name, seq_in_index "
                 "FROM information_schema.statistics "
                 "WHERE table_schema = DATABASE() AND table_name = :t "
                 "AND non_unique = :nu ORDER BY index_name, seq_in_index"),
            {"t": TABLE, "nu": flag})
        out: dict[str, list[str]] = {}
        for name, col, _seq in r:
            out.setdefault(name, []).append(col)
    return out


async def _fks() -> dict[str, tuple[str, str, str, str]]:
    """col -> (constraint_name, ref_table, ref_column, delete_rule)."""
    async with get_engine().connect() as conn:
        r = await conn.execute(
            text("SELECT kcu.constraint_name, kcu.column_name, "
                 "       kcu.referenced_table_name, kcu.referenced_column_name, "
                 "       rc.delete_rule "
                 "FROM information_schema.key_column_usage kcu "
                 "JOIN information_schema.referential_constraints rc "
                 "  ON rc.constraint_name = kcu.constraint_name "
                 " AND rc.constraint_schema = kcu.constraint_schema "
                 "WHERE kcu.table_schema = DATABASE() AND kcu.table_name = :t "
                 "ORDER BY kcu.column_name"),
            {"t": TABLE})
        return {row[1]: (row[0], row[2], row[3], row[4]) for row in r}


async def _engine_charset() -> tuple[str | None, str | None]:
    async with get_engine().connect() as conn:
        r = await conn.execute(
            text("SELECT ENGINE, TABLE_COLLATION FROM information_schema.TABLES "
                 "WHERE table_schema = DATABASE() AND table_name = :t"),
            {"t": TABLE})
        row = r.first()
    return (row[0], row[1]) if row else (None, None)


async def _check_names() -> list[str]:
    async with get_engine().connect() as conn:
        r = await conn.execute(
            text("SELECT constraint_name FROM information_schema.check_constraints "
                 "WHERE constraint_schema = DATABASE() AND table_name = :t "
                 "ORDER BY constraint_name"),
            {"t": TABLE})
        return [row[0] for row in r]


async def _table_count(table: str, where_sql: str = "",
                       params: dict | None = None) -> int:
    sql = f"SELECT COUNT(*) FROM {table}" + (f" WHERE {where_sql}" if where_sql else "")
    async with get_engine().connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar()


async def _row(alias_id: str) -> tuple | None:
    async with get_engine().connect() as conn:
        r = await conn.execute(
            text(f"SELECT path, recipe_id, category_id FROM {TABLE} WHERE id = :a"),
            {"a": alias_id})
        row = r.first()
    return tuple(row) if row else None


async def _exec(sql: str, params: dict | None) -> int:
    """One DML statement in begin/commit; raises on the first error. Disposal
    is deliberately left to the caller's outer `finally` (one outer drain)."""
    async with get_engine().begin() as conn:
        return (await conn.execute(text(sql), params or {})).rowcount


async def _free_category_position() -> int:
    """`position` is globally UNIQUE on categories (uq_categories_position):
    pick count(existing)+1 so a probe is accepted whether the DB is fresh (seeds
    1..8) or has stale probe rows. Our alias tests never create a membership, so
    a stray position is not FK-referenced and cleans up freely."""
    n = await _table_count("categories")
    return (n + 1) if n else 1


async def _expect_rejected(
        sql: str, params: dict | None, *, exc_class: type[Exception], code: str,
        marker: str) -> None:
    """Run `sql`; REQUIRE it to be rejected with `exc_class` carrying SQL error
    `code` and `marker` (a specific constraint name) in the message. A bare
    `Exception` is never accepted: an unrelated success OR a different error
    class both raise AssertionError (no broad-Exception swallowing)."""
    try:
        await _exec(sql, params)
    except exc_class as ex:
        msg = str(ex)
        assert code in msg, f"expected SQL code {code} in the error, got: {msg}"
        assert marker in msg, f"expected marker {marker!r} in the error, got: {msg}"
        return
    raise AssertionError(
        f"expected {exc_class.__name__} (code {code}, marker {marker!r}) for:\n{sql}")


async def _clean(
        alias_ids: list[str],
        recipe_ids: list[str],
        category_ids: list[str],
        recipe_slugs: list[str],
        category_slugs: list[str],
) -> None:
    """FK-safe cleanup: alias rows (children) first, then their recipe/category
    targets — by id and, for any wrongly-accepted probe row, by stable slug so a
    stray insert still leaves a deterministic row to remove. No disposal here —
    the caller's outer `finally` owns it (one outer drain on the live loop)."""
    async with get_engine().begin() as conn:
        for a in alias_ids:
            await conn.execute(text(f"DELETE FROM {TABLE} WHERE id = :a"), {"a": a})
        for r in recipe_ids:
            await conn.execute(text("DELETE FROM recipes WHERE id = :r"), {"r": r})
        for c in category_ids:
            await conn.execute(text("DELETE FROM categories WHERE id = :c"), {"c": c})
        for s in recipe_slugs:
            await conn.execute(text("DELETE FROM recipes WHERE slug = :s"), {"s": s})
        for s in category_slugs:
            await conn.execute(text("DELETE FROM categories WHERE slug = :s"), {"s": s})


# ===========================================================================
# SCHEMA (pure information_schema). Against the pre-v4 baseline the column and
# check tests fail hard (1146 table-doesn't-exist) and the index/FK tests fail
# because their objects are absent — genuine RED from a missing schema, not a
# model-import failure.
# ===========================================================================

def test_exact_column_set_types_nullability_default() -> None:
    """Exactly 5 columns, exact MariaDB 11 types, correct nullability, and a
    current_timestamp/now server default on created_at (id/path/created_at NOT
    NULL; recipe_id/category_id NULL). The named CHECK is NOT a column in
    MariaDB 11 (it lives in check_constraints), so the exact column set is 5 —
    which also proves there are no generated/helper/extra columns."""

    async def run() -> None:
        try:
            names = await _column_names()
            assert set(names) == {"id", "path", "recipe_id", "category_id", "created_at"}, (
                f"column set (must be exactly these 5, no helper columns): {sorted(names)}")

            got = {c: await _column_info(c) for c in names}
            types = {c: v[0] for c, v in got.items()}
            assert types == {
                "id": "varchar(36)", "path": "varchar(255)",
                "recipe_id": "varchar(36)", "category_id": "varchar(36)",
                "created_at": "datetime",
            }, f"types: {types}"

            nullable = {c: v[1] for c, v in got.items()}
            assert nullable["id"] == "NO", "id must be NOT NULL"
            assert nullable["path"] == "NO", "path must be NOT NULL"
            assert nullable["created_at"] == "NO", "created_at must be NOT NULL"
            assert nullable["recipe_id"] == "YES", "recipe_id must be NULL"
            assert nullable["category_id"] == "YES", "category_id must be NULL"

            # created_at's server default is CURRENT_TIMESTAMP in MariaDB 11
            # (information_schema reports it as "current_timestamp()"); confirm
            # the exact live value rather than allowing any of now()/CURRENT_TS.
            raw_default = (got["created_at"][2] or "").lower().replace(" ", "")
            assert "current_timestamp" in raw_default, (
                f"created_at server default should be CURRENT_TIMESTAMP, got {raw_default!r}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_primary_key_exactly_id() -> None:
    async def run() -> None:
        try:
            uniq = await _indexes(non_unique=False)
            assert uniq.get("PRIMARY") == ["id"], (
                f"PRIMARY should be exactly [id], got {uniq.get('PRIMARY')!r}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_uq_aliases_path_exactly_path() -> None:
    async def run() -> None:
        try:
            uniq = await _indexes(non_unique=False)
            assert uniq.get(UQ_NAME) == ["path"], (
                f"{UQ_NAME} should be exactly [path], got {uniq.get(UQ_NAME)!r} "
                f"(unique indexes: {list(uniq)})")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_explicit_nonunique_fk_indexes() -> None:
    async def run() -> None:
        try:
            nu = await _indexes(non_unique=True)
            assert nu.get("ix_recipe_url_aliases_recipe_id") == ["recipe_id"], (
                f"ix_recipe_url_aliases_recipe_id should be [recipe_id], "
                f"got {nu.get('ix_recipe_url_aliases_recipe_id')!r} "
                f"(non-unique indexes: {list(nu)})")
            assert nu.get("ix_recipe_url_aliases_category_id") == ["category_id"], (
                f"ix_recipe_url_aliases_category_id should be [category_id], "
                f"got {nu.get('ix_recipe_url_aliases_category_id')!r} "
                f"(non-unique indexes: {list(nu)})")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_both_fks_restrict() -> None:
    """Both FKs, exact names, source column, target table/column, RESTRICT rule
    (DL-6):
    recfk_aliases_recipe   : recipe_id   -> recipes.id     ON DELETE RESTRICT
    recfk_aliases_category : category_id -> categories.id  ON DELETE RESTRICT"""

    async def run() -> None:
        try:
            fks = await _fks()
            assert fks.get("recipe_id") == ("recfk_aliases_recipe", "recipes", "id", "RESTRICT"), (
                f"recipe_id FK: {fks.get('recipe_id')!r}")
            assert fks.get("category_id") == (
                "recfk_aliases_category", "categories", "id", "RESTRICT"), (
                f"category_id FK: {fks.get('category_id')!r}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_named_check_constraint_exists() -> None:
    """chk_alias_exactly_one is present by NAME in information_schema.
    check_constraints — existence proved without parsing MariaDB's reformatted
    clause text (whitespace/parenthesis formatting varies)."""

    async def run() -> None:
        try:
            names = await _check_names()
            assert CHK_NAME in names, (
                f"{CHK_NAME} missing from information_schema.check_constraints: {names}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_engine_innodb_utf8mb4() -> None:
    async def run() -> None:
        try:
            engine, collation = await _engine_charset()
            assert engine == "InnoDB", f"engine should be InnoDB, got {engine!r}"
            assert isinstance(collation, str) and collation.startswith("utf8mb4"), (
                f"collation {collation!r} must be a utf8mb4-charset collation")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


# ===========================================================================
# BEHAVIOR — exactly-one target, FK referents, unique path, and RESTRICT
# ===========================================================================

def test_recipe_alias_roundtrip_defaults_and_preserved_path() -> None:
    """Recipe-only ORM round-trip: the `_uuid` default yields a valid 36-char
    id, created_at is populated by its server default, and the full legacy path
    round-trips EXACTLY (no normalization/slugify). recipe_id is set,
    category_id is NULL (exactly one target). The ORM parent (recipe) is
    flushed before the dependent alias is inserted; both commit on exit."""
    rid = str(uuid.uuid4())
    slug = f"alrt-{rid[:8]}"
    path = "/?p=1234"
    alias_id: str | None = None

    async def run() -> None:
        try:
            async with session_scope() as s:
                s.add(Recipe(id=rid, slug=slug, title="RT recipe"))
                await s.flush()  # apply the parent FIRST, before the child alias
                alias = RecipeUrlAlias(path=path, recipe_id=rid, category_id=None)
                s.add(alias)
                await s.flush()          # apply the alias _uuid default
                alias_id = alias.id      # read back by id (default already applied)
            assert _is_uuid(alias_id), (
                f"_uuid default should be a valid UUID, got {alias_id!r}")

            row = await _row(alias_id)
            assert row is not None, "alias row must exist after round-trip"
            assert row[0] == path, (
                f"path must round-trip EXACTLY, got {row[0]!r} (want {path!r})")
            assert row[1] == rid, f"recipe_id should be {rid}, got {row[1]!r}"
            assert row[2] is None, f"category_id must be NULL, got {row[2]!r}"

            async with get_engine().connect() as conn:
                val = (await conn.execute(
                    text(f"SELECT created_at FROM {TABLE} WHERE id = :a"),
                    {"a": alias_id})).scalar_one()
            assert val is not None, "created_at must be populated by its server default"
        finally:
            try:
                await _clean(alias_ids=[alias_id] if alias_id else [],
                             recipe_ids=[rid], category_ids=[],
                             recipe_slugs=[slug], category_slugs=[])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_category_alias_roundtrip_defaults_and_preserved_path() -> None:
    """Category-only ORM round-trip (mirror of the recipe one): the `_uuid`
    default AND the server-created default on created_at both apply, the legacy
    path rounds-trips EXACTLY, category_id is set and recipe_id is NULL."""
    cid = str(uuid.uuid4())
    slug = f"alcg-{cid[:8]}"
    path = "/?page_id=16"
    alias_id: str | None = None

    async def run() -> None:
        try:
            pos = await _free_category_position()
            async with session_scope() as s:
                s.add(Category(id=cid, slug=slug, name="RT cat", position=pos))
                await s.flush()  # apply the parent FIRST, before the child alias
                alias = RecipeUrlAlias(path=path, recipe_id=None, category_id=cid)
                s.add(alias)
                await s.flush()
                alias_id = alias.id
            assert _is_uuid(alias_id), (
                f"_uuid default should be a valid UUID, got {alias_id!r}")

            row = await _row(alias_id)
            assert row is not None, "alias row must exist after round-trip"
            assert row[0] == path, (
                f"path must round-trip EXACTLY, got {row[0]!r} (want {path!r})")
            assert row[1] is None, f"recipe_id must be NULL, got {row[1]!r}"
            assert row[2] == cid, f"category_id should be {cid}, got {row[2]!r}"

            async with get_engine().connect() as conn:
                val = (await conn.execute(
                    text(f"SELECT created_at FROM {TABLE} WHERE id = :a"),
                    {"a": alias_id})).scalar_one()
            assert val is not None, "created_at must be populated by its server default"
        finally:
            try:
                await _clean(alias_ids=[alias_id] if alias_id else [],
                             recipe_ids=[], category_ids=[cid],
                             recipe_slugs=[], category_slugs=[slug])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_neither_and_both_target_insert_rejected() -> None:
    """The exactly-one CHECK rejects both sides at INSERT time: a row with BOTH
    targets set AND a row with NEITHER set each fail with the named constraint
    (4025, chk_alias_exactly_one) and leave NO row behind. (The UPDATE-side
    rejections are covered separately below.)"""
    rboth = str(uuid.uuid4()); cboth = str(uuid.uuid4())
    rslug = f"chkr-{rboth[:8]}"; cslug = f"chkc-{cboth[:8]}"

    async def run() -> None:
        try:
            pos = await _free_category_position()
            async with session_scope() as s:
                s.add(Recipe(id=rboth, slug=rslug, title="CHK recipe"))
                s.add(Category(id=cboth, slug=cslug, name="CHK cat", position=pos))
                await s.flush()
            await _expect_rejected(
                f"INSERT INTO {TABLE} (id, path, recipe_id, category_id) "
                "VALUES (UUID(), '/?x=chkboth', :r, :c)",
                {"r": rboth, "c": cboth},
                exc_class=CHK_ERR, code="4025", marker=CHK_NAME)
            assert await _table_count(TABLE, "path = '/?x=chkboth'") == 0, (
                "no row may be created when BOTH targets are set")
            await _expect_rejected(
                f"INSERT INTO {TABLE} (id, path, recipe_id, category_id) "
                "VALUES (UUID(), '/?x=chknone', NULL, NULL)",
                None,
                exc_class=CHK_ERR, code="4025", marker=CHK_NAME)
            assert await _table_count(TABLE, "path = '/?x=chknone'") == 0, (
                "no row may be created when NEITHER target is set")
        finally:
            try:
                await _clean(alias_ids=[], recipe_ids=[rboth], category_ids=[cboth],
                             recipe_slugs=[rslug], category_slugs=[cslug])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_invalid_target_update_rejected_alias_unchanged() -> None:
    """The exactly-one CHECK also rejects target-mutating UPDATEs, and a
    rejected UPDATE must leave the row byte-for-byte unchanged:
      - set category_id while recipe_id is still set -> BOTH -> 4025
      - clear recipe_id (leaving category_id NULL)   -> NEITHER -> 4025
    In both cases the alias stays recipe-only on the same path — never zero
    or two targets. A specific constraint marker is asserted beside a bare
    success, so an unrelated error can never be masked as a pass."""
    rid = str(uuid.uuid4())
    cid = str(uuid.uuid4())
    rslug = f"aupd-{rid[:8]}"
    cslug = f"aupd-{cid[:8]}"
    path = "/?x=update"
    ali = str(uuid.uuid4())

    async def run() -> None:
        try:
            pos = await _free_category_position()
            async with session_scope() as s:
                s.add(Recipe(id=rid, slug=rslug, title="UPD recipe"))
                s.add(Category(id=cid, slug=cslug, name="UPD cat", position=pos))
                await s.flush()
                s.add(RecipeUrlAlias(id=ali, path=path, recipe_id=rid, category_id=None))
                await s.flush()

            # (1) BOTH set: set category_id while recipe_id is still rid
            await _expect_rejected(
                f"UPDATE {TABLE} SET category_id = :c WHERE id = :a",
                {"c": cid, "a": ali},
                exc_class=CHK_ERR, code="4025", marker=CHK_NAME)
            row = await _row(ali)
            assert row == (path, rid, None), (
                f"row must be UNCHANGED after the both-set UPDATE, got {row!r}")

            # (2) NEITHER set: clear recipe_id (category_id is still NULL)
            await _expect_rejected(
                f"UPDATE {TABLE} SET recipe_id = NULL WHERE id = :a",
                {"a": ali},
                exc_class=CHK_ERR, code="4025", marker=CHK_NAME)
            row = await _row(ali)
            assert row == (path, rid, None), (
                f"row must be UNCHANGED after the neither-set UPDATE, got {row!r}")
        finally:
            try:
                await _clean(alias_ids=[ali], recipe_ids=[rid], category_ids=[cid],
                             recipe_slugs=[rslug], category_slugs=[cslug])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_missing_targets_rejected_by_correct_fk() -> None:
    """An alias whose recipe_id / category_id points at a NON-EXISTENT parent is
    rejected by the correct FK (both code 1452), naming the exact constraint:
    bogus recipe_id   -> recfk_aliases_recipe
    bogus category_id -> recfk_aliases_category
    and no alias row is created in either case (the bogus parent is never
    created, which is the whole point of the referent check)."""
    FAKE_UUID = "00000000-0000-4000-8000-0000000000ab"

    async def run() -> None:
        try:
            await _expect_rejected(
                f"INSERT INTO {TABLE} (id, path, recipe_id, category_id) "
                "VALUES (UUID(), '/?x=missr', :r, NULL)",
                {"r": FAKE_UUID},
                exc_class=FK_ERR, code="1452", marker="recfk_aliases_recipe")
            assert await _table_count(TABLE, "recipe_id = :r", {"r": FAKE_UUID}) == 0, (
                "no alias may be created for a missing recipe referent")

            await _expect_rejected(
                f"INSERT INTO {TABLE} (id, path, recipe_id, category_id) "
                "VALUES (UUID(), '/?x=missc', NULL, :c)",
                {"c": FAKE_UUID},
                exc_class=FK_ERR, code="1452", marker="recfk_aliases_category")
            assert await _table_count(TABLE, "category_id = :c", {"c": FAKE_UUID}) == 0, (
                "no alias may be created for a missing category referent")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_duplicate_path_rejected_including_across_types() -> None:
    """uq_aliases_path is table-wide UNIQUE: the SAME legacy path is rejected a
    second time whether the repeat targets the OTHER kind of parent (a
    recipe-typed and a category-typed alias cannot share a path) OR the SAME
    kind. Each rejected insert is verified not to exist afterwards, and the
    original owner row remains the only holder of the path."""
    rdup = str(uuid.uuid4()); rslug = f"dupr-{rdup[:8]}"
    cdup = str(uuid.uuid4()); cslug = f"dupc-{cdup[:8]}"
    path = "/?x=dup"
    a1 = str(uuid.uuid4()); a2 = str(uuid.uuid4()); a3 = str(uuid.uuid4())

    async def run() -> None:
        try:
            pos = await _free_category_position()
            async with session_scope() as s:
                s.add(Recipe(id=rdup, slug=rslug, title="DUP recipe"))
                s.add(Category(id=cdup, slug=cslug, name="DUP cat", position=pos))
                await s.flush()
                # baseline: this path is owned by a recipe-targeted alias
                s.add(RecipeUrlAlias(id=a1, path=path, recipe_id=rdup, category_id=None))
                await s.flush()

            # same path, OTHER target type (category) -> reject
            await _expect_rejected(
                f"INSERT INTO {TABLE} (id, path, recipe_id, category_id) "
                "VALUES (:a, :p, NULL, :c)",
                {"a": a2, "p": path, "c": cdup},
                exc_class=UNIQ_ERR, code="1062", marker=UQ_NAME)
            assert await _table_count(TABLE, "path = :p", {"p": path}) == 1, (
                "the duplicate (category-typed) row must NOT be created")

            # same path, SAME target type (recipe) -> reject
            await _expect_rejected(
                f"INSERT INTO {TABLE} (id, path, recipe_id, category_id) "
                "VALUES (:a, :p, :r, NULL)",
                {"a": a3, "p": path, "r": rdup},
                exc_class=UNIQ_ERR, code="1062", marker=UQ_NAME)
            assert await _table_count(TABLE, "path = :p", {"p": path}) == 1, (
                "the duplicate (recipe-typed) row must NOT be created")
        finally:
            try:
                await _clean(alias_ids=[a1, a2, a3], recipe_ids=[rdup],
                             category_ids=[cdup],
                             recipe_slugs=[rslug], category_slugs=[cslug])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_recipe_delete_blocked_until_alias_reassigned() -> None:
    """DL-6: a referenced recipe cannot be deleted while an alias points at it
    (ON DELETE RESTRICT, recfk_aliases_recipe). Reassigning the alias to a
    second, valid recipe RELEASES the block; the old recipe then deletes, while
    the alias id + path are unchanged and it still has exactly one valid target.
    Two PROBE recipes are created and only the first is later deleted (the
    second is the surviving alias target, removed in finally)."""
    r1 = str(uuid.uuid4()); r2 = str(uuid.uuid4())
    s1 = f"resx-{r1[:8]}"; s2 = f"resx-{r2[:8]}"
    path = "/?p=restr"
    ali = str(uuid.uuid4())

    async def run() -> None:
        try:
            async with session_scope() as s:
                s.add(Recipe(id=r1, slug=s1, title="RESTRICT r1"))
                s.add(Recipe(id=r2, slug=s2, title="RESTRICT r2"))
                await s.flush()
                s.add(RecipeUrlAlias(id=ali, path=path, recipe_id=r1, category_id=None))
                await s.flush()

            # in-use: delete r1 -> BLOCKED by RESTRICT
            await _expect_rejected(
                "DELETE FROM recipes WHERE id = :r", {"r": r1},
                exc_class=RESTRICT_ERR, code="1451", marker="recfk_aliases_recipe")
            assert await _table_count("recipes", "id = :r", {"r": r1}) == 1, (
                "recipe r1 must still exist after the blocked delete")
            row = await _row(ali)
            assert row == (path, r1, None), "alias must still point at r1, unchanged"

            # reassign the alias to r2 (still exactly one target) -> allowed
            await _exec(f"UPDATE {TABLE} SET recipe_id = :r WHERE id = :a",
                        {"r": r2, "a": ali})
            # now r1 is free to delete
            await _exec("DELETE FROM recipes WHERE id = :r", {"r": r1})
            assert await _table_count("recipes", "id = :r", {"r": r1}) == 0, (
                "r1 should be gone after reassignment")
            row = await _row(ali)
            assert row == (path, r2, None), (
                f"alias must retain id + path and point at the new target r2, got {row!r}")
        finally:
            try:
                await _clean(alias_ids=[ali], recipe_ids=[r1, r2], category_ids=[],
                             recipe_slugs=[s1, s2], category_slugs=[])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_category_delete_blocked_until_alias_reassigned() -> None:
    """DL-6 mirror on the category side — using PROBE categories, NEVER a seed:
    a referenced category cannot be deleted while an alias points at it
    (ON DELETE RESTRICT, recfk_aliases_category). Reassigning the alias to a
    second probe category RELEASES the block; the first deletes, the alias id +
    path are unchanged and it still has exactly one valid target."""
    c1 = str(uuid.uuid4()); c2 = str(uuid.uuid4())
    s1 = f"cresx-{c1[:8]}"; s2 = f"cresx-{c2[:8]}"
    path = "/?page_id=restrc"
    ali = str(uuid.uuid4())

    async def run() -> None:
        try:
            pos = await _free_category_position()
            async with session_scope() as s:
                s.add(Category(id=c1, slug=s1, name="RESTRICT c1", position=pos))
                s.add(Category(id=c2, slug=s2, name="RESTRICT c2", position=pos + 1))
                await s.flush()
                s.add(RecipeUrlAlias(id=ali, path=path, recipe_id=None, category_id=c1))
                await s.flush()

            # in-use: delete c1 -> BLOCKED by RESTRICT
            await _expect_rejected(
                "DELETE FROM categories WHERE id = :c", {"c": c1},
                exc_class=RESTRICT_ERR, code="1451", marker="recfk_aliases_category")
            assert await _table_count("categories", "id = :c", {"c": c1}) == 1, (
                "category c1 must still exist after the blocked delete")
            row = await _row(ali)
            assert row == (path, None, c1), "alias must still point at c1, unchanged"

            # reassign the alias to c2 (still exactly one target) -> allowed
            await _exec(f"UPDATE {TABLE} SET category_id = :c WHERE id = :a",
                        {"c": c2, "a": ali})
            # now c1 is free to delete
            await _exec("DELETE FROM categories WHERE id = :c", {"c": c1})
            assert await _table_count("categories", "id = :c", {"c": c1}) == 0, (
                "c1 should be gone after reassignment")
            row = await _row(ali)
            assert row == (path, None, c2), (
                f"alias must retain id + path and point at c2, got {row!r}")
        finally:
            try:
                await _clean(alias_ids=[ali], recipe_ids=[], category_ids=[c1, c2],
                             recipe_slugs=[], category_slugs=[s1, s2])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_recipe_to_category_target_switch_in_one_update() -> None:
    """A recipe-targeted alias can switch to a category target — in a SINGLE
    UPDATE — and land as a valid category-only alias (recipe_id -> NULL,
    category_id -> new target). The EXACTLY-ONE invariant holds through the
    write (the CHECK is re-evaluated and passes), and the alias id + path are
    unchanged by the switch. This is the legitimate reassignment flow that
    precedes freeing the old recipe target."""
    rid = str(uuid.uuid4()); cid = str(uuid.uuid4())
    rslug = f"swx-{rid[:8]}"; cslug = f"swx-{cid[:8]}"
    path = "/?p=switch"
    ali = str(uuid.uuid4())

    async def run() -> None:
        try:
            pos = await _free_category_position()
            async with session_scope() as s:
                s.add(Recipe(id=rid, slug=rslug, title="SWITCH recipe"))
                s.add(Category(id=cid, slug=cslug, name="SWITCH cat", position=pos))
                await s.flush()
                s.add(RecipeUrlAlias(id=ali, path=path, recipe_id=rid, category_id=None))
                await s.flush()

            # one UPDATE swaps the target type: recipe_id -> NULL, category_id -> cid
            await _exec(
                f"UPDATE {TABLE} SET recipe_id = NULL, category_id = :c WHERE id = :a",
                {"c": cid, "a": ali})
            row = await _row(ali)
            assert row == (path, None, cid), (
                f"alias must now be category-only on the same path, got {row!r}")
            assert await _table_count(TABLE, "id = :a", {"a": ali}) == 1, (
                "the alias row must still exist with exactly one target")
        finally:
            try:
                await _clean(alias_ids=[ali], recipe_ids=[rid], category_ids=[cid],
                             recipe_slugs=[rslug], category_slugs=[cslug])
            finally:
                await get_engine().dispose()

    asyncio.run(run())
