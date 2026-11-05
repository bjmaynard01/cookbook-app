# Ingest Review and Save Flow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let authenticated editors safely import a recipe URL, review one of four explicit parse outcomes in the existing recipe form, and save the edited draft with provenance and an optional safely downloaded image.

**Architecture:** Add a CSRF-protected ingest POST route that applies one 30-second overall budget to bounded fetch and optional parsing, then renders the existing form with explicit state and draft metadata. Keep save validation and persistence in the existing workflow; on save, attempt a bounded SSRF-safe fetch of a candidate image and preserve a successful local file, while image failures never block saving.

**Tech Stack:** FastAPI, async httpx, SQLAlchemy, Jinja2, pytest, Pillow.

**Spec:** `docs/superpowers/specs/2026-09-20-cookbook-design.md` §4.1–4.5.

## Global Constraints

- HTML fetches accept only public HTTP(S) URLs, validate every redirect destination, cap bodies at 5 MB, and allow up to five redirects.
- Fetch time budget is at most 20 seconds; optional LLM parsing is at most 10 seconds; the total pipeline has a 30-second hard cap.
- Never create a recipe until the editor submits the existing save form.
- Preserve ingredient `raw_text`; keep partial drafts valid and use the shared save validator.
- Never download remote images during ingest; at save, a failed image fetch must not block recipe persistence.
- Ingest and editor pages remain authenticated, CSRF-protected on POST, and `private, no-store`.
- Run the in-process ingest pool with `INGEST_MAX_CONCURRENT` (default 4; configured range 1–32).

## Review Focus

- Invalid or internal URLs and redirects remain blocked by the existing per-hop DNS-pinned fetch guard; test direct invalid URLs and use the fetcher’s established redirect tests as regression coverage.
- Fetch/parse timeout or failure returns a usable blank or partial form, never a 500 or an unbounded wait; test fetch failure and overall timeout.
- Repeated normalized source URLs produce a non-blocking duplicate notice without preventing a new save; test duplicate discovery and save behavior.
- Untrusted hidden form fields cannot create unsafe image requests or provenance values that exceed persistence constraints; test malformed candidate URLs and validate the standard form path.
- Missing or invalid remote image bytes do not prevent the recipe save and do not leave unmanaged files; test image failure and successful local storage.

## File Map

- Modify `src/cookbook/recipes/routes.py` to add ingest orchestration, state classification, provenance/dedup handling, and save-time image integration.
- Modify `src/cookbook/config.py`, `src/cookbook/app.py`, and `.env.example` to expose the bounded `INGEST_MAX_CONCURRENT` setting (default 4).
- Modify `src/cookbook/recipes/page_extraction.py` and `src/cookbook/recipes/ingest.py` only as needed to carry a meaningful source name and provide a reusable bounded safe download primitive.
- Modify `src/cookbook/templates/recipe_dashboard.html` to add the URL launcher and `src/cookbook/templates/recipe_form.html` to render the four state callouts, duplicate notice, and candidate/provenance fields.
- Modify `tests/test_editor_routes.py` for end-to-end editor behavior and `tests/test_recipe_fetch.py` / `tests/test_recipe_page_extraction.py` only for any new safe-download/source-name contract.
- Update `docs/BUILD-ROADMAP.md` to mark Task 15 complete after verification.

---

### Task 1: Render a URL launcher and four explicit review states

**Files:**
- Modify: `src/cookbook/templates/recipe_dashboard.html`
- Modify: `src/cookbook/templates/recipe_form.html`
- Modify: `src/cookbook/recipes/routes.py`
- Test: `tests/test_editor_routes.py`

**Interfaces:**
- Ingest route consumes `url` from an authenticated CSRF-protected form.
- Form context accepts `ingest_state`, `ingest_message`, and the normal `RecipeDraft`.

- [x] Add route tests proving an unauthenticated ingest is rejected, missing CSRF is rejected, and dashboard shows the URL launcher.
- [x] Run those tests and verify the route/launcher behavior fails before implementation.
- [x] Add the launcher and explicit fetch-failed/nothing-found/partial/full callouts to the standard form; keep the same save action and editable fields.
- [x] Run the focused route tests and confirm all outcomes render with private no-store headers.

### Task 2: Connect safe fetch, deterministic/LLM parsing, dedup, and provenance

**Files:**
- Modify: `src/cookbook/recipes/routes.py`
- Modify: `src/cookbook/recipes/page_extraction.py` (if site-name metadata is added)
- Test: `tests/test_editor_routes.py`
- Test: `tests/test_recipe_page_extraction.py` (if applicable)

**Interfaces:**
- POST `/recipes/ingest` invokes `fetch_page(url)` then `parse_recipe_page_with_fallback(page)` under an overall 30-second timeout.
- Successful drafts preserve their `extraction_method`, source URL, source name, notes, and remote image candidate.

- [x] Add tests for parsed draft preview, empty result, fetch exception, overall timeout, category suggestions, and normalized-source duplicate marker.
- [x] Run focused tests and verify expected failures.
- [x] Implement bounded orchestration, source-name derivation, state classification, duplicate lookup, and category suggestion preselection.
- [x] Run route and parser tests.

### Task 3: Preserve provenance and safely store a candidate image on explicit save

**Files:**
- Modify: `src/cookbook/recipes/routes.py`
- Modify: `src/cookbook/recipes/ingest.py` or create a focused safe image downloader module if needed
- Modify: `src/cookbook/recipes/service.py` only if a persistence API extension is required
- Test: `tests/test_editor_routes.py`, `tests/test_recipe_fetch.py`, and/or `tests/test_recipe_images.py`

**Interfaces:**
- The existing editor form submission remains the only recipe-create action.
- A remote image candidate is fetched with public-URL validation, per-redirect revalidation, a strict byte cap, and image-content validation; successful bytes use `store_image` and `set_recipe_image`.
- Image download failure leaves the recipe saved with no local image and a visible non-blocking status.

- [x] Add tests proving the submitted provenance reaches the saved `Recipe`, an image fetch failure still saves, and a successful candidate is served from local media storage.
- [x] Run tests and verify these fail before implementation.
- [x] Add provenance fields to form round-tripping and bounded safe image download at save; clean up a newly stored file if persistence fails.
- [x] Run editor, image, parser, and fetch tests.

### Task 4: Final verification and roadmap update

**Files:**
- Modify: `docs/BUILD-ROADMAP.md`

- [x] Run targeted tests, full pytest suite, and Docker build under isolated test DB/media settings.
- [x] Mark Task 15 complete with a link to this plan.
- [x] Review the final diff and verify no test data, local secrets, or generated artifacts are included.
