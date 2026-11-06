"""WXR parsing preserves the source while resolving only its known structure."""
from __future__ import annotations

import pytest

from cookbook.wxr import WxrParseError, parse_wxr_bytes


WXR_FIXTURE = b'''<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"
 xmlns:wp="http://wordpress.org/export/1.2/"
 xmlns:content="http://purl.org/rss/1.0/modules/content/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
 <channel>
  <wp:category><wp:term_id>1</wp:term_id><wp:category_nicename>baking</wp:category_nicename><wp:category_parent></wp:category_parent><wp:cat_name>Baking</wp:cat_name></wp:category>
  <wp:category><wp:term_id>2</wp:term_id><wp:category_nicename>bread</wp:category_nicename><wp:category_parent>baking</wp:category_parent><wp:cat_name>Bread</wp:cat_name></wp:category>
  <wp:category><wp:term_id>3</wp:term_id><wp:category_nicename>bbq</wp:category_nicename><wp:category_parent></wp:category_parent><wp:cat_name>BBQ</wp:cat_name></wp:category>
  <item>
   <title>Bread &amp; Butter Rolls</title><link>https://cookbook.maynardfolks.com/?p=100</link>
   <dc:creator>barbara</dc:creator><wp:post_id>100</wp:post_id>
   <wp:post_date>2021-02-03 12:30:00</wp:post_date><wp:post_type>post</wp:post_type>
   <wp:status>publish</wp:status><wp:post_name>bread-butter-rolls</wp:post_name>
   <content:encoded><![CDATA[<p>A family favorite.</p><p>Ingredients :</p><p>2 cups flour</p><p>1 cup milk</p><p>Filling:</p><p>3 tbsp butter</p><p>Directions :</p><p>Mix the flour and milk.</p><p>Bake until golden.</p>]]></content:encoded>
   <category domain="category" nicename="bread">Bread</category>
   <category domain="post_tag" nicename="untrusted-tag">Ignore this tag</category>
   <wp:postmeta><wp:meta_key>_thumbnail_id</wp:meta_key><wp:meta_value>501</wp:meta_value></wp:postmeta>
  </item>
  <item>
   <title>Smoked Ribs</title><link>https://cookbook.maynardfolks.com/?p=101</link>
   <wp:post_id>101</wp:post_id><wp:post_date>2021-02-04 12:30:00</wp:post_date>
   <wp:post_type>post</wp:post_type><wp:status>publish</wp:status><wp:post_name>101</wp:post_name>
   <content:encoded><![CDATA[<p>Salt and pepper</p><p>Cook slowly.</p>]]></content:encoded>
   <category domain="category" nicename="bbq">BBQ</category>
  </item>
  <item>
   <title>Breakfast</title><wp:post_id>16</wp:post_id><wp:post_type>page</wp:post_type>
   <wp:status>publish</wp:status><wp:post_name>breakfast</wp:post_name>
  </item>
  <item>
   <title>Unpublished Draft</title><wp:post_id>102</wp:post_id><wp:post_type>post</wp:post_type>
   <wp:status>draft</wp:status><wp:post_name>draft</wp:post_name>
  </item>
  <item>
   <title>rolls-original.jpg</title><link>https://cookbook.maynardfolks.com/uploads/rolls-original.jpg</link>
   <wp:post_id>501</wp:post_id><wp:post_type>attachment</wp:post_type><wp:status>inherit</wp:status>
   <wp:post_parent>100</wp:post_parent>
   <wp:attachment_url>https://cookbook.maynardfolks.com/uploads/rolls-original.jpg</wp:attachment_url>
  </item>
 </channel>
</rss>'''


def test_wxr_parser_maps_hierarchical_categories_and_keeps_raw_body_lines() -> None:
    document = parse_wxr_bytes(WXR_FIXTURE, source_name="fixture.xml")

    assert [recipe.post_id for recipe in document.recipes] == ["100", "101"]
    rolls = document.recipes[0]
    assert rolls.title == "Bread & Butter Rolls"
    assert rolls.source_slug == "bread-butter-rolls"
    assert rolls.published_date.isoformat() == "2021-02-03"
    assert rolls.legacy_path == "/?p=100"
    assert rolls.category_slugs == ("baking",)
    assert rolls.category_evidence == ("bread → baking",)
    assert rolls.image_url == "https://cookbook.maynardfolks.com/uploads/rolls-original.jpg"
    assert rolls.description == "A family favorite."
    assert [(section.kind, section.heading) for section in rolls.sections] == [
        ("ingredients", "Ingredients"),
        ("ingredients", "Filling"),
        ("steps", "Directions"),
    ]
    assert [line.raw_text for line in rolls.sections[0].lines] == ["2 cups flour", "1 cup milk"]
    assert rolls.sections[1].lines[0].raw_text == "3 tbsp butter"
    assert [line.body for line in rolls.sections[2].lines] == [
        "Mix the flour and milk.", "Bake until golden."
    ]
    assert "untrusted-tag" not in rolls.tags
    assert document.category_pages == (("16", "breakfast", "/?page_id=16"),)
    assert document.excluded_posts == (("102", "draft"),)


def test_unmenued_published_post_is_assigned_others_and_numeric_slug_is_marked() -> None:
    document = parse_wxr_bytes(WXR_FIXTURE)
    ribs = document.recipes[1]

    assert ribs.category_slugs == ("others",)
    assert ribs.category_evidence == ("bbq → Others",)
    assert ribs.needs_slug_regeneration is True
    assert ribs.image_url is None
    assert "servings" in ribs.empty_fields
    assert "times" in ribs.empty_fields


def test_malformed_and_oversized_wxr_are_rejected_before_import() -> None:
    with pytest.raises(WxrParseError, match="well-formed"):
        parse_wxr_bytes(b"<rss><channel>")

    with pytest.raises(WxrParseError, match="size limit"):
        parse_wxr_bytes(WXR_FIXTURE, max_bytes=100)


def test_plain_text_recipe_splits_at_line_level_and_keeps_intro_description() -> None:
    plain = WXR_FIXTURE.replace(
        b"<p>A family favorite.</p><p>Ingredients :</p><p>2 cups flour</p><p>1 cup milk</p><p>Filling:</p><p>3 tbsp butter</p><p>Directions :</p><p>Mix the flour and milk.</p><p>Bake until golden.</p>",
        b"A family favorite.\\nIngredients:\\n2 cups flour\\n1 cup milk\\nFilling:\\n3 tbsp butter\\nDirections:\\nMix the flour and milk.\\nBake until golden.",
    ).replace(b"\\n", bytes((10,)))
    rolls = parse_wxr_bytes(plain).recipes[0]

    assert rolls.description == "A family favorite."
    assert [(section.kind, section.heading) for section in rolls.sections] == [
        ("ingredients", "Ingredients"), ("ingredients", "Filling"), ("steps", "Directions"),
    ]
    assert [line.raw_text for line in rolls.sections[0].lines] == ["2 cups flour", "1 cup milk"]
