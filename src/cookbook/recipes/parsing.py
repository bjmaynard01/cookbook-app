"""Deterministic JSON-LD and microdata parsing into the shared RecipeDraft."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .drafts import (
    RecipeDraft,
    RecipeIngredientDraft,
    RecipeProvenanceDraft,
    RecipeSectionDraft,
    RecipeStepDraft,
)
from .ingest import FetchedPage


_QUANTITY_PREFIX = re.compile(
    r"^\s*(?P<quantity>(?:\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|"
    r"\d+(?:\.\d+)?\s*[¼½⅓⅔¾⅛⅜⅝⅞]?|[¼½⅓⅔¾⅛⅜⅝⅞]))(?=\s|$)"
)
_FRACTIONS = {
    "¼": Decimal(1) / 4,
    "½": Decimal(1) / 2,
    "⅓": Decimal(1) / 3,
    "⅔": Decimal(2) / 3,
    "¾": Decimal(3) / 4,
    "⅛": Decimal(1) / 8,
    "⅜": Decimal(3) / 8,
    "⅝": Decimal(5) / 8,
    "⅞": Decimal(7) / 8,
}
_UNITS = (
    "fluid ounces", "fluid ounce", "fl. oz.", "fl oz", "tablespoons", "tablespoon",
    "tbsp.", "tbsp", "teaspoons", "teaspoon", "tsp.", "tsp", "kilograms", "kilogram",
    "grams", "gram", "milligrams", "milligram", "milliliters", "milliliter", "ml",
    "liters", "liter", "litres", "litre", "pounds", "pound", "lbs.", "lbs", "lb.",
    "lb", "ounces", "ounce", "oz.", "oz", "cups", "cup", "pints", "pint", "pt",
    "quarts", "quart", "qt", "gallons", "gallon", "g", "tablespoonfuls", "pinches",
    "pinch", "cloves", "clove", "cans", "can", "packages", "package", "pkg", "slices",
    "slice", "sticks", "stick", "heads", "head", "bunches",
    "bunch", "sprigs", "sprig", "leaves", "leaf", "pieces", "piece", "°c", "°f",
)
_UNIT_PATTERN = re.compile(
    r"^(?P<unit>" + "|".join(re.escape(unit) for unit in sorted(_UNITS, key=len, reverse=True))
    + r")(?:\s+|$)",
    re.IGNORECASE,
)
_ISO_DURATION = re.compile(
    r"^P(?:(?P<weeks>\d+(?:\.\d+)?)W)?(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$",
    re.IGNORECASE,
)
_PLAIN_DURATION = re.compile(
    r"^\s*(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)\s*$", re.I
)
_GARBAGE_INGREDIENT = re.compile(r"^\s*(?:see|refer to)\b.*\b(?:photo|picture|image)\b.*$", re.I)
_ATTRIBUTE_VALUE_TAGS = {
    "audio": "src",
    "embed": "src",
    "iframe": "src",
    "img": "src",
    "link": "href",
    "meta": "content",
    "object": "data",
    "source": "src",
    "time": "datetime",
    "video": "src",
    "a": "href",
    "area": "href",
    "data": "value",
    "meter": "value",
}
_VOID_TAGS = {
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
    "param", "source", "track", "wbr",
}


def _text(value: object) -> str | None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if current.strip():
                return current.strip()
        elif isinstance(current, (int, float, Decimal)) and not isinstance(current, bool):
            return str(current)
        elif isinstance(current, list):
            pending.extend(reversed(current))
        elif isinstance(current, dict):
            pending.extend(
                reversed(
                    [
                        current[key]
                        for key in ("@value", "text", "name", "value")
                        if key in current
                    ]
                )
            )
    return None


def _raw_text(value: object) -> str | None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if current.strip():
                return current
        elif isinstance(current, (int, float, Decimal)) and not isinstance(current, bool):
            return str(current)
        elif isinstance(current, dict):
            pending.extend(
                reversed(
                    [
                        current[key]
                        for key in ("text", "@value", "name", "value")
                        if key in current
                    ]
                )
            )
    return None


def _lookup(mapping: dict[str, object], *names: str) -> object | None:
    wanted = {name.casefold() for name in names}
    for key, value in mapping.items():
        if key.casefold() in wanted and value is not None:
            return value
    return None


def _schema_type_name(value: str) -> str:
    value = value.strip().split("#")[-1].rstrip("/")
    return value.rsplit("/", 1)[-1].rsplit(":", 1)[-1]


def _recipe_type(value: object) -> bool:
    types = value if isinstance(value, list) else [value]
    for item in types:
        if not isinstance(item, str):
            continue
        name = _schema_type_name(item)
        if name.casefold() == "recipe":
            return True
    return False


def _walk_recipes(value: object, seen: set[int] | None = None):
    if seen is None:
        seen = set()
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(reversed(item))
        elif isinstance(item, dict) and id(item) not in seen:
            seen.add(id(item))
            if _recipe_type(item.get("@type")):
                yield item
            pending.extend(reversed(list(item.values())))


def _items(value: object) -> list[object]:
    result: list[object] = []
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, list):
            pending.extend(reversed(current))
        elif isinstance(current, dict):
            raw = _raw_text(current)
            if raw is not None:
                result.append(raw)
        elif _raw_text(current) is not None:
            result.append(current)
    return result


def _text_list(value: object, *, split_commas: bool = False) -> list[str]:
    result: list[str] = []
    for item in _items(value):
        text = _text(item)
        if not text:
            continue
        parts = text.split(",") if split_commas else [text]
        for part in parts:
            clean = part.strip()
            if clean and clean not in result:
                result.append(clean)
    return result


def _decimal_quantity(value: str) -> Decimal | None:
    token = value.strip().replace(" ", "")
    try:
        for fraction, decimal in _FRACTIONS.items():
            if token.endswith(fraction):
                whole = token[: -len(fraction)]
                return Decimal(whole or "0") + decimal
        if "/" in token:
            numerator, denominator = token.split("/", 1)
            return Decimal(numerator) / Decimal(denominator)
        return Decimal(token)
    except (InvalidOperation, ZeroDivisionError):
        return None


def _split_ingredient(raw_text: str) -> RecipeIngredientDraft:
    match = _QUANTITY_PREFIX.match(raw_text)
    if not match:
        return RecipeIngredientDraft(raw_text=raw_text)
    quantity = match.group("quantity").strip()
    remainder = raw_text[match.end() :].strip()
    unit: str | None = None
    unit_match = _UNIT_PATTERN.match(remainder)
    if unit_match:
        unit = unit_match.group("unit")
        remainder = remainder[unit_match.end() :].strip()
    if "," in remainder:
        item, preparation = remainder.split(",", 1)
        preparation = preparation.strip() or None
    else:
        item, preparation = remainder, None
    return RecipeIngredientDraft(
        raw_text=raw_text,
        quantity_text=quantity,
        unit=unit,
        item=item.strip() or None,
        preparation=preparation,
    )


def _minutes(value: object) -> int | None:
    text = _text(value)
    if not text:
        return None
    match = _ISO_DURATION.fullmatch(text)
    if match:
        groups = {key: Decimal(value or "0") for key, value in match.groupdict().items()}
        if not any(groups.values()):
            return None
        seconds = (
            groups["weeks"] * 604800
            + groups["days"] * 86400
            + groups["hours"] * 3600
            + groups["minutes"] * 60
            + groups["seconds"]
        )
    else:
        plain = _PLAIN_DURATION.fullmatch(text)
        if not plain:
            return None
        amount = Decimal(plain.group(1))
        unit = plain.group(2).casefold()
        if unit.startswith(("hour", "hr")):
            seconds = amount * 3600
        elif unit.startswith(("minute", "min")):
            seconds = amount * 60
        else:
            seconds = amount
    return int((seconds / 60).to_integral_value(rounding=ROUND_CEILING))


def _yield_parts(value: object) -> tuple[str | None, Decimal | None, str | None]:
    raw = _text(value)
    if not raw:
        return None, None, None
    parse_text = re.sub(r"^\s*(?:serves?|makes?)\s+", "", raw, flags=re.IGNORECASE)
    match = _QUANTITY_PREFIX.match(parse_text)
    if not match:
        return raw, None, None
    token = match.group("quantity").strip()
    number = _decimal_quantity(token)
    if number is not None and number <= 0:
        number = None
    unit = parse_text[match.end() :].strip() or None
    if unit and re.match(r"^(?:to\b|[-–])", unit, re.IGNORECASE):
        return raw, None, None
    return raw, number, unit


def _image_url(value: object, base_url: str) -> str | None:
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, list):
            pending.extend(reversed(current))
            continue
        if isinstance(current, dict):
            pending.extend(
                reversed([current[key] for key in ("url", "contentUrl", "@id") if key in current])
            )
            continue
        raw = current.strip() if isinstance(current, str) else None
        if not raw:
            continue
        absolute = urljoin(base_url, raw)
        if urlsplit(absolute).scheme.casefold() in {"http", "https"}:
            return absolute
    return None


def _step_sections(value: object) -> list[RecipeSectionDraft]:
    sections: list[RecipeSectionDraft] = []
    loose_lines: list[RecipeStepDraft] = []

    def flush_loose() -> None:
        if loose_lines:
            sections.append(RecipeSectionDraft(kind="steps", lines=list(loose_lines)))
            loose_lines.clear()

    def lines_for(items: object) -> list[RecipeStepDraft]:
        lines: list[RecipeStepDraft] = []
        pending = [items]
        while pending:
            item = pending.pop()
            if isinstance(item, list):
                pending.extend(reversed(item))
            elif isinstance(item, dict):
                nested = _lookup(item, "itemListElement", "steps")
                if nested is not None:
                    pending.append(nested)
                else:
                    body = _raw_text(_lookup(item, "text", "name", "description"))
                    if body and body.strip():
                        lines.append(RecipeStepDraft(body=body))
            else:
                body = _raw_text(item)
                if body and body.strip():
                    lines.append(RecipeStepDraft(body=body))
        return lines

    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, list):
            pending.extend(reversed(item))
            continue
        if isinstance(item, dict):
            types = item.get("@type")
            is_section = any(
                isinstance(t, str) and _schema_type_name(t).casefold() == "howtosection"
                for t in (types if isinstance(types, list) else [types])
            )
            nested = _lookup(item, "itemListElement", "steps")
            if is_section:
                flush_loose()
                lines = lines_for(nested)
                if lines:
                    sections.append(
                        RecipeSectionDraft(
                            kind="steps",
                            heading=_text(_lookup(item, "name", "headline")),
                            lines=lines,
                        )
                    )
            elif nested is not None:
                pending.append(nested)
            else:
                body = _raw_text(_lookup(item, "text", "name", "description"))
                if body and body.strip():
                    loose_lines.append(RecipeStepDraft(body=body))
            continue
        body = _raw_text(item)
        if body and body.strip():
            loose_lines.append(RecipeStepDraft(body=body))

    flush_loose()
    return sections


def _draft_from_properties(
    properties: dict[str, object],
    page: FetchedPage,
    *,
    method: str,
    parse_notes: list[str] | None = None,
) -> RecipeDraft:
    notes = list(parse_notes or [])
    title = _text(_lookup(properties, "name", "headline")) or page.title.strip()
    description = _text(_lookup(properties, "description")) or page.description
    raw_ingredients = []
    ingredient_values: list[object] = []
    for alias in ("recipeIngredient", "ingredients"):
        ingredient_values.extend(_items(_lookup(properties, alias)))
    for item in ingredient_values:
        raw = _raw_text(item)
        if raw is not None:
            raw_ingredients.append(raw)
    sections: list[RecipeSectionDraft] = []
    if raw_ingredients:
        sections.append(
            RecipeSectionDraft(
                kind="ingredients",
                lines=[_split_ingredient(raw) for raw in raw_ingredients],
            )
        )
        if any(_GARBAGE_INGREDIENT.fullmatch(raw) for raw in raw_ingredients):
            notes.append(
                "Some ingredient text appears incomplete (for example, it refers to a photo)."
            )
    sections.extend(_step_sections(_lookup(properties, "recipeInstructions", "instructions")))

    raw_yield = _lookup(properties, "recipeYield", "yield")
    if isinstance(raw_yield, list):
        raw_yield = next((item for item in raw_yield if _text(item)), None)
    if isinstance(raw_yield, dict) and raw_yield.get("value") is not None:
        raw_yield = (
            f"{_text(raw_yield['value']) or ''} "
            f"{_text(raw_yield.get('unitText')) or ''}"
        ).strip()
    servings_text, servings_number, servings_unit = _yield_parts(raw_yield)

    times: dict[str, int | None] = {}
    for field_name, names in (
        ("prep_minutes", ("prepTime",)),
        ("cook_minutes", ("cookTime",)),
        ("total_minutes", ("totalTime",)),
    ):
        raw = _lookup(properties, *names)
        times[field_name] = _minutes(raw)
        if raw is not None and _text(raw) and times[field_name] is None:
            notes.append("Some recipe timing information could not be interpreted.")

    hero_image = _image_url(_lookup(properties, "image"), page.final_url) or page.og_image
    return RecipeDraft(
        title=title,
        description=description,
        sections=sections,
        tags=_text_list(_lookup(properties, "keywords", "recipeKeywords"), split_commas=True),
        suggested_categories=_text_list(
            _lookup(properties, "recipeCategory"), split_commas=True
        ),
        parse_notes=list(dict.fromkeys(notes)),
        servings_text=servings_text,
        servings_number=servings_number,
        servings_unit=servings_unit,
        prep_minutes=times["prep_minutes"],
        cook_minutes=times["cook_minutes"],
        total_minutes=times["total_minutes"],
        hero_image=hero_image,
        provenance=RecipeProvenanceDraft(
            source_url=page.source_url,
            extraction_method=method,
        ),
    )


def _recipe_score(candidate: dict[str, object]) -> int:
    score = 0
    scalar_fields = (
        ("name", "headline"),
        ("description",),
        ("recipeYield", "yield"),
        ("prepTime",),
        ("cookTime",),
        ("totalTime",),
    )
    score += sum(bool(_text(_lookup(candidate, *aliases))) for aliases in scalar_fields)
    score += bool(_text_list(_lookup(candidate, "recipeCategory"), split_commas=True))
    score += bool(
        _text_list(_lookup(candidate, "keywords", "recipeKeywords"), split_commas=True)
    )
    score += bool(_image_url(_lookup(candidate, "image"), "https://example.invalid/"))
    score += any(
        _raw_text(item) is not None
        for alias in ("recipeIngredient", "ingredients")
        for item in _items(_lookup(candidate, alias))
    )
    score += bool(_step_sections(_lookup(candidate, "recipeInstructions", "instructions")))
    return int(score)


def _json_ld_draft(page: FetchedPage) -> tuple[RecipeDraft | None, list[str]]:
    notes: list[str] = []
    candidates: list[tuple[int, RecipeDraft]] = []
    for block in page.json_ld:
        try:
            parsed = json.loads(block)
        except (json.JSONDecodeError, TypeError, RecursionError):
            notes.append("A JSON-LD block on the page could not be read.")
            continue
        for candidate in _walk_recipes(parsed):
            score = _recipe_score(candidate)
            if score:
                candidates.append(
                    (
                        score,
                        _draft_from_properties(candidate, page, method="jsonld"),
                    )
                )
    if not candidates:
        return None, list(dict.fromkeys(notes))
    best_score = max(score for score, _ in candidates)
    chosen = next(draft for score, draft in candidates if score == best_score)
    if notes:
        chosen = chosen.model_copy(
            update={"parse_notes": list(dict.fromkeys([*notes, *chosen.parse_notes]))}
        )
    return chosen, notes


@dataclass(slots=True)
class _HTMLNode:
    tag: str
    attrs: dict[str, str]
    children: list[_HTMLNode | str] = field(default_factory=list)


class _MicrodataTreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HTMLNode("document", {})
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _HTMLNode(
            tag.casefold(),
            {key.casefold(): value or "" for key, value in attrs},
        )
        self.stack[-1].children.append(node)
        if node.tag not in _VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if self.stack[-1].tag == tag.casefold():
            self.stack.pop()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def _descendants(node: _HTMLNode):
    pending = [child for child in reversed(node.children) if isinstance(child, _HTMLNode)]
    while pending:
        child = pending.pop()
        yield child
        pending.extend(
            item for item in reversed(child.children) if isinstance(item, _HTMLNode)
        )


def _property_descendants(node: _HTMLNode):
    pending = [child for child in reversed(node.children) if isinstance(child, _HTMLNode)]
    while pending:
        child = pending.pop()
        if "itemscope" in child.attrs:
            continue
        yield child
        pending.extend(
            item for item in reversed(child.children) if isinstance(item, _HTMLNode)
        )


def _node_text(node: _HTMLNode) -> str:
    parts: list[str] = []
    pending: list[_HTMLNode | str] = list(reversed(node.children))
    while pending:
        child = pending.pop()
        if isinstance(child, str):
            parts.append(child)
        elif "itemscope" not in child.attrs:
            pending.extend(reversed(child.children))
    return "".join(parts)


def _microdata_value(node: _HTMLNode) -> str:
    attr = _ATTRIBUTE_VALUE_TAGS.get(node.tag)
    if attr and node.attrs.get(attr):
        return node.attrs[attr]
    return _node_text(node)


def _microdata_properties(scope: _HTMLNode) -> dict[str, object]:
    props: dict[str, list[str]] = {}
    for node in _property_descendants(scope):
        itemprop = node.attrs.get("itemprop", "")
        if not itemprop:
            continue
        value = _microdata_value(node)
        for raw_name in itemprop.split():
            name = raw_name.casefold()
            canonical = {
                "ingredients": "recipeIngredient",
                "recipeingredient": "recipeIngredient",
                "recipeinstructions": "recipeInstructions",
                "instructions": "instructions",
                "preptime": "prepTime",
                "cooktime": "cookTime",
                "totaltime": "totalTime",
                "recipeyield": "recipeYield",
                "recipecategory": "recipeCategory",
                "recipekeywords": "recipeKeywords",
            }.get(name, name)
            props.setdefault(canonical, []).append(value)
    return props


def _itemtype_is_recipe(itemtype: str) -> bool:
    return any(
        _schema_type_name(part).casefold() == "recipe"
        for part in itemtype.split()
    )


def _microdata_draft(page: FetchedPage) -> RecipeDraft | None:
    parser = _MicrodataTreeParser()
    parser.feed(page.html)
    parser.close()
    candidates: list[tuple[int, RecipeDraft]] = []
    for node in _descendants(parser.root):
        if "itemscope" not in node.attrs or not _itemtype_is_recipe(node.attrs.get("itemtype", "")):
            continue
        properties = _microdata_properties(node)
        score = _recipe_score(properties)
        if score:
            candidates.append(
                (score, _draft_from_properties(properties, page, method="microdata"))
            )
    if not candidates:
        return None
    best_score = max(score for score, _ in candidates)
    return next(draft for score, draft in candidates if score == best_score)


def parse_recipe_page(page: FetchedPage) -> RecipeDraft:
    """Parse JSON-LD first, then microdata; return an empty sparse draft if absent."""
    json_draft, parse_notes = _json_ld_draft(page)
    if json_draft is not None:
        return json_draft
    microdata_draft = _microdata_draft(page)
    if microdata_draft is not None:
        if parse_notes:
            microdata_draft = microdata_draft.model_copy(
                update={
                    "parse_notes": list(
                        dict.fromkeys([*parse_notes, *microdata_draft.parse_notes])
                    )
                }
            )
        return microdata_draft
    return RecipeDraft(
        title="",
        parse_notes=parse_notes,
        provenance=RecipeProvenanceDraft(source_url=page.source_url),
    )


__all__ = ["parse_recipe_page"]
