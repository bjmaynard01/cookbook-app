"""Full-chain schema verification (Task 6.6 merge gate, extended by Task 7.1).

The ONLY task that composes the whole ``v0 -> v1 -> v2 -> v3 -> v4 -> v5 -> v6``
chain in ONE pristine database and asserts it as a regression test. It adds
verification for EXISTING behavior only — no model/migration changes of its
own. Run against the ISOLATED test MariaDB (the dev stack and dev ``cookbook``
schema must remain untouched — plan shared contract). It is the proof the chain
composes, not just that each link passes on its own.

Areas:
  1. Exact base-table inventory (14 app tables + alembic_version) + the alembic
     head, asserted separately (fourteen, not thirteen — v6 adds three auth
     tables to the eleven v0–v5 tables).
  2. Alias RESTRICT guard, then a real parent DELETE: full recipe section/item
     CASCADE + both joins CASCADE + audit log SET NULL, while the alias's
     id/path survive and it re-points at one valid target.
  3. DL-4 seed stability (exactly the 8 locked rows, no "Others"/tag seeds)
     after the cross-table probe.
  4. UUID/type invariant (restructured by Task 7.1): discovery of ALL
     application PK/FK keys is TYPE-INDEPENDENT (no varchar(36) filter, so a
     UUID key that drifts off varchar(36) cannot hide), then compared to the
     union of an explicit EXPECTED_UUID_KEYS set and an explicit
     EXPECTED_NONUUID_KEYS set (Alembic's bookkeeping key excluded
     explicitly); key TYPES are asserted separately from discovery (every
     UUID key is varchar(36); the non-UUID PKs are their exact char(64)/
     varchar(40) types); the char(64)/char(32)/external-id ledgers are
     exact sets; and alembic_version.version_num is NOT misclassified.

Disposal convention (same as test_aliases.py / test_migration_audit.py): the
engine pool is process-global (lru_cache) and an asyncmy connection is bound to
the loop that opened it, so EVERY test does ALL DB work inside one ``async def
run()`` (one ``asyncio.run`` loop) and disposes the pool EXACTLY ONCE in that
loop's outermost ``finally`` — disposal therefore still drains the pool even if
an assertion or a cleanup DELETE raises, because cleanup lives in an inner
``try/finally`` before the ``await get_engine().dispose()``. The helpers below
never dispose.
"""
from __future__ import annotations

import asyncio
import uuid

import sqlalchemy
from sqlalchemy import text

from cookbook.db import get_engine, session_scope
from cookbook.models import (
    MigrationRecipeLog,
    MigrationRun,
    Recipe,
    RecipeItem,
    RecipeSection,
    RecipeUrlAlias,
    Tag,
)

# --------------------------------------------------------------------------
# Locked fixed literals
# --------------------------------------------------------------------------
# Plan §Task 3: exactly the 8 DL-4 locked seed categories (position, slug, name, id).
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
# Two seeded category ids used as membership parents. We NEVER delete seeds.
BREAKFAST_ID = "412b94fd-8106-40b8-863b-46e2b1f1daff"
FERMENTATION_ID = "26857e44-4786-4068-8faf-607d4a5a83a1"

# EXACT application table inventory — FOURTEEN tables. The v0–v5 chain
# yields ELEVEN application tables; v6 (auth-plan S1) adds THREE (sessions,
# login_attempts, auth_locks).
APP_TABLES = {
    "users",                  # v0
    "recipes",                # v1
    "recipe_sections",        # v2
    "recipe_items",           # v2
    "categories",             # v3
    "recipe_categories",      # v3
    "tags",                   # v3
    "recipe_tags",            # v3
    "recipe_url_aliases",     # v4
    "migration_runs",         # v5
    "migration_recipe_log",   # v5
    "sessions",               # v6
    "login_attempts",         # v6
    "auth_locks",             # v6
}
# Alembic ALSO creates this bookkeeping table; verified separately so the
# "fourteen application tables" assertion is not silently widened to fifteen.
ALEMBIC_VERSION_TABLE = "alembic_version"
EXPECTED_BASE_TABLES = APP_TABLES | {ALEMBIC_VERSION_TABLE}

