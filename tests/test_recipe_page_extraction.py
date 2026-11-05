from __future__ import annotations

import pytest

from cookbook.recipes.page_extraction import extract_page


def test_extract_page_reads_document_metadata_and_visible_text() -> None:
    page = extract_page(
        """<!doctype html><html><head>
        <title>  Sunday Pie </title>
        <meta name="description" content="A family pie">
        <link rel="canonical" href="/recipes/pie">
        <meta property="og:image" content="/images/pie.jpg">
        </head><body><h1>Sunday Pie</h1><p>Mix <em>well</em>.</p>
        <script>ignore this</script><style>also ignore</style>
        </body></html>""",
        source_url="https://recipes.example/import",
        final_url="https://recipes.example/final",
        status_code=200,
        content_type="text/html",
    )

    assert page.title == "Sunday Pie"
    assert page.description == "A family pie"
    assert page.canonical_url == "https://recipes.example/recipes/pie"
    assert page.og_image == "https://recipes.example/images/pie.jpg"
    assert page.visible_text == "Sunday Pie Mix well."
    assert page.json_ld == ()
    assert "ignore this" not in page.visible_text


def test_extract_page_reads_og_site_name_for_source_provenance() -> None:
    page = extract_page(
        "<meta property=\"og:site_name\" content=\"Grandma's Kitchen\">",
        source_url="https://recipes.example/pie",
        final_url="https://recipes.example/pie",
        status_code=200,
        content_type="text/html",
    )

    assert page.site_name == "Grandma's Kitchen"


def test_extract_page_preserves_each_json_ld_script_but_not_in_visible_text() -> None:
    page = extract_page(
        """<html><head>
        <script type="application/ld+json"> {"@type":"Recipe","name":"Pie"} </script>
        <script type="APPLICATION/LD+JSON; charset=utf-8">[{"@type":"Recipe"}]</script>
        </head><body>Pie instructions</body></html>""",
        source_url="https://recipes.example/pie",
        final_url="https://recipes.example/pie",
        status_code=200,
        content_type="text/html; charset=utf-8",
    )

    assert page.json_ld == (
        '{"@type":"Recipe","name":"Pie"}',
        '[{"@type":"Recipe"}]',
    )
    assert page.visible_text == "Pie instructions"


@pytest.mark.parametrize(
    "html",
    [
        "<title>Half-open title",
        "<body><p>One<p>Two<script type='application/ld+json'>{\"@type\":\"Recipe\"}",
        "<!-- unclosed comment <body>text</body>",
    ],
)
def test_extract_page_tolerates_malformed_html(html: str) -> None:
    page = extract_page(
        html,
        source_url="https://recipes.example/pie",
        final_url="https://recipes.example/pie",
        status_code=200,
        content_type="text/html",
    )

    assert page.source_url == "https://recipes.example/pie"
    assert isinstance(page.visible_text, str)
    assert isinstance(page.json_ld, tuple)


def test_extract_page_ignores_hostile_base_tag_for_relative_metadata() -> None:
    page = extract_page(
        '<base href="http://127.0.0.1/"><meta property="og:image" content="/pie.jpg">',
        source_url="https://recipes.example/pie",
        final_url="https://recipes.example/pie",
        status_code=200,
        content_type="text/html",
    )

    assert page.og_image == "https://recipes.example/pie.jpg"


def test_extract_page_removes_navigation_and_cookie_banner_from_visible_text() -> None:
    page = extract_page(
        """<nav>Home Recipes About</nav>
        <div class="cookie-consent-banner">We use cookies. Accept all.</div>
        <main><h1>Sunday Pie</h1><p>Mix and bake.</p></main>""",
        source_url="https://recipes.example/pie",
        final_url="https://recipes.example/pie",
        status_code=200,
        content_type="text/html",
    )

    assert page.visible_text == "Sunday Pie Mix and bake."
