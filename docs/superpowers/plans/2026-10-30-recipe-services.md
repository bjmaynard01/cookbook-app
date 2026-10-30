# Recipe Persistence Services Implementation Plan

> **For agentic workers:** Follow the owner’s workflow: implement one small task per fresh Qwen session, then review and verify it before the next session. Steps use checkbox (`- [ ]`) syntax for tracking. Do not commit or push without owner authorization.

**Goal:** Add caller-transaction-managed services for recipe create, load, update, rename, and delete using the Task 8 save contract and existing schema.

**Architecture:** Module-level async functions accept an `AsyncSession`, flush changes, and leave commit/rollback to the caller. A focused slug module handles deterministic normalization; recipe operations atomically persist the recipe, ordered body, memberships, auto-created tags, and old-slug aliases.

**Tech Stack:** Python 3.12+, SQLAlchemy 2.0 async, Pydantic 2, MariaDB 11, pytest. No dependencies or migrations.

**Spec:** `docs/superpowers/specs/2026-10-30-recipe-services-design.md`; `docs/superpowers/specs/2026-09-20-cookbook-design.md` §§3.1–3.5, 4, 8 and decisions D3/D5/D6/D12/D13/D17/D24; Task 8 contract in `docs/superpowers/plans/2026-10-30-recipe-draft.md`.

## Global Constraints

- `Recipe.id` is immutable; the recipe slug is editable and is not identity (D3).
- Generate the initial slug from title; normalize lowercase, replace non-alphanumeric runs with `-`, collapse and trim hyphens, preserve Unicode letters/digits, and add `-2`, `-3`, etc. on collision.
- Recipe body shape is ordered sections and ordered lines; `raw_text` is the source of truth, and ingredient parse columns apply only to ingredient lines (DL-3/D4).
- Save requires a trimmed nonempty title, at least one existing selected category, and at least one nonblank ingredient line. Steps remain optional; total time uses Task 8 validation.
- Tags are free-form. Match trimmed names case-insensitively and create missing tags with deterministic unique slugs in the caller’s transaction.
- A recipe rename keeps every former `/r/<slug>` path as an alias to the same recipe. Recipe aliases and category aliases have restrictive target FKs (DL-6).
- `source_*`, `extraction_method`, and `imported_at` are read-only on update. `servings_number` is the structured quantity; preserve `servings_text` if supplied; do not add a separate servings-unit column.
- No routes, authorization, UI, image-file handling, search, ingest, importer, migrations, or schema edits in this task. Keep `image_path` unchanged.
- All database tests use the disposable isolated MariaDB from the existing auth/data-model verification procedure. Never read `.env` or write to the running development database.
- Do not create or update progress, handoff, or checkpoint notes.

## Review Focus

- Generated title or explicit slug collides with a recipe slug or existing `/r/<slug>` alias: choose the next suffix without stealing another route; test in Task 2 and Task 3.
- Concurrent creates choose the same candidate slug or tag slug: savepoint retry preserves the caller’s outer transaction; test collision recovery in Task 2.
- Rename path already aliases another target, or the new canonical path is this recipe’s own alias: fail atomically or remove the self-alias as designed; test in Task 3.
- Category IDs are missing, duplicated, or unknown: deduplicate valid IDs and report unknown IDs without partial writes; test in Task 2 and Task 3.
- Recipe delete races with or encounters aliases: raise `RecipeHasAliases` and preserve recipe/body/memberships; test in Task 4.
- Inputs exceed stored widths or become empty after slug normalization: report stable field errors without silent truncation or partial writes; test title, section heading, tag name, and explicit slug boundaries in Tasks 1–3.

---

### Task 1: Add Deterministic Slug Helpers

**Files:**
- Create: `src/cookbook/recipes/slugs.py`
- Test: `tests/test_recipe_slugs.py`

