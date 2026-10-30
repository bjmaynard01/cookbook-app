# Recipe Persistence Services — Design

**Date:** 2026-10-30

## Intent

Build the recipe persistence layer on top of the schema and Task 8 `RecipeDraft` / `RecipeSaveData` contract. The services will let future routes and forms create, load, update, and delete recipes while keeping recipe fields, ordered body sections, category/tag memberships, and slug aliases consistent.

## Goals

- Create and update recipe records and their ordered `RecipeSection` / `RecipeItem` rows from validated save data.
- Require existing, explicitly selected category IDs; resolve them to names for Task 8 validation and persist the many-to-many memberships.
- Treat tags as free-form: normalize submitted names, reuse matching tags, and create missing tag rows in the same transaction.
- Generate a slug from the title on create, allow a submitted slug override, and add `-2`, `-3`, etc. when a slug is already in use.
- On slug rename, retain the old `/r/<slug>` path as a recipe alias so existing links keep resolving. Keep earlier aliases when a recipe is renamed more than once.
- Keep the recipe UUID immutable. Keep `source_*`, `extraction_method`, and `imported_at` unchanged on update.
- Load a recipe aggregate by immutable ID for future editor forms.
- Refuse hard deletion while any URL alias points to the recipe; aliases must be deliberately reassigned first (DL-6).
- Keep each operation atomic through the caller's transaction and provide stable service-level errors for missing records, bad inputs, slug/alias conflicts, and blocked deletion.

## Non-goals

- HTTP routes, authorization wiring, forms, dashboard/listing pages, or public recipe pages.
- Image validation, upload, remote fetch, or media storage; `image_path` remains unset unless a later image task supplies a validated stored path.
- Category or tag administration screens. This service may create tags needed by a saved recipe, but does not rename or delete tags.
- Search, ingest, WordPress import, migrations, or schema changes.
- Hard deletion or reassignment of recipe aliases as part of recipe deletion.

## Constraints and existing contracts

- `Recipe.id` is the immutable UUID identity; `Recipe.slug` is unique and editable.
- Slugs are generated from title at initial creation. The owner confirmed this resolves the contradictory D3 wording; after creation the slug remains editable.
- Recipe slug generation follows the spec's lowercase, non-alphanumeric-to-hyphen, collapse-and-trim convention. If a title has no alphanumeric characters, use `recipe` as the base. Keep Unicode letters and digits as alphanumeric.
- A recipe body is ordered sections (`ingredients`, `steps`, `note`) with ordered lines; `raw_text` is the source of truth. Ingredient parse columns are stored only for ingredient lines.
- At save time, title, at least one selected category, and at least one nonblank ingredient line are required. Steps are optional. Total time follows Task 8 validation.
- Category memberships and URL aliases use restrictive category/alias foreign keys. Recipe body and memberships cascade with recipe deletion, while aliases restrict it. Tag deletion cascades only its memberships.
- `RecipeUrlAlias.path` is unique and has exactly one valid target. Recipe rename aliases use `/r/<old-slug>` paths.
- Database-backed verification uses only the disposable isolated MariaDB. No development database migration or writes.

## Approaches considered

1. **Caller-managed `AsyncSession` transactions — selected.** Service functions accept the session and flush changes but never commit or roll back. The caller wraps the complete save in `async with session.begin()`, which composes cleanly with future request handling and makes the commit boundary explicit. All recipe, body, category/tag membership, generated tag, and alias changes remain one transaction.
2. **Services open and own sessions.** This matches existing auth helpers and makes standalone calls easy, but forces later route operations to cross service-owned transaction boundaries and makes it harder to compose recipe persistence with other request work. The owner selected caller-managed sessions.

Use module-level async service functions, consistent with the current package style; avoid adding a generic repository layer.

## Behavior decisions

- Create and update accept the validated `RecipeSaveData`, selected `category_ids`, actor user ID, and optional slug input. Creation stamps `created_by` and `last_modified_by`; update changes `last_modified_by` and ordinary editable fields only.
- Category IDs must all resolve to existing rows. Unknown IDs produce a stable category error; the recipe service does not create categories from suggested draft names.
- Tag matching uses trimmed, case-insensitive names. Missing tags are created with a deterministic slug derived from the name; if a different tag already uses that slug, add a numeric suffix rather than conflating the names.
- Persist `servings_number` as the structured quantity and preserve `servings_text` when supplied. The optional parsed unit is not stored separately; no schema column is added for it.
- Create-time generated slugs and explicit slug overrides are normalized with the same slug rule. Check both recipe slugs and `/r/<slug>` aliases before selecting a canonical slug. Resolve collisions with numeric suffixes, including races, inside a savepoint so the caller's outer transaction remains usable.
- On a rename, insert `/r/<old-slug>` pointing to the same recipe in the caller's transaction. If that path is already an alias for the same recipe, keep it. If it is owned by another target, raise an alias conflict and leave all rows unchanged. If the new canonical path is currently a self-alias, remove that alias so the canonical recipe serves the path directly; aliases pointing to another target block the change.
- Delete raises a dedicated `RecipeHasAliases` error before deleting when aliases point to the recipe. The database RESTRICT remains the final safety net.
- Update replaces body sections/items and category/tag memberships from the submitted complete save data. Provenance fields are retained from the stored row even if the submitted draft contains different provenance.
- Recipe loading returns the recipe plus ordered sections/items, category IDs, and tag names; it does not perform HTTP rendering.
- Save validation does not turn a remote hero-image URL into `image_path`. Image handling is deferred to the image task.

## What remains for the implementation plan

Define exact module/file boundaries, public function signatures and result/error types, ordered ORM mapping details, and the isolated MariaDB test cases. The implementation plan should split slug helpers, create, update/rename, load/delete into independently reviewable test slices while keeping every write within the caller's transaction.
