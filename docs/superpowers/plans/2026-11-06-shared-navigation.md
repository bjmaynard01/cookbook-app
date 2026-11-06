# Shared Navigation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let visitors and editors move between public browsing and recipe editing through one consistent navigation bar.

**Architecture:** Render the shared navigation from `base.html`, using the existing `session_csrf` template value to distinguish signed-in editors. Remove the public browsing template's duplicate nav; keep the existing Home, Search, editor, and logout routes and behavior.

**Implementation note:** Public routes already provide `session_csrf`; editor routes currently provide only `csrf_token`. Pass the same authenticated session token under both names on editor pages so the shared base can render editor links and its logout form. Login/setup keep their separate pre-auth `csrf_token` only.

**Tech Stack:** Python 3, FastAPI, Jinja2, HTMX, MariaDB 11, server-rendered HTML/CSS.

**Spec:** `docs/superpowers/specs/2026-11-06-shared-navigation-design.md`

## Global Constraints

- “Python 3 + FastAPI + Jinja2 + HTMX (server-rendered, single process, single Docker image).”
- “No SPA build pipeline.”
- “Public visitors: anonymous, read-only.”
- “category nav always visible; search always one action away; a home/start page that helps choose what to cook.”
- Do not change authentication, session, route authorization, recipe, or database behavior.
- Add no runtime dependencies.

## Review Focus

- Signed-out `/login` and `/setup` must expose public navigation without a redundant sign-in link — test both templates.
- Signed-in editor pages must show editor-only links and keep logout — test `/edit` and a recipe form.
- Signed-in public pages must show editor links while public pages render only one nav — test an authenticated home request and the signed-out home.
- Narrow screens must allow the links to wrap without horizontal overflow — verify the nav CSS has wrapping rules and check the rendered page at a narrow viewport.
- Print output must not include navigation — assert the shared nav is hidden in the print stylesheet and inspect the recipe print view.

---

### Task 1: Shared responsive navigation

**Files:**
- Modify: `src/cookbook/templates/base.html` — render and style the global nav; preserve the current logout form.
- Modify: `src/cookbook/templates/public_browse_base.html` — remove its duplicate nav and obsolete nav styles.
- Modify: `src/cookbook/recipes/routes.py` — expose the existing session CSRF token as `session_csrf` to editor templates.
- Test: `tests/test_login_routes.py` — cover public links on login and the absent redundant self-link.
- Test: `tests/test_setup_routes.py` — cover public links while first-owner setup is open.
- Test: `tests/test_editor_routes.py` — extend `test_editor_and_admin_can_open_private_dashboard_and_form` to cover editor links on the signed-in Home, dashboard, and new-recipe form.
- Test: `tests/test_root_routes.py` — cover one nav and public links when signed out.
- Test: `tests/test_public_recipe_routes.py` — extend the public Home and recipe-page tests to cover one nav and preserve print behavior.

**Interfaces:**
- Consumes: the existing Jinja `request` and `session_csrf` values, and routes `/`, `/#categories-title`, `/search`, `/edit`, `/recipes/new`, `/login`, and `/logout`.
- Produces: one `<nav aria-label="Primary navigation">` in the shared base; Home/Browse/Search for everyone; Recipes/New recipe/Logout for signed-in editors; Editor sign in only for signed-out pages other than `/login` and `/setup`.

- [ ] **Step 1: Add failing navigation assertions** to the named route tests for the link sets, authentication branches, single-nav count, and preservation of logout.
- [ ] **Step 2: Run the focused tests and confirm they fail** because login/editor templates lack the shared navigation and public pages still render their own nav.

Run: `pytest tests/test_login_routes.py tests/test_setup_routes.py tests/test_editor_routes.py tests/test_root_routes.py tests/test_public_recipe_routes.py -q`

Expected: new navigation assertions fail; existing behavior remains otherwise intact.

- [ ] **Step 3: Implement the shared nav** in `base.html`. Use `/#categories-title` for Browse (the Home category heading already has this id); wrap the nav on small viewports and hide it in print. Remove the duplicate public nav and its styles from `public_browse_base.html`.
- [ ] **Step 4: Run focused tests and confirm they pass.**

Run: `pytest tests/test_login_routes.py tests/test_setup_routes.py tests/test_editor_routes.py tests/test_root_routes.py tests/test_public_recipe_routes.py -q`

Expected: all focused tests pass, including signed-out and signed-in nav variants.

- [ ] **Step 5: Run the full suite on disposable MariaDB, build the Docker image, and smoke-test the public routes `/`, `/login`, `/search`, and `/r/oven-baked-tacos`.** The authenticated route tests cover `/edit` and `/recipes/new`. Confirm `alembic check` reports no schema changes and perform a narrow-viewport/print-style check.
- [ ] **Step 6: Commit the feature and tests on `dev`; push `dev`, fast-forward `main`, and push `main` after review.** Leave the unrelated pre-existing `uv.lock` modification unstaged.
