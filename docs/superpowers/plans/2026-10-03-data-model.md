# Cookbook Data Model Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define the full cookbook schema — `recipes`, ordered body `sections` + `items`, `categories`, `tags` + join tables, migration audit tables, and `recipe_url_aliases` — as SQLAlchemy models, one forward Alembic migration, and a regression test suite green against an **isolated** MariaDB database, without advancing the running dev database.

**Architecture:** SQLAlchemy 2.0 (async `asyncmy`) declarative models in `src/cookbook/models.py`; a single Alembic revision on top of v0 (`6d22befb6ec9_users_table`); shape tests read `information_schema` (columns, types, nullability, exactly-which-columns of each PK/UNIQUE index) plus INSERT/cascade probes through the real engine against a **throwaway `cookbook_test` schema** in a dedicated `testdb` container. The dev stack on `cookbook` is untouched while this schema is unmerged.

**Tech Stack:** Python 3.13.15 venv (uv), SQLAlchemy 2.0 async + asyncmy, Alembic, MariaDB 11 (`mariadb:11`), pytest (no pytest-asyncio — tests use `asyncio.run` directly, same as `tests/test_models.py`), Docker Compose.

**Spec:** `docs/superpowers/specs/2026-09-20-cookbook-design.md` — §2 register (D3, D4, D5, D6, D8, D12, D13, D15, D22, D23), §3 data model, §5.2–5.3 importer contract, §7.1–7.5 read surfaces (ordering requirements), §10 test strategy, §13 decision log (DL-1..DL-5 bind this plan). Spike: `spikes/001-wxr-discovery/findings.md` (388 published recipes; body = up to 24 named sub-blocks per recipe).

## Global Constraints

Verbatim from the spec + decision log — every task's requirements implicitly include this section.

- **D2:** "MariaDB in both dev and prod (identical dialect + FULLTEXT); no SQLite anywhere."
- **D3:** "Recipe identity: immutable UUID; slug editable, never auto-derived from title."
- **D4:** "Ingredients: `raw_text` NOT NULL is the source of truth; parsed columns nullable; ingestion never mutates `raw_text`." (DL-3 extends this to *every line of every section*.)
- **D5:** Provenance (`source_system` / `source_id` / `source_url` / `source_name` / `extraction_method` / `imported_at`); idempotency on `UNIQUE (source_system, source_id)`.
- **D6 / D17:** Legacy URL retention — `recipe_url_aliases.path` is the full legacy path as observed (`/?p=1234`, `/?page_id=16`); app-side 301 to the canonical path; readers never see a "moved" page.
- **D8:** WP migration audit trail (`migration_runs` + `migration_recipe_log`); dry-run mandatory; idempotent re-runs.
- **D12:** Categories are many-to-many; ≥1 category enforced at save-time validation (later plan), not by a DB constraint.
- **DL-3 (binding, supersedes §3.2/§3.3 for the store shape):** "Body model = ordered `section[]` = list of `{heading?, kind: 'ingredients'|'steps'|'note', lines[]}`. `raw_text`-first (D4) applies to every line in every section."
- **DL-4 (binding):** "Categories are data, never hardcoded" — the 8 WP menu categories + "Others" are seed data in owner-manageable order; deleting an in-use category must not lose the recipes' other membership.
- **DL-5 (binding):** UUIDv4 identifiers (all `id` PKs + FKs referencing them) are `VARCHAR(36)` — not `CHAR(36)`. Fixed-length hashes/tokens keep their spec-defined `CHAR` types (auth plan only).
- **DL-1 (binding for seed/import, not for schema):** all 388 published recipes migrate; the 14 unmenued get the seeded "Others" category; draft 1865 excluded.

**Owner standing orders (binding):** storage on host bind mounts under `/mnt/container/<container>`; one code path (only config differs between dev/prod); dev DB data disposable (D22) but **do not advance the running dev stack while this branch is unmerged** — test against an isolated `cookbook_test` database in a `testdb` container; branch-per-task, merge to `main` only on explicit owner approval.

## Schema overview (what this plan locks)

| table | purpose | PK | key uniques |
|---|---|---|---|
| `recipes` (§3.1) | one row per recipe; title/slug/provenance/servings/times/flags | `id` VARCHAR(36) | `slug`; `(source_system, source_id)` |
| `recipe_sections` (DL-3) | ordered named body blocks ("Filling:", layer 2, …) | `id` VARCHAR(36) | `(recipe_id, position)` |
| `recipe_items` (DL-3 + D4) | every ordered line (ingredient or step), raw_text first | `id` VARCHAR(36) | `(section_id, position)` |
| `categories` (§3.4, DL-4) | data, owner-orderable | `id` VARCHAR(36) | `slug`, `name`; `(position)` |
| `recipe_categories` (§3.4) | many-to-many | `(recipe_id, category_id)` | — |
| `tags` (§3.4) | free-form semantic | `id` VARCHAR(36) | `slug`, `name` |
| `recipe_tags` (§3.4) | many-to-many | `(recipe_id, tag_id)` | — |
| `recipe_url_aliases` (§3.5) | legacy `/?p=…` / `/?page_id=…` → recipe or category | `id` VARCHAR(36) | `path`; exactly-one-of recipe/category |
| `migration_runs` (§3.6) | one row per importer run | `id` VARCHAR(36) | — |
| `migration_recipe_log` (§3.6) | per-source-post outcome of a run | `id` VARCHAR(36) | — |

**Deliberately not in this plan** (their owning plans): `sessions`, `login_attempts` (auth plan, §3.5/§6); `users` changes (none — v0 shape is final per DL-5); FULLTEXT index creation and tokenizer choice (D14 — search plan, benchmarked on the migrated corpus; this plan keeps `title`/`description`/`raw_text` index-able as plain columns, adds no FULLTEXT now — **open decision O5**); UI ordering surfaces (read plan) that *consume* the orderings locked here.

---

## Shared contract for every task (read once, applies to all)

### Isolated test database (binding)

**Do not run any migration or test against the running dev `db` container, the `cookbook` dev schema under `/mnt/container/db`, or the dev `app` stack while this branch is unmerged.** Every MariaDB step in this plan runs against a throwaway single-instance container named `cookbook-test-db` on pinned port `127.0.0.1:33061`:

```bash
# 1) Pre-flight: record the dev stack's state so you can prove it's untouched afterwards
docker compose ps --format '{{.Name}} {{.Status}}' | tee /tmp/devstack-before.txt

# 2) Fresh throwaway MariaDB (disposable by design per D22 — no host mount: it
#    has no persistent storage by intent, so no /mnt/container entry is required)
docker rm -f cookbook-test-db 2>/dev/null || true
docker run -d --name cookbook-test-db \
  --health-cmd="healthcheck.sh --connect --innodb_initialized" \
  --health-interval=3s --health-timeout=3s --health-retries=24 \
  -e MARIADB_ROOT_PASSWORD=devrootpassword \
  -e MARIADB_DATABASE=cookbook \
  -e MARIADB_USER=cookbook \
  -e MARIADB_PASSWORD=devpassword \
  -p 127.0.0.1:33061:3306 mariadb:11

# 3) Wait until healthy
for i in $(seq 1 30); do
  docker inspect cookbook-test-db --format '{{.State.Health.Status}}' | grep -q healthy && break
  sleep 2
done
docker inspect cookbook-test-db --format '{{.State.Health.Status}}'   # must print: healthy
```

Env for every alembic/pytest invocation against it (explicit env vars win over the
local `.env` in pydantic-settings precedence — env vars beat dotenv — so the
secret-guarded local `.env` can never leak values into the test connection):

