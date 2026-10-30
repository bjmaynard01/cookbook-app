"""Frozen read-model records for the recipe service (plan Task 2, step 2).

These are the service's read-side return shapes, defined now (even though
the read methods land later) so the public contract is fixed up front.
Frozen: callers must not mutate them; the ORM objects inside are shared
with the session and expire normally.
"""
from __future__ import annotations

from dataclasses import dataclass

from ..models import Category, Recipe, RecipeItem, RecipeSection, Tag


@dataclass(frozen=True)
class RecipeSectionDetails:
    """One body section stored in order, with its lines in order."""

    section: RecipeSection
    items: tuple[RecipeItem, ...]


@dataclass(frozen=True)
class RecipeDetails:
    """A full recipe with everything a read view needs, pre-assembled."""

    recipe: Recipe
    sections: tuple[RecipeSectionDetails, ...]
    categories: tuple[Category, ...]
    tags: tuple[Tag, ...]
