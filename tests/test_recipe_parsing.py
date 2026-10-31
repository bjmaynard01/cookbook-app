from __future__ import annotations

import json
from decimal import Decimal

import pytest

from cookbook.recipes.ingest import FetchedPage
from cookbook.recipes.parsing import parse_recipe_page
from cookbook.recipes.drafts import RecipeIngredientDraft, RecipeStepDraft
from cookbook.recipes import parse_recipe_page as exported_parse_recipe_page


def page(*, json_ld: tuple[str, ...] = (), html: str = "", **kwargs) -> FetchedPage:
    return FetchedPage(
        source_url="https://recipes.example/import",
        final_url="https://recipes.example/recipes/pie",
        status_code=200,
        content_type="text/html",
        html=html,
        json_ld=json_ld,
        **kwargs,
    )


def test_parse_json_ld_maps_recipe_fields_and_preserves_ingredient_source() -> None:
    source_line = "½ cup crème fraîche, divided"
    recipe_json = """{
      "@context":"https://schema.org",
      "@type":"Recipe",
      "name":"Sunday Pie",
      "description":"A family pie.",
      "recipeYield":"4 servings",
      "prepTime":"PT15M",
      "cookTime":"PT1H",
      "totalTime":"PT1H15M",
      "recipeCategory":["Dessert", "Pie"],
      "keywords":"comfort food, Sunday",
      "image":{"url":"/images/pie.jpg"},
      "recipeIngredient":["½ cup crème fraîche, divided", "2 large eggs", "salt to taste"],
      "recipeInstructions":[
        {"@type":"HowToSection","name":"Prepare","itemListElement":[
          {"@type":"HowToStep","text":"Mix the filling."},
          {"@type":"HowToStep","text":"Pour into the shell."}
        ]},
        {"@type":"HowToStep","text":"Bake until golden."}
      ]
    }"""
    result = parse_recipe_page(
        page(
            json_ld=(recipe_json,),
            og_image="https://recipes.example/fallback.jpg",
        )
    )

    assert result.title == "Sunday Pie"
    assert result.description == "A family pie."
    assert result.servings_text == "4 servings"
    assert result.servings_number == Decimal("4")
    assert result.servings_unit == "servings"
    assert (result.prep_minutes, result.cook_minutes, result.total_minutes) == (15, 60, 75)
    assert result.suggested_categories == ["Dessert", "Pie"]
    assert result.tags == ["comfort food", "Sunday"]
    assert result.hero_image == "https://recipes.example/images/pie.jpg"
    assert result.provenance is not None
    assert result.provenance.source_url == "https://recipes.example/import"
    assert result.provenance.extraction_method == "jsonld"

    ingredients = result.sections[0]
    assert ingredients.kind == "ingredients"
    assert isinstance(ingredients.lines[0], RecipeIngredientDraft)
    assert ingredients.lines[0].raw_text == source_line
    assert ingredients.lines[0].quantity_text == "½"
    assert ingredients.lines[0].unit == "cup"
    assert ingredients.lines[0].item == "crème fraîche"
    assert ingredients.lines[0].preparation == "divided"
    assert isinstance(ingredients.lines[2], RecipeIngredientDraft)
    assert ingredients.lines[2].raw_text == "salt to taste"
    assert ingredients.lines[2].quantity_text is None
    assert ingredients.lines[2].item is None

    prepare, final = result.sections[1:]
    assert (prepare.kind, prepare.heading) == ("steps", "Prepare")
    assert [line.body for line in prepare.lines if isinstance(line, RecipeStepDraft)] == [
        "Mix the filling.",
        "Pour into the shell.",
    ]
    assert (final.kind, final.heading) == ("steps", None)
    assert final.lines[0].body == "Bake until golden."


def test_recipe_parser_is_exported_from_recipe_package() -> None:
    assert exported_parse_recipe_page is parse_recipe_page


