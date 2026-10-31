# Deterministic Recipe Parsing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Convert recipe JSON-LD or schema.org microdata on a fetched page into the shared sparse `RecipeDraft` without changing source ingredient text.

**Architecture:** Add a parser module that tries all JSON-LD blocks first, recursively searches arrays, `@graph`, and nested objects for `Recipe` types, and chooses the candidate with the most populated supported fields. If no supported JSON-LD candidate has content, parse Recipe microdata from the fetched HTML. Both paths map to the same `RecipeDraft`; the parser stays pure, deterministic, and database-free.

**Tech Stack:** Python 3.12, existing `FetchedPage` and Pydantic draft models, standard-library `json`, `html.parser`, `decimal`, and pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-cookbook-design.md`, §4.1 Stage 1a–1b, §4.3, and §4.5.

## Global Constraints

- JSON-LD candidate discovery walks arrays, `@graph`, and nested values; accept scalar and array-valued `@type` containing Recipe case-insensitively.
- Select the Recipe candidate with the most populated supported fields; keep input order for ties.
- Try microdata only when no JSON-LD candidate has supported content.
- Keep ingredient `raw_text` exactly as found; quantity/unit/item/preparation are conservative best-effort fields.
- Parse only schema.org fields; do not call an LLM, access the database, save recipes, or fetch image URLs.
- Every output is a `RecipeDraft`, including an empty sparse result when the page has no usable structured recipe.

## Review Focus

- Multiple JSON-LD blocks, arrays, graphs, and nested recipe objects: candidate selection remains deterministic.
- Malformed JSON-LD beside valid JSON-LD or microdata: usable data survives and parse notes explain ignored malformed blocks.
- HowToSection/HowToStep nesting and plain-text instructions: ordered step text and section headings are retained.
- Ambiguous quantities, unsupported units, and malformed durations: raw source remains intact and no guessed numeric data is invented.
- Nested non-Recipe microdata scopes: their text is not accidentally added to the enclosing recipe.

---

### Task 1: Parse JSON-LD candidates into RecipeDraft

**Files:**
- Create: `src/cookbook/recipes/parsing.py`
- Create: `tests/test_recipe_parsing.py`

**Interfaces:**
- Produces: `parse_recipe_page(page: FetchedPage) -> RecipeDraft`.
- JSON-LD parsing maps title, description, servings/yield, durations, categories, keywords, image, ingredients, and instructions into the existing draft and provenance models.

- [x] Write tests for single Recipe objects, `@graph`, top-level arrays, nested objects, scalar/array `@type`, richest-candidate selection, and all supported fields.
- [x] Run the focused tests to confirm they fail because `parsing.py` does not exist.
- [x] Implement deterministic JSON-LD discovery, candidate scoring, value normalization, duration parsing, and conservative ingredient splitting.
- [x] Run focused JSON-LD tests and verify raw ingredient source strings remain unchanged.

### Task 2: Parse Recipe microdata as fallback

**Files:**
- Modify: `src/cookbook/recipes/parsing.py`
- Modify: `tests/test_recipe_parsing.py`

- [x] Add tests for case-insensitive Recipe itemtypes, repeated `recipeIngredient` and legacy `ingredients` properties, metadata fields, nested non-Recipe scopes, and JSON-LD-to-microdata fallback.
- [x] Run the new microdata tests and confirm they fail before implementation.
- [x] Implement tolerant HTML microdata extraction using bounded fetched HTML; map supported itemprops into the same `RecipeDraft` contract.
- [x] Test malformed JSON-LD, empty pages, invalid values, and valid structured data alongside malformed blocks.

### Task 3: Document and verify Task 13

**Files:**
- Modify: `docs/BUILD-ROADMAP.md`
- Modify: `docs/PROJECT-STATUS.md`
- Modify: `src/cookbook/recipes/__init__.py`

- [x] Export the parser and mark Task 13 complete without claiming LLM fallback or the ingest UI is implemented.
- [x] Run focused tests, the full suite on disposable tmpfs MariaDB, `git diff --check`, and the Docker build.
- [x] Commit on `dev`, push `dev`, fast-forward and push `main`, then return the workspace to `dev`.
