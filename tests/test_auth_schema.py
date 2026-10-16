"""Task 7.1 — v6 authentication schema: sessions / login_attempts / auth_locks.

Schema-only (plan C0.1 / C1.2 / C0.2 / S1): exact columns, types,
nullability, defaults, PK/UNIQUE column order, non-unique indexes, FK
name/source/target/delete rule, InnoDB + utf8mb4, the two auth_locks seed
rows, and the FK/unique behavioral surface (duplicate and missing
referent rejection, CASCADE on user delete). No auth service, no policy
code — the sliding-TTL semantics documented in C0.1 are a contract for the
later session slice, not implemented here.

Run against the ISOLATED test MariaDB (plan shared contract); the dev stack
and ganteva containers must remain untouched.

Async disposal convention (same as test_migration_audit.py /
test_schema_inventory.py): every test does ALL of its DB work inside one
``async def run()`` — one ``asyncio.run`` loop. The engine pool is
process-global (``lru_cache``) and an asyncmy connection is bound to the
loop that opened it, so the pool is disposed EXACTLY ONCE in that loop's
outermost ``finally``; cleanup (which may itself fail) lives in a nested
``try/finally`` before the dispose, so both errors survive.

Parent-first flush: probe ``User`` parent rows are flushed before any
dependent ``Session`` child insert (no ORM relationships to auto-order).

Error expectation: only the SPECIFIC error is accepted —
``sqlalchemy.exc.IntegrityError`` carrying the exact SQL error code and
marker (duplicate key 1062, missing referent 1452). No broad-``Exception``
catches anywhere in this module.

New-model imports (``Session`` / ``LoginAttempt``) are deliberately
defered — taken inside the behavior tests that need the ORM classes — so
that the schema assertions below fail on "table missing" (SQL 1146)
rather than on a missing import while this task is still unimplemented.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime

import sqlalchemy
from sqlalchemy import text

from cookbook.db import get_engine, session_scope
from cookbook.models import Recipe, User

# MariaDB strict-mode error code for missing target table (asserted on the
# pre-implementation absent-schema state and on the FK-missing referent path
# only where the code is part of the specific expectation below).
FK_ERR = sqlalchemy.exc.IntegrityError  # 1452 (missing referent)
DUP_ERR = sqlalchemy.exc.IntegrityError  # 1062 (duplicate key)

SEED_LOCK_NAMES = ("first_admin", "last_admin")


def _is_uuid(s: object) -> bool:
    return bool(str(s) and len(str(s)) == 36 and str(s).count("-") == 4)


def _norm_default(v: object) -> object:
    # information_schema reports "no explicit default" on a nullable column
    # as the literal string 'NULL' — normalize to Python None for clean
    # equality comparisons (see test_migration_audit.py).
    return None if isinstance(v, str) and v.upper() == "NULL" else v


# ---------------------------------------------------------------------------
# Read / DML helpers — these do NOT dispose the pool. Each test drains the
# pool exactly once, in its own outer ``async def run()`` ``finally``, on
# the live loop.
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


async def _engine_collation(table: str) -> tuple:
    async with get_engine().connect() as conn:
        row = (await conn.execute(
            text("SELECT ENGINE, TABLE_COLLATION FROM information_schema.TABLES "
                 "WHERE table_schema = DATABASE() AND table_name = :t"),
            {"t": table})).first()
    return tuple(row) if row else (None, None)


async def _count(table: str, where_sql: str = "",
                 params: dict | None = None) -> int:
    sql = f"SELECT COUNT(*) FROM {table}" + (f" WHERE {where_sql}" if where_sql else "")
    async with get_engine().connect() as conn:
        return (await conn.execute(text(sql), params or {})).scalar_one()


async def _exec(sql: str, params: dict | None) -> int:
    """One DML statement in its own begin/commit. Disposal stays with the
    owning test's outer ``finally`` (one outer drain on the live loop)."""
    async with get_engine().begin() as conn:
        return (await conn.execute(text(sql), params or {})).rowcount


