"""Save-readiness validation for the shared RecipeDraft (spec §8, plan Task 2).

Pure and deterministic: no database access, no web framework imports.
Raw line text is the source of truth (DL-3), so validation only trims,
deduplicates, and removes blank lines/empty sections — never rewrites text.
"""
from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from pydantic import BaseModel, Field

from .drafts import (
    RecipeDraft,
    RecipeIngredientDraft,
    RecipeProvenanceDraft,
    RecipeSectionDraft,
    RecipeStepDraft,
)


class ValidationWarning(BaseModel):
    """A nonblocking issue reported alongside an otherwise valid save."""

    field: str
    message: str


class RecipeSaveData(BaseModel):
    """Normalized, save-ready view of a validated draft.

    Nonblank ingredient/step lines are kept exactly as supplied; only
    blank lines and sections left empty after that are dropped.
    """

    title: str
    description: str | None = None
    category_names: list[str]
    tags: list[str]
    sections: list[RecipeSectionDraft]
    servings_text: str | None = None
    servings_number: Decimal | None = None
    servings_unit: str | None = None
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    warnings: list[ValidationWarning] = Field(default_factory=list)
    total_minutes: int | None = None
    hero_image: str | None = None
    provenance: RecipeProvenanceDraft | None = None


class RecipeValidationError(Exception):
    """Raised when a draft cannot be saved yet.

    ``field_errors`` maps stable field names (``title``, ``categories``,
    ``ingredients``) to human-readable messages so later layers (forms,
    HTTP responses) can address problems without parsing text.
    """

    def __init__(self, field_errors: dict[str, str]) -> None:
        self.field_errors = field_errors
        super().__init__("; ".join(f"{field}: {message}" for field, message in field_errors.items()))


def _dedupe_and_trim_names(names: Sequence[str]) -> list[str]:
    """Trim each name, drop blanks, and keep the first occurrence of each."""
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        trimmed = name.strip()
        if not trimmed or trimmed in seen:
            continue
        seen.add(trimmed)
        unique.append(trimmed)
    return unique


def _line_text(line: RecipeIngredientDraft | RecipeStepDraft) -> str:
    """Source text of a line, whichever of the two line types it is."""
    if isinstance(line, RecipeIngredientDraft):
        return line.raw_text
    return line.body


def validate_for_save(
    draft: RecipeDraft,
    *,
    category_names: Sequence[str],
    allow_missing_ingredients_for_import: bool = False,
) -> RecipeSaveData:
    """Validate a draft for saving and return normalized save data.

    Raises RecipeValidationError carrying every field error found
    (all errors are reported, not just the first).
    """
    field_errors: dict[str, str] = {}

    title = draft.title.strip()
    if not title:
        field_errors["title"] = "A recipe title is required."

    resolved_categories = _dedupe_and_trim_names(category_names)
    if not resolved_categories:
        field_errors["categories"] = "At least one category is required."

    sections: list[RecipeSectionDraft] = []
    has_ingredient = False
    has_step = False
    for section in draft.sections:
        kept_lines = [line for line in section.lines if _line_text(line).strip()]
        if not kept_lines:
            # Drop sections that end up with no content (DL-3 blank lines).
            continue
        # Rebuild the section so the result never references dropped lines,
        # while the surviving line objects (and their raw text) are untouched.
        sections.append(
            RecipeSectionDraft(heading=section.heading, kind=section.kind, lines=kept_lines)
        )
        if section.kind == "ingredients":
            has_ingredient = True
        elif section.kind == "steps":
            has_step = True

    if not has_ingredient and not allow_missing_ingredients_for_import:
        field_errors["ingredients"] = "At least one ingredient line is required."

    if field_errors:
        raise RecipeValidationError(field_errors)

    warnings: list[ValidationWarning] = []
    if not has_step:
        warnings.append(
            ValidationWarning(field="steps", message="No preparation steps were provided.")
        )

    # Spec §8: an explicit total wins; otherwise sum only when both
    # components exist; never invent a missing value.
    if draft.total_minutes is not None:
        total_minutes: int | None = draft.total_minutes
    elif draft.prep_minutes is not None and draft.cook_minutes is not None:
        total_minutes = draft.prep_minutes + draft.cook_minutes
    else:
        total_minutes = None

    return RecipeSaveData(
        title=title,
        description=draft.description,
        category_names=resolved_categories,
        tags=_dedupe_and_trim_names(draft.tags),
        sections=sections,
        servings_text=draft.servings_text,
        servings_number=draft.servings_number,
        servings_unit=draft.servings_unit,
        prep_minutes=draft.prep_minutes,
        cook_minutes=draft.cook_minutes,
        warnings=warnings,
        total_minutes=total_minutes,
        hero_image=draft.hero_image,
        provenance=draft.provenance,
    )
