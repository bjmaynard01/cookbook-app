"""Save-readiness validation tests; no database or web application required."""
from __future__ import annotations

from datetime import datetime

import pytest

from cookbook.recipes import (
    RecipeDraft,
    RecipeIngredientDraft,
    RecipeProvenanceDraft,
    RecipeSectionDraft,
    RecipeStepDraft,
)
from cookbook.recipes.validation import (
    RecipeValidationError,
    validate_for_save,
)


def test_save_validation_trims_title_and_deduplicates_names_without_changing_raw_text() -> None:
    ingredient_text = "  1 cup flour  "
    step_text = "  Mix gently.  "
    draft = RecipeDraft(
        title="  Pancakes\n",
        sections=[
            RecipeSectionDraft(
                kind="ingredients",
                lines=[RecipeIngredientDraft(raw_text=ingredient_text)],
            ),
            RecipeSectionDraft(
                kind="steps",
                lines=[RecipeStepDraft(body=step_text)],
            ),
        ],
        tags=[" brunch ", "brunch", ""],
    )

    result = validate_for_save(
        draft,
        category_names=[" Breakfast ", "", "Breakfast"],
    )

    assert result.title == "Pancakes"
    assert result.category_names == ["Breakfast"]
    assert result.tags == ["brunch"]
    assert result.sections[0].lines[0].raw_text == ingredient_text
    assert result.sections[1].lines[0].body == step_text


@pytest.mark.parametrize("title", ["", "   ", "\t\n"])
def test_save_rejects_blank_title(title: str) -> None:
    draft = RecipeDraft(title=title)

    with pytest.raises(RecipeValidationError) as raised:
        validate_for_save(draft, category_names=["Dinner"])

    assert "title" in raised.value.field_errors


def test_save_reports_missing_category_and_ingredient_together() -> None:
    draft = RecipeDraft(title="Soup")

    with pytest.raises(RecipeValidationError) as raised:
        validate_for_save(draft, category_names=["", "  "])

    assert set(raised.value.field_errors) == {"categories", "ingredients"}


def test_import_exception_can_preserve_a_source_post_without_ingredient_lines() -> None:
    draft = RecipeDraft(
        title="Oven Baked Tacos",
        sections=[RecipeSectionDraft(kind="steps", lines=[RecipeStepDraft(body="Bake until hot.")])],
    )

    with pytest.raises(RecipeValidationError):
        validate_for_save(draft, category_names=["Main Dishes"])

    saved = validate_for_save(
        draft, category_names=["Main Dishes"], allow_missing_ingredients_for_import=True,
    )
    assert saved.title == "Oven Baked Tacos"
    assert saved.sections[0].lines[0].body == "Bake until hot."


def test_blank_ingredient_lines_do_not_satisfy_required_ingredient() -> None:
    draft = RecipeDraft(
        title="Soup",
        sections=[
            RecipeSectionDraft(
                kind="ingredients",
                lines=[RecipeIngredientDraft(raw_text=" \t ")],
            ),
        ],
    )

    with pytest.raises(RecipeValidationError) as raised:
        validate_for_save(draft, category_names=["Dinner"])

    assert "ingredients" in raised.value.field_errors


def test_blank_lines_are_removed_but_nonblank_raw_text_is_preserved() -> None:
    ingredient_text = "  2 onions  "
    draft = RecipeDraft(
        title="Stew",
        sections=[
            RecipeSectionDraft(
                heading="Base",
                kind="ingredients",
                lines=[
                    RecipeIngredientDraft(raw_text="   "),
                    RecipeIngredientDraft(raw_text=ingredient_text),
                ],
            ),
            RecipeSectionDraft(
                kind="steps",
                lines=[RecipeStepDraft(body=" \n ")],
            ),
        ],
    )

    result = validate_for_save(draft, category_names=["Dinner"])

    assert len(result.sections) == 1
    assert result.sections[0].heading == "Base"
    assert len(result.sections[0].lines) == 1
    assert result.sections[0].lines[0].raw_text == ingredient_text
    assert any(warning.field == "steps" for warning in result.warnings)


def test_missing_steps_are_allowed_and_reported_as_a_warning() -> None:
    draft = RecipeDraft(
        title="Fermentation starter",
        sections=[
            RecipeSectionDraft(
                kind="ingredients",
                lines=[RecipeIngredientDraft(raw_text="Flour and water")],
            ),
        ],
    )

    result = validate_for_save(draft, category_names=["Basics"])

    assert result.sections[0].kind == "ingredients"
    assert [(warning.field, warning.message) for warning in result.warnings] == [
        ("steps", "No preparation steps were provided.")
    ]


@pytest.mark.parametrize(
    ("prep", "cook", "explicit", "expected"),
    [
        (10, 20, None, 30),
        (10, 20, 45, 45),
        (10, None, None, None),
        (None, None, 0, 0),
    ],
)
def test_total_minutes_follow_server_save_rule(
    prep: int | None,
    cook: int | None,
    explicit: int | None,
    expected: int | None,
) -> None:
    draft = RecipeDraft(
        title="Timed recipe",
        prep_minutes=prep,
        cook_minutes=cook,
        total_minutes=explicit,
        sections=[
            RecipeSectionDraft(
                kind="ingredients",
                lines=[RecipeIngredientDraft(raw_text="1 ingredient")],
            ),
        ],
    )

    result = validate_for_save(draft, category_names=["Dinner"])

    assert result.total_minutes == expected


def test_provenance_is_retained_unchanged_by_save_validation() -> None:
    provenance = RecipeProvenanceDraft(
        source_system="wordpress",
        source_id="1869",
        source_url="https://example.test/recipe",
        source_name="Example Kitchen",
        extraction_method="wxr",
        imported_at=datetime(2026, 10, 30, 12, 0),
    )
    draft = RecipeDraft(
        title="Imported recipe",
        provenance=provenance,
        sections=[
            RecipeSectionDraft(
                kind="ingredients",
                lines=[RecipeIngredientDraft(raw_text="1 ingredient")],
            ),
        ],
    )

    result = validate_for_save(draft, category_names=["Dinner"])

    assert result.provenance == provenance
    assert result.provenance is provenance


def test_save_data_retains_recipe_fields_needed_by_the_persistence_layer() -> None:
    draft = RecipeDraft(
        title="Cake",
        description="A family cake.",
        servings_text="Serves 8",
        servings_number=8,
        servings_unit="people",
        prep_minutes=15,
        cook_minutes=30,
        hero_image="uploads/cake.webp",
        sections=[
            RecipeSectionDraft(
                kind="ingredients",
                lines=[RecipeIngredientDraft(raw_text="2 cups flour")],
            ),
        ],
    )

    result = validate_for_save(draft, category_names=["Dessert"])

    assert result.description == "A family cake."
    assert result.servings_text == "Serves 8"
    assert result.servings_number == 8
    assert result.servings_unit == "people"
    assert result.prep_minutes == 15
    assert result.cook_minutes == 30
    assert result.hero_image == "uploads/cake.webp"
