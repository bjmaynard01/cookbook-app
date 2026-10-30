# Editor Dashboard and Recipe CRUD Design

**Date:** 2026-10-30
**Status:** Approved for implementation by the owner's instruction to proceed
**Roadmap:** Task 10 in `docs/BUILD-ROADMAP.md`
**Builds on:** Task 7 authentication, Task 8 `RecipeDraft` validation, and Task 9 recipe services.

## Goal

Give authenticated editors a working server-rendered workflow to find, create, edit, and delete recipes. This is the first usable recipe-management surface and supplies the manual form that later URL ingestion will reuse.

## User-visible behavior

- `/edit` shows a private, no-store recipe dashboard with title, category names, update time, total time, and import source details when available. Editors can filter by text, category, and tag, and sort by title, update time, or total time.
- `/recipes/new` and `/recipes/{recipe_id}/edit` use one shared form. It edits title, description, slug, categories, tags, servings text/number, prep/cook/total minutes, ordered sections, ingredient raw text and parsed fields, and ordered steps.
- A valid submission calls `create_recipe` or `update_recipe` inside a request-owned `AsyncSession` transaction and redirects back to the editor. Service validation errors re-render the submitted values with field feedback. Existing provenance, publish date, and local image path remain service-managed.
- A POST delete action calls `delete_recipe`; successful deletion returns to `/edit`, while an alias-protected recipe stays intact and displays a clear message.
- The dashboard and form require at least the editor role (admins are admitted by the existing role hierarchy). Every POST requires the existing CSRF check. All editor responses, including errors, use `Cache-Control: private, no-store`.

## Boundaries

- Use FastAPI, Jinja2, and the existing server-rendered templates. No SPA or new frontend dependency.
- Reuse Task 9 persistence services and Task 8 validation. Routes adapt form data into the existing `RecipeDraft` contract; they do not write ORM rows directly.
- Keep category creation, image upload/storage, URL ingestion, public recipe pages, full-text search, and importer work in later roadmap tasks.
- Do not add schema changes or migrations.

## Acceptance

- Anonymous and inactive users cannot access editor pages; active editors and admins can.
- Missing/invalid CSRF blocks all recipe mutations without changing data.
- Create, update, and delete use the caller transaction and preserve the Task 9 slug/alias and immutable-field rules.
- Form validation errors are rendered without losing the user's entered data; malformed form fields do not expose exceptions or SQL details.
- Dashboard filters and sorting operate against the database and retain the selected controls in the response.
- Focused route/service tests and the complete suite pass against disposable MariaDB.
