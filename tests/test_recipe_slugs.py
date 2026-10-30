"""Deterministic slug normalization for recipes and tags (plan Task 9, Task 1)."""
import unicodedata

from cookbook.recipes.slugs import slugify


def test_simple_title_becomes_hyphenated_lowercase() -> None:
    assert slugify("Beef Wellington", fallback="recipe") == "beef-wellington"


def test_spaces_and_punctuation_become_single_hyphens() -> None:
    assert slugify("Tomato & Herb Soup!", fallback="recipe") == "tomato-herb-soup"


def test_runs_of_separators_collapse_to_one_hyphen() -> None:
    assert slugify("a  __  b---c", fallback="tag") == "a-b-c"


def test_edge_hyphens_are_trimmed() -> None:
    assert slugify("... leading -- tail ...", fallback="recipe") == "leading-tail"


def test_accented_letters_are_preserved() -> None:
    # slugify keeps Unicode letters per plan, so accents stay intact.
    assert slugify("Crème Brûlée", fallback="recipe") == "crème-brûlée"


def test_cjk_letters_are_preserved() -> None:
    assert slugify("麻婆豆腐", fallback="recipe") == "麻婆豆腐"


def test_nfc_equivalent_inputs_produce_same_slug() -> None:
    decomposed = "cafe\u0301 au lait"  # e as e + combining acute
    composed = "caf\u00e9 au lait"
    assert unicodedata.normalize("NFC", decomposed) != decomposed
    assert slugify(decomposed, fallback="recipe") == slugify(composed, fallback="recipe")
    assert slugify(decomposed, fallback="recipe") == "café-au-lait"


def test_recipe_fallback_when_result_is_empty() -> None:
    assert slugify("!!!", fallback="recipe") == "recipe"
    assert slugify("", fallback="recipe") == "recipe"
    assert slugify("   ", fallback="recipe") == "recipe"


def test_tag_fallback_when_result_is_empty() -> None:
    assert slugify("--", fallback="tag") == "tag"
    assert slugify("", fallback="tag") == "tag"


def test_digits_are_preserved() -> None:
    assert slugify("Top 3 of 5", fallback="recipe") == "top-3-of-5"


def test_unicode_numeric_characters_are_preserved() -> None:
    assert slugify("Ⅳ ²", fallback="recipe") == "ⅳ-²"


def test_max_length_caps_long_single_word() -> None:
    result = slugify("a" * 500, fallback="tag")
    assert result == "a" * 200


def test_max_length_parameter_limits_output() -> None:
    result = slugify("b" * 50, fallback="tag", max_length=10)
    assert result == "b" * 10