```bash
export CT="SECRET_KEY=devsecretkey DB_HOST=127.0.0.1 DB_PORT=33061 DB_NAME=cookbook DB_USER=cookbook DB_PASSWORD=devpassword"
# then, e.g.:
env $CT .venv/bin/alembic upgrade head
env $CT .venv/bin/pytest tests/test_recipes.py -v
```

Tear-down at the end of each task (before commit), plus the unchanged-dev-stack proof:

```bash
docker rm -f cookbook-test-db
docker compose ps --format '{{.Name}} {{.Status}}' | tee /tmp/devstack-after.txt
diff /tmp/devstack-before.txt /tmp/devstack-after.txt && echo "dev stack untouched"
```

The dev `app`/`db` containers may be running or stopped — either state is fine; the
diff must show whatever it was, still the same. Never `docker compose down`.

### Assertion conventions (carry over from Task 4's settled pattern)

- **Types are asserted exactly as MariaDB 11 reports them** in `information_schema.columns.column_type`: `varchar(36)`, `varchar(100)`, `varchar(200)`, `varchar(255)`, `varchar(500)`, `varchar(2000)`, `text`, `int(11)`, `decimal(10,2)`, `datetime`, `date`, `tinyint(1)`, `char(64)`. The engine is pinned to `mariadb:11` — type strings are stable within it. If an assertion fails, the migration diverged from this plan: fix the migration, never the test.
- **Indexes are asserted as complete ordered column lists** (the owner-verified v3 pattern from `tests/test_models.py`): query `information_schema.statistics … AND non_unique = 0 ORDER BY index_name, seq_in_index`, group into `{index_name: [ordered columns]}`, and assert e.g. `indexes["PRIMARY"] == ["id"]` — which proves a PK contains *exactly* those columns and each unique index contains *exactly* its own column set.
- **Foreign keys include their delete rule**: `key_column_usage` joined to `referential_constraints` (`DELETE_RULE`), asserting e.g. `recipes.created_by → users.id, SET NULL`.
- **UUID columns are `varchar(36)`** (DL-5) — no CHAR/VARCHAR-equivalence wording.
- All async DB tests are sync test functions wrapping `asyncio.run(run())` (no pytest-asyncio dep — same style as `tests/test_models.py`), and every test that writes rows cleans them up in a `finally` block and re-verifies `COUNT(*)`.

### Ordering rules (locked here; consumed by read/editor plans)

1. `recipe_sections.position` — dense `1..N` per recipe, unique per recipe (`UNIQUE (recipe_id, position)`). Render order = `ORDER BY position`. Reordering a recipe's sections renumbers that recipe's section rows 1..N (app-level save-time invariant, enforced by a later plan — not a DB trigger).
2. `recipe_items.position` — dense `1..M` per section, unique per section (`UNIQUE (section_id, position)`). Render order = `ORDER BY position`. Reorder = renumber within the section.
3. `categories.position` — dense `1..K` table-wide, unique (`UNIQUE (position)`). Drives the home "by category" lane order (DL-4 owner-manageable). Reorder = renumber table-wide.
4. Recipe listings (category page, tag page, search results) — `ORDER BY updated_at DESC` (spec §7.1 default). Query-side contract, fixed here so the read plan doesn't invent it.
5. Featured lane — `home_featured = TRUE ORDER BY home_position` (spec §7.1: "a handful of `home_featured` rows in `home_position` order"). `home_position` is only meaningful when `home_featured` is TRUE (app-level, spec §3.1 note).
6. `tags` carry **no position column in v1** (free-form, spec §3.4/D13); the home "top semantic tags" ranking is the read/search plan's query concern. If the owner later wants owner-ordered tags, that is a new migration — do not add `tags.position` now.

---

## Task 1: `recipes` table — model, migration, shape tests

**Branch:** `task/6.1-recipes` (cut from `main` after foundation merge is confirmed).

**Files:**
- Modify: `src/cookbook/models.py` (add `Recipe` alongside `User`)
- Create: `alembic/versions/<rev1>_v1_recipes_table.py`
- Create: `tests/test_recipes.py`

**Interfaces:**
- Consumes: `Base` from `cookbook.models` (existing), `get_engine()`/`session_scope()` from `cookbook.db`, `users.id` (v0, for FK targets).
- Produces: `cookbook.models.Recipe` mapped to table `recipes` with every column below; an Alembic revision `<rev1>` whose `down_revision = '6d22befb6ec9'` so `alembic upgrade head` from v0 creates it cleanly.

### Exact table definition (the contract tests below assert)

| column | type | null | notes |
|---|---|---|---|
| `id` | VARCHAR(36) | NOT NULL, PK | `default=_uuid()` (spec §3.1; D3 immutable) |
| `slug` | VARCHAR(200) | NOT NULL | UNIQUE; slug is not identity (D3); `uniques: uq_recipes_slug` — exactly `["slug"]` |
| `title` | VARCHAR(300) | NOT NULL | required for save (spec §8) |
| `description` | TEXT | NULL | |
| `servings_text` | VARCHAR(100) | NULL | free text |
| `servings_number` | DECIMAL(10,2) | NULL | optional scaling support (D15); nullable, not derived |
| `prep_minutes` | INT | NULL | |
| `cook_minutes` | INT | NULL | |
| `total_minutes` | INT | NULL | derived at save time (spec §3.1) |
| `home_featured` | BOOLEAN | NOT NULL, default `false` (SQLA default; server default `false`) | |
| `home_position` | INT | NULL | only meaningful when featured |
| `source_system` | VARCHAR(20) | NULL | `web` \| `wordpress` (spec §3.1; D5) |
| `source_id` | VARCHAR(64) | NULL | WP post id for wordpress; NULL for web |
| `source_name` | VARCHAR(300) | NULL | |
| `source_url` | VARCHAR(2000) | NULL | |
| `extraction_method` | VARCHAR(20) | NULL | `manual`\|`jsonld`\|`microdata`\|`llm`\|`wxr` (spec §3.1; D5) |
| `imported_at` | DATETIME | NULL | |
| `published_date` | DATE | NULL | WP post date |
| `image_path` | VARCHAR(500) | NULL | media volume path; one image per recipe in v1 |
| `created_by` | VARCHAR(36) | NULL | FK → `users.id` **ON DELETE SET NULL** (D10 attribution only) |
| `last_modified_by` | VARCHAR(36) | NULL | FK → `users.id` **ON DELETE SET NULL** |
| `created_at` | DATETIME | NOT NULL | `server_default=func.now()` |
| `updated_at` | DATETIME | NOT NULL | `server_default=func.now()`, `onupdate=func.now()` |

Constraints: `PK (id)`; `UNIQUE (slug)` named `uq_recipes_slug`; `UNIQUE (source_system, source_id)` named `uq_recipes_source` — this is the importer idempotency key (D5/D8; MySQL/MariaDB multiple-NULL semantics leave web rows with NULL `source_id` unaffected); FKs `recfk_created_by`/`recfk_modified_by` → `users(id)` ON DELETE SET NULL.

- [ ] **Step 1: Write the failing test**

`tests/test_recipes.py`:

