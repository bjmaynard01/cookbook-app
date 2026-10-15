"""v5 `migration_runs` + `migration_recipe_log` (spec §3.6, D8) — schema,
server defaults, enum locks, FK delete rules, and cascade/SET-NULL behavior.
Run against the ISOLATED test MariaDB (Task 6.5); the dev stack and dev
`cookbook` schema must remain untouched (plan shared contract).

Async disposal convention (same as `test_aliases.py`): every test does ALL of
its DB work inside one `async def run()` — one `asyncio.run` loop. The engine
pool is process-global (`lru_cache`) and an asyncmy connection is bound to the
loop that opened it, so the pool MUST be drained from WITHIN that same live
loop: each `run()` ends with `await get_engine().dispose()` in the outermost
`finally`, which still drains the pool even if a cleanup DELETE or an
assertion raises (cleanup runs in an inner `try/finally`).
"""
from __future__ import annotations

import asyncio
import uuid

import sqlalchemy
from sqlalchemy import text

from cookbook.db import get_engine, session_scope
from cookbook.models import MigrationRecipeLog, MigrationRun, Recipe, User

RUNS = "migration_runs"
LOG = "migration_recipe_log"

RUNS_COLS = {
    "id", "started_at", "finished_at", "source_file", "file_sha256", "status",
    "recipes_total", "recipes_imported", "recipes_skipped", "recipes_errors",
    "images_ok", "images_failed", "operator_id", "notes",
}
LOG_COLS = {"id", "run_id", "source_post_id", "recipe_id", "outcome", "detail"}
COUNTERS = (
    "recipes_total", "recipes_imported", "recipes_skipped", "recipes_errors",
    "images_ok", "images_failed",
)

# MariaDB rejects an invalid ENUM literal with SQL error 1265
# (ER_TRUNCATED_WRONG_VALUE, "Data truncated for column '<col>' at row 1").
# The EXACT exception class is OBSERVED on live MariaDB 11 + asyncmy, not
# assumed — it is sqlalchemy.exc.DataError (orig: asyncmy.errors.DataError),
# NOT IntegrityError. The message names the column, so the column itself is
# the "marker" asserted (no separate constraint name is available for codes).
ENUM_ERR = sqlalchemy.exc.DataError
ENUM_CODE = "1265"
FK_ERR = sqlalchemy.exc.IntegrityError  # 1452 (missing referent)


def _is_uuid(s: object) -> bool:
    return bool(str(s) and len(str(s)) == 36 and str(s).count("-") == 4)


def _norm_default(v: object) -> object:
    # information_schema reports "no explicit default" on a nullable column as
    # the literal string 'NULL' — normalize to Python None for clean equality
    # comparisons (the value is still a genuine SQL NULL / no default either way).
    return None if isinstance(v, str) and v.upper() == "NULL" else v


# ---------------------------------------------------------------------------
# Read / DML helpers — these do NOT dispose the pool. Each test drains the pool
# exactly once, in its own outer `async def run()` `finally`, on the live loop.
# ---------------------------------------------------------------------------

async def _all_columns(table: str) -> dict[str, tuple]:
    async with get_engine().connect() as conn:
        res = await conn.execute(
            text("SELECT column_name, column_type, is_nullable, column_default "
                 "FROM information_schema.columns "
                 "WHERE table_schema = DATABASE() AND table_name = :t "
                 "ORDER BY ordinal_position"),
            {"t": table})
        return {r[0]: (r[1], r[2], _norm_default(r[3])) for r in res}


async def _indexes(table: str, non_unique: bool) -> dict[str, list[str]]:
    async with get_engine().connect() as conn:
        flag = 1 if non_unique else 0
        res = await conn.execute(
            text("SELECT index_name, column_name, seq_in_index "
                 "FROM information_schema.statistics "
                 "WHERE table_schema = DATABASE() AND table_name = :t "
                 "AND non_unique = :nu ORDER BY index_name, seq_in_index"),
            {"t": table, "nu": flag})
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
            {"t": table})
        return {r[1]: (r[0], r[2], r[3], r[4]) for r in res}


async def _engine_charset(table: str) -> tuple:
    async with get_engine().connect() as conn:
        row = (await conn.execute(
            text("SELECT ENGINE, TABLE_COLLATION FROM information_schema.TABLES "
                 "WHERE table_schema = DATABASE() AND table_name = :t"),
            {"t": table})).first()
    return tuple(row) if row else (None, None)


async def _table_exists(name: str) -> int:
    async with get_engine().connect() as conn:
        return (await conn.execute(
            text("SELECT COUNT(*) FROM information_schema.tables "
                 "WHERE table_schema = DATABASE() AND table_name = :t"),
            {"t": name})).scalar_one()


async def _count(table: str, where_sql: str = "",
                 params: dict | None = None) -> int:
    sql = f"SELECT COUNT(*) FROM {table}" + (f" WHERE {where_sql}" if where_sql else "")
    async with get_engine().connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar_one()