@pytest.mark.parametrize(
    "json_ld,expected",
    [
        (
            '[{"@type":"Article","name":"Not a recipe"},'
            '{"@type":["Thing","https://schema.org/Recipe"],"name":"Nested pie",'
            '"mainEntity":{"@type":"Recipe","name":"Nested child",'
            '"recipeIngredient":["1 cup flour"]}}]',
            "Nested child",
        ),
        (
            '{"@graph":[{"@type":"Recipe","name":"Short"},'
            '{"@type":"Recipe","name":"Rich", "recipeIngredient":["3 apples"],'
            '"recipeInstructions":["Peel.","Bake."]}]}',
            "Rich",
        ),
        ('{"@type":"schema:Recipe","name":"Prefixed"}', "Prefixed"),
    ],
)
def test_parse_json_ld_walks_graphs_arrays_and_nested_candidates(
    json_ld: str, expected: str
) -> None:
    result = parse_recipe_page(page(json_ld=(json_ld,)))

    assert result.title == expected
    assert result.provenance is not None
    assert result.provenance.extraction_method == "jsonld"


def test_parse_json_ld_uses_first_candidate_when_populated_field_scores_tie() -> None:
    result = parse_recipe_page(
        page(
            json_ld=(
                '[{"@type":"Recipe","name":"First","description":"Pie"},'
                '{"@type":"Recipe","name":"Second","description":"Cake"}]',
            )
        )
    )

    assert result.title == "First"


def test_parse_json_ld_returns_partial_draft_with_note_for_unreadable_block() -> None:
    result = parse_recipe_page(
        page(
            json_ld=("{not json", '{"@type":"Recipe","name":"Usable"}'),
        )
    )

    assert result.title == "Usable"
    assert result.parse_notes
    assert result.provenance is not None
    assert result.provenance.extraction_method == "jsonld"


def test_parse_recipe_page_falls_back_to_case_insensitive_microdata() -> None:
    html = """<main itemscope itemtype="https://schema.org/recipe">
      <span itemscope itemtype="https://schema.org/Person">
        <span itemprop="name">Should not become the recipe title</span>
      </span>
      <h1 itemprop="name">Apple Cake</h1>
      <meta itemprop="description" content="A simple cake">
      <span itemprop="recipeIngredient">1 cup sugar</span>
      <span itemprop="ingredients">2 eggs</span>
      <meta itemprop="prepTime" content="PT10M">
      <time itemprop="cookTime" datetime="PT35M">35 minutes</time>
      <meta itemprop="recipeYield" content="8 servings">
      <img itemprop="image" src="/cake.jpg">
      <span itemprop="keywords">cake, easy</span>
      <span itemprop="recipeCategory">Dessert</span>
    </main>"""

    result = parse_recipe_page(
        page(html=html, og_image="https://recipes.example/fallback.jpg")
    )

    assert result.title == "Apple Cake"
    assert result.description == "A simple cake"
    assert result.servings_number == Decimal("8")
    assert result.servings_unit == "servings"
    assert (result.prep_minutes, result.cook_minutes) == (10, 35)
    assert result.hero_image == "https://recipes.example/cake.jpg"
    assert result.tags == ["cake", "easy"]
    assert result.suggested_categories == ["Dessert"]
    assert result.sections[0].kind == "ingredients"
    assert [line.raw_text for line in result.sections[0].lines] == [
        "1 cup sugar",
        "2 eggs",
    ]
    assert result.provenance is not None
    assert result.provenance.extraction_method == "microdata"


def test_json_ld_without_recipe_content_uses_microdata_fallback() -> None:
    html = """<div itemscope itemtype="http://schema.org/Recipe">
      <span itemprop="name">Fallback</span>
      <span itemprop="recipeIngredient">3 pears</span>
    </div>"""
    result = parse_recipe_page(
        page(
            json_ld=(
                '{"@type":"Recipe","mainEntity":{"@type":"Thing","name":"No recipe"}}',
            ),
            html=html,
        )
    )

    assert result.title == "Fallback"
    assert result.provenance is not None
    assert result.provenance.extraction_method == "microdata"


