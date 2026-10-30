# Shared Recipe Draft and Save Validation Implementation Plan

> **For agentic workers:** Follow the owner's established workflow: one small subtask per fresh Qwen session, then independent review and verification. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Define one reusable `RecipeDraft` contract for manual, imported, and ingested recipe data, with explicit validation for saving a complete recipe.

**Architecture:** Keep draft parsing separate from save readiness: partial drafts remain valid while `validate_for_save` reports the structural requirements before persistence. Reuse the existing SQLAlchemy recipe body and relationship models; defer database writes, CRUD routes, forms, and slug collision handling to Task 9.

**Tech Stack:** Python 3.12+, Pydantic 2, SQLAlchemy 2.0 models as existing persistence shapes, pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-cookbook-design.md` §§2, 3.1, 3.2, 3.4, 4, 8; owner resolution: generate a slug from title when creating a recipe, then allow editing it.

## Global Constraints

- `RecipeDraft` is the single contract emitted by every parse stage, rendered by the preview form, and accepted by the save endpoint (§4).
- Partial parse is valid; all draft fields may be absent except `title` as a model field, and missing recipe content must not make draft construction fail (§4, D4).
- `recipe_sections` and `recipe_items` remain the body shape: ordered sections with `kind` `ingredients`, `steps`, or `note`; `raw_text` is the source of truth for every line (DL-3).
- Structural requirements are enforced at save/API time, not as database constraints (§8, D12).
- The existing Pydantic 2 and SQLAlchemy 2 dependencies are sufficient; do not add dependencies.
- Do not advance the running development app or database while this task branch is unmerged; any database verification must use the disposable test database.
- Do not create or update progress, handoff, or checkpoint notes.

## Review Focus

- Empty or whitespace-only title: a draft may be represented, but save validation rejects it with a title field error.
- Missing categories or ingredient lines: partial drafts remain valid, but saving reports the corresponding field errors.
- Ingredient lines with blank `raw_text`: they do not satisfy the required-ingredient rule and are omitted or reported consistently by validation.
- Missing or partial prep/cook times: total time is recomputed from submitted values; preserve an explicit total when the contract allows it and the design rule permits it.
- Provenance supplied on an update: the contract may carry it for display, but update persistence must treat provenance as read-only (handled by Task 9).

---

### Task 1: Define the Shared RecipeDraft Contract

**Files:**
- Create: `src/cookbook/recipes/__init__.py`
- Create: `src/cookbook/recipes/drafts.py`
- Create: `tests/test_recipe_draft.py`

**Interfaces:**
- Produces `RecipeDraft`, `RecipeIngredientDraft`, `RecipeStepDraft`, `RecipeSectionDraft`, and `RecipeProvenanceDraft` Pydantic models, exported by `cookbook.recipes` for later ingestion, forms, and save services.
- `RecipeDraft` fields: `title`; nullable description, servings text/number, prep/cook/total minutes, hero image reference, provenance; ordered sections; tag names; suggested category names; parse notes; optional duplicate marker `{id, title}`. Keep optional fields nullable/defaulted so sparse parses are valid.
- A section carries optional `heading`, `kind` (`ingredients`, `steps`, or `note`), and ordered lines. Ingredient lines carry `raw_text` plus optional `quantity_text`, `unit`, `item`, and `preparation`; step/note lines carry `raw_text` as their source text. This matches DL-3 and preserves named ingredient subsections.
- Provenance contains `source_system`, `source_id`, `source_url`, `source_name`, `extraction_method`, and `imported_at`; the model is a transport contract and does not grant update-write permission.

- [ ] **Step 1: Add contract tests** for sparse/partial drafts, Unicode text, ingredient parse fields, section ordering and kinds, provenance, parse notes, and duplicate marker.
- [ ] **Step 2: Run the focused tests and confirm they fail** because the draft models do not exist.
- [ ] **Step 3: Implement and export the Pydantic models** in `drafts.py` and `__init__.py`, using Pydantic 2 field types and defaults; avoid database access and coercions that discard source text.
- [ ] **Step 4: Run `pytest tests/test_recipe_draft.py -q`** and confirm all contract cases pass.

### Task 2: Add Save-Readiness Validation

**Files:**
- Create: `src/cookbook/recipes/validation.py`
- Modify: `src/cookbook/recipes/drafts.py` only if a validation-facing type is needed
- Test: `tests/test_recipe_validation.py`

**Interfaces:**
- Produces `validate_for_save(draft: RecipeDraft, *, category_names: Sequence[str]) -> RecipeSaveData` (or an equivalent typed result) and `RecipeValidationError` carrying stable field-to-message errors. This stays independent of FastAPI and database sessions; Task 9 maps the result to ORM rows and HTTP responses.
- `RecipeSaveData` contains the trimmed title, normalized nonblank ingredient/step/section content, categories, tags, and resolved total minutes; provenance remains separate and unchanged.
- Category names are supplied by the caller after category resolution; require at least one category and at least one nonblank ingredient `raw_text`.
- Missing steps are allowed and produce a nonblocking warning, not a validation error.
- Recompute `total_minutes` from prep + cook when both are supplied; when a total is explicitly supplied, preserve it. Never invent missing values.
- Slug formatting, collision suffixing, recipe persistence, and provenance write policy belong to Task 9.

- [ ] **Step 1: Add failing tests** for trimmed/nonblank title, each required-field error, blank ingredient lines, valid whitespace cleanup, optional steps warning, total-time cases (both components, explicit total, incomplete components), and unchanged provenance.
- [ ] **Step 2: Run the focused tests and confirm the missing validator causes failure.**
- [ ] **Step 3: Implement the pure validator** with deterministic field errors and warning output; do not add database or request-framework dependencies.
- [ ] **Step 4: Run `pytest tests/test_recipe_draft.py tests/test_recipe_validation.py -q`** and confirm all cases pass.

## Task 8 Acceptance

- One Pydantic contract represents sparse parse results and save-ready recipe content without translating between competing draft shapes.
- Save validation enforces trimmed nonempty title, at least one category, and at least one ingredient with nonblank `raw_text`; missing steps remain allowed with a warning.
- Total time follows §8 and missing values remain missing.
- The draft’s provenance and raw text survive validation unchanged.
- The package exports its public contract without importing the web application or opening a database connection.
- No routes, UI, image-file handling, recipe CRUD writes, migrations, or slug collision logic are added in this task.