**Interfaces:**
- Produces `slugify(value: str, *, fallback: Literal["recipe", "tag"], max_length: int = 200) -> str`.
- Normalize with Unicode NFC, lowercase, retain Unicode letters and digits, turn each run of other characters into one hyphen, trim edge hyphens, and use the requested fallback if the result is empty.
- Return a base no longer than `max_length`; the database-backed suffix resolver reserves suffix length before appending `-2`, `-3`, etc.

- [ ] **Step 1: Add failing unit tests** for spaces/punctuation, repeated separators, accented and CJK letters, title/tag fallback, and the 200-character base limit.
- [ ] **Step 2: Run `tests/test_recipe_slugs.py`** and confirm the missing helper causes collection/import failure.
- [ ] **Step 3: Implement `slugify`** in `slugs.py` without database or web imports.
- [ ] **Step 4: Run `.venv/Scripts/python.exe -m pytest tests/test_recipe_slugs.py -q --tb=short -p no:cacheprovider`** and confirm all cases pass.

### Task 2: Create Recipes, Bodies, and Tag/Category Memberships

**Files:**
- Create: `src/cookbook/recipes/errors.py`
- Create: `src/cookbook/recipes/records.py`
- Create: `src/cookbook/recipes/service.py`
- Modify: `src/cookbook/recipes/__init__.py`
- Test: `tests/test_recipe_services.py`

**Interfaces:**
- Consumes `RecipeSaveData` from `cookbook.recipes`, `AsyncSession`, and existing `Recipe`, `RecipeSection`, `RecipeItem`, `Category`, `Tag`, `recipe_categories`, and `recipe_tags` models.
- Produces `create_recipe(db: AsyncSession, data: RecipeSaveData, *, category_ids: Sequence[str], actor_user_id: str | None = None, slug: str | None = None) -> Recipe`.
- Produces `get_recipe(db: AsyncSession, recipe_id: str) -> RecipeDetails | None`.
- `RecipeSectionDetails` contains `section: RecipeSection` and `items: tuple[RecipeItem, ...]`. `RecipeDetails` contains `recipe`, ordered `sections`, categories as `tuple[Category, ...]` ordered by `Category.position`, and tags as `tuple[Tag, ...]` ordered by name.
- Define and export `RecipeServiceError`, `UnknownCategories`, `InvalidRecipeInput`, `RecipeNotFound`, `RecipeAliasConflict`, and `RecipeHasAliases` in `errors.py` as used by later tasks. `UnknownCategories` retains the unknown IDs; `InvalidRecipeInput` retains field errors.
- These functions flush but never commit/rollback. The caller must invoke them inside its transaction. An omitted create slug derives from title; a supplied slug is normalized, while an explicitly blank slug raises `InvalidRecipeInput`. Recipe slug and `/r/<slug>` alias paths both reserve canonical paths.
- Enforce the VARCHAR lengths declared by the existing models on service-written values; return field errors and never silently truncate. For candidate suffix `-N`, keep the slug at or below 200 characters by shortening only the base.

- [ ] **Step 1: Add failing disposable-DB tests** for create with generated and explicit slugs; blank explicit slug and stored-width errors; field/provenance/actor mapping; `servings_number` and supplied `servings_text` persistence with no separate unit column; section and line order; ingredient parse columns only on ingredient lines; selected category memberships; existing tag reuse by trimmed case-insensitive name; missing tag creation and tag-slug suffixing; collision with both recipe slug and alias path; concurrent same-slug create retry; aggregate loading order; caller rollback removing every write.
- [ ] **Step 2: Run the focused database tests** against the disposable MariaDB and confirm failure is caused by the absent service/API.
- [ ] **Step 3: Implement slug allocation and transaction-safe tag resolution** using nested savepoints for unique-key races so the caller’s outer transaction remains usable.
- [ ] **Step 4: Implement `create_recipe`**: verify all selected category IDs exist, insert the recipe, stamp create/modify actor IDs, persist provenance on create, create ordered sections/items and memberships, and flush. Keep `image_path` unchanged.
- [ ] **Step 5: Implement `get_recipe`** with ordered queries for sections/items, categories, and tags; return `None` for an unknown ID.
- [ ] **Step 6: Export the public service and record/error types** and run `.venv/Scripts/python.exe -m pytest tests/test_recipe_services.py -q --tb=short -p no:cacheprovider` against the disposable database.

