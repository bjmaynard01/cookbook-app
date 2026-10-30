# Task 10: Editor Dashboard and Recipe CRUD Plan

> Use one small, bounded Qwen session per implementation slice. Review every patch locally. Do not commit or push without a new explicit authorization for this task.

**Goal:** Build the authenticated editor workflow described in `docs/superpowers/specs/2026-10-30-editor-crud-design.md`.

**Architecture:** A recipes router renders Jinja templates and delegates all persistence to Task 9 services. One form adapter maps ordered form fields to `RecipeDraft` and `RecipeSaveData`. Request-scoped sessions own commit/rollback. Existing auth role and CSRF dependencies gate every private/mutating route.

**Global constraints:**

- Follow the approved product spec: server-rendered FastAPI/Jinja/HTMX app, MariaDB, no SPA pipeline.
- Do not read `.env` or touch the development database; route tests use the disposable test database.
- Never allow submitted provenance, publish date, or image path to overwrite stored immutable/service-managed fields.
- Keep Task 10 limited to editor CRUD. Image handling, ingest, public pages, and search are later roadmap tasks.
- Keep the project on the owner's single `main` checkout; do not create a feature branch/worktree or a checkpoint note.

## Step 1: Establish the route and authorization contract

- Add tests for `/edit`, `/recipes/new`, and `/recipes/{id}/edit` for anonymous, editor, and admin users; assert private cache headers.
- Add mutation tests proving missing and invalid CSRF tokens make no writes.
- Run the focused test module first and confirm failures occur because the editor routes are absent.

## Step 2: Dashboard read surface

- Add the recipes router and include it in `create_app`.
- Implement `/edit` with text/category/tag filters and title/update-time/total-time sorting.
- Render a compact dashboard with categories, update time, duration, and source/import badges.
- Verify filters/sorts and the role/cache contract against disposable MariaDB.

## Step 3: Shared form adapter and create flow

- Add route tests for GET `/recipes/new`, valid create, invalid save data, and unknown category IDs.
- Implement one ordered form parser for repeated sections/lines, preserving ingredient `raw_text` and parsed fields.
- Render field errors with submitted values and the selected category/tag controls.
- Call `create_recipe` in a caller-owned transaction; verify the service slug and category/tag rules remain authoritative.

## Step 4: Edit and delete flows

- Test edit prefill, complete replacement, rename aliases, immutable provenance/image/publish fields, and alias-protected deletion.
- Implement GET/POST edit and POST delete using the existing update/delete services.
- Preserve submitted values on validation failure and redirect on success.

## Step 5: Integration verification

- Run editor route tests, recipe service tests, authentication/CSRF tests, and the complete suite on disposable MariaDB.
- Inspect the final diff and verify `main` contains only Task 10 work plus the roadmap/design/plan documentation.