async def _expect_rejected(sql: str, params: dict | None, *,
                           exc_class: type[Exception], code: str,
                           marker: str) -> None:
    """Run ``sql``; REQUIRE rejection with ``exc_class`` carrying SQL error
    ``code`` and ``marker`` in the message. No broad-Exception swallowing:
    an unrelated success OR a different error class both raise
    AssertionError."""
    try:
        await _exec(sql, params)
    except exc_class as ex:
        msg = str(ex)
        assert code in msg, f"expected SQL code {code} in the error, got: {msg}"
        assert marker in msg, f"expected marker {marker!r} in the error, got: {msg}"
        return
    raise AssertionError(
        f"expected {exc_class.__name__} (code {code}, marker {marker!r}) for:\n{sql}")


def _probe_user(username_prefix: str) -> tuple[str, str]:
    uid = str(uuid.uuid4())
    return uid, f"t71_{username_prefix}_{uuid.uuid4().hex[:10]}"


async def _insert_user(uid: str, username: str) -> None:
    async with session_scope() as s:
        s.add(User(id=uid, username=username, password_hash="x" * 60))
        await s.flush()
        # The parent user row must be committed before any dependent
        # session insert that references it (no ORM relationship orders it).


async def _cleanup(session_hashes: list[str], login_row_ids: list[str],
                   recipe_ids: list[str], user_ids: list[str]) -> None:
    """FK-safe probe cleanup: children (sessions / login rows / recipes)
    first, then their probe users. Never touches seed rows (categories,
    auth_locks). Disposal is left to the owning test's outer ``finally``."""
    async with get_engine().begin() as conn:
        for h in session_hashes:
            await conn.execute(text("DELETE FROM sessions WHERE id_hash = :h"), {"h": h})
        for lid in login_row_ids:
            await conn.execute(text("DELETE FROM login_attempts WHERE id = :l"), {"l": lid})
        for r in recipe_ids:
            await conn.execute(text("DELETE FROM recipes WHERE id = :r"), {"r": r})
        for u in user_ids:
            await conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": u})


# ===========================================================================
# SCHEMA — sessions (plan C0.1 complete contract)
# ===========================================================================

