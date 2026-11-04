from __future__ import annotations

import asyncio
import json

import httpx
import pytest
import respx

from cookbook.config import Settings
from cookbook.recipes.drafts import RecipeIngredientDraft
from cookbook.recipes.ingest import FetchedPage
from cookbook.recipes.llm import parse_recipe_page_with_fallback


CHAT_COMPLETIONS_URL = "https://llm.example/v1/chat/completions"


def page(*, visible_text: str = "", json_ld: tuple[str, ...] = ()) -> FetchedPage:
    return FetchedPage(
        source_url="https://recipes.example/import",
        final_url="https://recipes.example/pie",
        status_code=200,
        content_type="text/html",
        html="",
        title="Page title",
        visible_text=visible_text,
        json_ld=json_ld,
    )


def settings(**overrides: object) -> Settings:
    values = {
        "_env_file": None,
        "DEBUG": False,
        "LLM_BASE_URL": "https://llm.example/v1",
        "LLM_MODEL": "recipe-model",
        "LLM_API_KEY": "test-key",
        "LLM_TIMEOUT_SECONDS": 10,
    }
    values.update(overrides)
    return Settings(**values)


def completion(content: object) -> httpx.Response:
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": content}}]},
    )


def recipe_content() -> str:
    return json.dumps(
        {
            "title": "Sunday Pie",
            "description": "A simple pie.",
            "sections": [
                {
                    "kind": "ingredients",
                    "lines": [{"raw_text": "2 apples", "quantity_text": "2", "item": "apples"}],
                },
                {"kind": "steps", "lines": [{"body": "Bake until golden."}]},
            ],
            "tags": ["dessert"],
        }
    )


@pytest.mark.asyncio
async def test_llm_fallback_sends_bounded_strict_json_request_and_stamps_provenance() -> None:
    with respx.mock as router:
        route = router.post(CHAT_COMPLETIONS_URL).mock(
            return_value=completion(recipe_content())
        )
        result = await parse_recipe_page_with_fallback(
            page(visible_text="A family recipe."), settings=settings()
        )

    assert result.title == "Sunday Pie"
    assert result.provenance is not None
    assert result.provenance.extraction_method == "llm"
    assert result.provenance.source_url == "https://recipes.example/import"
    request = route.calls[0].request
    body = json.loads(request.content)
    assert request.headers["authorization"] == "Bearer test-key"
    assert body["model"] == "recipe-model"
    assert body["temperature"] == 0
    assert body["response_format"] == {"type": "json_object"}
    assert "untrusted" in body["messages"][0]["content"].casefold()
    assert body["messages"][1]["content"].endswith("A family recipe.")


@pytest.mark.asyncio
async def test_llm_fallback_truncates_visible_text_to_24000_characters() -> None:
    source = "x" * 24_100

    with respx.mock as router:
        route = router.post(CHAT_COMPLETIONS_URL).mock(
            return_value=completion(recipe_content())
        )
        await parse_recipe_page_with_fallback(page(visible_text=source), settings=settings())

    body = json.loads(route.calls[0].request.content)
    sent_source = body["messages"][1]["content"].split("\n\n", 1)[1]
    assert len(sent_source) == 24_000


@pytest.mark.asyncio
async def test_llm_fallback_does_not_call_provider_for_usable_deterministic_recipe() -> None:
    source = json.dumps(
        {
            "@type": "Recipe",
            "name": "Structured Pie",
            "recipeIngredient": ["2 apples"],
            "recipeInstructions": ["Bake."],
        }
    )

    with respx.mock as router:
        route = router.post(CHAT_COMPLETIONS_URL).mock(
            return_value=completion(recipe_content())
        )
        result = await parse_recipe_page_with_fallback(
            page(visible_text="A family recipe.", json_ld=(source,)),
            settings=settings(),
        )

    assert result.title == "Structured Pie"
    assert result.provenance is not None
    assert result.provenance.extraction_method == "jsonld"
    assert not route.called


@pytest.mark.asyncio
async def test_llm_fallback_runs_for_obviously_garbage_deterministic_ingredients() -> None:
    source = json.dumps(
        {
            "@type": "Recipe",
            "name": "Pie",
            "recipeIngredient": ["See photo for ingredients"],
        }
    )

    with respx.mock as router:
        route = router.post(CHAT_COMPLETIONS_URL).mock(
            return_value=completion(recipe_content())
        )
        result = await parse_recipe_page_with_fallback(
            page(visible_text="Apples, sugar, and cinnamon. Bake.", json_ld=(source,)),
            settings=settings(),
        )

    assert result.title == "Sunday Pie"
    assert result.sections[0].lines[0].raw_text == "2 apples"
    assert result.provenance is not None
    assert result.provenance.extraction_method == "llm"