```python
"""v1 `recipes` shape tests — run against the ISOLATED test MariaDB
(task/6.1; the dev stack must remain untouched; see plan's shared contract).
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


def _query(engine, sql, **params):
    async def run():
        async with engine.connect() as conn:
            return (await conn.execute(text(sql), params)).all()
    return asyncio.run(run())


def test_recipes_table_shape() -> None:
    engine = get_engine()
    rows = _query(engine,
        "SELECT column_name, column_type, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = 'recipes'")
    cols = {r[0]: {"type": r[1], "nullable": r[2]} for r in rows}
    assert set(cols) == EXPECTED_COLUMNS, f"columns: {set(cols)}"
    for col, typ in EXPECTED_TYPES.items():
        assert cols[col]["type"] == typ, f"{col}: {cols[col]['type']} != {typ}"
    assert cols["home_featured"]["nullable"] == "NO"
    assert cols["title"]["nullable"] == "NO"
    assert cols["slug"]["nullable"] == "NO"

    # Indexes: complete ordered column lists (v3 pattern, Task 4)
    irows = _query(engine,
        "SELECT index_name, column_name, seq_in_index "
        "FROM information_schema.statistics "
        "WHERE table_schema = DATABASE() AND table_name = 'recipes' AND non_unique = 0 "
        "ORDER BY index_name, seq_in_index")
    indexes: dict[str, list[str]] = {}
    for name, col, _seq in irows:
        indexes.setdefault(name, []).append(col)
    assert indexes["PRIMARY"] == ["id"], f"PRIMARY: {indexes.get('PRIMARY')}"
    assert indexes["uq_recipes_slug"] == ["slug"], f"slug uniq: {indexes}"
    assert indexes["uq_recipes_source"] == ["source_system", "source_id"], f"source uniq: {indexes}"
    asyncio.run(get_engine().dispose())


def test_recipes_fk_targets() -> None:
    engine = get_engine()
    fks = _query(engine,
        "SELECT kcu.constraint_name, kcu.column_name, kcu.referenced_table_name, "
        "       rc.delete_rule "
        "FROM information_schema.key_column_usage kcu "
        "JOIN information_schema.referential_constraints rc "
        "  ON rc.constraint_name = kcu.constraint_name "
        " AND rc.constraint_schema = kcu.constraint_schema "
        "WHERE kcu.table_schema = DATABASE() AND kcu.table_name = 'recipes' "
        "ORDER BY kcu.column_name")
    by_col = {r[1]: (r[3], r[2]) for r in fks}
    assert by_col.get("created_by") == ("SET NULL", "users")
    assert by_col.get("last_modified_by") == ("SET NULL", "users")
    asyncio.run(get_engine().dispose())


def test_recipe_defaults_roundtrip() -> None:
    """INSERT without optional fields: server defaults fill created_at/updated_at,
    home_featured defaults false; UUID default applied client-side via Python
    `default=_uuid()`. Then clean up and re-verify."""
    rid = str(uuid.uuid4())

    async def run() -> None:
        async with session_scope() as s:
            s.add(Recipe(
                id=rid, slug=f"rt-{rid[:8]}", title="Roundtrip title",
            ))
        async with session_scope() as s:
            r = await s.get(Recipe, rid)
            assert r is not None
            assert r.home_featured is False
            assert r.created_at is not None and r.updated_at is not None
            assert r.servings_number is None and r.prep_minutes is None
        async with session_scope() as s:
            await s.execute(text("DELETE FROM recipes WHERE id = :id"), {"id": rid})

    try:
        asyncio.run(run())
    finally:
        # re-verify cleanup
        async def verify() -> int:
            async with get_engine().connect() as conn:
                res = await conn.execute(
                    text("SELECT COUNT(*) FROM recipes WHERE id = :id"), {"id": rid}
                )
                return res.scalar()
        assert asyncio.run(verify()) == 0
        asyncio.run(get_engine().dispose())


def test_recipe_source_idempotency_key() -> None:
    """Two recipes with the same (source_system, source_id) must be rejected;
    two web recipes with source_id NULL must both be allowed (D5/D8)."""
    rid1, rid2, rid3 = (str(uuid.uuid4()) for _ in range(3))
    try:
        async def run() -> None:
            async with session_scope() as s:
                s.add(Recipe(
                    id=rid1, slug=f"src-{rid1[:8]}", title="S1",
                    source_system="wordpress", source_id="1234",
                ))
                s.add(Recipe(
                    id=rid2, slug=f"src2-{rid2[:8]}", title="S2",
                    source_system="wordpress", source_id="5678",
                ))
                s.add(Recipe(
                    id=rid3, slug=f"web-{rid3[:8]}", title="Web A",
                    source_system="web", source_id=None,
                ))
        asyncio.run(run())
        async def dup() -> None:
            async with session_scope() as s:
                s.add(Recipe(
                    id=str(uuid.uuid4()), slug=f"dup-x", title="Duplicate source",
                    source_system="wordpress", source_id="1234",
                ))
        try:
            asyncio.run(dup())
            raise AssertionError("duplicate (source_system, source_id) was ACCEPTED")
        except sqlalchemy.exc.IntegrityError as exc:
            assert "uq_recipes_source" in str(exc) or "Duplicate" in str(exc), str(exc)
    finally:
        async def cleanup() -> None:
            async with session_scope() as s:
                for rid in (rid1, rid2, rid3):
                    await s.execute(text("DELETE FROM recipes WHERE id = :id"), {"id": rid})
        asyncio.run(cleanup())
        asyncio.run(get_engine().dispose())

- [ ] **Step 2: Bring up the isolated test DB and run the test — RED**

```bash
# (shared contract: pre-flight snapshot, then)
docker run -d --name cookbook-test-db \
  --health-cmd="healthcheck.sh --connect --innodb_initialized" \
  --health-interval=3s --health-timeout=3s --health-retries=24 \
  -e MARIADB_ROOT_PASSWORD=devrootpassword -e MARIADB_DATABASE=cookbook \
  -e MARIADB_USER=cookbook -e MARIADB_PASSWORD=devpassword \
  -p 127.0.0.1:33061:3306 mariadb:11
for i in $(seq 1 30); do docker inspect cookbook-test-db --format '{{.State.Health.Status}}' | grep -q healthy && break; sleep 2; done

export CT="SECRET_KEY=devsecretkey DB_HOST=127.0.0.1 DB_PORT=33061 DB_NAME=cookbook DB_USER=cookbook DB_PASSWORD=devpassword"
env $CT .venv/bin/pytest tests/test_recipes.py -v
```

Expected: FAIL — `sqlalchemy.exc.ProgrammingException` / `Table 'cookbook.recipes' doesn't exist` (the migration doesn't exist yet). If it instead dials port `3306`, the env override didn't take — stop and check `env $CT .venv/bin/python -c "from cookbook.config import get_settings; print(get_settings().database_url)"`.

- [ ] **Step 3: Implement — model + migration**

`src/cookbook/models.py` final imports after this task (add `DECIMAL, Date, Integer, Text, ForeignKey, text`; extend the datetime import; add Decimal):

```python
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DECIMAL,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
```

then append after `User`:

```python
class Recipe(Base):
    __tablename__ = "recipes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    servings_text: Mapped[str | None] = mapped_column(String(100), nullable=True)
    servings_number: Mapped[Decimal | None] = mapped_column(DECIMAL(10, 2), nullable=True)
    prep_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cook_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    home_featured: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    home_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_system: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    extraction_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    last_modified_by: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Recipe {self.slug!r}>"
```

Top-of-file: the imports block above (add `DECIMAL, Date, Integer, Text, ForeignKey, text`; extend `from datetime import datetime` with `date`; add `from decimal import Decimal`).

New migration `alembic/versions/<rev1>_v1_recipes_table.py` (generate: `env $CT .venv/bin/alembic revision -m "v1_recipes_table"` after the model is in place, then align the emitted DDL with the table above — `down_revision = '6d22befb6ec9'`):

