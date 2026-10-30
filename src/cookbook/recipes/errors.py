"""Service-layer error types for the recipe service (plan Task 2, step 2).

Callers (routes, CLI) catch these instead of raw SQLAlchemy/DB errors or
validation-internal exceptions: the state each one retains is enough to build
an HTTP response or form feedback without parsing messages.
"""
from __future__ import annotations

from collections.abc import Sequence


class RecipeServiceError(Exception):
    """Base error for all recipe service failures."""


class UnknownCategories(RecipeServiceError):
    """One or more supplied category IDs do not exist.

    ``unknown_ids`` retains the offending IDs (first-occurrence order,
    already de-duplicated) so a caller can report exactly which ones failed.
    """

    def __init__(self, unknown_ids: Sequence[str]) -> None:
        self.unknown_ids = list(unknown_ids)
        super().__init__(f"Unknown category id(s): {', '.join(self.unknown_ids)}")


class InvalidRecipeInput(RecipeServiceError):
    """The save data is not save-ready as submitted.

    ``field_errors`` maps stable field names (``title``, ``slug``, ``tags``,
    ...) to human-readable messages, mirroring
    :class:`RecipeValidationError.field_errors` from Task 8.
    """

    def __init__(self, field_errors: dict[str, str]) -> None:
        self.field_errors = dict(field_errors)
        super().__init__("; ".join(f"{field}: {message}" for field, message in field_errors.items()))


class RecipeNotFound(RecipeServiceError):
    """A recipe id or slug was not found."""

    def __init__(self, recipe_ref: str | None = None) -> None:
        self.recipe_ref = recipe_ref
        super().__init__(f"Recipe not found: {recipe_ref}" if recipe_ref else "Recipe not found.")


class RecipeAliasConflict(RecipeServiceError):
    """A slug/path the recipe wants is already taken by a URL alias."""

    def __init__(self, slug: str | None = None) -> None:
        self.slug = slug
        super().__init__(f"Slug conflict with a URL alias: {slug}" if slug else "Slug conflict with a URL alias.")


class RecipeHasAliases(RecipeServiceError):
    """The recipe still owns URL alias rows, which block the operation."""

    def __init__(self, recipe_id: str) -> None:
        self.recipe_id = recipe_id
        super().__init__(f"Recipe {recipe_id} has URL aliases.")
