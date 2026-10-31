"""Shared recipe draft contract (ingest plan Task 1)."""
from .drafts import (
    DuplicateRecipeDraft,
    RecipeDraft,
    RecipeIngredientDraft,
    RecipeProvenanceDraft,
    RecipeSectionDraft,
    RecipeStepDraft,
)
from .errors import (
    InvalidRecipeInput,
    RecipeAliasConflict,
    RecipeHasAliases,
    RecipeNotFound,
    RecipeServiceError,
    UnknownCategories,
)
from .ingest import FetchError, FetchedPage, UnsafeURLError, fetch_page
from .records import RecipeDetails, RecipeSectionDetails
from .service import create_recipe, delete_recipe, get_recipe, set_recipe_image, update_recipe
from .validation import RecipeSaveData, RecipeValidationError, ValidationWarning, validate_for_save

__all__ = [
    "DuplicateRecipeDraft",
    "FetchError",
    "FetchedPage",
    "InvalidRecipeInput",
    "RecipeAliasConflict",
    "RecipeDraft",
    "RecipeDetails",
    "RecipeHasAliases",
    "RecipeIngredientDraft",
    "RecipeNotFound",
    "RecipeProvenanceDraft",
    "RecipeSaveData",
    "RecipeSectionDetails",
    "RecipeSectionDraft",
    "RecipeServiceError",
    "RecipeStepDraft",
    "RecipeValidationError",
    "UnknownCategories",
    "UnsafeURLError",
    "ValidationWarning",
    "create_recipe",
    "delete_recipe",
    "get_recipe",
    "fetch_page",
    "set_recipe_image",
    "update_recipe",
    "validate_for_save",
]