```python
"""v1: recipes table

Revision ID: <rev1>
Revises: 6d22befb6ec9
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "<rev1>"
down_revision: Union[str, None] = "6d22befb6ec9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recipes",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("slug", sa.String(length=200), nullable=False),
        sa.Column("title", sa.String(length=300), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("servings_text", sa.String(length=100), nullable=True),
        sa.Column("servings_number", sa.DECIMAL(precision=10, scale=2), nullable=True),
        sa.Column("prep_minutes", sa.Integer(), nullable=True),
        sa.Column("cook_minutes", sa.Integer(), nullable=True),
        sa.Column("total_minutes", sa.Integer(), nullable=True),
        sa.Column("home_featured", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("home_position", sa.Integer(), nullable=True),
        sa.Column("source_system", sa.String(length=20), nullable=True),
        sa.Column("source_id", sa.String(length=64), nullable=True),
        sa.Column("source_name", sa.String(length=300), nullable=True),
        sa.Column("source_url", sa.String(length=2000), nullable=True),
        sa.Column("extraction_method", sa.String(length=20), nullable=True),
        sa.Column("imported_at", sa.DateTime(), nullable=True),
        sa.Column("published_date", sa.Date(), nullable=True),
        sa.Column("image_path", sa.String(length=500), nullable=True),
        sa.Column("created_by", sa.String(length=36), nullable=True),
        sa.Column("last_modified_by", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], name="recfk_created_by", ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["last_modified_by"], ["users.id"], name="recfk_modified_by", ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_recipes_slug"),
        sa.UniqueConstraint("source_system", "source_id", name="uq_recipes_source"),
    )


def downgrade() -> None:
    op.drop_table("recipes")
```

- [ ] **Step 4: Apply to the isolated DB, re-run — GREEN**

```bash
env $CT .venv/bin/alembic upgrade head
env $CT .venv/bin/pytest tests/test_recipes.py -v
```

Expected: `alembic upgrade head` applies `<rev1>` cleanly (v0 → v1); **4 passed**. Cross-check the live shape directly (independent of the test suite):

```bash
docker exec cookbook-test-db mariadb -ucookbook -pdevpassword cookbook -e "SHOW CREATE TABLE recipes\G" | grep -E 'UNIQUE|PRIMARY|CONSTRAINT|KEY'
```

Expected: `PRIMARY KEY (id)`, `UNIQUE KEY uq_recipes_slug (slug)`, `UNIQUE KEY uq_recipes_source (source_system,source_id)`, both `recfk_*` → `users.id` ON DELETE SET NULL.

- [ ] **Step 5: Tear down the isolated DB, prove the dev stack is untouched, commit**

```bash
docker rm -f cookbook-test-db
docker compose ps --format '{{.Name}} {{.Status}}' > /tmp/devstack-after.txt
diff /tmp/devstack-before.txt /tmp/devstack-after.txt && echo "dev stack untouched"
git ls-files | grep -c egg-info   # must be 0

git add src/cookbook/models.py alembic/versions/<rev1>_v1_recipes_table.py tests/test_recipes.py
git commit -m "feat: v1 recipes table (spec §3.1, D3/D4/D5) + shape tests"
```

---

## Task 2: Ordered body — `recipe_sections` + `recipe_items` (DL-3)

**Branch:** `task/6.2-recipe-body` (cut from `main` after Task 1 is merged).

**Files:**
- Modify: `src/cookbook/models.py`
- Create: `alembic/versions/<r2>_v2_recipe_body.py`
- Create: `tests/test_sections.py`, `tests/test_items.py`

**Interfaces:**
- Consumes: `cookbook.models.Recipe` (Task 1); isolated-DB contract (Global Constraints)
- Produces: `cookbook.models.RecipeSection` and `cookbook.models.RecipeItem`; revision `<r2>` with `down_revision = "<rev1>"`. Locked shape: `recipe_sections` holds ordered named body blocks (`heading` nullable, `kind` enum), `recipe_items` holds every ordered line — `raw_text TEXT NOT NULL` is the source of truth for **every kind** (D4, extended by DL-3); the four parse columns are populated only for `kind='ingredients'` lines and stay nullable everywhere.

**Locked table shapes (DL-3 supersedes spec §3.2/§3.3 for the store shape):**

`recipe_sections`:

| column | type | notes |
|---|---|---|
| `id` | VARCHAR(36) PK | |
| `recipe_id` | VARCHAR(36) NOT NULL FK → `recipes.id` ON DELETE CASCADE | |
| `position` | INT NOT NULL | dense `1..N` per recipe; `UNIQUE (recipe_id, position)`; shared-contract ordering rule 1 |
| `heading` | VARCHAR(200) NULL | section sub-heading ("Filling:", "Layer 2") — NULL for unlabelled sections |
| `kind` | ENUM('ingredients','steps','note') NOT NULL | DL-3's literal kind set |

`recipe_items`:

| column | type | notes |
|---|---|---|
| `id` | VARCHAR(36) PK | |
| `section_id` | VARCHAR(36) NOT NULL FK → `recipe_sections.id` ON DELETE CASCADE | |
| `position` | INT NOT NULL | dense `1..M` per section; `UNIQUE (section_id, position)`; shared-contract ordering rule 2 |
| `raw_text` | TEXT NOT NULL | verbatim line, source of truth (D4, every section) |
| `quantity_text` | VARCHAR(50) NULL | best-effort parse ("1 1/2") — meaningful for `ingredients` lines |
| `unit` | VARCHAR(50) NULL | best-effort parse ("cup"), no vocabulary |
| `item` | VARCHAR(300) NULL | best-effort parse ("brown sugar") |
| `preparation` | VARCHAR(300) NULL | best-effort parse ("finely chopped") |

No `created_at`/`updated_at` on either table — body rows are wholesale-replaced on save (app-level, later plan); provenance lives on `recipes`. Nothing here is nullable where DL-3/D4 demand NOT NULL, and nothing is NOT NULL where the source may be absent.

- [ ] **Step 1: Isolated DB pre-flight** — run the shared contract's pre-flight block (dev-stack snapshot + start `cookbook-test-db` + health wait).

- [ ] **Step 2: Add the two models to `src/cookbook/models.py`**

```python
class RecipeSection(Base):
    __tablename__ = "recipe_sections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    recipe_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("recipes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    heading: Mapped[str | None] = mapped_column(String(200), nullable=True)
    kind: Mapped[str] = mapped_column(
        Enum("ingredients", "steps", "note", name="recipe_section_kind"), nullable=False
    )

    __table_args__ = (
        sa.UniqueConstraint("recipe_id", "position", name="uq_sections_recipe_position"),
        {"mysql_engine": "InnoDB"},
    )


class RecipeItem(Base):
    __tablename__ = "recipe_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    section_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("recipe_sections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)  # D4, every section
    quantity_text: Mapped[str | None] = mapped_column(String(50), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    item: Mapped[str | None] = mapped_column(String(300), nullable=True)
    preparation: Mapped[str | None] = mapped_column(String(300), nullable=True)

    __table_args__ = (
        sa.UniqueConstraint("section_id", "position", name="uq_items_section_position"),
        {"mysql_engine": "InnoDB"},
    )
```

