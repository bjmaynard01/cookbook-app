# Public Home, Category, and Tag Browsing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a public home index and category/tag recipe lists that help readers find something to cook.

**Architecture:** Add focused read-side queries that return recipe cards with their category and tag links, plus home category counts and popular tags. Render `/`, `/c/{slug}`, and `/t/{slug}` through server-rendered Jinja templates and the existing public cache/auth middleware.

**Tech Stack:** FastAPI, async SQLAlchemy, MariaDB, Jinja2, pytest, Pillow.

**Spec:** `docs/superpowers/specs/2026-09-20-cookbook-design.md` §3.1, §3.4, §5.1, D11–D13.

## Approaches

- Reuse the existing editor dashboard query and template: rejected because it is authenticated, has editor-only data, and its controls are not a public reading experience.
- Add public read queries in a focused browse service and render shared recipe-card partials on the three surfaces: selected because it keeps SQL out of templates and gives category/tag listings one consistent card format.

The spec leaves a few display limits open. Use six featured recipes, the first twelve tags by recipe count, and time filters for 30, 60, and 90 minutes. Keep all categories in owner order and show all matching recipes ordered by `updated_at` descending with stable title/id ties.

## Global Constraints

- Home is a working index, not a marketing hero; featured picks, categories, tags, and search are first-class lanes.
- Categories are data and remain in `Category.position` order; show a recipe count for each.
- Category and tag recipe lists default to `updated_at` descending.
- Recipe cards show an optional safe thumbnail or a neutral typographic placeholder, title, one-line description, total minutes, and servings text when present.
- Keep time as a structured predicate on `total_minutes`; keep semantic tag filters distinct from time controls.
- Preserve anonymous public caching and authenticated `private, no-store`; unknown category/tag slugs return 404.
- No accounts, favorites, ratings, comments, related-recipe widgets, ads, or share-button rows on public pages.

## Review Focus

- A missing category or tag slug returns 404 with private cache headers; cover both paths.
- Recipes with NULL time are not included in a selected maximum-time filter; cover a NULL-time recipe and an over-limit recipe.
- Selecting a category tag applies both the category and tag predicates; test that an unrelated category recipe is excluded.
- Missing or unsafe image paths use the neutral placeholder and never produce a broken URL; cover both.
- Empty collections and no featured recipes render useful page structure without errors; cover empty featured/tag states.

## File Map

- Create `src/cookbook/recipes/browse_service.py` for ordered category, tag, featured, and recipe-card reads.
- Modify `src/cookbook/recipes/public_routes.py` to serve `/`, `/c/{slug}`, and `/t/{slug}`, retaining the `/r/{slug}` route.
- Modify `src/cookbook/app.py` to remove the old root login redirect; public home becomes `/`.
- Create `src/cookbook/templates/public_home.html`, `public_listing.html`, and a shared `recipe_card.html` partial.
- Modify `tests/test_public_recipe_routes.py` for home/list rendering, ordering, count, filters, missing slugs, and cache behavior.
- Update `docs/BUILD-ROADMAP.md` after final verification.

---

### Task 1: Build the browse read model

**Interfaces:** `get_home_browse_data(db) -> HomeBrowseData`, `get_category_browse_data(db, slug, *, max_minutes=None, tag_slug=None) -> ListingBrowseData | None`, and `get_tag_browse_data(db, slug) -> ListingBrowseData | None`. Card data carries the recipe and ordered category/tag tuples; category/tag summary data includes recipe counts.

- [x] Add integration tests for featured order/limit, category owner order/count, popular-tag order/limit, recipe card links, NULL-time exclusion, and combined category/time/tag filters.
- [x] Run the focused tests and verify the home redirects to login and browse routes are missing.
- [x] Implement read-only SQLAlchemy queries with deterministic tie-breaking and combined category/time/tag filters.
- [x] Run browse route tests and existing recipe-service tests.

### Task 2: Render the public home index

- [x] Add a failing HTTP test proving `/` renders the public home and no longer redirects to `/login`.
- [x] Render featured recipes, category counts, popular tags, and a search entry point; render gracefully when optional lanes have no content.
- [x] Verify anonymous public caching and authenticated private caching.

### Task 3: Render category and tag lists

- [x] Add failing route tests for ordered category/tag results, the 30/60/90-minute category predicates, separate semantic tag filter chips, safe image placeholders, and unknown slugs.
- [x] Add the reusable recipe card and responsive home/list templates with links to canonical `/r`, `/c`, and `/t` paths.
- [x] Run focused browse tests, the full suite on disposable MariaDB, `git diff --check`, and a Docker image build.
- [x] Mark Task 17 complete with this plan linked; commit/push on `dev`, fast-forward/push `main`, and return to `dev`.