# Expected migration head after the full forward chain through v6.
EXPECTED_HEAD = "e4c7a91d2f30"

# --------------------------------------------------------------------------
# Locked key sets (explicit). The invariant test discovers ALL application
# PK/FK keys TYPE-INDEPENDENTLY (no varchar(36) filter, so a UUID key that
# drifts off varchar(36) is still discovered and cannot hide), then compares
# the discovered set to EXPECTED_UUID_KEYS ∪ EXPECTED_NONUUID_KEYS, with the
# Alembic bookkeeping key EXCLUDED EXPLICITLY. Key types are then asserted
# SEPARATELY from discovery (every UUID key is varchar(36); the non-UUID PKs
# are their exact char(64)/varchar(40) types). ``version_num`` is explicitly
# excluded: it is alembic's bookkeeping column, not an application key.
# --------------------------------------------------------------------------
EXPECTED_UUID_KEYS = {
    ("users", "id"),
    ("recipes", "id"),
    ("recipes", "created_by"),
    ("recipes", "last_modified_by"),
    ("recipe_sections", "id"),
    ("recipe_sections", "recipe_id"),
    ("recipe_items", "id"),
    ("recipe_items", "section_id"),
    ("categories", "id"),
    ("recipe_categories", "recipe_id"),
    ("recipe_categories", "category_id"),
    ("tags", "id"),
    ("recipe_tags", "recipe_id"),
    ("recipe_tags", "tag_id"),
    ("recipe_url_aliases", "id"),
    ("recipe_url_aliases", "recipe_id"),
    ("recipe_url_aliases", "category_id"),
    ("migration_runs", "id"),
    ("migration_runs", "operator_id"),
    ("migration_recipe_log", "id"),
    ("migration_recipe_log", "run_id"),
    ("migration_recipe_log", "recipe_id"),
    # v6 — the only new key-bearing UUID columns (sessions.id_hash is a
    # non-UUID char(64) PK, auth_locks.name a non-UUID varchar(40) PK; the
    # uq_login_attempts_pair unique key is NOT a PK/FK so key discovery does
    # not list it — it is asserted as a uniqueness contract in test_auth_schema).
    ("sessions", "user_id"),
    ("login_attempts", "id"),
}
# v6 non-UUID primary keys (the ONLY application keys that are not uuid
# columns). Discovered alongside the UUID keys and asserted by exact type.
EXPECTED_NONUUID_KEYS = {
    ("sessions", "id_hash"),      # char(64) PK
    ("auth_locks", "name"),       # varchar(40) PK
}
# External (non-UUID) identifiers that happen to be varchar(64). They reference
# WordPress post ids, NOT application UUIDs.
EXTERNAL_ID_COLUMNS = {
    ("recipes", "source_id"),
    ("migration_recipe_log", "source_post_id"),
}
# EXACT char(64) ledger (DL-5's fixed-length-hash exception set): the ONLY
# two char(64) columns in the schema (a NEW char(64) column is a failure).
CHAR64_COLUMNS = {
    ("migration_runs", "file_sha256"),
    ("sessions", "id_hash"),
}
# EXACT char(32) ledger: the ONLY char(32) column in the schema.
CHAR32_COLUMNS = {
    ("sessions", "csrf_token"),
}


# --------------------------------------------------------------------------
# Read/DML helpers (do NOT dispose — the owning test owns disposal; the single
# ``asyncio.run`` loop per test keeps the pool bound to one live event loop).
# --------------------------------------------------------------------------
async def _base_tables() -> set[str]:
    async with get_engine().connect() as conn:
        res = await conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE'"))
        return {r[0] for r in res}


