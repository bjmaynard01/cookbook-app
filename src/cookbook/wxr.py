"""Bounded, deterministic parsing for WordPress eXtended RSS exports."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path

from .recipes.drafts import RecipeIngredientDraft, RecipeSectionDraft, RecipeStepDraft

WP = "http://wordpress.org/export/1.2/"
CONTENT = "http://purl.org/rss/1.0/modules/content/"
MAX_WXR_BYTES = 50 * 1024 * 1024
MENU_CATEGORIES = {
    "appetizers", "baking", "breakfast", "desserts", "drinks",
    "fermentation", "main-dishes", "side-dishes",
}
MENU_PAGE_TITLES = {
    "appetizers": "appetizers", "baking": "baking", "breakfast": "breakfast",
    "desserts": "desserts", "drinks": "drinks", "fermentation": "fermentation",
    "main dishes": "main-dishes", "side dishes": "side-dishes",
}
_IMPERATIVE = re.compile(
    r"^\s*(preheat|mix|stir|pour|add|heat|cook|bake|boil|blend|spread|place|put|"
    r"set|whisk|fold|drain|chop|dice|slice|shred|grate|season|combine|beat|whip|"
    r"knead|roll|flatten|brush|top|garnish|serve|remove|let|cool|refrigerat|taste|"
    r"adjust|transfer|bring|simmer|sift|scoop|drizzle|melt|cover|cut|press|arrange|"
    r"sprinkle|divide|continue|repeat)\b",
    re.IGNORECASE,
)
_QUANTITY = re.compile(
    r"^\s*(?:\d|[½¼¾⅓⅔⅛⅜⅝⅞]|one\b|two\b|three\b|four\b|five\b|six\b|"
    r"seven\b|eight\b|nine\b|ten\b|some\b|few\b|half\b|"
    r"(?:a|an)\s+(?:pinch|dash|handful|clove|can|package|stick|pound|cup|tablespoon|teaspoon)\b)",
    re.IGNORECASE,
)
_EXPLICIT_HEADING = re.compile(
    r"^\s*(ingredients?|directions?|instructions?|preparation|method|steps?)\s*:?\s*(.*)$",
    re.IGNORECASE,
)
_EXPLICIT_NOTE_HEADING = re.compile(r"^\s*(notes?|substitutions?)\s*:?\s*(.*)$", re.IGNORECASE)
_KNOWN_SUBHEADINGS = {
    "crust", "filling", "topping", "icing", "frosting", "glaze", "sauce",
    "garnish", "marinade", "dressing", "batter", "dough", "first layer",
    "second layer", "third layer", "fourth layer",
}
_INVALID_SLUG = re.compile(r"^(?:\d+|.*__trashed(?:-\d+)?)$", re.IGNORECASE)


class WxrParseError(ValueError):
    """The input is not a supported, bounded WordPress export."""


@dataclass(frozen=True, slots=True)
class WxrPost:
    post_id: str
    title: str
    source_slug: str
    source_url: str
    published_date: date | None
    description: str | None
    sections: tuple[RecipeSectionDraft, ...]
    category_slugs: tuple[str, ...]
    category_evidence: tuple[str, ...]
    tags: tuple[str, ...]
    legacy_path: str
    image_url: str | None
    needs_slug_regeneration: bool
    empty_fields: tuple[str, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WxrDocument:
    source_name: str
    recipes: tuple[WxrPost, ...]
    category_pages: tuple[tuple[str, str, str], ...]
    excluded_posts: tuple[tuple[str, str], ...]
    attachment_urls: tuple[tuple[str, str], ...]
    total_post_count: int


@dataclass(slots=True)
class _SectionBuilder:
    kind: str
    heading: str | None
    lines: list[RecipeIngredientDraft | RecipeStepDraft]


class _TextExtractor(HTMLParser):
    _BLOCKS = {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        elif not self.ignored_depth and tag == "br":
            self.parts.append("\n")
        elif not self.ignored_depth and tag in self._BLOCKS:
            self.parts.append("\n\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif not self.ignored_depth and tag in self._BLOCKS:
            self.parts.append("\n\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def _tag(name: str) -> str:
    return f"{{{WP}}}{name}"


def _text(element: ET.Element | None) -> str:
    return (element.text or "").strip() if element is not None else ""


def _child_text(item: ET.Element, name: str) -> str:
    return _text(item.find(_tag(name)))


def _postmeta(item: ET.Element) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in item.findall(_tag("postmeta")):
        key = _text(entry.find(_tag("meta_key")))
        value = _text(entry.find(_tag("meta_value")))
        if key:
            result[key] = value
    return result


def _ancestors(slug: str, parents: dict[str, str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    current = slug
    while current and current not in seen:
        seen.add(current)
        result.append(current)
        current = parents.get(current, "")
    return tuple(result)


def _resolve_categories(
    item: ET.Element,
    parents: dict[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    resolved: list[str] = []
    evidence: list[str] = []
    for element in item.findall("category"):
        if element.get("domain", "category") != "category":
            continue
        source_slug = (element.get("nicename") or "").strip().casefold()
        if not source_slug:
            continue
        target = next((slug for slug in _ancestors(source_slug, parents) if slug in MENU_CATEGORIES), None)
        if target:
            if target not in resolved:
                resolved.append(target)
            evidence.append(f"{source_slug} → {target}" if source_slug != target else source_slug)
    if not resolved:
        uncategorized: list[str] = []
        for element in item.findall("category"):
            if element.get("domain", "category") != "category":
                continue
            source_slug = (element.get("nicename") or "").strip().casefold()
            if source_slug:
                root = _ancestors(source_slug, parents)[-1]
                uncategorized.append(f"{root} → Others")
        return ("others",), tuple(dict.fromkeys(uncategorized)) or ("no menu-category ancestor → Others",)
    return tuple(resolved), tuple(evidence)


def _extract_text(body: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(body or "")
        parser.close()
    except (ValueError, AssertionError):
        return re.sub(r"<[^>]*>", " ", body or "")
    return "".join(parser.parts).replace("\r\n", "\n").replace("\r", "\n").replace("\xa0", " ")


def _subheading(line: str) -> bool:
    value = line.rstrip(":").strip()
    if not value or len(value) > 48 or len(value.split()) > 6:
        return False
    if value[-1:] in ".!?" or _QUANTITY.match(value) or _IMPERATIVE.match(value):
        return False
    return (
        line.endswith(":")
        or value.casefold() in _KNOWN_SUBHEADINGS
        or (len(value.split()) > 1 and all(word[:1].isupper() for word in value.split()))
    )


def _parse_body(body: str) -> tuple[str | None, tuple[RecipeSectionDraft, ...], tuple[str, ...]]:
    rendered = _extract_text(body)
    lines = [line.strip() for line in rendered.splitlines() if line.strip()]
    if not lines:
        return None, (), ("The published post has no readable body text.",)

    builders: list[_SectionBuilder] = []
    description: list[str] = []
    warnings: list[str] = []
    current_kind: str | None = None
    pending_heading: str | None = None

    def set_heading(kind: str, heading: str | None) -> None:
        nonlocal current_kind, pending_heading
        current_kind = kind
        pending_heading = heading

    def append_line(kind: str, value: str) -> None:
        nonlocal pending_heading
        if (
            builders and builders[-1].kind == kind and pending_heading is None
        ):
            target = builders[-1]
        else:
            target = _SectionBuilder(kind=kind, heading=pending_heading, lines=[])
            builders.append(target)
            pending_heading = None
        if kind == "ingredients":
            target.lines.append(RecipeIngredientDraft(raw_text=value))
        else:
            target.lines.append(RecipeStepDraft(body=value))

    # The discovery spike found that 320 of 389 source posts use plain text.
    # Classify line-by-line so one body can contain both ingredients and steps.
    strong_kinds = [
        ("ingredients" if _QUANTITY.match(line) else
         "steps" if _IMPERATIVE.match(line) or len(line) > 60 else None)
        for line in lines
    ]
    for index, line in enumerate(lines):
        note = _EXPLICIT_NOTE_HEADING.match(line)
        if note:
            _label, remainder = note.groups()
            set_heading("note", "Notes")
            if remainder.strip():
                append_line("note", remainder.strip())
            continue
        explicit = _EXPLICIT_HEADING.match(line)
        if explicit:
            label, remainder = explicit.groups()
            kind = "ingredients" if label.casefold().startswith("ingredient") else "steps"
            heading = "Ingredients" if kind == "ingredients" else "Directions"
            set_heading(kind, heading)
            if remainder.strip():
                append_line(kind, remainder.strip())
            continue

        if _subheading(line):
            if current_kind is None:
                next_kind = next((candidate for candidate in strong_kinds[index + 1:] if candidate), None)
                current_kind = next_kind or "ingredients"
            pending_heading = line.rstrip(":").strip()
            continue

        kind = strong_kinds[index]
        if kind is None:
            if current_kind is not None:
                kind = current_kind
            else:
                next_kind = next((candidate for candidate in strong_kinds[index + 1:] if candidate), None)
                # Introductory sentence fragments stay descriptions. Unlabeled
                # food lines immediately before instructions stay ingredients.
                if next_kind == "steps" and not line.rstrip().endswith((".", "!", "?")):
                    kind = "ingredients"
                elif next_kind == "ingredients":
                    description.append(line)
                    continue
                else:
                    description.append(line)
                    continue
        append_line(kind, line)
        current_kind = kind

    has_ingredients = any(section.kind == "ingredients" and section.lines for section in builders)
    has_steps = any(section.kind == "steps" and section.lines for section in builders)
    if not has_ingredients and not has_steps:
        # Unknown-only bodies are preserved verbatim in a single ingredient
        # section so the save contract never drops source text.
        for line in lines:
            append_line("ingredients", line)
        description = []
        warnings.append("No ingredient lines were confidently detected; all body lines were preserved as ingredients.")
    elif not has_ingredients:
        warnings.append("No ingredient lines were confidently detected; preparation text is preserved and this post needs review.")
    if not any(section.kind == "steps" and section.lines for section in builders):
        warnings.append("No preparation steps were confidently detected.")
    sections = tuple(
        RecipeSectionDraft(kind=section.kind, heading=section.heading, lines=section.lines)
        for section in builders if section.lines
    )
    return "\n".join(description).strip() or None, sections, tuple(warnings)


def _parse_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").date()
    except ValueError:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None


def _read_channel(data: bytes, max_bytes: int) -> ET.Element:
    if max_bytes <= 0 or len(data) > max_bytes:
        raise WxrParseError(f"WXR exceeds the {max_bytes}-byte size limit.")
    declarations = data.lower()
    if b"<!doctype" in declarations or b"<!entity" in declarations:
        raise WxrParseError("WXR document type and entity declarations are not supported.")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise WxrParseError("WXR must be well-formed XML.") from exc
    channel = root.find("channel")
    if channel is None or root.tag.rsplit("}", 1)[-1] != "rss":
        raise WxrParseError("Input must be a WordPress RSS export with a channel.")
    return channel


def parse_wxr_bytes(
    data: bytes,
    *,
    source_name: str = "<memory>",
    max_bytes: int = MAX_WXR_BYTES,
) -> WxrDocument:
    """Parse a WXR byte sequence without filesystem, database, or network effects."""
    channel = _read_channel(data, max_bytes)
    taxonomy: dict[str, str] = {}
    for term in channel.findall(_tag("category")):
        slug = _text(term.find(_tag("category_nicename"))).casefold()
        if slug:
            taxonomy[slug] = _text(term.find(_tag("category_parent"))).casefold()

    recipes: list[WxrPost] = []
    pages: list[tuple[str, str, str]] = []
    excluded: list[tuple[str, str]] = []
    attachment_urls: dict[str, str] = {}
    published: list[ET.Element] = []
    total_posts = 0
    for item in channel.findall("item"):
        post_type = _child_text(item, "post_type")
        post_id = _child_text(item, "post_id")
        status = _child_text(item, "status") or "unknown"
        if post_type == "attachment":
            url = _child_text(item, "attachment_url") or _text(item.find("link"))
            if post_id and url:
                attachment_urls[post_id] = url
        elif post_type == "page" and status == "publish":
            slug = MENU_PAGE_TITLES.get(_text(item.find("title")).casefold())
            if slug and post_id:
                pages.append((post_id, slug, f"/?page_id={post_id}"))
        elif post_type == "post":
            total_posts += 1
            if status == "publish":
                published.append(item)
            else:
                excluded.append((post_id, status))

    seen_ids: set[str] = set()
    for item in published:
        post_id = _child_text(item, "post_id")
        title = _text(item.find("title"))
        if not post_id or not title:
            raise WxrParseError("Every published post must have a non-empty ID and title.")
        if post_id in seen_ids:
            raise WxrParseError(f"Published post ID {post_id!r} occurs more than once.")
        seen_ids.add(post_id)

        source_slug = _child_text(item, "post_name")
        description, sections, body_warnings = _parse_body(_text(item.find(f"{{{CONTENT}}}encoded")))
        category_slugs, evidence = _resolve_categories(item, taxonomy)
        image_id = _postmeta(item).get("_thumbnail_id", "")
        image_url = attachment_urls.get(image_id) if image_id else None
        warnings = list(body_warnings)
        if image_id and not image_url:
            warnings.append(f"Featured attachment {image_id!r} is missing from the WXR.")
        empty_fields = ["servings", "times"]
        if not image_url:
            empty_fields.append("image")
        recipes.append(
            WxrPost(
                post_id=post_id,
                title=title,
                source_slug=source_slug,
                source_url=_text(item.find("link")),
                published_date=_parse_date(_child_text(item, "post_date")),
                description=description,
                sections=sections,
                category_slugs=category_slugs,
                category_evidence=evidence,
                tags=(),
                legacy_path=f"/?p={post_id}",
                image_url=image_url,
                needs_slug_regeneration=(not source_slug or bool(_INVALID_SLUG.fullmatch(source_slug))),
                empty_fields=tuple(empty_fields),
                warnings=tuple(warnings),
            )
        )
    pages.sort(key=lambda row: (0, int(row[0])) if row[0].isdigit() else (1, row[0]))
    return WxrDocument(
        source_name=source_name,
        recipes=tuple(recipes),
        category_pages=tuple(pages),
        excluded_posts=tuple(excluded),
        attachment_urls=tuple(sorted(attachment_urls.items())),
        total_post_count=total_posts,
    )


def parse_wxr(path: Path, *, max_bytes: int = MAX_WXR_BYTES) -> tuple[WxrDocument, bytes]:
    """Read one bounded WXR file and return parsed content plus the exact bytes."""
    try:
        if path.stat().st_size > max_bytes:
            raise WxrParseError(f"WXR exceeds the {max_bytes}-byte size limit.")
        data = path.read_bytes()
    except WxrParseError:
        raise
    except OSError as exc:
        raise WxrParseError("WXR file could not be read.") from exc
    return parse_wxr_bytes(data, source_name=str(path), max_bytes=max_bytes), data