### Task 3: Update Recipes and Preserve Slug Aliases

**Files:**
- Modify: `src/cookbook/recipes/service.py`
- Modify: `tests/test_recipe_services.py`

**Interfaces:**
- Produces `update_recipe(db: AsyncSession, recipe_id: str, data: RecipeSaveData, *, category_ids: Sequence[str], actor_user_id: str | None = None, slug: str | None = None) -> Recipe`.
- `slug=None` preserves the current slug. An explicit nonblank slug is normalized and may receive a collision suffix; an explicit blank slug raises `InvalidRecipeInput`.
- Lock the recipe row with `SELECT ... FOR UPDATE`. Preserve immutable ID, original provenance fields, `published_date`, and `image_path`; update ordinary recipe fields and `last_modified_by` when an actor ID is supplied, otherwise keep the existing modifier.
- Replace body sections/items and category/tag memberships from the submitted complete save data. Preserve all existing aliases.
- When the slug changes, add `/r/<old-slug>` targeting this recipe in the same transaction. Keep an existing self-alias, remove a self-alias if that path becomes the canonical slug again, and raise `RecipeAliasConflict` if a required alias path belongs to another target.

- [ ] **Step 1: Add failing DB tests** for title-only update preserving slug; explicit rename creating old-path alias; multiple renames preserving every old path; changing back to an old self-alias; slug collision suffixing including a concurrent rename; other-target alias conflict causing no partial mutation; full section/category/tag replacement; provenance and image path immutability; unknown recipe/category errors.
- [ ] **Step 2: Run each focused test** and confirm it fails for the missing update behavior.
- [ ] **Step 3: Implement `update_recipe`** within the caller-managed transaction; re-use slug allocation and tag resolution from Task 2.
- [ ] **Step 4: Run the full focused recipe-service file** against disposable MariaDB and confirm updates, aliases, and rollback behavior pass.

### Task 4: Delete Safely and Verify Recipe-Service Integration

**Files:**
- Modify: `src/cookbook/recipes/service.py`
- Modify: `tests/test_recipe_services.py`

**Interfaces:**
- Produces `delete_recipe(db: AsyncSession, recipe_id: str) -> None`.
- Raise `RecipeNotFound` when absent. Raise `RecipeHasAliases` before deletion when any alias targets the recipe. Otherwise delete and flush; DB cascades remove recipe sections/items and category/tag memberships, while tag rows remain.
- The caller remains responsible for transaction commit or rollback.

- [ ] **Step 1: Add failing tests** for deleting an unaliased recipe and cascade behavior, alias-blocked delete preserving all data, missing ID, and caller rollback after a service operation.
- [ ] **Step 2: Run the focused delete tests and verify expected failures.**
- [ ] **Step 3: Implement `delete_recipe`** with an explicit alias lookup and the FK restriction as the final guard.
- [ ] **Step 4: Run slug, recipe-service, recipe schema/body, alias, and organization tests** against the disposable database.
- [ ] **Step 5: Run the complete test suite** against the disposable database and confirm it passes; report any unrelated pre-existing failure by name.

## Task 9 Acceptance

- Create/load/update/delete operate through a caller-supplied `AsyncSession` and never commit or roll back.
- Recipe ID never changes; create and rename slug behavior follows the design and preserves old links.
- Ordered recipe body, category memberships, free-form tags, provenance, actor attribution, and restricted alias behavior match the approved design.
- Service calls leave the caller’s outer transaction usable after supported unique-key collision retries.
- No schema, HTTP, UI, or image-storage changes are included.