async def _exec(sql: str, params: dict | None) -> int:
    """One DML statement in begin/commit. Disposal stays with the caller's
    outer `finally` (one outer drain on the live loop)."""
    async with get_engine().begin() as conn:
        return (await conn.execute(text(sql), params or {})).rowcount


async def _expect_rejected(
        sql: str, params: dict | None, *, exc_class: type[Exception],
        code: str, marker: str) -> None:
    """Run `sql`; REQUIRE rejection with `exc_class` carrying SQL error `code`
    and `marker` in the message. No broad-Exception swallowing: unrelated
    success OR a different error class both raise AssertionError."""
    try:
        await _exec(sql, params)
    except exc_class as ex:
        msg = str(ex)
        assert code in msg, f"expected SQL code {code} in the error, got: {msg}"
        assert marker in msg, f"expected marker {marker!r} in the error, got: {msg}"
        return
    raise AssertionError(
        f"expected {exc_class.__name__} (code {code}, marker {marker!r}) for:\n{sql}")


async def _clean(run_ids: list[str], log_ids: list[str],
                 recipe_ids: list[str], user_ids: list[str]) -> None:
    """FK-safe cleanup: logs (children) first, then their runs, then the
    probe recipes/users they pointed at. No disposal here — the caller's
    outer `finally` owns it."""
    async with get_engine().begin() as conn:
        for lid in log_ids:
            await conn.execute(text(f"DELETE FROM {LOG} WHERE id = :l"), {"l": lid})
        for rid in run_ids:
            await conn.execute(text(f"DELETE FROM {RUNS} WHERE id = :r"), {"r": rid})
        for r in recipe_ids:
            await conn.execute(text("DELETE FROM recipes WHERE id = :r"), {"r": r})
        for u in user_ids:
            await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": u})


# ===========================================================================
# SCHEMA — pure information_schema assertions against the v5 DDL
# ===========================================================================

