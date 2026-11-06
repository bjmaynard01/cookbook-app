"""Planning and reporting for the WordPress importer has no DB side effects."""
from __future__ import annotations

import asyncio
from argparse import Namespace
from pathlib import Path

from test_wp_import import WXR_FIXTURE
from cookbook.cli import build_parser
from cookbook.wp_import import build_import_report, run_wp_import
from cookbook import cli_wp_import
from cookbook.wxr import parse_wxr_bytes


def test_report_shows_category_evidence_empty_fields_aliases_and_slug_collisions() -> None:
    document = parse_wxr_bytes(WXR_FIXTURE)
    report = build_import_report(document, existing_slugs={"bread-butter-rolls"})

    assert report["published_posts"] == 2
    assert report["drafts_excluded"] == 1
    rolls, ribs = report["recipes"]
    assert rolls["slug"] == "bread-butter-rolls-2"
    assert rolls["slug_collision"] is True
    assert rolls["categories"] == [{"slug": "baking", "evidence": ["bread → baking"]}]
    assert rolls["fields_found"]["ingredient_lines"] == 3
    assert rolls["fields_found"]["step_lines"] == 2
    assert rolls["legacy_path"] == "/?p=100"
    assert ribs["categories"][0]["slug"] == "others"
    assert ribs["slug"] == "smoked-ribs"
    assert report["category_pages"] == [{"source_id": "16", "category": "breakfast", "path": "/?page_id=16", "alias_conflict": False}]


def test_import_report_marks_existing_source_keys_as_skips() -> None:
    report = build_import_report(parse_wxr_bytes(WXR_FIXTURE), source_ids={"100"})
    assert report["recipes"][0]["action"] == "skip"
    assert report["recipes"][1]["action"] == "import"


def test_existing_recipe_route_aliases_reserve_the_same_slug() -> None:
    report = build_import_report(parse_wxr_bytes(WXR_FIXTURE), existing_aliases={"/r/bread-butter-rolls"})
    assert report["recipes"][0]["slug"] == "bread-butter-rolls-2"
    assert report["recipes"][0]["slug_collision"] is True


def test_pinto_bean_collection_is_kept_and_flagged() -> None:
    collection = WXR_FIXTURE.replace(b"<wp:post_id>100</wp:post_id>", b"<wp:post_id>1950</wp:post_id>")
    collection = collection.replace(b"<title>Bread &amp; Butter Rolls</title>", b"<title>Pinto Bean Recipes</title>")
    report = build_import_report(parse_wxr_bytes(collection))
    assert report["recipes"][0]["action"] == "import"
    assert report["recipes"][0]["review_flags"]
    assert report["owner_review_flags"][0]["source_post_id"] == "1950"


def test_owner_approved_missing_ingredient_post_is_explicitly_flagged() -> None:
    source = WXR_FIXTURE.replace(b"<wp:post_id>100</wp:post_id>", b"<wp:post_id>210</wp:post_id>")
    source = source.replace(b"<title>Bread &amp; Butter Rolls</title>", b"<title>Oven Baked Tacos</title>")
    source = source.replace(
        b"<p>A family favorite.</p><p>Ingredients :</p><p>2 cups flour</p><p>1 cup milk</p><p>Filling:</p><p>3 tbsp butter</p><p>Directions :</p><p>Mix the flour and milk.</p><p>Bake until golden.</p>",
        b"<p>Brown the beef, then drain it.</p><p>Sprinkle cheese and bake until hot.</p>",
    )
    report = build_import_report(parse_wxr_bytes(source))

    taco = next(row for row in report["recipes"] if row["source_post_id"] == "210")
    assert taco["fields_found"]["ingredient_lines"] == 0
    assert taco["approved_exceptions"] == ["missing_ingredient_lines"]
    assert "owner-approved" in taco["review_flags"][0].lower()
    assert report["approved_exceptions"] == [
        {"source_post_id": "210", "title": "Oven Baked Tacos", "code": "missing_ingredient_lines"}
    ]


def test_report_destination_cannot_overwrite_the_source() -> None:
    from pathlib import Path

    source = Path("same-wxr.xml")
    try:
        asyncio.run(run_wp_import(None, source, media_dir=Path("media"), report_path=source))
    except ValueError as exc:
        assert "must not overwrite" in str(exc)
    else:
        raise AssertionError("source-overwriting report path was not rejected")


def test_import_wp_command_defaults_to_dry_run_and_resumes_explicitly() -> None:
    parser = build_parser()
    dry_run = parser.parse_args(["import-wp", "--file", "recipes.xml"])
    explicit_dry_run = parser.parse_args(["import-wp", "--file", "recipes.xml", "--dry-run"])
    resume = parser.parse_args(["import-wp", "--file", "recipes.xml", "--resume"])
    verify = parser.parse_args(["import-wp", "--verify", "--run", "abc"])
    retry_images = parser.parse_args(["import-wp", "--retry-images", "--run", "abc"])

    assert not dry_run.apply and not dry_run.resume and not dry_run.verify
    assert explicit_dry_run.dry_run and not explicit_dry_run.apply
    assert resume.resume and not resume.apply
    assert verify.verify and verify.run_id == "abc"
    assert retry_images.retry_images and retry_images.run_id == "abc"


def test_cli_json_output_escapes_unicode_for_windows_consoles(monkeypatch, capsys) -> None:
    report = {"category_evidence": "bread → baking"}
    monkeypatch.setattr(cli_wp_import, "cli_config", lambda: object())
    monkeypatch.setattr(cli_wp_import, "_run", lambda args, cfg: object())
    monkeypatch.setattr(cli_wp_import.asyncio, "run", lambda _awaitable: report)
    args = Namespace(
        verify=False, retry_images=False, run_id=None, file=Path("fixture.xml"), apply=False,
        resume=False, report=None,
    )

    assert cli_wp_import.run_import_command(args) == 0
    output = capsys.readouterr().out
    assert "bread \\u2192 baking" in output
