"""SQLAlchemy declarative base + v0 models.

v0 carries only the ``users`` table — enough to exercise the Alembic ->
MariaDB migrations path in the foundation plan. All other models land in the
data-model plan. PKs are VARCHAR(36) UUIDv4 (spec section 3, DL-5),
DATETIME UTC.
"""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    CHAR,
    DECIMAL,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    username: Mapped[str] = mapped_column(String(60), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    role: Mapped[str] = mapped_column(String(10), nullable=False, default="editor")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username}>"


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
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("source_system", "source_id", name="uq_recipes_source"),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Recipe {self.slug!r}>"


class RecipeSection(Base):
    """Ordered body block for a recipe (DL-3: `heading?` + `kind` + `lines[]`).

    `kind` is a native MariaDB ENUM('ingredients','steps','note') — invalid
    literals are rejected at the DB layer (strict mode), not the app.
    `position` is UNIQUE within a recipe (`uq_sections_recipe_position`);
    dense 1..N is a future save-layer invariant, not a DB guarantee.
    No timestamps: body rows are wholesale-replaced on save (later plan);
    provenance lives on `recipes`.
    """
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
        UniqueConstraint("recipe_id", "position", name="uq_sections_recipe_position"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RecipeSection {self.recipe_id}/{self.position} {self.kind}>"


class RecipeItem(Base):
    """One ordered line inside a `recipe_sections` row (D4, extended by DL-3).

    `raw_text` is the source of truth — NOT NULL for every section kind.
    The four `quantity_text` / `unit` / `item` / `preparation` columns are
    best-effort parses, populated only for `kind='ingredients'` lines but
    defined uniformly (nullable) so the reader does not special-case.
    `position` is UNIQUE within a section (`uq_items_section_position`);
    dense 1..M is a future save-layer invariant, not a DB guarantee.
    No timestamps: see `RecipeSection`.
    """
    __tablename__ = "recipe_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    section_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("recipe_sections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False)
    quantity_text: Mapped[str | None] = mapped_column(String(50), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(50), nullable=True)
    item: Mapped[str | None] = mapped_column(String(300), nullable=True)
    preparation: Mapped[str | None] = mapped_column(String(300), nullable=True)

    __table_args__ = (
        UniqueConstraint("section_id", "position", name="uq_items_section_position"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RecipeItem {self.section_id}/{self.position}>"


class Category(Base):
    """A recipe grouping the owner can manage (DL-4: categories are data).

    `position` is a dense table-wide order (`uq_categories_position`) driving
    the home "by category" lane (DL-4 owner-manageable); dense 1..K is a
    future save-layer invariant, not a DB trigger (ordering rule 3).
    No timestamps: categories are order + identity, not audit rows.
    An in-use category cannot be deleted (`recipe_categories.category_id`
    uses ON DELETE RESTRICT — DL-6).
    """
    __tablename__ = "categories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint("slug", name="uq_categories_slug"),
        UniqueConstraint("position", name="uq_categories_position"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Category {self.slug!r} pos={self.position}>"


class Tag(Base):
    """A free-form semantic tag (D13). No `position` in v1 (ordering rule 6:
    tags carry no owner order; the "top semantic tags" ranking is a read/query
    concern). No timestamps. Deleting a tag in use is allowed and removes its
    memberships (`recipe_tags.tag_id` uses ON DELETE CASCADE — DL-7)."""

    __tablename__ = "tags"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    slug: Mapped[str] = mapped_column(String(200), nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    __table_args__ = (
        UniqueConstraint("slug", name="uq_tags_slug"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tag {self.slug!r}>"


# Many-to-many joins. Plain association tables on Base.metadata (no ORM
# relationship per the v1 scope — no relationship()/backref/back_populates).
# PK is (recipe_id, <target>_id) in that order, so recipe_id is the leading
# PK column and needs no secondary index. The non-leading target FK column
# gets an explicit non-unique index (ix_<table>_<target>_id), mirroring the
# v2 convention where the PK-leading FK column carries no redundant index.
# Dialect options are passed as keyword arguments to Table(), per convention.
recipe_categories = Table(
    "recipe_categories",
    Base.metadata,
    Column("recipe_id", String(36),
           ForeignKey("recipes.id", name="recfk_recipe_categories_recipe",
                      ondelete="CASCADE"),
           primary_key=True),
    Column("category_id", String(36),
           ForeignKey("categories.id", name="recfk_recipe_categories_category",
                      ondelete="RESTRICT"),
           primary_key=True),
    Index("ix_recipe_categories_category_id", "category_id"),
    mysql_engine="InnoDB", mysql_charset="utf8mb4",
)

recipe_tags = Table(
    "recipe_tags",
    Base.metadata,
    Column("recipe_id", String(36),
           ForeignKey("recipes.id", name="recfk_recipe_tags_recipe",
                      ondelete="CASCADE"),
           primary_key=True),
    Column("tag_id", String(36),
           ForeignKey("tags.id", name="recfk_recipe_tags_tag",
                      ondelete="CASCADE"),
           primary_key=True),
    Index("ix_recipe_tags_tag_id", "tag_id"),
    mysql_engine="InnoDB", mysql_charset="utf8mb4",
)


class RecipeUrlAlias(Base):
    """A legacy URL (e.g. ``/?p=1234``, ``/?page_id=16``) mapped to exactly one
    existing recipe OR category (spec §3.5, D6/D17).

    ``path`` is the full legacy path as observed — never normalized or slugified
    at write time (the observed string is what must match on redirect). The
    app-side 301 middleware (read plan) reads this table by ``path``; nothing in
    this module serves HTTP.

    Exactly-one-target rule (DL-6, owner-approved): the named
    ``chk_alias_exactly_one`` CHECK requires that exactly one of ``recipe_id`` /
    ``category_id`` is non-NULL (it rejects both set and neither set). Both FKs
    use ``ON DELETE RESTRICT`` so an alias always retains a valid target; a
    referenced recipe/category cannot be deleted until its aliases are
    deliberately reassigned. Tag deletion policy (DL-7) does not touch this table.
    No generated or helper columns; ``created_at`` is server ``CURRENT_TIMESTAMP``.
    """
    __tablename__ = "recipe_url_aliases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    path: Mapped[str] = mapped_column(String(255), nullable=False)
    recipe_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("recipes.id", name="recfk_aliases_recipe",
                               ondelete="RESTRICT"), nullable=True)
    category_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("categories.id", name="recfk_aliases_category",
                               ondelete="RESTRICT"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now())

    __table_args__ = (
        CheckConstraint(
            "(recipe_id IS NOT NULL) <> (category_id IS NOT NULL)",
            name="chk_alias_exactly_one",
        ),
        UniqueConstraint("path", name="uq_aliases_path"),
        Index("ix_recipe_url_aliases_recipe_id", "recipe_id"),
        Index("ix_recipe_url_aliases_category_id", "category_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<RecipeUrlAlias {self.path!r}>"


class MigrationRun(Base):
    """One importer run (spec §3.6, D8). The §5.2 importer writes a row per
    invocation; the §5.3 verification gate reads this by run id. This model
    locks the audit SHAPE only — there is no CLI/scheduler here.

    ``status`` is a native MariaDB ENUM (``running``|``completed``|``failed``) —
    the importer's state machine, checked at the DB layer (strict mode) so a typo
    cannot be silent. It has a PYTHON default of ``'running'`` (a run starts
    running) but NO server default: the importer always supplies the status it
    wants (or relies on the Python default on insert).

    The six INT counters carry a server default of 0 so a raw SQL insert that
    omits them still lands a consistent zero (proven independently of the Python
    default by the audit tests). ``file_sha256`` is ``CHAR(64)`` — DL-5's explicit
    fixed-length-hash exception. ``operator_id`` is attribution-only (D10), SET
    NULL on user delete. No timestamps beyond the server-default ``started_at``.
    """
    __tablename__ = "migration_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    source_file: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    status: Mapped[str] = mapped_column(
        Enum("running", "completed", "failed", name="migration_run_status"),
        nullable=False, default="running")
    recipes_total: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"))
    recipes_imported: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"))
    recipes_skipped: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"))
    recipes_errors: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"))
    images_ok: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"))
    images_failed: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"))
    operator_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", name="recfk_run_operator",
                               ondelete="SET NULL"), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_migration_runs_operator_id", "operator_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MigrationRun {self.id} {self.status}>"


class MigrationRecipeLog(Base):
    """Per-source-post outcome of one migration run (spec §3.6, D8).

    Rows ``CASCADE`` with their run (per-run rows die with the run). ``recipe_id``
    is NULL for rows that never produced a recipe (skipped-before-insert, errors)
    and uses ``SET NULL`` on recipe delete so an audit trail is never destroyed —
    only its recipe reference is cleared. ``outcome`` is a native ENUM
    (``imported``|``skipped``|``error``). No ``created_at`` — spec §3.6 lists none.
    """
    __tablename__ = "migration_recipe_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("migration_runs.id", name="recfk_log_run",
                               ondelete="CASCADE"), nullable=False)
    source_post_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    recipe_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("recipes.id", name="recfk_log_recipe",
                               ondelete="SET NULL"), nullable=True)
    outcome: Mapped[str] = mapped_column(
        Enum("imported", "skipped", "error", name="migration_log_outcome"),
        nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    __table_args__ = (
        Index("ix_migration_recipe_log_run_id", "run_id"),
        Index("ix_migration_recipe_log_recipe_id", "recipe_id"),
        {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"},
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<MigrationRecipeLog {self.run_id}/{self.outcome}>"