(Uses Task 1's `_uuid` helper and imported `Text`; add `Enum` to the `sqlalchemy` import line. `default` and FK style mirror Task 1's `Recipe` — don't introduce a second id-generator style.)

- [ ] **Step 3: Write the failing shape tests**

`tests/test_sections.py` and `tests/test_items.py` — same conventions as Task 1 (`information_schema` column/index/FK asserts, `asyncio.run` wrappers, cleanup in `finally`, engine `dispose()` in `finally`):

- `test_sections_columns`: exactly `{id, recipe_id, position, heading, kind}`; types `varchar(36)`, `varchar(36)`, `int(11)`, `varchar(200)`, `enum('ingredients','steps','note')` (the exact string MariaDB 11 reports in `column_type`); nullability: NOT NULL on `id, recipe_id, position, kind`; NULL ok on `heading`.
- `test_sections_indexes`: `PRIMARY == [id]`, `uq_sections_recipe_position == [recipe_id, position]`, plus the `recipe_id` FK index.
- `test_sections_fk`: `recipe_id → recipes.id`, `DELETE_RULE == 'CASCADE'`.
- `test_items_columns`: exactly `{id, section_id, position, raw_text, quantity_text, unit, item, preparation}`; types `varchar(36)`, `varchar(36)`, `int(11)`, `text`, `varchar(50)`, `varchar(50)`, `varchar(300)`, `varchar(300)`; NOT NULL: `id, section_id, position, raw_text`; NULL ok: the four parse columns.
- `test_items_indexes`: `PRIMARY == [id]`, `uq_items_section_position == [section_id, position]`, plus the `section_id` FK index.
- `test_items_fk`: `section_id → recipe_sections.id`, `DELETE_RULE == 'CASCADE'`.
- `test_kind_values_locked`: round-trip INSERT one valid value per kind and assert read-back (`'ingredients'`, `'steps'`, `'"note"'` — i.e. the three accepted literals); then assert an INSERT with `kind='other'` raises `IntegrityError` (the enum rejects it at the DB, not the app).
- `test_cascade_chain`: insert probe recipe → 2 sections → 1 item each; `DELETE FROM recipes WHERE id = <probe>`; assert `recipe_sections` and `recipe_items` both drop to their baseline `COUNT(*)` (deep cascade both levels).

- [ ] **Step 4: Migration `alembic/versions/<r2>_v2_recipe_body.py`** — generate, then align DDL to the two locked tables: both `CREATE TABLE` (InnoDB, `utf8mb4`), the two `sa.UniqueConstraint` names above, the two explicit `ForeignKey` constraints named `recfk_sections_recipe` / `recfk_items_section` (match the Task 1 `recfk_*` naming), indexes as above; `downgrade()` drops `recipe_items` then `recipe_sections` (FK-safe order). Apply: `env $CT .venv/bin/alembic upgrade head` must report v0→v1→v2 clean.

- [ ] **Step 5: Run tests against the isolated DB** — `env $CT .venv/bin/pytest tests/test_sections.py tests/test_items.py -v` → all pass. Cross-check live DDL:

```bash
docker exec cookbook-test-db mariadb -ucookbook -pdevpassword cookbook -e "SHOW CREATE TABLE recipe_sections\G" | grep -E 'UNIQUE|PRIMARY|CONSTRAINT|KEY|enum'
docker exec cookbook-test-db mariadb -ucookbook -pdevpassword cookbook -e "SHOW CREATE TABLE recipe_items\G" | grep -E 'UNIQUE|PRIMARY|CONSTRAINT|KEY'
```

- [ ] **Step 6: Tear down, prove dev stack untouched, commit** (shared-contract tear-down block, then):

```bash
git add src/cookbook/models.py alembic/versions/<r2>_v2_recipe_body.py tests/test_sections.py tests/test_items.py
git commit -m "feat: v2 ordered body — recipe_sections + recipe_items (DL-3, D4)"
```

---

## Task 3: Organization — `categories`, `tags`, both join tables + DL-4 seed

**Branch:** `task/6.3-organization` (cut from `main` after Task 2 is merged).

**Files:**
- Modify: `src/cookbook/models.py`
- Create: `alembic/versions/<r3>_v3_organization.py`
- Create: `tests/test_organization.py`

**Interfaces:**
- Consumes: `cookbook.models.Recipe` (Task 1); isolated-DB contract
- Produces: `cookbook.models.Category`, `cookbook.models.Tag`, join-table mappings for `recipe_categories` / `recipe_tags`; revision `<r3>` with `down_revision = "<r2>"`; the 8 DL-4 seed categories present with locked UUIDs (seeded by this migration — "seed data, not constants").

**Locked table shapes (spec §3.4 + DL-4):**

`categories`:

| column | type | notes |
|---|---|---|
| `id` | VARCHAR(36) PK | locked seed UUIDs below |
| `slug` | VARCHAR(200) UNIQUE NOT NULL | spec §3.4 doesn't give a slug width — locked to the slug convention Task 1 established for `recipes.slug` |
| `name` | VARCHAR(100) NOT NULL | spec §3.4 |
| `position` | INT NOT NULL | DL-4 owner-manageable order; dense `1..K` table-wide; `UNIQUE (position)`; shared-contract ordering rule 3 |

No timestamps (spec §3.4 lists none; DL-4 adds only order).

`tags`:

| column | type | notes |
|---|---|---|
| `id` | VARCHAR(36) PK | |
| `slug` | VARCHAR(200) UNIQUE NOT NULL | same slug-convention lock as `categories.slug` |
| `name` | VARCHAR(100) NOT NULL | spec §3.4 |

No `position` — shared-contract ordering rule 6 (free-form per D13; an owner-ordered-tags migration is a future change).

`recipe_categories`: composite PK `(recipe_id, category_id)` — many-to-many, no per-recipe uniqueness beyond it (D12). FKs: `recipe_id → recipes.id ON DELETE CASCADE` (membership dies with the recipe — required for Task 2's deep-cascade to keep working); `category_id → categories.id ON DELETE RESTRICT` (DB hard guard for DL-4 "deleting an in-use category must not lose the recipes' other membership" — the *presentation* of that guard is app-level, later plan).

`recipe_tags`: identical shape: composite PK `(recipe_id, tag_id)`, `recipe_id → recipes.id ON DELETE CASCADE`, `tag_id → tags.id ON DELETE RESTRICT`.

**Locked seed set (spec §3.4 EVIDENCE + DL-4; seeded by this migration's `upgrade()`):**

| position | slug | name | id |
|---|---|---|---|
| 1 | `breakfast` | Breakfast | `412b94fd-8106-40b8-863b-46e2b1f1daff` |
| 2 | `fermentation` | Fermentation | `26857e44-4786-4068-8faf-607d4a5a83a1` |
| 3 | `appetizers` | Appetizers | `8fb7691b-6dd7-4cbc-a337-6a62d2a53b7f` |
| 4 | `drinks` | Drinks | `ac5980f3-4d82-4f63-9ac7-73602ddbb682` |
| 5 | `main-dishes` | Main Dishes | `3889e580-bb77-4a11-a87f-97c501f5ea87` |
| 6 | `side-dishes` | Side Dishes | `d51b5466-d402-4157-a09f-6e63588913bc` |
| 7 | `baking` | Baking | `58777180-feb6-42a5-a4b1-e6fb24e45bea` |
| 8 | `desserts` | Desserts | `bf86f19e-3ce9-4159-a558-bba87672e822` |

**"Others" is NOT seeded here** — DL-4: it is seeded by the §5.2 WXR import when it assigns the 14 unmenued posts (the import plan owns that row; the DB must not pre-create it, or the import's idempotency-by-slug sees a phantom it didn't create).

- [ ] **Step 1: Isolated DB pre-flight** — shared contract block.

- [ ] **Step 2: Add `Category` and `Tag` models** to `src/cookbook/models.py` (`_uuid` id default, `Text`/`String` as above; `__table_args__` with `UNIQUE (position)` named `uq_categories_position`, InnoDB). Join tables are **plain `sa.Table`** (no mapped class needed at v1):

```python
recipe_categories = sa.Table(
    "recipe_categories", Base.metadata,
    sa.Column("recipe_id", sa.String(36), sa.ForeignKey("recipes.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("category_id", sa.String(36), sa.ForeignKey("categories.id", ondelete="RESTRICT"), primary_key=True),
    {"mysql_engine": "InnoDB"},
)

recipe_tags = sa.Table(
    "recipe_tags", Base.metadata,
    sa.Column("recipe_id", sa.String(36), sa.ForeignKey("recipes.id", ondelete="CASCADE"), primary_key=True),
    sa.Column("tag_id", sa.String(36), sa.ForeignKey("tags.id", ondelete="RESTRICT"), primary_key=True),
    {"mysql_engine": "InnoDB"},
)
```

- [ ] **Step 3: Write the failing shape tests** (`tests/test_organization.py`, Task 1 conventions):
  - `test_categories_columns`: exactly `{id, slug, name, position}`; types `varchar(36)`, `varchar(200)`, `varchar(100)`, `int(11)`; all NOT NULL.
  - `test_categories_indexes`: `PRIMARY == [id]`, `uq_categories_slug == [slug]`, `uq_categories_position == [position]`.
  - `test_tags_columns`: exactly `{id, slug, name}` (proves **no** `position` column exists — rule 6); types `varchar(36)`, `varchar(200)`, `varchar(100)`; all NOT NULL.
  - `test_tags_indexes`: `PRIMARY == [id]`, `uq_tags_slug == [slug]`.
  - `test_join_primary_keys`: `recipe_categories.PRIMARY == [recipe_id, category_id]`; `recipe_tags.PRIMARY == [recipe_id, tag_id]`.
  - `test_join_fks`: `recipe_id → recipes.id CASCADE`; `category_id → categories.id RESTRICT`; `tag_id → tags.id RESTRICT` (each via `key_column_usage` ⨝ `referential_constraints`).
  - `test_in_use_category_protected`: create probe recipe; INSERT `recipe_categories` row (seed `breakfast` + probe); `DELETE FROM categories WHERE id = '412b94fd-…'` → `IntegrityError` (RESTRICT); cleanup restores count.
  - `test_membership_cascades_from_recipe`: probe recipe + membership in `breakfast` and `fermentation`; delete the recipe → both join rows gone (baseline `COUNT(*)` restored).
  - `test_seed_rows_locked`: after `alembic upgrade head`, `SELECT slug,name,position FROM categories ORDER BY position` returns **exactly** the 8-row table above (order, slugs, names); each locked UUID present; `COUNT(*) == 8`; no row with `slug='others'` (import's job).
  - `test_seed_positions_dense`: `MIN(position)=1`, `MAX(position)=8`, no gaps, all unique.

- [ ] **Step 4: Migration `<r3>_v3_organization.py`** — generate; align DDL to the four locked tables + FK/index names (`uq_categories_slug`, `uq_categories_position`, `uq_tags_slug`, composite PKs, RESTRICT/CASCADE exactly as above). In `upgrade()`, after the DDL, seed the 8 rows (id, slug, name, position) as a literal INSERT (data migration lives in the revision that creates the table). `downgrade()`: delete the seed rows, drop join tables, then `tags`, then `categories` (FK-safe order). Apply: `env $CT .venv/bin/alembic upgrade head` → v0→v1→v2→v3 clean; `alembic current` shows `<r3>`.

- [ ] **Step 5: Run tests** — `env $CT .venv/bin/pytest tests/test_organization.py -v` → all pass. Cross-check live DDL with `SHOW CREATE TABLE` for all four tables; verify FK `ON DELETE` verbs match the locked shape.

- [ ] **Step 6: Tear down, prove dev stack untouched, commit** (shared-contract tear-down, then):

```bash
git add src/cookbook/models.py alembic/versions/<r3>_v3_organization.py tests/test_organization.py
git commit -m "feat: v3 organization — categories/tags + joins + DL-4 seed (spec §3.4, D12)"
```

---

## Task 4: `recipe_url_aliases` — legacy `/?p=…` / `/?page_id=…` (spec §3.5, D6)

**Branch:** `task/6.4-aliases` (cut from `main` after Task 3 is merged).

**Files:**
- Modify: `src/cookbook/models.py`
- Create: `alembic/versions/<r4>_v4_url_aliases.py`
- Create: `tests/test_aliases.py`

**Interfaces:**
- Consumes: `Recipe` (Task 1), `Category` (Task 3); isolated-DB contract
- Produces: `cookbook.models.RecipeUrlAlias`; revision `<r4>` with `down_revision = "<r3>"`. App-side 301 middleware (read plan, spec §7.1) will read this table by `path`; nothing in this task serves HTTP.

**Locked table shape (spec §3.5 verbatim column set):**

| column | type | notes |
|---|---|---|
| `id` | VARCHAR(36) PK | `_uuid` default |
| `path` | VARCHAR(255) UNIQUE NOT NULL | **full legacy path as observed** — `/?p=1234`, `/?page_id=16` (spec §3.5 + EVIDENCE). Not a slug; never normalized at write time beyond trim — the WXR/observed string is what must match on redirect |
| `recipe_id` | VARCHAR(36) NULL FK → `recipes.id` ON DELETE SET NULL | NULL for category aliases |
| `category_id` | VARCHAR(36) NULL FK → `categories.id` ON DELETE SET NULL | NULL for recipe aliases |
| `has_recipe` | TINYINT(1) GENERATED VIRTUAL | `(recipe_id IS NOT NULL)` — helper for the exactly-one check; not writable |
| `has_category` | TINYINT(1) GENERATED VIRTUAL | `(category_id IS NOT NULL)` — helper for the exactly-one check; not writable |
| `created_at` | DATETIME NOT NULL | server `CURRENT_TIMESTAMP` |

**Exactly-one-of rule (spec §3.5: "CHECK exactly one of the two is non-NULL"):** MariaDB CHECK constraints skip rows with NULLs, so the "both NULL" case can't be written directly. Locked mechanism — **two generated columns**, one per forbidden case, each checked:

- `has_recipe` = `(recipe_id IS NOT NULL)` → TINYINT(1) GENERATED ALWAYS AS (…) VIRTUAL
- `has_category` = `(category_id IS NOT NULL)` → TINYINT(1) GENERATED ALWAYS AS (…) VIRTUAL
- `CONSTRAINT chk_alias_exactly_one CHECK (NOT (has_recipe = 1 AND has_category = 1) AND NOT (has_recipe = 0 AND has_category = 0))`

This rejects both "both set" and "neither set"; a clean single-target row passes. (The two helper columns are `GENERATED … VIRTUAL` → not stored, invisible to the ORM's INSERT column list — Alembic emits them in the DDL, not in data rows.)

FK delete rules: **SET NULL** on both (spec §3.5 gives no rule; SET NULL is the least-destructive for D6's purpose — an alias is a redirect record, and deleting the target recipe/category is a data event the operator resolves by pointing the alias elsewhere; RESTRICT would make recipe deletion depend on stale redirects). The alias row itself is admin-managed (spec §7.4 role table) — app-level deletion is the read/admin plan's concern, not this one.

- [ ] **Step 1: Isolated DB pre-flight** — shared contract block.

- [ ] **Step 2: Add the `RecipeUrlAlias` model** (uses Task 1's `_uuid`, `DateTime` patterns; generated columns declared via `Computed`):

```python
class RecipeUrlAlias(Base):
    __tablename__ = "recipe_url_aliases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    path: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    recipe_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("recipes.id", ondelete="SET NULL"), nullable=True)
    category_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        sa.Computed("has_recipe", sa.text("(recipe_id IS NOT NULL)")),
        sa.Computed("has_category", sa.text("(category_id IS NOT NULL)")),
        sa.CheckConstraint(
            "NOT (has_recipe = 1 AND has_category = 1) AND NOT (has_recipe = 0 AND has_category = 0)",
            name="chk_alias_exactly_one",
        ),
        sa.UniqueConstraint("path", name="uq_aliases_path"),
        {"mysql_engine": "InnoDB"},
    )
```

- [ ] **Step 3: Write the failing tests** (`tests/test_aliases.py`):
  - `test_columns`: exactly `{id, path, recipe_id, category_id, has_recipe, has_category, created_at}`; `id varchar(36)`, `path varchar(255)`, `recipe_id varchar(36)`, `category_id varchar(36)`, `has_recipe/tinyint(1)` (MariaDB reports the generated boolean as `tinyint(1)`), `has_category tinyint(1)`, `created_at datetime`.
  - `test_indexes`: `PRIMARY == [id]`, `uq_aliases_path == [path]`.
  - `test_fks`: `recipe_id → recipes.id SET NULL`; `category_id → categories.id SET NULL`.
  - `test_recipe_alias_ok`: INSERT `(path='/\u002fp=1234', recipe_id=<probe recipe>, category_id=NULL)` → succeeds; read back; cleanup.
  - `test_category_alias_ok`: INSERT `(path='/\u002fpage_id=16', recipe_id=NULL, category_id='<seeded breakfast uuid>')` → succeeds; cleanup.
  - `test_both_targets_rejected`: INSERT with **both** ids set → `IntegrityError` (check `chk_alias_exactly_one` violation in the error text or constraint name where the driver exposes it).
  - `test_neither_target_rejected`: INSERT `(path='/\u002fp=9999', recipe_id=NULL, category_id=NULL)` → `IntegrityError`.
  - `test_duplicate_path_rejected`: same `path` twice (different targets) → `IntegrityError` on `uq_aliases_path`.
  - `test_set_null_recipe`: seeded category alias untouched; create probe recipe + its alias; `DELETE FROM recipes WHERE id=<probe>` → alias row survives with `recipe_id IS NULL`, now rejected-by-check… (expectation: after SET NULL fires, the row has both NULL → the check constraint would then be violated on **future** statements, but MariaDB applies CHECKs at write time only — the row persists. Assert: row exists, `recipe_id IS NULL`. This is the documented operator-resolution case; note it in a test comment so a future FK-rule change is a conscious edit, not a surprise.)

- [ ] **Step 4: Migration `<r4>_v4_url_aliases.py`** — generate; align DDL: the two `GENERATED ALWAYS AS (…)` VIRTUAL columns, the named `CHECK chk_alias_exactly_one`, `uq_aliases_path`, both SET NULL FKs (named `recfk_aliases_recipe` / `recfk_aliases_category` per Task 1 convention). Apply: v0→…→v4 clean.

- [ ] **Step 5: Run tests** — `env $CT .venv/bin/pytest tests/test_aliases.py -v` → all pass. Cross-check `SHOW CREATE TABLE recipe_url_aliases` (generated columns + CHECK must appear verbatim).

- [ ] **Step 6: Tear down, prove dev stack untouched, commit**:

```bash
git add src/cookbook/models.py alembic/versions/<r4>_v4_url_aliases.py tests/test_aliases.py
git commit -m "feat: v4 recipe_url_aliases — legacy URL retention with exactly-one target (spec §3.5, D6)"
```

---

## Task 5: Migration audit — `migration_runs` + `migration_recipe_log` (spec §3.6, D8)

**Branch:** `task/6.5-migration-audit` (cut from `main` after Task 4 is merged).

**Files:**
- Modify: `src/cookbook/models.py`
- Create: `alembic/versions/<r5>_v5_migration_audit.py`
- Create: `tests/test_migration_audit.py`

**Interfaces:**
- Consumes: `Recipe`, `Category` (not strictly — the log's `recipe_id` FK is to `recipes`); `User` (v0, for `operator_id`); isolated-DB contract
- Produces: `cookbook.models.MigrationRun`, `cookbook.models.MigrationRecipeLog`; revision `<r5>` with `down_revision = "<r4>"`. The §5.2 importer (import plan) writes here; the §5.3 verification gate reads `migration_runs` by run id. This task locks the shape only — no CLI.

**Locked table shapes (spec §3.6 column sets, verbatim):**

`migration_runs`:

| column | type | notes |
|---|---|---|
| `id` | VARCHAR(36) PK | `_uuid` default |
| `started_at` | DATETIME NOT NULL | server `CURRENT_TIMESTAMP` on create |
| `finished_at` | DATETIME NULL | set by the importer when the run ends |
| `source_file` | VARCHAR(500) NULL | WXR path as given on the CLI (`--file`); NULL only for non-file runs (none in v1, but the spec's column is unconditional) |
| `file_sha256` | CHAR(64) NULL | **CHAR, not VARCHAR** — fixed-length hash keeps its spec-defined `CHAR` type (DL-5's explicit exception) |
| `status` | ENUM('running','completed','failed') NOT NULL | spec §3.6's literal set as a DB-checked enum (importer's state machine, not app logic — locking the values at the schema stops typos from being silent) |
| `recipes_total` | INT NOT NULL DEFAULT 0 | |
| `recipes_imported` | INT NOT NULL DEFAULT 0 | |
| `recipes_skipped` | INT NOT NULL DEFAULT 0 | |
| `recipes_errors` | INT NOT NULL DEFAULT 0 | |
| `images_ok` | INT NOT NULL DEFAULT 0 | |
| `images_failed` | INT NOT NULL DEFAULT 0 | |
| `operator_id` | VARCHAR(36) NULL FK → `users.id` ON DELETE SET NULL | attribution only (D10), same rule as `recipes.created_by` |
| `notes` | TEXT NULL | |

`migration_recipe_log`:

| column | type | notes |
|---|---|---|
| `id` | VARCHAR(36) PK | `_uuid` default |
| `run_id` | VARCHAR(36) NOT NULL FK → `migration_runs.id` ON DELETE CASCADE | per-run rows die with the run; no `created_at` (spec §3.6 lists none) |
| `source_post_id` | VARCHAR(64) NULL | WP post id — width matches `recipes.source_id` (spec §3.1 says VARCHAR(64) for source ids; §3.6 is explicit on 64 here) |
| `recipe_id` | VARCHAR(36) NULL FK → `recipes.id` ON DELETE SET NULL | NULL for rows that never produced a recipe (skipped-before-insert, errors) |
| `outcome` | ENUM('imported','skipped','error') NOT NULL | spec §3.6's literal set |
| `detail` | TEXT NULL | free-form (D5 idempotency resume evidence, image path on import, error text) |

`no migration_tag_log` — spec §3.6 says it is not needed (v1 source has no tags); do **not** create it.

- [ ] **Step 1: Isolated DB pre-flight** — shared contract block.

- [ ] **Step 2: Add both models** to `src/cookbook/models.py` (Task 1/4 conventions; `BigInteger` not needed — counts fit `Integer`; `ENUM` values exactly as locked; `server_default=text("0")` on the six INT columns):

```python
class MigrationRun(Base):
    __tablename__ = "migration_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_file: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)  # DL-5's CHAR exception
    status: Mapped[str] = mapped_column(Enum("running", "completed", "failed", name="migration_run_status"), nullable=False, default="running")
    recipes_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    recipes_imported: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    recipes_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    recipes_errors: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    images_ok: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    images_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    operator_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class MigrationRecipeLog(Base):
    __tablename__ = "migration_recipe_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("migration_runs.id", ondelete="CASCADE"), nullable=False, index=True)
    source_post_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recipe_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("recipes.id", ondelete="SET NULL"), nullable=True)
    outcome: Mapped[str] = mapped_column(Enum("imported", "skipped", "error", name="migration_log_outcome"), nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
```

(Add `CHAR` to the sqlalchemy import line if not already present.)

- [ ] **Step 3: Write the failing tests** (`tests/test_migration_audit.py`):
  - `test_runs_columns`: exactly the 14 columns above; `file_sha256` type **`char(64)`** (not varchar — this is DL-5's exception under test); the six INT columns `int(11)` NOT NULL with default `0`; `status` enum type string; `operator_id varchar(36)` NULL.
  - `test_runs_indexes`: `PRIMARY == [id]`.
  - `test_runs_fks`: `operator_id → users.id SET NULL`.
  - `test_log_columns`: exactly `{id, run_id, source_post_id, recipe_id, outcome, detail}`; `run_id varchar(36)` NOT NULL; `source_post_id varchar(64)` NULL; `recipe_id varchar(36)` NULL; `outcome` enum; `detail text` NULL.
  - `test_log_indexes`: `PRIMARY == [id]` + the `run_id` FK index.
  - `test_log_fks`: `run_id → migration_runs.id CASCADE`; `recipe_id → recipes.id SET NULL`.
  - `test_status_values_locked`: INSERT run with `status='running'` → ok; `'completed'` → ok; `'failed'` → ok; `'bogus'` → `IntegrityError` (enum).
  - `test_outcome_values_locked`: same probe against `outcome` (`imported`/`skipped`/`error` ok; `'unknown'` → `IntegrityError`).
  - `test_run_cascades_logs`: probe run + 3 log rows; delete the run → logs gone (baseline restored) — proves DELETE CASCADE before the *importer* code ever exercises it.
  - `test_integrity_counts_roundtrip`: insert a run with explicit counts (total=5, imported=3, skipped=1, errors=1, images_ok=2, images_failed=1); read back field-by-field — the column set the §5.3 gate script will assert against is exactly these names.
  - `test_no_tag_log_table`: `SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='migration_tag_log'` → `0` (spec §3.6: "not needed"; this test is the guard against a later "convenient" addition without a decision).

- [ ] **Step 4: Migration `<r5>_v5_migration_audit.py`** — generate; align DDL to the two locked tables (FK names `recfk_run_operator`, `recfk_log_run`, `recfk_log_recipe`; enum names `migration_run_status` / `migration_log_outcome`; defaults exactly as above). Apply: v0→…→v5 clean.

- [ ] **Step 5: Run tests** — `env $CT .venv/bin/pytest tests/test_migration_audit.py -v` → all pass; `SHOW CREATE TABLE` cross-check both tables.

- [ ] **Step 6: Tear down, prove dev stack untouched, commit**:

```bash
git add src/cookbook/models.py alembic/versions/<r5>_v5_migration_audit.py tests/test_migration_audit.py
git commit -m "feat: v5 migration audit — runs + recipe log (spec §3.6, D8)"
```

---

## Task 6: Full-chain verification + schema inventory + merge gate

**Branch:** `task/6.6-chain-verify` (cut from `main` after Task 5 is merged). This is the only task that runs **all** five revisions in one fresh database — the gate that proves the chain composes, not just each link.

**Files:**
- Create: `tests/test_schema_inventory.py`
- No model or migration changes: if this task requires touching DDL, stop — that failure means a prior task diverged from its locked shape; fix the offender on its own branch first.

**Interfaces:**
- Consumes: all of Tasks 1–5; isolated-DB contract
- Produces: proof that `v0 → v1 → v2 → v3 → v4 → v5` applies cleanly on a pristine schema; the locked 10-table inventory as a regression test; the merge checklist for owner approval.

- [ ] **Step 1: Fresh isolated DB from scratch** — shared-contract pre-flight, then apply the **entire** chain in one shot, recording each step:

```bash
env $CT .venv/bin/alembic upgrade head   # must print all five revisions, none skipped, no re-runs
env $CT .venv/bin/alembic current        # must equal the v5 revision
env $CT .venv/bin/alembic history        # v5 ← v4 ← v3 ← v2 ← v1 ← 6d22befb6ec9, exactly
```

- [ ] **Step 2: Locked inventory test** — `tests/test_schema_inventory.py`:

```python
EXPECTED_TABLES = {
    "users",                     # v0 — foundation, not re-asserted beyond presence
    "recipes",                   # Task 1
    "recipe_sections",           # Task 2
    "recipe_items",              # Task 2
    "categories",                # Task 3
    "recipe_categories",         # Task 3
    "tags",                      # Task 3
    "recipe_tags",               # Task 3
    "recipe_url_aliases",        # Task 4
    "migration_runs",            # Task 5
    "migration_recipe_log",      # Task 5
}

def test_exact_table_inventory():
    async def run():
        engine = get_engine()
        async with engine.begin() as conn:
            rows = (await conn.execute(
                text("SELECT table_name FROM information_schema.tables "
                     "WHERE table_schema = DATABASE() AND table_type = 'BASE TABLE')))
            tables = {r[0] for r in rows}
        await engine.dispose()
    # exact set match — any extra table (e.g. an accidental migration_tag_log)
    # or missing one fails here rather than later at import time
    assert EXPECTED_TABLES <= tables and tables <= EXPECTED_TABLES
```

(Exact-set assert both directions — the missing-table case is the obvious half; the extra-table half is what catches unreviewed DDL.)

- [ ] **Step 3: Cross-table consistency probes:**
  - **Full deep cascade**: probe recipe + 3 sections (one per kind) + 2 items each + membership in 2 seeded categories + 1 tag + 1 recipe alias + 1 audit log row (in a probe run) → `DELETE FROM recipes WHERE id = <probe>` → assert: `recipe_sections` and `recipe_items` back to baseline; `recipe_categories` rows for this recipe gone (the *other* seed-category rows for other recipes untouched); `recipe_tags` row gone; alias row survives with `recipe_id IS NULL` (Task 4's SET NULL, per its locked behavior); audit log row survives with `recipe_id IS NULL` (Task 5's SET NULL). One delete, every rule verified at once.
  - **Seed stability after the full chain**: `SELECT slug, position FROM categories ORDER BY position` still exactly the 8 DL-4 rows — proves the v5 migration didn't perturb v3's data.
  - **ID-width invariant**: query `information_schema.columns` for every column of every table in the inventory; assert every column named `id` or ending `_id` that is a UUID FK/PK is `varchar(36)`, and the **only** `char(64)` column in the whole schema is `migration_runs.file_sha256` (DL-5's single exception — the auth plan will add `sessions.id_hash` etc. as char-64 by explicit later decision; this test is the guard that keeps that door in the auth plan, not this one).

- [ ] **Step 4: Full suite, one container** — with the Task 6 DB up:

```bash
env $CT .venv/bin/pytest tests/ -v        # every test from every task, same isolated DB
```

All green (the v0 `tests/test_models.py` users-shape test runs against the same instance — the v0 row shape is part of the chain proof).

- [ ] **Step 5: Prove the dev stack is untouched**, then **commit**:

```bash
docker rm -f cookbook-test-db
docker compose ps --format '{{.Name}} {{.Status}}' > /tmp/devstack-after.txt
diff /tmp/devstack-before.txt /tmp/devstack-after.txt && echo "dev stack untouched"
git add tests/test_schema_inventory.py
git commit -m "test: full-chain schema inventory + cross-table cascade/seed/id-width proofs"
```

---

## Merge order and owner gate

Merge sequence (each gate before the next):

1. `task/6.1-recipes` → 2. `task/6.2-recipe-body` → 3. `task/6.3-organization` → 4. `task/6.4-aliases` → 5. `task/6.5-migration-audit` → 6. `task/6.6-chain-verify`

Each merge requires owner approval (branch-per-task; merge to `main` only on explicit owner direction). After 6.6 lands, `alembic upgrade head` against the **dev** `cookbook` schema is a deliberate owner action (it is the first time the running schema advances while the branch series is merged — D22 says dev data is disposable).

**Deliberately still out of scope** (owning plans): `sessions` / `login_attempts` (auth plan); FULLTEXT indexes and tokenizer choice (search plan, O5 — this plan left `title`, `description`, `raw_text` plain-index-able and added no FULLTEXT); the `Others` category row (import plan, DL-4); the 301 middleware and the read surfaces that *consume* the locked orderings (read plan); the importer CLI and §5.3 gate script (import plan).

**Open decisions carried forward:** O5 — FULLTEXT/index strategy, benchmarked on the migrated corpus in the search plan; nothing in this plan blocks on it. If the owner wants owner-ordered tags (ordering rule 6), that is a new migration under the same task/branch discipline.
