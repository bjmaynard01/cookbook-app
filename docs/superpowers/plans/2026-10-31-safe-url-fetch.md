# Safe URL Fetch and Page Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fetch public recipe pages safely within strict time and size bounds and extract page metadata, visible text, and JSON-LD script blocks for later parsing.

**Architecture:** Add isolated modules under `cookbook.recipes` for safe HTTP fetching and page extraction. The fetcher validates and resolves each HTTP(S) URL, rejects any host with a non-public address, and pins each request's connection to the validated DNS answers. Redirects are followed manually up to five times with the same checks. A bounded fetch result is converted to a simple page data object using Python's standard HTML parser; recipe schema interpretation remains Task 13.

**Tech Stack:** Python 3.12, httpx, httpcore `>=1.0,<2`, asyncio, ipaddress, html.parser, pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-cookbook-design.md`, §4.1 fetch guard and §4.3 fetch failure surface.

## Global Constraints

- Only HTTP and HTTPS URLs may be fetched.
- Reject loopback, private, link-local, multicast, reserved, and every other non-public IP address before connection.
- Fetch timeout is 20 seconds; decoded response body cap is 5 MiB.
- Follow no more than five redirects and validate each redirect destination before connecting.
- Never allow DNS rebinding between validation and connection; preserve the original hostname for HTTP Host, TLS SNI, and certificate validation.
- Do not parse recipe JSON-LD or microdata into `RecipeDraft` in Task 12.
- Keep database and application state out of this module.

## Review Focus

- Mixed public/private DNS answers: reject the entire destination.
- Redirect to a private host or a sixth redirect: fail before connecting to the target.
- DNS answer changes after validation: connection must still use only validated addresses.
- Compressed body exceeding 5 MiB when decoded: stop decompression as soon as the cap is exceeded.
- Malformed or hostile HTML: extraction remains bounded and ignores script/style text other than preserving JSON-LD script contents.

---

### Task 1: Validate URLs and pin public DNS answers

**Files:**
- Create: `src/cookbook/recipes/ingest.py`
- Modify: `pyproject.toml`, `uv.lock`
- Create: `tests/test_recipe_fetch.py`

**Interfaces:**
- Produces: `async validate_url(url: str) -> ValidatedURL`; `async resolve_public_addresses(host: str, port: int) -> list[str]`; `ValidatedURL(url: httpx.URL, addresses: tuple[str, ...])`.
- Reject malformed URLs, non-HTTP(S) schemes, credentials, missing hosts, and any DNS answer for which `ipaddress.ip_address(...).is_global` is false.

- [x] Write tests for valid public IPv4/IPv6 and rejected schemes, credentials, localhost, literal private/reserved addresses, and mixed public/private DNS answers.
- [x] Run `pytest tests/test_recipe_fetch.py -q` and confirm the new tests fail because the module does not exist.
- [x] Implement URL validation and asynchronous resolution; normalize and retain validated IPs.
- [x] Add a custom httpcore network backend that connects to only those retained IPs while preserving the requested host for TLS and HTTP.
- [x] Run the focused tests and verify validation rejects unsafe inputs.

### Task 2: Bounded HTTP fetch and safe redirects

**Files:**
- Modify: `src/cookbook/recipes/ingest.py`
- Modify: `tests/test_recipe_fetch.py`

**Interfaces:**
- Produces: `async fetch_page(url: str) -> FetchedPage` and typed `FetchError` failures.
- `FetchedPage` retains original/final URL, status, content type, raw HTML, and extracted page metadata.

- [x] Add tests for successful fetch, timeout, HTTP errors, declared and streamed body over 5 MiB, redirect validation, five allowed redirects, and sixth redirect rejection.
- [x] Run the focused new tests and confirm each new behavior test fails before implementation.
- [x] Implement streaming reads with a 20-second total timeout, a browser-like User-Agent, `trust_env=False`, and manual redirects; validate and pin every hop.
- [x] Run `pytest tests/test_recipe_fetch.py -q` and verify the fetch behaviors pass without network access.

### Task 3: Extract bounded page metadata, visible text, and JSON-LD blocks

**Files:**
- Create: `src/cookbook/recipes/page_extraction.py`
- Modify: `src/cookbook/recipes/ingest.py`
- Create: `tests/test_recipe_page_extraction.py`
- Modify: `src/cookbook/recipes/__init__.py`

**Interfaces:**
- Produces: `extract_page(html: str, *, source_url: str, final_url: str, status_code: int, content_type: str) -> FetchedPage`.
- Extracts document title, description, canonical URL, Open Graph image, visible text, and raw JSON-LD script bodies; it does not interpret schema.org recipe data.

- [x] Add tests for title/meta/canonical/Open Graph extraction, visible-text cleanup, multiple JSON-LD scripts, malformed HTML, and ignored script/style text.
- [x] Run the new extraction tests and confirm they fail before implementation.
- [x] Implement a standard-library `HTMLParser` extractor and wire it into the fetch flow.
- [x] Run focused tests, then the complete test suite and static/build checks used by the repository.

### Task 4: Document and verify the Task 12 deliverable

**Files:**
- Modify: `docs/BUILD-ROADMAP.md`
- Modify: `docs/PROJECT-STATUS.md`

- [x] Mark Task 12 complete and describe its public fetch and page extraction contract without claiming Task 13 parsing is implemented.
- [x] Run the full test suite and Docker image build; confirm the application database was not accessed or changed.
- [x] Commit the completed Task 12 implementation on `dev` after verification.
