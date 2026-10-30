"""Pydantic draft contract shared by ingest, preview, and save (spec §4, DL-3).

These models are a transport contract only: no validation of save-readiness,
no database access, no app imports. A sparse draft is a valid state (D4), so
every field except ``title`` and line text is nullable or defaulted.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RecipeIngredientDraft(BaseModel):
    """One ingredients line. ``raw_text`` is the untouched source text (D4)."""

    raw_text: str
    quantity_text: str | None = None
    unit: str | None = None
    item: str | None = None
    preparation: str | None = None


class RecipeStepDraft(BaseModel):
    """One step (or note) line; the step body is its source text."""

    body: str


# Union so one section holds only its own line type, but a single list type
# works for every section kind. Pydantic's smart union matches on the
# required field (raw_text vs body), so LLM JSON dicts resolve correctly too.
SectionLine = Annotated[
    Union[RecipeIngredientDraft, RecipeStepDraft],
    Field(description="An ingredient line or a step/note line."),
]


class RecipeSectionDraft(BaseModel):
    """A named body section (DL-3): ordered lines under an optional heading."""

    heading: str | None = None
    kind: Literal["ingredients", "steps", "note"]
    lines: list[SectionLine] = Field(default_factory=list)

    @model_validator(mode="after")
    def lines_match_kind(self) -> RecipeSectionDraft:
        expected_type = RecipeIngredientDraft if self.kind == "ingredients" else RecipeStepDraft
        if any(not isinstance(line, expected_type) for line in self.lines):
            raise ValueError(f"{self.kind} sections contain incompatible line types")
        return self


class RecipeProvenanceDraft(BaseModel):
    """Where the draft came from; transport metadata, not a permission grant."""

    source_system: str | None = None
    source_id: str | None = None
    source_url: str | None = None
    source_name: str | None = None
    extraction_method: Literal["jsonld", "microdata", "llm", "manual", "wxr"] | None = None
    imported_at: datetime | None = None


class DuplicateRecipeDraft(BaseModel):
    """Non-blocking banner marker: a recipe from this source already exists."""

    id: str
    title: str


class RecipeDraft(BaseModel):
    """One contract object for every parse stage, the preview form, and save.

    Partial parse is valid here (D4): empty title and empty sections are
    accepted; save-readiness rules live in the Task 2 validator.
    """

    model_config = ConfigDict(extra="forbid")

    title: str
    description: str | None = None
    sections: list[RecipeSectionDraft] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    suggested_categories: list[str] = Field(default_factory=list)
    parse_notes: list[str] = Field(default_factory=list)
    servings_text: str | None = None
    servings_number: Decimal | None = None
    servings_unit: str | None = None
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    total_minutes: int | None = None
    hero_image: str | None = None
    provenance: RecipeProvenanceDraft | None = None
    already_exists: DuplicateRecipeDraft | None = None