def test_malformed_json_ld_note_survives_microdata_fallback() -> None:
    html = """<div itemscope itemtype="https://schema.org/Recipe">
      <span itemprop="name">Fallback</span>
      <span itemprop="recipeIngredient">3 pears</span>
    </div>"""
    result = parse_recipe_page(page(json_ld=("not json",), html=html))

    assert result.title == "Fallback"
    assert result.parse_notes
    assert result.provenance is not None
    assert result.provenance.extraction_method == "microdata"


def test_deeply_nested_unrelated_json_ld_is_reported_without_failing_parsing() -> None:
    deep_block = '{"@type":"Thing","child":' + "[" * 1100 + "null" + "]" * 1100 + "}"

    result = parse_recipe_page(page(json_ld=(deep_block,)))

    assert result.title == ""
    assert result.sections == []


def test_parse_recipe_page_returns_sparse_draft_when_no_recipe_data_exists() -> None:
    result = parse_recipe_page(
        page(json_ld=("{bad json",), html="<main>No recipe here</main>")
    )

    assert result.title == ""
    assert result.sections == []
    assert result.provenance is not None
    assert result.provenance.source_url == "https://recipes.example/import"
    assert result.provenance.extraction_method is None
    assert result.parse_notes


def test_how_to_section_alone_is_usable_and_keeps_page_title() -> None:
    result = parse_recipe_page(
        page(
            title="Recipe from page title",
            json_ld=(
                '{"@type":"Recipe","recipeInstructions":{"@type":"HowToSection",'
                '"name":"Dough","itemListElement":[{"@type":"HowToStep",'
                '"text":"Mix."}]}}',
            ),
        )
    )

    assert result.title == "Recipe from page title"
    assert result.sections[0].heading == "Dough"
    assert result.sections[0].lines[0].body == "Mix."


def test_serves_prefix_produces_numeric_servings_without_losing_yield_text() -> None:
    result = parse_recipe_page(
        page(json_ld=('{"@type":"Recipe","name":"Pie","recipeYield":"Serves 4"}',))
    )

    assert result.servings_text == "Serves 4"
    assert result.servings_number == Decimal("4")


def test_servings_range_is_preserved_without_guessing_a_single_number() -> None:
    result = parse_recipe_page(
        page(json_ld=('{"@type":"Recipe","name":"Pie","recipeYield":"4 to 6 servings"}',))
    )

    assert result.servings_text == "4 to 6 servings"
    assert result.servings_number is None


def test_unrecognized_duration_is_omitted_and_explained_in_parse_notes() -> None:
    result = parse_recipe_page(
        page(json_ld=('{"@type":"Recipe","name":"Pie","prepTime":"a while"}',))
    )

    assert result.prep_minutes is None
    assert any("timing" in note for note in result.parse_notes)


@pytest.mark.parametrize(
    "raw,quantity,unit,item,preparation",
    [
        ("1 1/2 cups flour, sifted", "1 1/2", "cups", "flour", "sifted"),
        ("2 large eggs", "2", None, "large eggs", None),
        ("1½ tbsp. oil", "1½", "tbsp.", "oil", None),
    ],
)
def test_parse_ingredient_splits_only_clear_prefixes(
    raw: str,
    quantity: str,
    unit: str | None,
    item: str,
    preparation: str | None,
) -> None:
    result = parse_recipe_page(
        page(
            json_ld=(
                json.dumps({"@type": "Recipe", "recipeIngredient": [raw]}),
            )
        )
    )

    ingredient = result.sections[0].lines[0]
    assert isinstance(ingredient, RecipeIngredientDraft)
    assert ingredient.raw_text == raw
    assert ingredient.quantity_text == quantity
    assert ingredient.unit == unit
    assert ingredient.item == item
    assert ingredient.preparation == preparation