def test_runs_columns_types_nullability_defaults() -> None:
    """Exactly the 14 locked §3.6 columns, exact MariaDB 11 types, correct
    nullability, the six INT counters `int(11) NOT NULL DEFAULT 0`, `status` a
    native ENUM with NO server default (Python default only — spec §3.6), and
    `file_sha256` as **char(64)** (DL-5's fixed-length-hash exception, not
    varchar)."""

    async def run() -> None:
        try:
            cols = await _all_columns(RUNS)
            assert set(cols) == RUNS_COLS, (
                f"column set (must be exactly the 14 locked, no extras): {sorted(cols)}")

            def col(c: str) -> tuple:
                return cols[c]

            assert col("id") == ("varchar(36)", "NO", None), f"id: {col('id')!r}"
            # started_at: server CURRENT_TIMESTAMP (default text varies by
            # MariaDB version: current_timestamp()/now() — accept both).
            t, n, d = col("started_at")
            assert (t, n) == ("datetime", "NO") and d, f"started_at: {col('started_at')!r}"
            assert "current_timestamp" in d.lower() or d.lower().startswith("now"), (
                f"started_at server default should be CURRENT_TIMESTAMP, got {d!r}")
            assert col("finished_at") == ("datetime", "YES", None)
            assert col("source_file") == ("varchar(500)", "YES", None)
            # CHAR — the DL-5 exception, asserted literally (NOT varchar).
            assert col("file_sha256") == ("char(64)", "YES", None), (
                f"file_sha256 must be char(64), got {col('file_sha256')!r}")
            assert col("status") == (
                "enum('running','completed','failed')", "NO", None), (
                f"status must be the locked ENUM with NO server default: {col('status')!r}")
            for c in COUNTERS:
                assert cols[c] == ("int(11)", "NO", "0"), (
                    f"counter {c} must be int(11) NOT NULL DEFAULT 0, got {cols[c]!r}")
            assert col("operator_id") == ("varchar(36)", "YES", None)
            assert col("notes") == ("text", "YES", None)
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_log_columns_types_nullability_defaults() -> None:
    """Exactly the six locked §3.6 log columns: `run_id` varchar(36) NOT NULL,
    `source_post_id` varchar(64) NULL, `recipe_id` varchar(36) NULL, `outcome`
    a native ENUM with NO server default (the importer always supplies it),
    `detail` text NULL. No `created_at` — spec §3.6 lists none."""

    async def run() -> None:
        try:
            cols = await _all_columns(LOG)
            assert set(cols) == LOG_COLS, (
                f"column set (must be exactly these 6, no created_at): {sorted(cols)}")
            assert cols["id"] == ("varchar(36)", "NO", None)
            assert cols["run_id"] == ("varchar(36)", "NO", None)
            assert cols["source_post_id"] == ("varchar(64)", "YES", None)
            assert cols["recipe_id"] == ("varchar(36)", "YES", None)
            assert cols["outcome"] == (
                "enum('imported','skipped','error')", "NO", None), (
                f"outcome must be the locked ENUM with NO server default: {cols['outcome']!r}")
            assert cols["detail"] == ("text", "YES", None)
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_runs_primary_and_fk_index_exact() -> None:
    """`migration_runs` has EXACTLY the PRIMARY key on [id] and the single
    explicit non-unique index `ix_migration_runs_operator_id` on [operator_id]
    (no extras, no missing, exact column order)."""

    async def run() -> None:
        try:
            uniq = await _indexes(RUNS, non_unique=False)
            nu = await _indexes(RUNS, non_unique=True)
            assert uniq == {"PRIMARY": ["id"]}, (
                f"unique indexes must be exactly PRIMARY [id]: {uniq!r}")
            assert nu == {"ix_migration_runs_operator_id": ["operator_id"]}, (
                f"non-unique indexes must be exactly the one FK index: {nu!r}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_log_primary_and_fk_indexes_exact() -> None:
    """`migration_recipe_log` has EXACTLY the PRIMARY key on [id] and the two
    explicit non-unique FK indexes in column order:
    `ix_migration_recipe_log_run_id` [run_id],
    `ix_migration_recipe_log_recipe_id` [recipe_id]."""

    async def run() -> None:
        try:
            uniq = await _indexes(LOG, non_unique=False)
            nu = await _indexes(LOG, non_unique=True)
            assert uniq == {"PRIMARY": ["id"]}, (
                f"unique indexes must be exactly PRIMARY [id]: {uniq!r}")
            assert nu == {
                "ix_migration_recipe_log_recipe_id": ["recipe_id"],
                "ix_migration_recipe_log_run_id": ["run_id"],
            }, f"non-unique indexes must be exactly the two FK indexes: {nu!r}"
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_runs_fk_name_source_target_rule() -> None:
    """The single `migration_runs` FK, exact name/columns/rule:
    recfk_run_operator : operator_id -> users.id ON DELETE SET NULL (D10
    attribution-only — a user delete clears the reference, never the run)."""

    async def run() -> None:
        try:
            fks = await _fks(RUNS)
            assert fks == {
                "operator_id": ("recfk_run_operator", "users", "id", "SET NULL"),
            }, f"runs FKs must be exactly the one operator FK: {fks!r}"
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_log_fks_names_sources_targets_rules() -> None:
    """Both log FKs, exact names, source/target columns, delete rules
    (run_id is the column that references migration_runs.id):
    recfk_log_run    : run_id    -> migration_runs.id ON DELETE CASCADE
    recfk_log_recipe : recipe_id -> recipes.id        ON DELETE SET NULL"""

    async def run() -> None:
        try:
            fks = await _fks(LOG)
            assert fks == {
                "recipe_id": ("recfk_log_recipe", "recipes", "id", "SET NULL"),
                "run_id": ("recfk_log_run", "migration_runs", "id", "CASCADE"),
            }, f"log FKs must be exactly these two: {fks!r}"
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_engine_innodb_utf8mb4_both_tables() -> None:
    async def run() -> None:
        try:
            for table in (RUNS, LOG):
                engine, collation = await _engine_charset(table)
                assert engine == "InnoDB", (
                    f"{table} engine should be InnoDB, got {engine!r}")
                assert isinstance(collation, str) and collation.startswith("utf8mb4"), (
                    f"{table} collation {collation!r} must be a utf8mb4-charset collation")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_no_migration_tag_log_table() -> None:
    """spec §3.6: `migration_tag_log` is NOT needed in v1 — this test is the
    guard against a later "convenient" addition without a decision."""

    async def run() -> None:
        try:
            n = await _table_exists("migration_tag_log")
            assert n == 0, (
                f"migration_tag_log must not exist, found {n} row(s) in information_schema")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


# ===========================================================================
# BEHAVIOR — server/Python defaults, explicit round-trips, enum locks, FK rules
# ===========================================================================

def test_orm_bare_run_defaults_uuid_status_running_counters_zero() -> None:
    """ORM defaults: a bare `MigrationRun` (no explicit fields) picks up the
    Python `_uuid` id, the PYTHON-side `status='running'` default (no server
    default — the importer always supplies the status it wants), a server
    `started_at` from `CURRENT_TIMESTAMP`, and all six INT counters = 0. The
    two sources (Python vs server) are both independently visible in one row:
    id and status come from the model; `started_at` and the counters come from
    the DB. `finished_at`/`source_file`/`file_sha256`/`operator_id`/`notes`
    all remain NULL (no server defaults on any of them; `file_sha256` is a
    nullable CHAR(64) so NULL is the default)."""
    run_id: str | None = None

    async def run() -> None:
        try:
            async with session_scope() as s:
                r = MigrationRun()
                s.add(r)
                await s.flush()
                run_id = r.id

            assert _is_uuid(run_id), (
                f"the `_uuid` Python default must be a valid UUID, got {run_id!r}")

            async with get_engine().connect() as conn:
                row = (await conn.execute(
                    text(f"SELECT started_at, status, recipes_total, recipes_imported, "
                         f"recipes_skipped, recipes_errors, images_ok, images_failed, "
                         f"finished_at, source_file, file_sha256, operator_id, notes "
                         f"FROM {RUNS} WHERE id = :r"),
                    {"r": run_id})).first()
            assert row is not None, "the ORM run row must exist"

            assert row[0] is not None, (
                "started_at must be populated by the server CURRENT_TIMESTAMP")
            assert row[1] == 'running', (
                f"status must be the PYTHON default 'running', got {row[1]!r}")
            assert row[2:8] == (0, 0, 0, 0, 0, 0), (
                f"all six counters must be 0 (python default=server default), got {row[2:8]!r}")
            assert row[8:13] == (None, None, None, None, None), (
                "finished_at/source_file/file_sha256/operator_id/notes must be NULL, "
                f"got {row[8:13]!r}")
        finally:
            try:
                await _clean(run_ids=[run_id] if run_id else [],
                             log_ids=[], recipe_ids=[], user_ids=[])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_raw_sql_defaults_independent_of_python() -> None:
    """The two DB-backed server defaults are OBSERVABLE from raw SQL alone —
    independent of and provable without the ORM layer's Python defaults:

    - ``started_at``: server ``CURRENT_TIMESTAMP`` — a raw insert that omits
      it must still land a timestamp.
    - the six INT counters: ``DEFAULT 0`` (spec §3.6 'NOT NULL DEFAULT 0') — a
      raw insert that omits them must still land 0.

    Both are proven by a single raw ``INSERT INTO`` (explicit id + explicit
    status) reading the row back: the columns we did NOT name are filled by
    the DB, not by any Python-side default (there is no session/model here at
    all — this is pure SQL through the same asyncmy connection).

    Note on ``status`` (an honest limitation, not asserted as a rejection):
    ``status`` is a NOT NULL ENUM with NO explicit column default
    (information_schema.column_default is NULL — see
    ``test_runs_columns_types_nullability_defaults``). MariaDB nevertheless
    auto-fills a raw ENUM insert with the FIRST enumerated value ('running')
    as a type-level implicit default. That runtime fallback is NOT an explicit
    server default and differs from the counters' explicit ``DEFAULT 0``;
    asserting the distinction is the column-default check above, so this test
    supplies ``status`` explicitly and does not rely on the implicit fallback.
    """
    run_id = str(uuid.uuid4())

    async def run() -> None:
        try:
            # raw SQL: name only id + status; omit started_at + all six
            # counters so their SERVER defaults do the work.
            await _exec(
                f"INSERT INTO {RUNS} (id, status) VALUES (:r, 'running')",
                {"r": run_id})

            async with get_engine().connect() as conn:
                row = (await conn.execute(
                    text(f"SELECT recipes_total, recipes_imported, recipes_skipped, "
                         f"recipes_errors, images_ok, images_failed, started_at "
                         f"FROM {RUNS} WHERE id = :r"),
                    {"r": run_id})).first()
            assert row is not None, "the raw-SQL run row must exist"
            assert row[0:6] == (0, 0, 0, 0, 0, 0), (
                f"the six counters must all be 0 (server DEFAULT 0, independent "
                f"of any Python default), got {row[0:6]!r}")
            assert row[6] is not None, (
                "started_at must be populated by the server CURRENT_TIMESTAMP")
        finally:
            try:
                await _clean(run_ids=[run_id], log_ids=[],
                             recipe_ids=[], user_ids=[])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_explicit_counters_and_nullable_fields_roundtrip() -> None:
    """Round-trip with EXPLICIT values for every non-default field:
    total=5, imported=3, skipped=1, errors=1, images_ok=2, images_failed=1
    (the exact counter set the §5.3 gate asserts), plus all five nullable
    fields (finished_at, source_file, file_sha256, operator_id, notes) and a
    log row with explicit source_post_id/recipe_id/outcome/detail — each
    read back field-by-field to prove the live column set and types."""
    run_id = str(uuid.uuid4())
    log_id = str(uuid.uuid4())
    recipe_id = str(uuid.uuid4())
    recipe_slug = f"t65-rt-{run_id[:8]}"

    async def run() -> None:
        try:
            finished_at = "2026-10-14 12:00:00"
            source_file = "fixtures/wxr.xml"
            file_sha = "a" * 64

            async with session_scope() as s:
                s.add(Recipe(id=recipe_id, slug=recipe_slug, title="RT recipe"))
                await s.flush()
                # `started_at` is intentionally omitted (not passed as None) so
                # the SERVER `CURRENT_TIMESTAMP` default populates it — passing
                # an explicit None would emit NULL directly and bypass the
                # server default.
                r = MigrationRun(
                    id=run_id,
                    source_file=source_file,
                    file_sha256=file_sha,
                    status="completed",
                    recipes_total=5,
                    recipes_imported=3,
                    recipes_skipped=1,
                    recipes_errors=1,
                    images_ok=2,
                    images_failed=1,
                    operator_id=None,         # leave NULL (nullable)
                    notes="explicit roundtrip",
                )
                s.add(r)
                await s.flush()
                lg = MigrationRecipeLog(
                    id=log_id,
                    run_id=run_id,
                    source_post_id="1234",
                    recipe_id=recipe_id,
                    outcome="imported",
                    detail="explicit detail text",
                )
                s.add(lg)
                await s.flush()

            async with get_engine().connect() as conn:
                src = (await conn.execute(
                    text(f"SELECT started_at, finished_at, source_file, file_sha256, status "
                         f"FROM {RUNS} WHERE id = :r"),
                    {"r": run_id})).first()
                counters = (await conn.execute(
                    text(f"SELECT recipes_total, recipes_imported, recipes_skipped, "
                         f"recipes_errors, images_ok, images_failed "
                         f"FROM {RUNS} WHERE id = :r"),
                    {"r": run_id})).first()
                lg_row = (await conn.execute(
                    text(f"SELECT run_id, source_post_id, recipe_id, outcome, detail "
                         f"FROM {LOG} WHERE id = :l"),
                    {"l": log_id})).first()

            assert src is not None and counters is not None and lg_row is not None
            # finished_at and operator_id stay NULL (not explicitly set);
            # started_at is populated by the server default (we passed None,
            # the server CURRENT_TIMESTAMP default fills it in).
            assert src[0] is not None, "started_at must be server-populated"
            assert src[1] is None, f"finished_at must remain NULL, got {src[1]!r}"
            assert src[2] == source_file, f"source_file: got {src[2]!r}"
            assert src[3] == file_sha, f"file_sha256: got {src[3]!r}"
            assert src[4] == "completed", f"status: got {src[4]!r}"

            assert tuple(counters) == (5, 3, 1, 1, 2, 1), (
                f"counters must round-trip exactly (5,3,1,1,2,1), got {tuple(counters)!r}")

            assert lg_row[0] == run_id
            assert lg_row[1] == "1234", f"source_post_id: got {lg_row[1]!r}"
            assert lg_row[2] == recipe_id
            assert lg_row[3] == "imported", f"outcome: got {lg_row[3]!r}"
            assert lg_row[4] == "explicit detail text", f"detail: got {lg_row[4]!r}"
        finally:
            try:
                await _clean(run_ids=[run_id], log_ids=[log_id],
                             recipe_ids=[recipe_id], user_ids=[])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_status_valid_values_all_accepted() -> None:
    """All three locked ``migration_run_status`` values are accepted by the DB
    on INSERT (the enum is the importer's state machine, checked at the DB
    layer so a typo cannot be silent). Each is proven by an accepted raw
    insert that lands that exact value, then cleaned up."""

    async def run() -> None:
        try:
            valid = ("running", "completed", "failed")
            ids = [str(uuid.uuid4()) for _ in valid]
            for rid, status in zip(ids, valid):
                await _exec(f"INSERT INTO {RUNS} (id, status) VALUES (:r, :s)",
                            {"r": rid, "s": status})
            landed = []
            for rid in ids:
                async with get_engine().connect() as conn:
                    landed.append((await conn.execute(
                        text(f"SELECT status FROM {RUNS} WHERE id = :r"),
                        {"r": rid})).scalar_one())
            assert sorted(landed) == sorted(valid), (
                f"all three valid status values must be accepted, got {landed!r}")
        finally:
            try:
                await _clean(run_ids=ids, log_ids=[], recipe_ids=[], user_ids=[])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_status_invalid_rejected_insert_and_update() -> None:
    """An invalid ``status`` literal is rejected at the DB layer (code 1265)
    on BOTH insert and update. A rejected UPDATE leaves the run's original
    status intact (the enum is the lock that stops a silent typo). The exact
    exception class is ``sqlalchemy.exc.DataError`` (observed on MariaDB 11
    + asyncmy, not IntegrityError); the message names the ``status`` column,
    which is the marker asserted."""

    run_id = str(uuid.uuid4())
    upd_id = str(uuid.uuid4())

    async def run() -> None:
        try:
            # INSERT reject: bogus status -> 1265, no row left behind.
            await _expect_rejected(
                f"INSERT INTO {RUNS} (id, status) VALUES (:r, 'bogus')",
                {"r": run_id},
                exc_class=ENUM_ERR, code=ENUM_CODE, marker="status")
            assert await _count(RUNS, "id = :r", {"r": run_id}) == 0, (
                "no run may be created with an invalid status")

            # UPDATE reject on a VALID row: status must not change.
            await _exec(f"INSERT INTO {RUNS} (id, status) VALUES (:r, 'running')",
                        {"r": upd_id})
            await _expect_rejected(
                f"UPDATE {RUNS} SET status = 'bogus' WHERE id = :r",
                {"r": upd_id},
                exc_class=ENUM_ERR, code=ENUM_CODE, marker="status")
            async with get_engine().connect() as conn:
                kept = (await conn.execute(
                    text(f"SELECT status FROM {RUNS} WHERE id = :r"),
                    {"r": upd_id})).scalar_one()
            assert kept == "running", (
                f"the run must keep its original status after a rejected "
                f"UPDATE, got {kept!r}")
        finally:
            try:
                await _clean(run_ids=[run_id, upd_id], log_ids=[],
                             recipe_ids=[], user_ids=[])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_outcome_valid_values_all_accepted() -> None:
    """All three locked ``migration_log_outcome`` values are accepted on
    INSERT (per-source-post audit rows), each landing its exact value."""

    async def run() -> None:
        run_id = str(uuid.uuid4())
        try:
            valid = ("imported", "skipped", "error")
            ids = [str(uuid.uuid4()) for _ in valid]
            await _exec(f"INSERT INTO {RUNS} (id, status) VALUES (:r, 'running')",
                        {"r": run_id})
            for lid, outcome in zip(ids, valid):
                await _exec(
                    f"INSERT INTO {LOG} (id, run_id, outcome) VALUES (:l, :r, :o)",
                    {"l": lid, "r": run_id, "o": outcome})
            landed = []
            for lid in ids:
                async with get_engine().connect() as conn:
                    landed.append((await conn.execute(
                        text(f"SELECT outcome FROM {LOG} WHERE id = :l"),
                        {"l": lid})).scalar_one())
            assert sorted(landed) == sorted(valid), (
                f"all three valid outcome values must be accepted, got {landed!r}")
        finally:
            try:
                await _clean(run_ids=[run_id], log_ids=ids,
                             recipe_ids=[], user_ids=[])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_outcome_invalid_rejected_insert_and_update() -> None:
    """An invalid ``outcome`` literal is rejected at the DB layer (code 1265)
    on BOTH insert and update; a rejected UPDATE leaves the log row's original
    outcome intact. Same observed exception contract as the status test."""

    run_id = str(uuid.uuid4())
    lid = str(uuid.uuid4())
    upd_lid = str(uuid.uuid4())

    async def run() -> None:
        try:
            await _exec(f"INSERT INTO {RUNS} (id, status) VALUES (:r, 'running')",
                        {"r": run_id})
            # INSERT reject: bogus outcome -> 1265, no row left behind.
            await _expect_rejected(
                f"INSERT INTO {LOG} (id, run_id, outcome) "
                "VALUES (:l, :r, 'unknown')",
                {"l": lid, "r": run_id},
                exc_class=ENUM_ERR, code=ENUM_CODE, marker="outcome")
            assert await _count(LOG, "id = :l", {"l": lid}) == 0, (
                "no log row may be created with an invalid outcome")

            # UPDATE reject on a VALID row: outcome must not change.
            await _exec(
                f"INSERT INTO {LOG} (id, run_id, outcome) "
                "VALUES (:l, :r, 'imported')",
                {"l": upd_lid, "r": run_id})
            await _expect_rejected(
                f"UPDATE {LOG} SET outcome = 'unknown' WHERE id = :l",
                {"l": upd_lid},
                exc_class=ENUM_ERR, code=ENUM_CODE, marker="outcome")
            async with get_engine().connect() as conn:
                kept = (await conn.execute(
                    text(f"SELECT outcome FROM {LOG} WHERE id = :l"),
                    {"l": upd_lid})).scalar_one()
            assert kept == "imported", (
                f"the log row must keep its original outcome after a rejected "
                f"UPDATE, got {kept!r}")
        finally:
            try:
                await _clean(run_ids=[run_id], log_ids=[upd_lid],
                             recipe_ids=[], user_ids=[])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


# ===========================================================================
# FK DELETE RULES — CASCADE (run->log), SET NULL (user/run, recipe/log), and
# missing-referent rejection on the three named FKs.
# ===========================================================================

def test_run_delete_cascades_only_its_logs() -> None:
    """ON DELETE CASCADE (recfk_log_run): deleting a run removes EXACTLY its
    log rows and nothing else. A SECOND run (with its own log rows) survives,
    as does a referenced user and a referenced recipe — the cascade is scoped
    to the one deleted run's children, not the whole audit trail."""
    run_a = str(uuid.uuid4())
    run_b = str(uuid.uuid4())
    log_a1 = str(uuid.uuid4()); log_a2 = str(uuid.uuid4()); log_a3 = str(uuid.uuid4())
    log_b1 = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    user_slug = f"t65-u-{run_a[:8]}"
    recipe_id = str(uuid.uuid4())
    recipe_slug = f"t65-r-{run_a[:8]}"

    async def run() -> None:
        try:
            async with session_scope() as s:
                # probe user + recipe (survivors), plus two runs, parent->child.
                s.add(User(id=user_id, username=user_slug, password_hash="x"))
                s.add(Recipe(id=recipe_id, slug=recipe_slug, title="CASC"))
                await s.flush()
                s.add(MigrationRun(id=run_a, status="completed",
                                   recipes_total=3,
                                   operator_id=user_id))
                s.add(MigrationRun(id=run_b, status="running"))
                await s.flush()
                for lid in (log_a1, log_a2, log_a3):
                    s.add(MigrationRecipeLog(id=lid, run_id=run_a,
                                             outcome="imported",
                                             recipe_id=recipe_id))
                s.add(MigrationRecipeLog(id=log_b1, run_id=run_b,
                                        outcome="skipped"))
                await s.flush()

            # baseline counts
            assert await _count(LOG, "run_id = :r", {"r": run_a}) == 3
            assert await _count(LOG, "run_id = :r", {"r": run_b}) == 1
            assert await _count(RUNS, "id = :r", {"r": run_b}) == 1

            # delete run A (children + parent in one statement)
            await _exec(f"DELETE FROM {RUNS} WHERE id = :r", {"r": run_a})

            assert await _count(RUNS, "id = :r", {"r": run_a}) == 0, (
                "run A must be gone")
            assert await _count(LOG, "run_id = :r", {"r": run_a}) == 0, (
                f"run A's cascade must have removed ALL its logs")
            for x in (log_a1, log_a2, log_a3):
                assert await _count(LOG, "id = :l", {"l": x}) == 0, (
                    f"cascade log {x} must be gone")

            # survivors untouched
            assert await _count(RUNS, "id = :r", {"r": run_b}) == 1, (
                "run B must survive run A's cascade")
            assert await _count(LOG, "id = :l", {"l": log_b1}) == 1, (
                "run B's log must survive run A's cascade")
            assert await _count("users", "id = :u", {"u": user_id}) == 1, (
                "the referenced user must survive run A's cascade")
            assert await _count("recipes", "id = :r", {"r": recipe_id}) == 1, (
                "the referenced recipe (only SET-NULL by run A's recipe logs) "
                "must SURVIVE the run delete (recipe FK is SET NULL, not CASCADE)")
        finally:
            try:
                await _clean(run_ids=[run_a, run_b],
                             log_ids=[log_a1, log_a2, log_a3, log_b1],
                             recipe_ids=[recipe_id], user_ids=[user_id])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_user_delete_clears_operator_id_preserving_run_and_log() -> None:
    """ON DELETE SET NULL (recfk_run_operator): deleting the operator user
    NULLs ``migration_runs.operator_id`` but PRESERVES both the run and its
    log rows (attribution-only, D10 — an audit row is never destroyed by the
    disappearance of the user who ran it)."""
    run_id = str(uuid.uuid4())
    log_id = str(uuid.uuid4())
    user_id = str(uuid.uuid4())
    user_slug = f"t65-us-{run_id[:8]}"

    async def run() -> None:
        try:
            async with session_scope() as s:
                s.add(User(id=user_id, username=user_slug, password_hash="x"))
                await s.flush()
                s.add(MigrationRun(id=run_id, status="completed",
                                   operator_id=user_id))
                await s.flush()
                s.add(MigrationRecipeLog(id=log_id, run_id=run_id,
                                        outcome="imported"))
                await s.flush()

            # sanity: operator is set before the delete
            async with get_engine().connect() as conn:
                before = (await conn.execute(
                    text(f"SELECT operator_id FROM {RUNS} WHERE id = :r"),
                    {"r": run_id})).scalar_one()
            assert before == user_id, "operator_id must be the user id pre-delete"

            # delete the user -> SET NULL on the run
            await _exec("DELETE FROM users WHERE id = :u", {"u": user_id})

            async with get_engine().connect() as conn:
                after = (await conn.execute(
                    text(f"SELECT operator_id FROM {RUNS} WHERE id = :r"),
                    {"r": run_id})).scalar_one()

            assert await _count(RUNS, "id = :r", {"r": run_id}) == 1, (
                "the run must SURVIVE the user delete")
            assert after is None, (
                f"operator_id must be SET NULL by the user delete, got {after!r}")
            assert await _count(LOG, "id = :l", {"l": log_id}) == 1, (
                "the run's log row must SURVIVE the user delete")
        finally:
            try:
                await _clean(run_ids=[run_id], log_ids=[log_id],
                             recipe_ids=[], user_ids=[user_id])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_recipe_delete_clears_log_recipe_id_preserving_audit() -> None:
    """ON DELETE SET NULL (recfk_log_recipe): deleting an UNALIASED recipe
    (one referenced only by ``migration_recipe_log.recipe_id``, not by a
    ``recipe_url_aliases`` row — so no RESTRICT block from v4) NULLs the log
    row's ``recipe_id`` but PRESERVES the log row and its run — the audit
    trail is never destroyed, only its recipe reference is cleared."""
    run_id = str(uuid.uuid4())
    log_id = str(uuid.uuid4())
    recipe_id = str(uuid.uuid4())
    recipe_slug = f"t65-rn-{run_id[:8]}"

    async def run() -> None:
        try:
            async with session_scope() as s:
                s.add(Recipe(id=recipe_id, slug=recipe_slug, title="SETNULL"))
                await s.flush()
                s.add(MigrationRun(id=run_id, status="completed"))
                await s.flush()
                s.add(MigrationRecipeLog(id=log_id, run_id=run_id,
                                        outcome="imported",
                                        recipe_id=recipe_id))
                await s.flush()

            # sanity: the log points at the recipe pre-delete
            async with get_engine().connect() as conn:
                before = (await conn.execute(
                    text(f"SELECT recipe_id FROM {LOG} WHERE id = :l"),
                    {"l": log_id})).scalar_one()
            assert before == recipe_id, "log.recipe_id must be the recipe pre-delete"

            # delete the unaliased recipe -> SET NULL on the log row
            await _exec("DELETE FROM recipes WHERE id = :r", {"r": recipe_id})

            assert await _count("recipes", "id = :r", {"r": recipe_id}) == 0, (
                "the recipe must be gone")
            assert await _count(LOG, "id = :l", {"l": log_id}) == 1, (
                "the log row must SURVIVE the recipe delete (SET NULL, not CASCADE)")
            async with get_engine().connect() as conn:
                after = (await conn.execute(
                    text(f"SELECT recipe_id, outcome FROM {LOG} WHERE id = :l"),
                    {"l": log_id})).first()
            assert after is not None
            assert after[0] is None, (
                f"log.recipe_id must be SET NULL by the recipe delete, got {after[0]!r}")
            assert after[1] == "imported", (
                "log.outcome must be preserved by the recipe delete")
            assert await _count(RUNS, "id = :r", {"r": run_id}) == 1, (
                "the run must SURVIVE the recipe delete")
        finally:
            try:
                await _clean(run_ids=[run_id], log_ids=[log_id],
                             recipe_ids=[recipe_id], user_ids=[])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_missing_run_user_recipe_rejected_by_named_fks() -> None:
    """A row whose FK points at a NON-EXISTENT parent is rejected by the
    correctly NAMED constraint (code 1452), and no row is left behind:
    log.run_id    -> recfk_log_run    (missing migration_runs.id)
    run.operator_id -> recfk_run_operator (missing users.id)
    log.recipe_id  -> recfk_log_recipe (missing recipes.id)
    The constraint name is the MariaDB 1452 message marker itself. Each probe
    uses a DISTINCT fake id so the rejection is attributable to exactly one FK
    and the correct constraint name surfaces (no second FK in the same
    statement can shadow the first)."""
    FAKE_USER = "00000000-0000-4100-8200-000000000001"
    FAKE_RUN = "00000000-0000-4200-8200-000000000002"
    FAKE_RECIPE = "00000000-0000-4300-8200-000000000003"
    # A real run is required so recfk_log_recipe (missing recipe) is NOT
    # shadowed by a simultaneous recfk_log_run rejection (which would have
    # the same code 1452 but the wrong constraint-name marker).
    RUN_OK = str(uuid.uuid4())

    async def run() -> None:
        try:
            # (1) missing operator_id -> recfk_run_operator
            await _expect_rejected(
                f"INSERT INTO {RUNS} (id, status, operator_id) "
                "VALUES (:r, 'running', :u)",
                {"r": "00000000-0000-4000-8000-000000000000", "u": FAKE_USER},
                exc_class=FK_ERR, code="1452", marker="recfk_run_operator")
            assert await _count(RUNS, "operator_id = :u", {"u": FAKE_USER}) == 0, (
                "no run may be created for a missing user referent")

            # (2) missing run_id in log -> recfk_log_run
            await _expect_rejected(
                f"INSERT INTO {LOG} (id, run_id, outcome) "
                "VALUES (:l, :r, 'imported')",
                {"l": "00000000-0000-4400-8000-000000000000", "r": FAKE_RUN},
                exc_class=FK_ERR, code="1452", marker="recfk_log_run")
            assert await _count(LOG, "run_id = :r", {"r": FAKE_RUN}) == 0, (
                "no log may be created for a missing run referent")

            # (3) missing recipe_id in log -> recfk_log_recipe
            #     (uses a REAL run so only the recipe FK is in question)
            await _exec(f"INSERT INTO {RUNS} (id, status) VALUES (:r, 'running')",
                        {"r": RUN_OK})
            await _expect_rejected(
                f"INSERT INTO {LOG} (id, run_id, recipe_id, outcome) "
                "VALUES (:l, :rr, :rp, 'imported')",
                {"l": "00000000-0000-4500-8000-000000000000",
                 "rr": RUN_OK, "rp": FAKE_RECIPE},
                exc_class=FK_ERR, code="1452", marker="recfk_log_recipe")
            assert await _count(LOG, "recipe_id = :rp",
                                {"rp": FAKE_RECIPE}) == 0, (
                "no log may be created for a missing recipe referent")
        finally:
            try:
                await _clean(run_ids=[RUN_OK], log_ids=[],
                             recipe_ids=[], user_ids=[])
            finally:
                await get_engine().dispose()

    asyncio.run(run())
