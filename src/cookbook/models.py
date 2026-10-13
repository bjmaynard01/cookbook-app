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
    DECIMAL,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
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