def test_sessions_columns_types_nullability_defaults() -> None:
    """Exactly the six locked C0.1 sessions columns with exact MariaDB 11
    types and nullability: id_hash char(64) NOT NULL (PK, no default),
    user_id varchar(36) NOT NULL, csrf_token char(32) NOT NULL (no default),
    created_ip varchar(45) NULL, and created_at / last_seen_at / expires_at
    DATETIME(6) NOT NULL — the time columns are APP-supplied, so NONE of the
    six carries a server default (no NOW(), no CURRENT_TIMESTAMP)."""
    EXPECTED = {
        "id_hash":    ("char(64)",    "NO",  None),
        "user_id":    ("varchar(36)", "NO",  None),
        "csrf_token": ("char(32)",    "NO",  None),
        "created_ip": ("varchar(45)", "YES", None),
        "created_at": ("datetime(6)", "NO",  None),
        "last_seen_at": ("datetime(6)", "NO", None),
        "expires_at": ("datetime(6)", "NO",  None),
    }

    async def run() -> None:
        try:
            cols = await _all_columns("sessions")
            assert set(cols) == set(EXPECTED), (
                f"sessions columns must be EXACTLY the six C0.1 columns "
                f"(no raw-token column), got {sorted(cols)}")
            for name, expected in EXPECTED.items():
                assert cols[name] == expected, (
                    f"sessions.{name} must be {expected!r} (type, nullable, "
                    f"default), got {cols[name]!r}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_sessions_primary_index_and_fk_index_exact() -> None:
    """sessions has EXACTLY: the PRIMARY key on [id_hash] and the single
    non-unique index ix_sessions_user_id on exactly [user_id] (no extras,
    exact column lists)."""
    async def run() -> None:
        try:
            uniq = await _indexes("sessions", non_unique=False)
            nu = await _indexes("sessions", non_unique=True)
            assert uniq == {"PRIMARY": ["id_hash"]}, (
                f"sessions unique indexes must be exactly PRIMARY [id_hash]: {uniq!r}")
            assert nu == {"ix_sessions_user_id": ["user_id"]}, (
                f"sessions non-unique indexes must be exactly "
                f"ix_sessions_user_id [user_id]: {nu!r}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_sessions_fk_name_source_target_rule() -> None:
    """The single sessions FK, exact name/columns/rule:
    fk_sessions_user : user_id -> users.id ON DELETE CASCADE (R1 — a user
    delete kills their sessions)."""
    async def run() -> None:
        try:
            fks = await _fks("sessions")
            assert fks == {
                "user_id": ("fk_sessions_user", "users", "id", "CASCADE"),
            }, f"sessions FKs must be exactly the one user FK: {fks!r}"
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_sessions_has_no_raw_token_column() -> None:
    """R1: the raw session token is issued once and NEVER stored — it lives
    only in the browser cookie. The sessions table must contain only the
    sha256 id_hash; this asserts no column holds the token itself."""
    async def run() -> None:
        try:
            cols = await _all_columns("sessions")
            offenders = [c for c in cols if "token" in c and c != "csrf_token"]
            assert offenders == [], (
                f"sessions must not store raw token material; found {offenders}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


# ===========================================================================
# SCHEMA — login_attempts (plan C1.2 locked shape)
# ===========================================================================

def test_login_attempts_columns_types_nullability_defaults() -> None:
    """Exactly the eight locked C1.2 login_attempts columns: id varchar(36)
    NOT NULL (Python ``_uuid`` default; NO server default), username
    varchar(60) NOT NULL, ip varchar(45) NOT NULL, attempts int(11) NOT NULL
    DEFAULT 0 (server default 0 — proven independently of Python in the raw
    SQL test below), first_failed_at / last_failed_at datetime NOT NULL
    (app-supplied, no defaults), lock_count int(11) NOT NULL DEFAULT 0,
    locked_until datetime NULL."""
    EXPECTED = {
        "id":              ("varchar(36)", "NO",  None),
        "username":        ("varchar(60)", "NO",  None),
        "ip":              ("varchar(45)", "NO",  None),
        "attempts":        ("int(11)",     "NO",  "0"),
        "first_failed_at": ("datetime",    "NO",  None),
        "last_failed_at":  ("datetime",    "NO",  None),
        "lock_count":      ("int(11)",     "NO",  "0"),
        "locked_until":    ("datetime",    "YES", None),
    }

    async def run() -> None:
        try:
            cols = await _all_columns("login_attempts")
            assert set(cols) == set(EXPECTED), (
                f"login_attempts columns must be EXACTLY the eight C1.2 columns, "
                f"got {sorted(cols)}")
            for name, expected in EXPECTED.items():
                assert cols[name] == expected, (
                    f"login_attempts.{name} must be {expected!r} (type, "
                    f"nullable, default), got {cols[name]!r}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_login_attempts_indexes_exact_orders() -> None:
    """login_attempts unique indexes EXACTLY: PRIMARY [id] and
    uq_login_attempts_pair on exactly [username, ip] in that order (the
    one-row-per-pair uniqueness contract, asserted separately from key
    discovery); non-unique indexes EXACTLY: ix_login_attempts_locked on
    [locked_until]. Exact column ORDER via seq_in_index."""
    async def run() -> None:
        try:
            uniq = await _indexes("login_attempts", non_unique=False)
            nu = await _indexes("login_attempts", non_unique=True)
            assert uniq == {
                "PRIMARY": ["id"],
                "uq_login_attempts_pair": ["username", "ip"],
            }, f"login_attempts unique indexes must be exactly these two: {uniq!r}"
            assert nu == {"ix_login_attempts_locked": ["locked_until"]}, (
                f"login_attempts non-unique indexes must be exactly "
                f"ix_login_attempts_locked [locked_until]: {nu!r}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_login_attempts_has_no_fks() -> None:
    """login_attempts stands alone — it names usernames (text), not user
    ids, so it has NO foreign keys (no row must die on account delete)."""
    async def run() -> None:
        try:
            fks = await _fks("login_attempts")
            assert fks == {}, f"login_attempts must have NO foreign keys: {fks!r}"
        finally:
            await get_engine().dispose()

    asyncio.run(run())


# ===========================================================================
# SCHEMA — auth_locks (plan C0.2) + seed rows
# ===========================================================================

def test_auth_locks_columns_key_engine() -> None:
    """auth_locks: exactly ONE column — name varchar(40) NOT NULL — which is
    the PRIMARY key [name]; no other columns, no non-unique indexes;
    InnoDB + utf8mb4 (asserted with the other three tables below via the
    shared engine check; asserted here for the PK/column set)."""
    async def run() -> None:
        try:
            cols = await _all_columns("auth_locks")
            assert set(cols) == {"name"}, (
                f"auth_locks must have EXACTLY the single PK column, got {sorted(cols)}")
            assert cols["name"] == ("varchar(40)", "NO", None), (
                f"auth_locks.name must be varchar(40) NOT NULL, got {cols['name']!r}")
            uniq = await _indexes("auth_locks", non_unique=False)
            assert uniq == {"PRIMARY": ["name"]}, (
                f"auth_locks unique indexes must be exactly PRIMARY [name]: {uniq!r}")
            nu = await _indexes("auth_locks", non_unique=True)
            assert nu == {}, f"auth_locks must have no non-unique indexes: {nu!r}"
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_auth_locks_seed_exactly_two_rows() -> None:
    """The v6 revision seeds EXACTLY the two lock rows (first_admin,
    last_admin) — no more, no fewer, no runtime re-seeding path."""
    async def run() -> None:
        try:
            n = await _count("auth_locks")
            assert n == 2, f"auth_locks must contain EXACTLY 2 seed rows, got {n}"
            async with get_engine().connect() as conn:
                res = await conn.execute(text("SELECT name FROM auth_locks ORDER BY name"))
                names = [r[0] for r in res]
            assert names == sorted(SEED_LOCK_NAMES), (
                f"auth_locks seed rows must be exactly {list(SEED_LOCK_NAMES)}, "
                f"got {names}")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


# ===========================================================================
# ENGINE + CHARSET — all three v6 tables
# ===========================================================================

def test_engine_innodb_utf8mb4_all_three_tables() -> None:
    """All three v6 tables are explicitly InnoDB with a utf8mb4-charset
    collation (pinned in both the model metadata and the migration)."""
    async def run() -> None:
        try:
            for table in ("sessions", "login_attempts", "auth_locks"):
                engine, collation = await _engine_collation(table)
                assert engine == "InnoDB", (
                    f"{table} engine should be InnoDB, got {engine!r}")
                assert isinstance(collation, str) and collation.startswith("utf8mb4"), (
                    f"{table} collation {collation!r} must be a utf8mb4-charset "
                    f"collation")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


# ===========================================================================
# BEHAVIOR — ORM round-trips, defaults, uniqueness, FK rules, CASCADE
# ===========================================================================

def test_session_orm_roundtrip_supplied_values() -> None:
    """A Session row round-trips with ALL values app-supplied (caller-
    supplied id_hash + csrf_token, client IP, and the three timestamps with
    microsecond precision) — proving the columns exist with the right types
    and no hidden server defaults interfere. Parent user flushed first."""
    uid, username = _probe_user("sess")
    sid_hash = "ab" * 32
    csrf = "cd" * 16
    created_ip = "192.0.2.14"
    created = datetime(2026, 10, 16, 12, 0, 0, 123456)
    expires = datetime(2026, 10, 17, 0, 0, 0, 123456)
    inserted = False

    async def run() -> None:
        try:
            await _insert_user(uid, username)
            from cookbook.models import Session  # lazy: absent while unimplemented

            async with session_scope() as s:
                s.add(Session(
                    id_hash=sid_hash,
                    user_id=uid,
                    csrf_token=csrf,
                    created_ip=created_ip,
                    created_at=created,
                    last_seen_at=created,
                    expires_at=expires,
                ))
                await s.flush()
            inserted = True  # committed inside the scope's clean exit

            # read back through the ORM (exact values incl. microseconds)
            async with session_scope() as s:
                row = await s.get(Session, sid_hash)
                assert row is not None, "session row not found after commit"
                assert row.id_hash == sid_hash
                assert row.user_id == uid
                assert row.csrf_token == csrf
                assert row.created_ip == created_ip
                assert row.created_at == created
                assert row.last_seen_at == created
                assert row.expires_at == expires

            # and through raw SQL (an independent read path)
            async with get_engine().connect() as conn:
                raw = (await conn.execute(text(
                    "SELECT user_id, csrf_token, created_ip, created_at, "
                    "last_seen_at, expires_at FROM sessions WHERE id_hash = :h"),
                    {"h": sid_hash})).first()
            assert raw is not None, "the raw-SQL session read must find the row"
            assert tuple(raw) == (uid, csrf, created_ip, created, created, expires)
        finally:
            try:
                await _cleanup(session_hashes=[sid_hash] if inserted else [],
                               login_row_ids=[], recipe_ids=[], user_ids=[uid])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_login_attempt_orm_roundtrip_and_nullable_locked_until() -> None:
    """A LoginAttempt round-trips with every field explicit — including a
    SET locked_until (the 'still locked' state) — and separately a bare ORM
    row proves the Python ``_uuid`` id default, the server defaults
    attempts=0 / lock_count=0, and a NULL locked_until (the unlocked state)."""
    row_id = str(uuid.uuid4())
    first = datetime(2026, 10, 16, 11, 0, 0)
    last = datetime(2026, 10, 16, 11, 4, 0)
    locked = datetime(2026, 10, 16, 12, 0, 0)
    u1, uname1 = _probe_user("la-full")
    u2, uname2 = _probe_user("la-bare")
    bare_id: str | None = None
    inserted_bare = False

    async def run() -> None:
        try:
            await _insert_user(uid=u1, username=uname1)
            await _insert_user(uid=u2, username=uname2)
            from cookbook.models import LoginAttempt  # lazy: absent while unimplemented

            # full row: every column explicit, locked_until SET
            async with session_scope() as s:
                s.add(LoginAttempt(
                    id=row_id,
                    username=f"alice_{u1[:8]}",
                    ip="198.51.100.77",
                    attempts=3,
                    first_failed_at=first,
                    last_failed_at=last,
                    lock_count=1,
                    locked_until=locked,
                ))

            # bare row: Python uuid default + server counter defaults + NULL lock
            async with session_scope() as s:
                la = LoginAttempt(
                    username=f"bob_{u2[:8]}",
                    ip="198.51.100.78",
                    first_failed_at=first,
                    last_failed_at=last,
                )
                s.add(la)
                await s.flush()
                bare_id = la.id
            inserted_bare = True

            assert _is_uuid(bare_id), (
                f"the `_uuid` Python default must be a valid UUID, got {bare_id!r}")

            # full row read back via raw SQL (all fields exact)
            async with get_engine().connect() as conn:
                full = (await conn.execute(text(
                    "SELECT username, ip, attempts, first_failed_at, last_failed_at, "
                    "lock_count, locked_until FROM login_attempts WHERE id = :i"),
                    {"i": row_id})).first()
            assert full is not None, "full login attempt row must exist"
            assert tuple(full) == (
                f"alice_{u1[:8]}", "198.51.100.77", 3, first, last, 1, locked
            ), f"full login attempt row must read back exactly, got {tuple(full)!r}"

            # bare row: counters from the SERVER default, locked_until NULL
            async with get_engine().connect() as conn:
                bare = (await conn.execute(text(
                    "SELECT attempts, lock_count, locked_until "
                    "FROM login_attempts WHERE id = :i"),
                    {"i": bare_id})).first()
            assert bare is not None, "bare login attempt row must exist"
            assert tuple(bare) == (0, 0, None), (
                f"bare row must show server defaults attempts=0/lock_count=0 and "
                f"NULL locked_until (nullable round-trip), got {tuple(bare)!r}")
        finally:
            try:
                await _cleanup(session_hashes=[],
                               login_row_ids=[row_id] + ([bare_id] if inserted_bare else []),
                               recipe_ids=[], user_ids=[u1, u2])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_login_attempts_raw_sql_counter_defaults() -> None:
    """Raw SQL proves the counter defaults INDEPENDENTLY of Python defaults:
    a plain INSERT that omits attempts and lock_count (naming only id +
    username + ip + the two required timestamps) must still land 0/0 from the
    DB server defaults — there is no ORM layer here at all, so any Python
    default is impossible in this path (same technique as the v5 audit tests)."""
    row_id = str(uuid.uuid4())
    first = datetime(2026, 10, 16, 10, 0, 0)
    last = datetime(2026, 10, 16, 10, 0, 0)

    async def run() -> None:
        try:
            await _exec(
                "INSERT INTO login_attempts "
                "(id, username, ip, first_failed_at, last_failed_at) "
                "VALUES (:i, :u, :ip, :f, :l)",
                {"i": row_id, "u": f"raw_{row_id[:8]}", "ip": "198.51.100.79",
                 "f": first, "l": last})

            async with get_engine().connect() as conn:
                row = (await conn.execute(text(
                    "SELECT attempts, lock_count, locked_until "
                    "FROM login_attempts WHERE id = :i"),
                    {"i": row_id})).first()
            assert row is not None, "the raw-SQL login attempt row must exist"
            assert tuple(row) == (0, 0, None), (
                f"attempts/lock_count must be 0 from the server DEFAULT 0 "
                f"(independent of any Python default) and locked_until NULL; "
                f"got {tuple(row)!r}")
        finally:
            try:
                await _cleanup(session_hashes=[], login_row_ids=[row_id],
                               recipe_ids=[], user_ids=[])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_duplicate_login_attempt_pair_rejected_distinct_pairs_allowed() -> None:
    """The uq_login_attempts_pair (username, ip) unique key: a second row
    with the SAME (username, ip) pair is rejected (SQL 1062 naming the
    constraint), while rows differing in exactly one coordinate — distinct
    ip, distinct username — both succeed (one row per pair, not per user,
    not per ip)."""
    u1, uname1 = _probe_user("pair-a")
    u2, uname2 = _probe_user("pair-b")
    u3, uname3 = _probe_user("pair-c")
    ip = "198.51.100.81"
    t = datetime(2026, 10, 16, 9, 0, 0)
    row_ids: list[str] = []
    uname_a = f"pairA_{u1[:8]}"

    async def run() -> None:
        try:
            await _insert_user(uid=u1, username=uname1)
            await _insert_user(uid=u2, username=uname2)
            await _insert_user(uid=u3, username=uname3)
            # users are context only (login_attempts carries usernames, not
            # FKs); the probes guarantee cleanup has exact handles.

            # (1) first row of the pair lands
            await _exec(
                "INSERT INTO login_attempts "
                "(id, username, ip, first_failed_at, last_failed_at) "
                "VALUES (:i, :u, :ip, :f, :l)",
                {"i": str(uuid.uuid4()), "u": uname_a, "ip": ip, "f": t, "l": t})
            row_ids.append(str(uuid.uuid4()))

            # (2) SAME (username, ip) again -> rejected with 1062 + constraint name
            await _expect_rejected(
                "INSERT INTO login_attempts "
                "(id, username, ip, first_failed_at, last_failed_at) "
                "VALUES (:i, :u, :ip, :f, :l)",
                {"i": str(uuid.uuid4()), "u": uname_a, "ip": ip, "f": t, "l": t},
                exc_class=DUP_ERR, code="1062", marker="uq_login_attempts_pair")

            # (3) distinct ip (same username) -> allowed
            r_b = str(uuid.uuid4())
            await _exec(
                "INSERT INTO login_attempts "
                "(id, username, ip, first_failed_at, last_failed_at) "
                "VALUES (:i, :u, :ip, :f, :l)",
                {"i": r_b, "u": uname_a, "ip": "198.51.100.82", "f": t, "l": t})
            row_ids.append(r_b)

            # (4) distinct username (same ip) -> allowed
            r_c = str(uuid.uuid4())
            await _exec(
                "INSERT INTO login_attempts "
                "(id, username, ip, first_failed_at, last_failed_at) "
                "VALUES (:i, :u, :ip, :f, :l)",
                {"i": r_c, "u": f"pairC_{u3[:8]}", "ip": ip, "f": t, "l": t})
            row_ids.append(r_c)

            # the pair key is intact: exactly 3 rows, 2 distinct pairs
            assert await _count("login_attempts", "username = :u", {"u": uname_a}) == 2, \
                "username pairA must have exactly two rows (two distinct ips)"
            assert await _count(
                "login_attempts", "username = :u AND ip = :ip", {"u": uname_a, "ip": ip}) == 1, \
                "the (pairA, .81) pair must have exactly one row"
        finally:
            try:
                await _cleanup(session_hashes=[], login_row_ids=row_ids,
                               recipe_ids=[], user_ids=[u1, u2, u3])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_duplicate_session_hash_rejected() -> None:
    """PK id_hash: a second session — even for a DIFFERENT user — with the
    same id_hash must be rejected (SQL 1062), proving the PK is on id_hash
    alone."""
    uid_a, uname_a = _probe_user("dup-a")
    uid_b, uname_b = _probe_user("dup-b")
    sid_hash = "ef" * 32
    t = datetime(2026, 10, 16, 8, 30, 0)

    async def run() -> None:
        try:
            await _insert_user(uid=uid_a, username=uname_a)
            await _insert_user(uid=uid_b, username=uname_b)

            await _exec(
                "INSERT INTO sessions (id_hash, user_id, csrf_token, created_at, "
                "last_seen_at, expires_at) VALUES (:h, :u, :c, :f, :l, :e)",
                {"h": sid_hash, "u": uid_a, "c": "11" * 16,
                 "f": t, "l": t, "e": t})

            await _expect_rejected(
                "INSERT INTO sessions (id_hash, user_id, csrf_token, created_at, "
                "last_seen_at, expires_at) VALUES (:h, :u, :c, :f, :l, :e)",
                {"h": sid_hash, "u": uid_b, "c": "22" * 16, "f": t, "l": t, "e": t},
                exc_class=DUP_ERR, code="1062", marker="PRIMARY")
        finally:
            try:
                await _cleanup(session_hashes=[sid_hash], login_row_ids=[],
                               recipe_ids=[], user_ids=[uid_a, uid_b])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_duplicate_auth_lock_name_rejected() -> None:
    """PK name: re-inserting a seeded lock row ('first_admin') or any new
    duplicate must be rejected (SQL 1062) — the seed rows are immutable via
    the PK, and there is no runtime re-seeding path."""
    async def run() -> None:
        try:
            await _expect_rejected(
                "INSERT INTO auth_locks (name) VALUES (:n)",
                {"n": "first_admin"},
                exc_class=DUP_ERR, code="1062", marker="auth_locks")
        finally:
            await get_engine().dispose()

    asyncio.run(run())


def test_missing_user_fk_rejected() -> None:
    """fk_sessions_user: a session whose user_id names no users row must be
    rejected (SQL 1452 naming the constraint) — sessions cannot point at
    deleted or nonexistent users."""
    uid, uname = _probe_user("nok")
    phantom = str(uuid.uuid4())
    t = datetime(2026, 10, 16, 8, 0, 0)

    async def run() -> None:
        try:
            # a real user is created (and cleaned up) so this test also proves
            # the happy path's parent existed in the schema; the INSERT below
            # intentionally references a NONEXISTENT user id.
            await _insert_user(uid=uid, username=uname)

            await _expect_rejected(
                "INSERT INTO sessions (id_hash, user_id, csrf_token, created_at, "
                "last_seen_at, expires_at) VALUES (:h, :u, :c, :f, :l, :e)",
                {"h": "aa" * 32, "u": phantom, "c": "33" * 16,
                 "f": t, "l": t, "e": t},
                exc_class=FK_ERR, code="1452", marker="fk_sessions_user")
        finally:
            try:
                await _cleanup(session_hashes=[], login_row_ids=[],
                               recipe_ids=[], user_ids=[uid])
            finally:
                await get_engine().dispose()

    asyncio.run(run())


def test_user_delete_cascades_sessions_other_user_survives() -> None:
    """fk_sessions_user ON DELETE CASCADE: deleting a user removes exactly
    that user's sessions, while another user's sessions and the untouched
    seed data (all eight DL-4 categories) survive intact — a real parent
    DELETE, same style as the v6.6 inventory probe."""
    uid_a, uname_a = _probe_user("casc-a")
    uid_b, uname_b = _probe_user("casc-b")
    hash_a1 = "41" * 32
    hash_a2 = "42" * 32
    hash_b = "43" * 32
    recipe_id = str(uuid.uuid4())
    t = datetime(2026, 10, 16, 7, 0, 0)

    async def run() -> None:
        try:
            await _insert_user(uid=uid_a, username=uname_a)
            await _insert_user(uid=uid_b, username=uname_b)
            async with session_scope() as s:
                s.add(Recipe(id=recipe_id, slug=f"t71unrel_{recipe_id[:8]}",
                             title="Unrelated probe recipe"))
                await s.flush()

            for h in (hash_a1, hash_a2, hash_b):
                await _exec(
                    "INSERT INTO sessions (id_hash, user_id, csrf_token, "
                    "created_at, last_seen_at, expires_at) "
                    "VALUES (:h, :u, :c, :f, :l, :e)",
                    {"h": h,
                     "u": uid_a if h in (hash_a1, hash_a2) else uid_b,
                     "c": "55" * 16, "f": t, "l": t, "e": t})

            # baseline: both users' sessions + the unrelated recipe + seeds
            assert await _count("sessions", "user_id = :u", {"u": uid_a}) == 2
            assert await _count("sessions", "user_id = :u", {"u": uid_b}) == 1
            assert await _count("recipes", "id = :r", {"r": recipe_id}) == 1
            assert await _count("categories") == 8

            # (the real parent DELETE) user_a goes -> exactly its sessions die
            await _exec("DELETE FROM users WHERE id = :u", {"u": uid_a})

            assert await _count("sessions", "user_id = :u", {"u": uid_a}) == 0, \
                "CASCADE: user_a's sessions must be gone"
            assert await _count("sessions", "id_hash = :h", {"h": hash_a1}) == 0
            assert await _count("sessions", "id_hash = :h", {"h": hash_a2}) == 0
            assert await _count("sessions", "user_id = :u", {"u": uid_b}) == 1, \
                "other user's sessions must survive user_a's delete"
            assert await _count("sessions", "id_hash = :h", {"h": hash_b}) == 1
            assert await _count("recipes", "id = :r", {"r": recipe_id}) == 1, \
                "unrelated recipe data must survive"
            assert await _count("categories") == 8, "seed categories must be intact"
        finally:
            try:
                await _cleanup(session_hashes=[hash_a1, hash_a2, hash_b],
                               login_row_ids=[], recipe_ids=[recipe_id],
                               user_ids=[uid_a, uid_b])
            finally:
                await get_engine().dispose()

    asyncio.run(run())