@pytest.mark.asyncio
async def test_missing_api_key_keeps_sparse_draft_and_does_not_call_provider() -> None:
    with respx.mock as router:
        route = router.post(CHAT_COMPLETIONS_URL).mock(
            return_value=completion(recipe_content())
        )
        result = await parse_recipe_page_with_fallback(
            page(visible_text="A family recipe."),
            settings=settings(LLM_API_KEY=None),
        )

    assert result.title == ""
    assert result.parse_notes
    assert result.provenance is not None
    assert result.provenance.source_url == "https://recipes.example/import"
    assert not route.called


@pytest.mark.asyncio
async def test_empty_visible_text_does_not_call_provider() -> None:
    with respx.mock as router:
        route = router.post(CHAT_COMPLETIONS_URL).mock(
            return_value=completion(recipe_content())
        )
        result = await parse_recipe_page_with_fallback(page(), settings=settings())

    assert result.title == ""
    assert result.parse_notes
    assert not route.called


@pytest.mark.asyncio
async def test_llm_key_file_takes_precedence_over_plain_key(tmp_path) -> None:
    key_file = tmp_path / "llm-key"
    key_file.write_text("file-key\nignored\n", encoding="utf-8")
    with respx.mock as router:
        route = router.post(CHAT_COMPLETIONS_URL).mock(
            return_value=completion(recipe_content())
        )
        await parse_recipe_page_with_fallback(
            page(visible_text="A family recipe."),
            settings=settings(LLM_API_KEY_FILE=key_file),
        )

    assert route.calls[0].request.headers["authorization"] == "Bearer file-key"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(503, text="provider detail must not leak"),
        httpx.Response(200, json={"choices": []}),
        completion("not JSON"),
        completion('{"title": 5, "unexpected": true}'),
        httpx.InvalidURL("invalid provider URL"),
        httpx.ReadTimeout("provider timed out"),
    ],
    ids=[
        "http-error", "missing-choice", "invalid-json", "invalid-draft",
        "invalid-url", "timeout",
    ],
)
async def test_provider_failures_preserve_deterministic_draft_and_add_safe_note(
    response: httpx.Response | Exception,
) -> None:
    source = json.dumps(
        {
            "@type": "Recipe",
            "name": "Existing Pie",
            "recipeIngredient": ["See photo for amounts"],
        }
    )

    with respx.mock as router:
        route = router.post(CHAT_COMPLETIONS_URL).mock(side_effect=response)
        result = await parse_recipe_page_with_fallback(
            page(visible_text="Visible recipe details.", json_ld=(source,)),
            settings=settings(),
        )

    assert result.title == "Existing Pie"
    ingredient = result.sections[0].lines[0]
    assert isinstance(ingredient, RecipeIngredientDraft)
    assert ingredient.raw_text == "See photo for amounts"
    assert result.parse_notes
    assert "provider detail must not leak" not in " ".join(result.parse_notes)
    if isinstance(response, httpx.ReadTimeout):
        assert any("timed out" in note for note in result.parse_notes)


@pytest.mark.asyncio
async def test_overall_llm_timeout_returns_deterministic_draft() -> None:
    async def too_slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.05)
        return completion(recipe_content())

    source = json.dumps(
        {
            "@type": "Recipe",
            "name": "Existing Pie",
            "recipeIngredient": ["See photo for amounts"],
        }
    )
    with respx.mock as router:
        router.post(CHAT_COMPLETIONS_URL).mock(side_effect=too_slow)
        result = await parse_recipe_page_with_fallback(
            page(visible_text="Visible recipe details.", json_ld=(source,)),
            settings=settings(LLM_TIMEOUT_SECONDS=0.01),
        )

    assert result.title == "Existing Pie"
    assert any("timed out" in note for note in result.parse_notes)


@pytest.mark.asyncio
async def test_unreadable_optional_key_file_returns_deterministic_result(tmp_path) -> None:
    with respx.mock as router:
        route = router.post(CHAT_COMPLETIONS_URL).mock(
            return_value=completion(recipe_content())
        )
        result = await parse_recipe_page_with_fallback(
            page(visible_text="Some details."),
            settings=settings(LLM_API_KEY_FILE=tmp_path / "missing"),
        )

    assert result.title == ""
    assert result.parse_notes
    assert not route.called
