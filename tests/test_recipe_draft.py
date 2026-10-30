"""Shared recipe draft contract tests; these do not require a database."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from cookbook.recipes import (
    RecipeDraft,
    RecipeIngredientDraft,
    RecipeProvenanceDraft,
    RecipeSectionDraft,
    RecipeStepDraft,
)


def test_sparse_draft_is_valid_and_optional_fields_default_empty() -> None:
    draft = RecipeDraft(title="")

    assert draft.title == ""
    assert draft.description is None
    assert draft.sections == []
    assert draft.tags == []
    assert draft.suggested_categories == []
    assert draft.parse_notes == []
    assert draft.already_exists is None


def test_draft_preserves_unicode_raw_text_and_ingredient_parse_fields() -> None:
    ingredient = RecipeIngredientDraft(
        raw_text="½ cup crème fraîche, divided",
        quantity_text="½",
        unit="cup",
        item="crème fraîche",
        preparation="divided",
    )

    assert ingredient.model_dump() == {
        "raw_text": "½ cup crème fraîche, divided",
        "quantity_text": "½",
        "unit": "cup",
        "item": "crème fraîche",
        "preparation": "divided",
    }


def test_sections_preserve_heading_kind_and_line_order() -> None:
    filling = RecipeSectionDraft(
        heading="Filling",
        kind="ingredients",
        lines=[
            RecipeIngredientDraft(raw_text="2 apples"),
            RecipeIngredientDraft(raw_text="1 tsp cinnamon"),
        ],
    )
    directions = RecipeSectionDraft(
        kind="steps",
        lines=[RecipeStepDraft(body="Peel the apples."), RecipeStepDraft(body="Add cinnamon.")],
    )
    draft = RecipeDraft(title="Apple pie", sections=[filling, directions])

    assert [section.kind for section in draft.sections] == ["ingredients", "steps"]
    assert draft.sections[0].heading == "Filling"
    assert [line.raw_text for line in draft.sections[0].lines] == [
        "2 apples",
        "1 tsp cinnamon",
    ]
    assert [line.body for line in draft.sections[1].lines] == [
        "Peel the apples.",
        "Add cinnamon.",
    ]


def test_section_kind_is_limited_to_supported_body_kinds() -> None:
    with pytest.raises(ValidationError):
        RecipeSectionDraft(kind="garnish", lines=[])


def test_section_lines_must_match_the_section_kind() -> None:
    with pytest.raises(ValidationError):
        RecipeSectionDraft(kind="ingredients", lines=[RecipeStepDraft(body="Mix.")])

    with pytest.raises(ValidationError):
        RecipeSectionDraft(
            kind="steps",
            lines=[RecipeIngredientDraft(raw_text="1 cup flour")],
        )


def test_provenance_and_parse_notes_survive_draft_construction() -> None:
    imported_at = datetime(2026, 10, 30, 12, 0)
    provenance = RecipeProvenanceDraft(
        source_system="web",
        source_id=None,
        source_url="https://example.test/recipe",
        source_name="Example Kitchen",
        extraction_method="jsonld",
        imported_at=imported_at,
    )
    draft = RecipeDraft(
        title="Soup",
        provenance=provenance,
        parse_notes=["The source omitted cooking time."],
        tags=["comfort food"],
        suggested_categories=["Dinner"],
        hero_image="https://example.test/soup.jpg",
        servings_number=Decimal("4"),
    )

    assert draft.provenance == provenance
    assert draft.parse_notes == ["The source omitted cooking time."]
    assert draft.tags == ["comfort food"]
    assert draft.suggested_categories == ["Dinner"]
    assert draft.hero_image == "https://example.test/soup.jpg"
    assert draft.servings_number == Decimal("4")


def test_wordpress_import_provenance_uses_wxr_extraction_method() -> None:
    provenance = RecipeProvenanceDraft(
        source_system="wordpress",
        source_id="1869",
        extraction_method="wxr",
    )

    assert provenance.extraction_method == "wxr"


def test_duplicate_marker_is_optional_and_keeps_public_recipe_details() -> None:
    draft = RecipeDraft(
        title="Adapted recipe",
        already_exists={"id": "recipe-123", "title": "Original recipe"},
    )

    assert draft.already_exists is not None
    assert draft.already_exists.id == "recipe-123"
    assert draft.already_exists.title == "Original recipe"


def test_draft_contract_is_exported_without_web_app_import() -> None:
    import cookbook.recipes as recipes

    assert recipes.RecipeDraft is RecipeDraft
    assert recipes.RecipeIngredientDraft is RecipeIngredientDraft
    assert recipes.RecipeStepDraft is RecipeStepDraft
    assert recipes.RecipeSectionDraft is RecipeSectionDraft
    assert recipes.RecipeProvenanceDraft is RecipeProvenanceDraft
