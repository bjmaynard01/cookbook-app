# Optional LLM Parsing Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional OpenAI-compatible parsing fallback that enriches unusable deterministic recipe parses without affecting no-key operation.

**Architecture:** Keep deterministic JSON-LD and microdata parsing first. Add an async fallback entry point that sends only cleaned visible page text, capped at 24,000 characters, to the configured chat-completions endpoint; validate the returned JSON against `RecipeDraft` and stamp trusted source provenance locally. Missing configuration, unreadable optional keys, timeouts, provider failures, and invalid output return the deterministic draft with a concise parse note.

**Tech Stack:** Python 3.12, Pydantic settings/models, existing HTTPX dependency, asyncio, pytest, respx.

**Spec:** `docs/superpowers/specs/2026-09-20-cookbook-design.md`, §4.1 Stage 2, §4.2, §4.3, §4.5, and §9.1.

## Global Constraints

- Configure `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY` or `LLM_API_KEY_FILE`, and `LLM_TIMEOUT_SECONDS` (default 10 seconds, never above 10).
- Send at most 24,000 characters of cleaned visible text to an OpenAI-compatible chat completions endpoint with `temperature=0` and strict JSON output.
- The LLM is optional and web-ingest-only; failures and missing keys preserve the deterministic result and add a `parse_notes` entry.
- Never access the database, fetch remote recipe images, log or return provider credentials, or turn provider failure into an ingest exception.

## Review Focus

- Deterministic recipe data bypasses the provider unless it is absent or contains obviously garbage ingredient text.
- Source text containing instructions is treated as untrusted data; the prompt must not treat it as instructions.
- Invalid JSON, schema-invalid JSON, non-2xx responses, and timeouts return the original deterministic draft with a note.
- Optional key-file absence or read failure does not prevent application startup or deterministic parsing.
- Missing configuration and empty visible text never make a provider request.

---

### Task 1: Add optional LLM configuration and clean page text

**Files:**
- Modify: `src/cookbook/config.py`
- Modify: `src/cookbook/recipes/page_extraction.py`
- Test: `tests/test_config.py`
- Test: `tests/test_page_extraction.py`

**Interfaces:**
- Produces optional `Settings.llm_base_url`, `Settings.llm_model`, `Settings.llm_api_key`, `Settings.llm_api_key_file`, and `Settings.llm_timeout_seconds` fields.
- Visible text excludes navigation and cookie/consent banner containers while retaining recipe content.

- [x] Test no-key defaults, the 10-second cap, and file-first LLM key loading behavior.
- [x] Test visible text excludes nav and cookie/consent banner text while retaining main content.
- [x] Implement the settings and extractor behavior; run both focused test files.

### Task 2: Implement the async fallback client and pipeline

**Files:**
- Create: `src/cookbook/recipes/llm.py`
- Modify: `src/cookbook/recipes/__init__.py`
- Test: `tests/test_llm_parsing.py`

**Interfaces:**
- Produces: `async parse_recipe_page_with_fallback(page: FetchedPage, *, settings: Settings | None = None) -> RecipeDraft`.
- Keeps `parse_recipe_page` as the synchronous deterministic-only parser.

- [x] Test provider request shape, zero temperature, JSON validation, text cap, untrusted-source prompt, no-key behavior, and deterministic bypass.
- [x] Test garbage-triggered fallback and safe fallback on HTTP error, timeout, malformed JSON, and invalid draft schema.
- [x] Implement the async OpenAI-compatible client, timeout, local provenance stamping, and non-throwing fallback notes.
- [x] Run focused LLM, config, extraction, and deterministic parser tests.

### Task 3: Document and verify Task 14

**Files:**
- Modify: `docs/BUILD-ROADMAP.md`
- Modify: `docs/PROJECT-STATUS.md`

- [x] Mark Task 14 complete and describe the parser interface without claiming the Task 15 route or review UI.
- [x] Run the full suite against disposable tmpfs MariaDB, `git diff --check`, and the Docker build.
- [x] Commit on `dev`, push `dev`, fast-forward and push `main`, then return the workspace to `dev`.