async def _alembic_version_head() -> str | None:
    async with get_engine().connect() as conn:
        row = (await conn.execute(text(
            f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE}"))).first()
        return row[0] if row else None


async def _alembic_version_rowcount() -> int:
    async with get_engine().connect() as conn:
        return (await conn.execute(text(
            f"SELECT COUNT(*) FROM {ALEMBIC_VERSION_TABLE}"))).scalar_one()


async def _seed_rows() -> list[tuple[int, str, str, str]]:
    """Return (position, slug, name, id) ordered by position."""
    async with get_engine().connect() as conn:
        res = await conn.execute(text(
            "SELECT position, slug, name, id FROM categories ORDER BY position"))
        return [tuple(r) for r in res]


async def _count(table: str, where_sql: str = "",
                 params: dict | None = None) -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if where_sql:
        sql += f" WHERE {where_sql}"
    async with get_engine().connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar_one()


async def _exec(sql: str, params: dict | None) -> int:
    """A single DML statement in its own connection/transaction. Disposal is
    deliberately left to the owning test's outer ``finally`` (one drain)."""
    async with get_engine().begin() as conn:
        return (await conn.execute(text(sql), params or {})).rowcount


# ===========================================================================
# A. Exact base-table inventory + alembic head (the merge gate)
# ===========================================================================

async def _all_columns() -> list[tuple[str, str, str]]:
    """(table_name, column_name, column_type) for every base table column in
    this schema. Used by the invariant test to discover the live column set."""
    async with get_engine().connect() as conn:
        res = await conn.execute(text(
            "SELECT c.table_name, c.column_name, c.column_type "
            "FROM information_schema.columns c "
            "JOIN information_schema.tables t "
            "  ON t.table_schema = c.table_schema AND t.table_name = c.table_name "
            "WHERE c.table_schema = DATABASE() AND t.table_type = 'BASE TABLE'"))
        return [tuple(r) for r in res]


def test_exact_base_table_inventory_and_alembic_head() -> None:
    """The live BASE TABLE set is EXACTLY the fourteen application tables PLUS
    alembic_version — no extras (catches a stray migration_tag_log or later
    convenience table), no missing (catches a dropped slice). alembic_version
    also contains EXACTLY the expected v6 head.

    The head is asserted separately from the table-inventory so that 'the
    fourteen application tables are present' and 'the migration is at the
    expected head' are independent proofs (the inventory test still would pass
    on a partial chain where alembic_version exists but is not at the v6 head).
    """
    async def run() -> None:
        try:
            tables = await _base_tables()
            assert tables == EXPECTED_BASE_TABLES, (
                f"BASE TABLE set must be EXACTLY the 14 app tables + alembic_version.\n"
                f" missing : {sorted(EXPECTED_BASE_TABLES - tables)}\n"
                f" extra   : {sorted(tables - EXPECTED_BASE_TABLES)}")
            # the fourteen application tables only:
            assert tables - {ALEMBIC_VERSION_TABLE} == APP_TABLES, (
                f"the fourteen application tables must be EXACTLY this set (no extras "
                f"beyond alembic_version).\n got: {sorted(tables)}")

            head = await _alembic_version_head()
            n = await _alembic_version_rowcount()
            assert n == 1, (
                f"alembic_version must contain EXACTLY one row (one linear head), "
                f"found {n}")
            assert head == EXPECTED_HEAD, (
                f"alembic_version.head must be the expected "
                f"{EXPECTED_HEAD!r}; got {head!r}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


# ===========================================================================
# C. Seed stability — the DL-4 locked 8 rows survive the cross-table probe
# ===========================================================================

def test_seed_rows_locked_and_no_others_or_tag_seeds() -> None:
    """The 8 locked DL-4 seed categories (exact id/slug/name/position, dense
    1..8) are still present with no 'Others' row (the importer owns it) and no
    tag seeds. The cascading probe separately checks seeds before and after
    its deletes, without depending on test execution order."""
    async def run() -> None:
        try:
            rows = await _seed_rows()   # (position, slug, name, id)
            assert len(rows) == 8, f"expected exactly 8 seed categories, got {len(rows)}"
            # exact match (position, slug, name, id) — order by position
            got = sorted(rows)
            expected = sorted(SEED_ROWS)
            assert got == expected, (
                "seed rows must be byte-for-byte the locked 8.\n"
                f" got     : {sorted(rows)}\n expected: {expected}")
            # dense 1..8, no gap, no dup
            positions = sorted(p for p, _s, _n, _i in rows)
            assert positions == list(range(1, 9)), (
                f"positions must be dense 1..8 (no gaps/dups), got {positions}")
            # no 'Others'
            others = await _count("categories", "slug = 'others'")
            assert others == 0, f"unexpected 'others' category row: {others}"
            # no tag seeds
            tcnt = await _count("tags")
            assert tcnt == 0, f"expected 0 tag rows (no seeds), got {tcnt}"
        finally:
            await get_engine().dispose()

    asyncio.run(run())


# ===========================================================================
# D. UUID/type invariant — every varchar(36) column is in the locked UUID set,
#    the two external ids are varchar(64), the only char(64) is
#    migration_runs.file_sha256, and version_num is NOT misclassified.
# ===========================================================================

def test_uuid_and_char_type_invariants() -> None:
    """Key discovery is TYPE-INDEPENDENT (no varchar(36) filter): discover
    ALL application PK/FK columns (constraint_name = 'PRIMARY' OR
    referenced_table_name IS NOT NULL) — a UUID key that drifts off varchar(36)
    is STILL discovered, so it cannot hide. Then exclude Alembic's
    bookkeeping key EXPLICITLY, and require the discovered set to EQUAL
    EXPECTED_UUID_KEYS ∪ EXPECTED_NONUUID_KEYS.

    Types are asserted SEPARATELY from discovery (independent proofs):
      (1) every locked UUID key column is varchar(36);
      (2) the non-UUID PK key types are exactly char(64) [sessions.id_hash]
          and varchar(40) [auth_locks.name];
      (3) the two external ids are varchar(64) and NOT in the UUID key set;
      (4) the EXACT char(64) ledger is {migration_runs.file_sha256,
          sessions.id_hash} (a new char(64) column is a failure);
      (5) the EXACT char(32) ledger is {sessions.csrf_token};
      (6) alembic_version.version_num IS in the live schema (so the test did
          not silently filter alembic out) but is NOT in the locked key sets
          and NOT classified as varchar(36).

    Every invariant is an EXACT-SET (reverse-inclusion) proof: a future slice
    that adds a key of any kind, or a new char(64)/char(32)/varchar(64)
    column, fails here — the test was restructured, not weakened.
    """
    async def run() -> None:
        try:
            cols = await _all_columns()
            live = {(t, c): typ for t, c, typ in cols}
            live_v36 = {tc for tc, typ in live.items() if typ == "varchar(36)"}

            # (0) Discover ALL application PK/FK keys — independently of
            #     column type (a new key with an incorrect width must not
            #     escape detection by a varchar filter).
            async with get_engine().connect() as conn:
                keys = await conn.execute(text(
                    "SELECT DISTINCT table_name, column_name "
                    "FROM information_schema.key_column_usage "
                    "WHERE table_schema = DATABASE() "
                    "AND (constraint_name = 'PRIMARY' "
                    "OR referenced_table_name IS NOT NULL)"))
                key_columns = {tuple(row) for row in keys}

            # EXPLICIT exclusion of the Alembic bookkeeping key (it is a PK
            # but NOT an application key): discarded BEFORE the comparison.
            assert (ALEMBIC_VERSION_TABLE, "version_num") in key_columns, (
                "alembic_version.version_num must be DISCOVERED by the type-"
                "independent key query (explicit exclusion, not filtered away)")
            key_columns.discard((ALEMBIC_VERSION_TABLE, "version_num"))
            expected_keys = EXPECTED_UUID_KEYS | EXPECTED_NONUUID_KEYS
            assert key_columns == expected_keys, (
                f"discovered application PK/FK key set must EQUAL the locked "
                f"UUID + non-UUID key sets.\n"
                f" discovered-only : {sorted(key_columns - expected_keys)}\n"
                f" locked-only     : {sorted(expected_keys - key_columns)}")

            # (1) TYPE assertion for every locked UUID key column (separate
            #     proof from discovery): exactly varchar(36).
            for tc in EXPECTED_UUID_KEYS:
                assert tc in live, f"locked UUID key column {tc} is missing"
                assert live[tc] == "varchar(36)", (
                    f"locked UUID key column {tc} must be varchar(36), "
                    f"got {live[tc]!r}")

            # (2) EXACT types of the non-UUID PK key columns (char(64) and
            #     varchar(40) respectively) — explicit, not 'absent from
            #     the varchar(36) set'.
            assert live[("sessions", "id_hash")] == "char(64)", (
                f"sessions.id_hash must be char(64), got "
                f"{live[('sessions', 'id_hash')]!r}")
            assert live[("auth_locks", "name")] == "varchar(40)", (
                f"auth_locks.name must be varchar(40), got "
                f"{live[('auth_locks', 'name')]!r}")
            # sanity: the non-UUID keys are NOT classified as UUID keys
            assert not (EXPECTED_NONUUID_KEYS & EXPECTED_UUID_KEYS)

            # (3) the two external ids are varchar(64) (NOT UUIDs)
            for tc in EXTERNAL_ID_COLUMNS:
                assert tc in live, f"external id column {tc} is missing"
                assert live[tc] == "varchar(64)", (
                    f"external id {tc} must be varchar(64), got {live[tc]!r}")
                assert tc not in EXPECTED_UUID_KEYS, (
                    f"{tc} must NOT be in the locked UUID key set (external id)")

            # (4) EXACT char(64) ledger (reverse inclusion: a NEW char(64)
            #     column that is not one of these two fails)
            char64 = {tc for tc, typ in live.items() if typ == "char(64)"}
            assert char64 == CHAR64_COLUMNS, (
                f"the char(64) column set must be EXACTLY {sorted(CHAR64_COLUMNS)}; "
                f"live char(64): {sorted(char64)}")

            # (5) EXACT char(32) ledger
            char32 = {tc for tc, typ in live.items() if typ == "char(32)"}
            assert char32 == CHAR32_COLUMNS, (
                f"the char(32) column set must be EXACTLY {sorted(CHAR32_COLUMNS)}; "
                f"live char(32): {sorted(char32)}")

            # (6) alembic_version.version_num IS present in the live schema
            #     (so the invariant test did not silently filter alembic out)
            #     but is NOT in the locked key set (it is not an application
            #     key) and NOT classified as varchar(36).
            assert (ALEMBIC_VERSION_TABLE, "version_num") in live, (
                "alembic_version.version_num must be discoverable in the live schema")
            assert (ALEMBIC_VERSION_TABLE, "version_num") not in (
                EXPECTED_UUID_KEYS | EXPECTED_NONUUID_KEYS), (
                "version_num must NOT be treated as an application key column")
            assert (ALEMBIC_VERSION_TABLE, "version_num") not in live_v36, (
                f"version_num ({live.get((ALEMBIC_VERSION_TABLE, 'version_num'))!r}) "
                f"must NOT be classified as varchar(36)")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


# --------------------------------------------------------------------------
# Probe-test read helpers (do NOT dispose — the probe test owns disposal).
# --------------------------------------------------------------------------
async def _alias_row(a: str) -> tuple:
    async with get_engine().connect() as conn:
        row = (await conn.execute(text(
            "SELECT path, recipe_id, category_id FROM recipe_url_aliases "
            "WHERE id = :a"), {"a": a})).first()
        return tuple(row) if row else ()


async def _log_row(l: str) -> tuple:
    async with get_engine().connect() as conn:
        row = (await conn.execute(text(
            "SELECT run_id, source_post_id, recipe_id, outcome, detail "
            "FROM migration_recipe_log WHERE id = :l"), {"l": l})).first()
        return tuple(row) if row else ()


async def _run_row(r: str) -> tuple:
    async with get_engine().connect() as conn:
        row = (await conn.execute(text(
            "SELECT status, source_file, file_sha256, recipes_total, "
            "recipes_imported, recipes_skipped, recipes_errors, images_ok, "
            "images_failed, notes FROM migration_runs WHERE id = :r"),
            {"r": r})).first()
        return tuple(row) if row else ()


async def _cleanup(alias_id, run_ids, log_ids, recipe_ids, tag_id) -> None:
    """FK-safe probe cleanup: alias (child of recipes) and log (child of runs)
    and runs first, then the probe recipes (cascading their body/memberships),
    then the probe tag. NEVER touches the seed categories. Disposal is left to
    the owning test's outer ``finally``."""
    async with get_engine().begin() as conn:
        if alias_id:
            await conn.execute(text(
                "DELETE FROM recipe_url_aliases WHERE id = :a"), {"a": alias_id})
        for l in log_ids:
            await conn.execute(text("DELETE FROM migration_recipe_log WHERE id = :l"), {"l": l})
        for r in run_ids:
            await conn.execute(text("DELETE FROM migration_runs WHERE id = :r"), {"r": r})
        for r in recipe_ids:
            await conn.execute(text("DELETE FROM recipe_categories WHERE recipe_id = :r"), {"r": r})
            await conn.execute(text("DELETE FROM recipe_tags WHERE recipe_id = :r"), {"r": r})
        for r in recipe_ids:
            await conn.execute(text("DELETE FROM recipes WHERE id = :r"), {"r": r})
        if tag_id:
            await conn.execute(text("DELETE FROM tags WHERE id = :t"), {"t": tag_id})


# ===========================================================================
# B. Alias RESTRICT guard, then a REAL parent DELETE: full CASCADE + audit
#    SET NULL preservation (the cross-table merge-gate probe).
# ===========================================================================

def test_alias_guard_then_full_cascade_and_audit_preservation() -> None:
    """Two probe recipes. Recipe #1 has a rich body (3 sections / 6 items), two
    seed-category memberships, a probe-tag membership, a legacy alias, and a
    one-row audit run/log. Recipe #2 has its own independent body + memberships.

    A direct ``DELETE FROM recipes WHERE id=<r1>`` while the alias still points
    at r1 must be BLOCKED by RESTRICT (1451, recfk_aliases_recipe) — every
    related row stays intact. Reassigning the alias to r2 and re-issuing the
    SAME parent DELETE then succeeds, exercising the real CASCADE/SET-NULL rules:
    r1's body + both membership kinds disappear; the seed categories, the probe
    tag, and r2's body/memberships all survive; the alias keeps its id/path and
    now points only at r2; the audit run and log survive with log.recipe_id
    cleared to NULL and every other audit field unchanged. Real parent DELETEs
    are used (the cascade happens inside the statement, not via child cleanup).
    """
    r1 = str(uuid.uuid4())
    r2 = str(uuid.uuid4())
    s1 = f"t66r1-{r1[:8]}"
    s2 = f"t66r2-{r2[:8]}"
    sec_ing = str(uuid.uuid4())
    sec_steps = str(uuid.uuid4())
    sec_note = str(uuid.uuid4())
    r1_items = [str(uuid.uuid4()) for _ in range(6)]
    r2sec = str(uuid.uuid4())
    r2_items = [str(uuid.uuid4()) for _ in range(2)]
    tag = str(uuid.uuid4())
    tag_slug = f"t66-tag-{tag[:8]}"
    alias = str(uuid.uuid4())
    path = "/?p=t66-legacy"
    run_id = str(uuid.uuid4())
    log = str(uuid.uuid4())
    run_vals = ("completed", "probes/wxr.xml", "ab" * 32, 7, 5, 1, 1, 2, 0, "t66 probe run")
    log_post = "90210"
    log_outcome = "imported"
    log_detail = "t66 audit detail"

    async def run() -> None:
        try:
            seeds_before = await _seed_rows()
            assert seeds_before == SEED_ROWS
            # No ORM relationships exist between Section->Item (v1 scope), so the
            # unit of work does NOT auto-order the INSERTs FK-first. We therefore
            # flush each parent BEFORE adding its children (same convention as
            # test_organization / test_aliases), not a single giant flush at the
            # end of the block.
            async with session_scope() as s:
                # Batch 1: top-level parents (recipes + tag)
                s.add(Recipe(id=r1, slug=s1, title="Probe r1"))
                s.add(Recipe(id=r2, slug=s2, title="Probe r2"))
                s.add(Tag(id=tag, slug=tag_slug, name="Probe Tag"))
                await s.flush()

                # Batch 2: recipe_sections (depend on recipes)
                s.add(RecipeSection(id=sec_ing,   recipe_id=r1, position=1,
                                    kind="ingredients", heading="Ingredients"))
                s.add(RecipeSection(id=sec_steps, recipe_id=r1, position=2,
                                    kind="steps",       heading="Steps"))
                s.add(RecipeSection(id=sec_note,  recipe_id=r1, position=3,
                                    kind="note",        heading=None))
                s.add(RecipeSection(id=r2sec,     recipe_id=r2, position=1,
                                    kind="steps",       heading="Steps"))
                await s.flush()

                # Batch 3: recipe_items (depend on the flushed sections)
                r1_sids = (sec_ing, sec_ing, sec_steps, sec_steps, sec_note, sec_note)
                for i, sid in enumerate(r1_sids):
                    s.add(RecipeItem(id=r1_items[i], section_id=sid,
                                     position=(i % 2) + 1, raw_text=f"r1 line {i}"))
                s.add(RecipeItem(id=r2_items[0], section_id=r2sec, position=1,
                                 raw_text="r2 line 0"))
                s.add(RecipeItem(id=r2_items[1], section_id=r2sec, position=2,
                                 raw_text="r2 line 1"))
                await s.flush()

                # Batch 4: alias + audit rows (depend on the flushed recipes/run id)
                s.add(RecipeUrlAlias(id=alias, path=path, recipe_id=r1, category_id=None))
                s.add(MigrationRun(id=run_id, status=run_vals[0], source_file=run_vals[1],
                                   file_sha256=run_vals[2], recipes_total=run_vals[3],
                                   recipes_imported=run_vals[4],
                                   recipes_skipped=run_vals[5],
                                   recipes_errors=run_vals[6],
                                   images_ok=run_vals[7], images_failed=run_vals[8],
                                   notes=run_vals[9]))
                # The log's run_id FK depends on the flushed run — flush it first.
                await s.flush()
                s.add(MigrationRecipeLog(id=log, run_id=run_id, recipe_id=r1,
                                        source_post_id=log_post, outcome=log_outcome,
                                        detail=log_detail))

            # Membership rows (all parents above are committed now)
            await _exec("INSERT INTO recipe_categories (recipe_id, category_id) "
                        "VALUES (:r, :c)", {"r": r1, "c": BREAKFAST_ID})
            await _exec("INSERT INTO recipe_categories (recipe_id, category_id) "
                        "VALUES (:r, :c)", {"r": r1, "c": FERMENTATION_ID})
            await _exec("INSERT INTO recipe_tags (recipe_id, tag_id) "
                        "VALUES (:r, :t)", {"r": r1, "t": tag})
            await _exec("INSERT INTO recipe_categories (recipe_id, category_id) "
                        "VALUES (:r, :c)", {"r": r2, "c": BREAKFAST_ID})
            await _exec("INSERT INTO recipe_tags (recipe_id, tag_id) "
                        "VALUES (:r, :t)", {"r": r2, "t": tag})

            # baseline: r1's full body + memberships exist
            assert await _count("recipes", "id = :r", {"r": r1}) == 1
            assert await _count("recipe_sections", "recipe_id = :r", {"r": r1}) == 3
            r1_item_w = "section_id IN (:a, :b, :c)"
            r1_item_p = {"a": sec_ing, "b": sec_steps, "c": sec_note}
            assert await _count("recipe_items", r1_item_w, r1_item_p) == 6
            assert await _count("recipe_categories", "recipe_id = :r", {"r": r1}) == 2
            assert await _count("recipe_tags", "recipe_id = :r", {"r": r1}) == 1
            assert (await _alias_row(alias)) == (path, r1, None)
            assert await _count("migration_recipe_log", "recipe_id = :r", {"r": r1}) == 1
            run_before = await _run_row(run_id)
            assert run_before == run_vals, (
                f"probe run row should read back as the inserted values, got {run_before!r}")

            # (1) direct delete of r1 -> BLOCKED by the alias RESTRICT (1451)
            try:
                await _exec("DELETE FROM recipes WHERE id = :r", {"r": r1})
            except sqlalchemy.exc.IntegrityError as exc:
                msg = str(exc)
                assert "1451" in msg, f"expected SQL code 1451, got: {msg}"
                assert "recfk_aliases_recipe" in msg, (
                    f"expected recfk_aliases_recipe in the error, got: {msg}")
            else:
                raise AssertionError(
                    "the recipe delete should have been blocked by recfk_aliases_recipe")

            # r1 + every related row is STILL intact after the blocked delete
            assert await _count("recipes", "id = :r", {"r": r1}) == 1
            assert await _count("recipe_sections", "recipe_id = :r", {"r": r1}) == 3
            assert await _count("recipe_items", r1_item_w, r1_item_p) == 6
            assert await _count("recipe_categories", "recipe_id = :r", {"r": r1}) == 2
            assert await _count("recipe_tags", "recipe_id = :r", {"r": r1}) == 1
            assert (await _alias_row(alias)) == (path, r1, None)
            assert await _count("migration_runs", "id = :r", {"r": run_id}) == 1
            log_row = await _log_row(log)
            assert log_row[2] == r1, "audit log.recipe_id must still be r1 pre-delete"

            # (2) reassign the alias to r2, then the SAME parent DELETE now works
            await _exec("UPDATE recipe_url_aliases SET recipe_id = :r WHERE id = :a",
                        {"r": r2, "a": alias})
            await _exec("DELETE FROM recipes WHERE id = :r", {"r": r1})

            # r1 + its whole body + both membership kinds are GONE
            assert await _count("recipes", "id = :r", {"r": r1}) == 0
            assert await _count("recipe_sections", "recipe_id = :r", {"r": r1}) == 0
            for sid in (sec_ing, sec_steps, sec_note):
                assert await _count("recipe_sections", "id = :s", {"s": sid}) == 0
            assert await _count("recipe_items", r1_item_w, r1_item_p) == 0
            assert await _count("recipe_categories", "recipe_id = :r", {"r": r1}) == 0
            assert await _count("recipe_tags", "recipe_id = :r", {"r": r1}) == 0

            # seeds + probe tag SURVIVE (CASCADE is on recipe_id, never the parent)
            assert await _count("categories", "id = :c", {"c": BREAKFAST_ID}) == 1
            assert await _count("categories", "id = :c", {"c": FERMENTATION_ID}) == 1
            assert await _count("tags", "id = :t", {"t": tag}) == 1

            # r2 + its independent body + memberships SURVIVE the r1 delete
            assert await _count("recipes", "id = :r", {"r": r2}) == 1
            assert await _count("recipe_sections", "recipe_id = :r", {"r": r2}) == 1
            assert await _count("recipe_items", "section_id = :s", {"s": r2sec}) == 2
            assert await _count(
                "recipe_categories", "recipe_id = :r AND category_id = :c",
                {"r": r2, "c": BREAKFAST_ID}) == 1
            assert await _count(
                "recipe_tags", "recipe_id = :r AND tag_id = :t",
                {"r": r2, "t": tag}) == 1

            # alias keeps its id/path and now points ONLY at the valid r2
            assert (await _alias_row(alias)) == (path, r2, None)

            # audit SURVIVES: run unchanged, log.recipe_id -> NULL, rest unchanged
            assert await _count("migration_runs", "id = :r", {"r": run_id}) == 1
            assert await _count("migration_recipe_log", "id = :l", {"l": log}) == 1
            assert (await _run_row(run_id)) == run_vals, "run audit data must be unchanged"
            log_row = await _log_row(log)
            assert log_row[0] == run_id, "log.run_id must be unchanged"
            assert log_row[1] == log_post, "log.source_post_id must be unchanged"
            assert log_row[2] is None, (
                f"log.recipe_id must be SET NULL by the recipe delete, got {log_row[2]!r}")
            assert log_row[3] == log_outcome, "log.outcome must be unchanged"
            assert log_row[4] == log_detail, "log.detail must be unchanged"
            assert await _seed_rows() == seeds_before, (
                "the cross-table probe must preserve all exact seed rows")
        finally:
            try:
                await _cleanup(alias, [run_id], [log], [r1, r2], tag)
            finally:
                await get_engine().dispose()

    asyncio.run(run())
