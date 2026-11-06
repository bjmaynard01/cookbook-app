# Shared Navigation Design

## Intent

Make it easy to move between public reading and recipe editing. The sign-in and editor screens currently lack a way to return to the public site, and the editor has no direct path to public search or category browsing.

## Goals

- Keep Home, category Browse, and Search available from sign-in, public, and editor pages.
- Give signed-in editors direct links to the recipe list and new-recipe form.
- Preserve logout and avoid duplicate navigation on public pages.
- Keep the links usable on narrow screens and hide navigation when printing a recipe.

## Approaches

1. Add one-off Home links to sign-in and editor pages. This fixes the reported dead end but leaves navigation inconsistent and public search difficult to reach while editing.
2. Use one shared navigation bar in the base template. This keeps reader and editor navigation consistent and avoids per-page link drift. **Selected by the owner.**
3. Add an editor-only menu or sidebar. This could expose more editor actions, but adds a second navigation system before the owner has identified other missing flows.

## Design

The shared base template owns the navigation. It shows Home (`/`), Browse (the category section on Home), and Search (`/search`) to everyone. Signed-in users also see Recipes (`/edit`) and New recipe (`/recipes/new`); the existing logout form remains in the header. Signed-out users see Editor sign in (`/login`), except while already on `/login` or `/setup`.

The public browsing base removes its current duplicate nav. The category section on Home receives a stable anchor for Browse. Navigation wraps on small screens and is hidden in print styles. No authentication, route, recipe, or session behavior changes.

## Acceptance checks

- Sign-in and setup pages provide paths to Home, Browse, and Search.
- Signed-out public pages show Home, Browse, Search, and Editor sign in.
- Signed-in public and editor pages show Home, Browse, Search, Recipes, New recipe, and Logout.
- Public pages render one navigation bar, not two.
- Existing editor links, logout, and recipe print behavior continue to work.
