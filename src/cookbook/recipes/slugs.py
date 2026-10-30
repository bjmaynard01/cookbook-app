"""Deterministic slug normalization for recipes and tags (plan Task 9, Task 1).

Pure and deterministic: no database, web, or third-party imports. Suffix
allocation for collisions (`-2`, `-3`, ...) is intentionally NOT done here —
that is the database-backed resolver's job in later tasks.
"""
from __future__ import annotations

import unicodedata
from typing import Literal


def _is_slug_char(char: str) -> bool:
    # Keep every Unicode letter plus any numeric character; everything else
    # is treated as a separator. Category L* covers all letter scripts
    # (latin, CJK, cyrillic, ...); N* covers all numerics: Nd (0-9 and other
    # decimal digits), Nl (letter numerics like Ⅳ), and No (other numerics
    # like ², ①).
    category = unicodedata.category(char)
    return category.startswith("L") or category.startswith("N")


def slugify(
    value: str,
    *,
    fallback: Literal["recipe", "tag"],
    max_length: int = 200,
) -> str:
    """Normalize a title/tag name into a stable, URL-safe slug.

    Normalizes to NFC, lowercases, keeps Unicode letters and digits,
    replaces each run of other characters with a single hyphen, trims
    edge hyphens, and caps the result at ``max_length`` characters.
    The ``fallback`` is returned when nothing survives normalization.
    """
    slug_chars: list[str] = []
    for char in unicodedata.normalize("NFC", value).lower():
        if _is_slug_char(char):
            slug_chars.append(char)
        elif not slug_chars or slug_chars[-1] != "-":
            # Separator that is not already adjacent to one, so runs of
            # any separators collapse to a single hyphen.
            slug_chars.append("-")

    slug = "".join(slug_chars).strip("-")
    if not slug:
        slug = fallback
    return slug[:max_length]
