# Public Recipe Page and Legacy Redirects Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Serve readable, anonymous `/r/<slug>` recipe pages and issue permanent redirects from stored legacy aliases to their canonical recipe or category paths.

**Architecture:** Extend the recipe read service with slug lookup and render its ordered aggregate in a dedicated public template. Add an alias middleware that reconstructs the exact observed request path/query and looks up a recipe or category target before normal routing; authenticated requests retain the existing private cache policy.

**Tech Stack:** FastAPI, SQLAlchemy async, Starlette middleware, Jinja2, pytest, Pillow.

**Spec:** `docs/superpowers/specs/2026-09-20-cookbook-design.md` §3.5, §5.2–5.3, and D6/D11/D17.

## Global Constraints

- Public recipe path is `/r/<slug>` and legacy aliases issue app-side `301` redirects to canonical paths.
- Anonymous public reads use `Cache-Control: public, max-age=300, s-maxage=600`; authenticated reads remain `private, no-store`.
- Render saved recipe sections in stored order; ingredients render parsed fields when present and otherwise fall back directly to `raw_text`.
- Do not show provenance for WordPress-migrated recipes; web-ingested recipes show a quiet source line and safe HTTP(S) link.
- No public accounts, favorites, ratings, comments, related-recipes widget, or print-view button; browser print is the only print path.
- Recipe layout is ingredient-first on mobile and a two-column ingredient/steps layout on desktop; print uses one legible column.

## Review Focus

- A missing slug returns a private, non-cacheable 404 without leaking database errors; test unknown recipe paths.
- An unrecognized query URL falls through to the existing root behavior, while only exact stored alias paths redirect; test raw query matching and a miss.
- Recipe aliases and category aliases each target the canonical path from their immutable target row; test both target types.
- User-controlled provenance cannot create a `javascript:` link, and WordPress provenance remains absent; test unsafe and WordPress metadata.
- Missing or invalid stored image paths render without a broken image URL; test an imageless recipe and safe media resolution.

## File Map

- Modify `src/cookbook/recipes/service.py` and `src/cookbook/recipes/__init__.py` to expose read-only recipe lookup by slug.
- Create `src/cookbook/recipes/public_routes.py` for the anonymous recipe view and `src/cookbook/recipes/legacy_aliases.py` for exact-path 301 handling.
- Modify `src/cookbook/app.py` to register the alias middleware in the correct order and include the public router.
- Create `src/cookbook/templates/public_recipe.html` for semantic reading, responsive columns, provenance/chips, scroll affordances, and print styles.
- Create `tests/test_public_recipe_routes.py` for route rendering, cache policy, provenance, image handling, and alias redirects; extend `tests/test_recipe_services.py` for slug lookup if appropriate.
- Update `docs/BUILD-ROADMAP.md` after final verification.

---

### Task 1: Read and render a recipe by public slug

**Files:**
- Modify: `src/cookbook/recipes/service.py`
- Modify: `src/cookbook/recipes/__init__.py`
- Create: `src/cookbook/recipes/public_routes.py`
- Create: `src/cookbook/templates/public_recipe.html`
- Modify: `src/cookbook/app.py`
- Test: `tests/test_public_recipe_routes.py`

**Interfaces:**
- `get_recipe_by_slug(db, slug) -> RecipeDetails | None` reuses the existing ordered aggregate reader.
- GET `/r/{slug}` is anonymous, renders the public recipe template, and returns the existing public cache policy for successful responses.

- [x] Add tests for a complete recipe, an imageless recipe, an unknown slug, ingredient parse fallback, ordered sections, and anonymous/authenticated cache headers.
- [x] Run the tests and verify the public route or slug lookup is genuinely missing.
- [x] Add read-by-slug and the dedicated route/template with desktop two-column layout, mobile ingredient-first stack, safe optional image, metadata, provenance, category/tag chips, scroll strip/top link, and print stylesheet.
- [x] Run the focused public recipe tests and the existing recipe service tests.

### Task 2: Redirect exact legacy aliases to canonical paths

**Files:**
- Create: `src/cookbook/recipes/legacy_aliases.py`
- Modify: `src/cookbook/app.py`
- Test: `tests/test_public_recipe_routes.py`

**Interfaces:**
- Middleware matches the exact request path plus raw query string against `recipe_url_aliases.path` for GET requests.
- Recipe targets redirect to `/r/{recipe.slug}`; category targets redirect to `/c/{category.slug}`; unmatched requests continue unchanged.

- [x] Add tests for recipe alias, category alias, query-string preservation, unknown alias fall-through, and authenticated `private, no-store` headers.
- [x] Run the tests and verify the redirect middleware is absent.
- [x] Add exact alias lookup and register it inside the auth middleware so authenticated users retain normal session renewal and cache policy.
- [x] Run the focused public/alias tests and existing auth/cache tests.

### Task 3: Final verification and roadmap update

**Files:**
- Modify: `docs/BUILD-ROADMAP.md`

- [x] Run the full test suite against disposable MariaDB and build the Docker image.
- [x] Mark Task 16 complete with a link to this plan.
- [x] Review the final diff, commit on `dev`, push `dev`, fast-forward and push `main`, then return to `dev`.
