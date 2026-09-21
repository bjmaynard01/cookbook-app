# Cookbook App — Design Specification

- **Date:** 2026-09-20 (approved in session 2026-09-21)
- **Status:** Approved for implementation planning
- **Product context:** `PRODUCT.md` (same repository)
- **Scope:** v1 — a self-hosted recipe management web app that replaces the WordPress site at `cookbook.maynardfolks.com`

## 1. Purpose and Success Criteria

A local-first personal cookbook: store and organize a shared recipe collection, ingest recipes from arbitrary web URLs, and migrate the existing WordPress collection with full fidelity. Public visitors read without accounts; a small group of named editors (owner plus family/friends) share full create/update/delete rights.

Success looks like:
- Adding a new recipe from a pasted link takes under a minute, including review of the draft.
- Every migrated WordPress recipe is complete: no lost photos, ingredient text, steps, category membership, or permalink — old `?p=…` and `?page_id=…` URLs keep resolving.
- The public site reads at least as well as the WordPress version, with search and category navigation working.
- The app runs as a single Docker image, with one MariaDB, and works fully without an LLM key or internet access (except for web ingestion, which is inherently outbound).

## 2. Locked Decisions Register

| # | Decision | Source |
|---|---|---|
| D1 | Stack: Python 3 + FastAPI + Jinja2 + HTMX, server-rendered, single process, single Docker image | user, approach choice |
| D2 | MariaDB in both dev and prod (identical dialect + FULLTEXT); no SQLite anywhere | user, §1 |
| D3 | Recipe identity: immutable UUID; slug editable, never auto-derived from title | user, §1 |
| D4 | Ingredients: `raw_text` NOT NULL is the source of truth; parsed columns nullable; ingestion never mutates `raw_text` | user, §1 |
| D5 | Provenance: `source_system` / `source_id` / `source_url` / `source_name` / `extraction_method` / `imported_at`; idempotency on `(source_system, source_id)` | user, §3 |
| D6 | Legacy URL retention: `recipe_url_aliases` maps complete old URL paths (incl. query URLs like `/?p=1234`) → recipe UUID or category | user, §3 |
| D7 | Ingest pipeline: fetch (SSRF-guarded) → JSON-LD → microdata → LLM fallback (OpenAI-compatible, optional) → `RecipeDraft` → pre-filled form → save; 30 s hard cap (20 s fetch + 10 s LLM) | §2, adopted |
| D8 | WP migration: WXR XML, dry-run mandatory, image download to media volume, `migration_log` audit trail, verification gate, idempotent re-runs | §3, adopted |
| D9 | Auth: server-side sessions in MariaDB, argon2id, roles `editor`/`admin`, one-time `/setup`, CLI password reset, CSRF on every mutation, per-IP+username lockout | §4, confirmed |
| D10 | No per-recipe ownership in v1 — all editors edit everything; `created_by`/`last_modified_by` attribution only | §4, confirmed |
| D11 | Public read: home wayfinding page, category pages (8 real categories), tag pages, full-text search, two-column cookbook recipe page, quiet provenance, clean print stylesheet | §5, confirmed |
| D12 | Categories are many-to-many; `/c/<slug>` is a view over the relationship; ≥1 category enforced at save-time validation, not by DB constraint | user, §5 |
| D13 | Time is data, not tags: "under 30 minutes" is a predicate on `total_minutes`, never a tag; tags stay semantic/free-form | user, §5 |
| D14 | Search: MariaDB FULLTEXT baseline; spec commits to ranking behavior (title > tags/categories/ingredients > description/instructions; useful partial matching), not to a tokenizer — benchmark against the real corpus at implementation time | user, §5 |
| D15 | Servings scaling is an explicit future enhancement; model keeps `raw_text`-first and does not foreclose optional machine-readable quantities later | user, §5 |
| D16 | Visual direction (palette/type/composition) is chosen in a separate user-participated `impeccable` direction process at build time; this spec fixes structure and behavior only | §5, standing caveat |
| D17 | Domain: new app takes over `cookbook.maynardfolks.com`; legacy WP paths resolve via the alias table (app-side 301s) | user, §6 |
| D18 | Ingress: the owner's existing reverse proxy is the TLS/HTTP edge; the app is proxy-agnostic and only consumes standard forwarded headers from a trusted proxy; Tailscale is optional management access, never on the anonymous-reader path | user, §6 |
| D19 | `/healthz` = liveness (process only); `/readyz` = readiness (MariaDB reachable + media storage usable) | user, §6 |
| D20 | Secrets via file-based inputs preferred (`SECRET_KEY_FILE`, `LLM_API_KEY_FILE`, `FIRST_ADMIN_PASSWORD_FILE`); plain env vars permitted for dev/fallback | user, §6 |
| D21 | Backup/restore: DB + media, retention tiers, tested restore — but the app never touches the Docker socket or orchestrates Docker; restore targets are explicitly supplied (CLI flags); host-side scripts compose the drill | user, §6 |
| D22 | Dev DB data may be disposable; dev media and test fixtures are independently preservable (dev media as a host bind mount, fixtures in the repo) | user, §6 |
| D23 | Alembic for forward-only schema migrations, run at image entrypoint | user, §6 |
| D24 | Single-node, intentionally boring operations: no HA, no LB, no CI/CD, no monitoring stack in v1 | user, §6 |

## 3. §1 — Data Model

Single MariaDB database named `cookbook`. SQLAlchemy 2.0 (async, `asyncmy` driver) + Alembic. All IDs are UUIDv4 stored as `CHAR(36)`. Timestamps are `DATETIME` UTC.

### 3.1 `recipes`

| column | type | notes |
|---|---|---|
| `id` | CHAR(36) PK | **Immutable** — identity, never changes (D3) |
| `slug` | VARCHAR(200) UNIQUE NOT NULL | Editable; generated from title at save time only; on rename the old `/r/<old-slug>` path is recorded in `recipe_url_aliases` |
| `title` | VARCHAR(300) NOT NULL | required for save |
| `description` | TEXT NULL | intro/notes prose |
| `servings_text` | VARCHAR(100) NULL | free text ("Serves 6") — source data is heterogeneous |
| `servings_number` | DECIMAL(10,2) NULL | optional, for future scaling (D15); nullable, not derived |
| `prep_minutes` | INT NULL | |
| `cook_minutes` | INT NULL | |
| `total_minutes` | INT NULL | derived at save time (prep + cook, or explicit total); the filter predicate for time UI (D13) |
| `home_featured` | BOOLEAN NOT NULL DEFAULT FALSE | curation flag |
| `home_position` | INT NULL | only meaningful when featured |
| `source_system` | VARCHAR(20) NULL | `web` \| `wordpress` |
| `source_id` | VARCHAR(64) NULL | `wordpress`: WP post id; `web`: NULL |
| `source_name` | VARCHAR(300) NULL | og:site_name / domain, or WP site |
| `source_url` | VARCHAR(2000) NULL | normalized for web; legacy permalink for WP |
| `extraction_method` | VARCHAR(20) NULL | `manual` \| `jsonld` \| `microdata` \| `llm` \| `wxr` (D5) |
| `imported_at` | DATETIME NULL | when this source was pulled in |
| `published_date` | DATE NULL | original publication date (WXR post date) |
| `image_path` | VARCHAR(500) NULL | hero image on the media volume; one image per recipe in v1 |
| `created_by` / `last_modified_by` | CHAR(36) NULL FK `users.id` | attribution only (D10) |
| `created_at` / `updated_at` | DATETIME NOT NULL | |

Constraint: `UNIQUE (source_system, source_id)` — MariaDB permits multiple NULL `source_id` rows, so web recipes with no source ID are unaffected. This is the importer idempotency key (D5, D8).

### 3.2 `recipe_ingredients`

| column | type | notes |
|---|---|---|
| `id` | CHAR(36) PK | |
| `recipe_id` | CHAR(36) FK → `recipes.id` ON DELETE CASCADE | |
| `position` | INT NOT NULL | display order; unique per recipe |
| `raw_text` | TEXT NOT NULL | **source of truth** (D4); verbatim line from whatever origin |
| `quantity_text` | VARCHAR(50) NULL | best-effort parse ("1 1/2") |
| `unit` | VARCHAR(50) NULL | best-effort parse ("cup"), no vocabulary |
| `item` | VARCHAR(300) NULL | best-effort parse ("brown sugar") |
| `preparation` | VARCHAR(300) NULL | best-effort parse ("finely chopped") |

Rendered from parse columns when present, falling back to `raw_text` — readers never see parse seams. Nothing is ever discarded at any pipeline stage (D4, D15).

### 3.3 `recipe_steps`

`id`, `recipe_id` (FK, cascade), `position` INT NOT NULL (unique per recipe), `body` TEXT NOT NULL. Steps keep line breaks; minimal inline formatting.

### 3.4 Organization

- `categories`: `id`, `slug` UNIQUE NOT NULL, `name` VARCHAR(100) NOT NULL.
- `recipe_categories`: `recipe_id` + `category_id`, composite PK — **many-to-many**, no uniqueness per recipe (D12).
- `tags`: `id`, `slug` UNIQUE NOT NULL, `name` VARCHAR(100) NOT NULL. Free-form, semantic (D13).
- `recipe_tags`: `recipe_id` + `tag_id`, composite PK.

The eight initial categories are seeded from the WordPress site (EVIDENCE): Breakfast, Fermentation, Appetizers, Drinks, Main Dishes, Side Dishes, Baking, Desserts — slugs `breakfast`, `fermentation`, `appetizers`, `drinks`, `main-dishes`, `side-dishes`, `baking`, `desserts`.

### 3.5 Identity & auth

- `users`: `id`, `username` VARCHAR(60) UNIQUE NOT NULL (case-insensitive unique), `password_hash` NOT NULL (argon2id), `display_name` VARCHAR(100), `role` VARCHAR(10) NOT NULL (`editor`|`admin`), `is_active` BOOLEAN NOT NULL DEFAULT TRUE, `created_at`, `updated_at`.
- `sessions`: `id_hash` CHAR(64) PK (SHA-256 of the raw session token — raw token never stored), `user_id` FK, `csrf_token` CHAR(32) NOT NULL, `created_ip` VARCHAR(45), `created_at`, `last_seen_at`, `expires_at`. Sliding 12 h idle TTL (config `SESSION_TTL_HOURS`).
- `login_attempts`: `id`, `username` VARCHAR(60), `ip` VARCHAR(45), `attempts` INT NOT NULL, `locked_until` DATETIME NULL — per-IP+username lockout state (§6.6). The `users.failed_logins`/`locked_until` columns are dropped; this table is the lockout store.
- `recipe_url_aliases`: `id`, `path` VARCHAR(255) UNIQUE NOT NULL — the full legacy URL path as observed, e.g. `/?p=1869` or `/?page_id=16`; `recipe_id` CHAR(36) NULL FK; `category_id` CHAR(36) NULL FK; CHECK exactly one of the two is non-NULL; `created_at`. Served by an app-side middleware issuing **301** to the canonical URL (D6, D17).

### 3.6 Migration audit

- `migration_runs`: `id`, `started_at`, `finished_at` NULL, `source_file` VARCHAR(500), `file_sha256` CHAR(64) NULL, `status` VARCHAR(20) (`running`|`completed`|`failed`), `recipes_total`/`recipes_imported`/`recipes_skipped`/`recipes_errors` INT, `images_ok`/`images_failed` INT, `operator_id` CHAR(36) NULL, `notes` TEXT.
- `migration_recipe_log`: `id`, `run_id` FK, `source_post_id` VARCHAR(64) NULL, `recipe_id` NULL FK, `outcome` VARCHAR(20) (`imported`|`skipped`|`error`), `detail` TEXT.
- `migration_tag_log` not needed — tags in v1 migration are not present on the WordPress source (free-form categories only); ingest and manual entry supply tags later. (If the discovery spike finds tag-like structure, the importer will record it in `migration_recipe_log.detail` and a follow-up decision follows.)

## 4. §2 — Ingestion Pipeline

One contract object: **`RecipeDraft`** (pydantic model) — title, description, servings (text + optional number/unit), prep/cook/total minutes, ingredients (list of `{raw_text, quantity_text?, unit?, item?, preparation?}`), steps (ordered `{body}`), tags (name list), suggested categories (name list), hero image reference (remote URL or uploaded path), and provenance (`source_system`, `source_id`, `source_url`, `source_name`, `extraction_method`, `imported_at`, `parse_notes`). All fields nullable except `title`. Every parse stage emits it; the preview form renders it; the save endpoint accepts it. One type, zero translation layers. **Partial parse is a valid state, not an error** (D4).

### 4.1 Flow for `POST /recipes/ingest {url}`

1. **Normalize & guard.** URL must be `http(s)`. Before connecting: resolve the hostname and reject if the IP is loopback, private (RFC 1918), link-local (incl. `169.254.0.0/16`), or otherwise non-public — a logged-in editor typing an internal address is the one path that turns ingest into SSRF. Fetch with `httpx`, browser-like `User-Agent`, **20 s timeout**, **5 MB body cap**.
2. **Stage 1a — JSON-LD.** Extract every `<script type="application/ld+json">` block; walk arrays, `@graph`, and nested candidates; keep all `@type: Recipe` (and array-valued `@type`s); pick the candidate with the most populated Recipe fields; map to `RecipeDraft` (ingredients/steps as `raw_text` + best-effort split, image first of `image` / `og:image`).
3. **Stage 1b — microdata.** If 1a found no usable recipe: schema.org microdata with `itemtype*recipe` (case-insensitive, `itemprop = ingredients/description/prepTime/cookTime/totalTime/recipeYield`). Cheaper than an LLM call and catches older recipe sites.
4. **Stage 2 — LLM fallback.** Only if both stages found nothing usable (or fields that are obviously garbage — e.g., ingredient lines like "See photo"). Cleaned visible text (nav/cookie banners stripped), truncated to ~24k chars, to an OpenAI-compatible chat endpoint. Strict-JSON system prompt requiring `RecipeDraft` JSON; response validated against the model; **`temperature=0`**. Config: `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY` (or `LLM_API_KEY_FILE`, D20). **No key, or call fails or times out: return the stage-1 result (possibly empty) with a `parse_notes` entry explaining what happened — never a hard error, never a hang** (D7, principle 5).
5. **Dedup guard.** Before returning the draft, look up an existing recipe with the same normalized `source_url`. If found, `RecipeDraft.already_exists = {id, title}` and the preview shows a **non-blocking** banner ("A recipe from this source already exists — X"). Saving with a different slug still works.
6. **Preview → save.** The editor gets the standard recipe form pre-filled from the draft, with provenance already stamped. `extraction_method` = the stage that actually populated the usable fields (`jsonld` | `microdata` | `llm`); if all fields were hand-entered, `manual`. Save → `POST /recipes` (or `PUT /recipes/{id}`) creates the row with the server-generated UUID and generates `slug` from title; on uniqueness collision the slug gets a `-2` suffix (the slug is *not* part of identity, D3/D5).

### 4.2 30-second hard cap

Fetch 20 s + LLM 10 s = **30 s worst-case**, enforced with an overall `asyncio.wait_for` around the pipeline. Any stage overrunning is treated as that stage's failure state, never a hang (D7). Deterministic stages are sub-second in practice, so LLM time is the dominant budget and only spent when needed.

### 4.3 Failure surface — four distinct, non-ambiguous UI states

1. **Fetch failed** (bot-wall, timeout, SSL, >5 MB) — says exactly what happened; form offers to proceed blank.
2. **Nothing found** (both deterministic + LLM stages empty, or LLM absent with nothing deterministic) — blank form with `source_url`/`source_name` preserved and a hint that the fields are hand-entry-able; **not** an error page.
3. **Partial** (some fields parsed, `parse_notes` present) — form pre-filled, `parse_notes` visible in a quiet callout so the editor sees what was guessed.
4. **Full** (all expected fields populated) — form pre-filled, `parse_notes` empty or informational.

Each state is a distinct renderable template, not a boolean the UI has to interpret.

### 4.4 Image handling at ingest

Remote hero image URLs are **not downloaded at ingest time** — only at save. At save, if no local image exists, the downloader fetches the first candidate URL (with the same SSRF guard as the HTML fetcher) into the media volume and sets `image_path`. If download fails at save time, the recipe saves with `image_path = NULL` and `parse_notes` gains an "image download failed at save" entry — **never blocks the save** (D8).

### 4.5 What ingest never does

- Never mutates `raw_text` of ingredients (D4).
- Never creates a recipe without an editor explicitly clicking save.
- Never writes to `recipe_url_aliases` (those are WP-migration and slug-rename paths only).
- Never exposes fetch failures, LLM prompts, or keys to the public read side.

## 5. §3 — WordPress Importer

One-time-ish migration; run by the owner or an admin via the CLI. WXR-first (D8); a live REST/DB fallback is out of scope unless the WXR proves insufficient — the discovery spike decides.

### 5.1 Discovery spike (must precede implementation)

The owner supplies **both** a WXR export and the site URL (already on hand: `https://cookbook.maynardfolks.com/`). The spike, in a scratch container against a disposable MariaDB:

1. **Enumerate post/postmeta keys in the WXR** (`<wp:post_meta>`) — expected: recipe plugin meta, or none. The EVIDENCE section shows plain posts with `Ingredients :` … `Directions :` literal markers and **no recipe plugin** — the spike confirms and records any postmeta actually present (including any `yield` / `prep_time` / `cook_time` keys if they exist).
2. **Pick the WXR recipe set**: which `<wp:post post_type>` values are recipes (likely `post`), which are pages (likely `page`), and which page ids are the eight category pages (EVIDENCE: `page_id` 16, 1557, 18, 10, 8, 14, 20, 12 → Breakfast, Fermentation, Appetizers, Drinks, Main Dishes, Side Dishes, Baking, Desserts).
3. **Resolve category membership**: membership is *not* a clean 2-column relationship in the WXR — for this site it is *which category page links to which recipe posts*. Spike the resolution rule (page → `<link>`/hrefs → post ids) and record it in the plan. Inline breadcrumb text in the body ("Baking Breakfast") is a secondary signal only.
4. **Sample 5 recipes end-to-end** with a prototype parser (title, description, ingredients, steps, image, category, legacy permalink, original publish date) and diff against the live rendered pages. This diff *is* the "spike" — it is not a design doc, it is evidence the plan can rely on.
5. **Record** (in the implementation plan, not here): which postmeta keys exist and map to which draft fields; which image URLs in the WXR are the canonical originals (vs. thumbnails); whether `comments.xml`/attached-comments exist in the WXR and whether the owner wants them (out of v1 scope per §3, but the spike notes it either way).

The spike's output is a short findings note committed to the repo, feeding the implementation plan. Design assumptions are locked from there, not from this document.

### 5.2 The importer

CLI: `cookbook import-wp --file wp-export.xml [--dry-run] [--report FILE]`.

**Dry-run (always first):** parse → `RecipeDraft`s → resolve categories → report. Report contents:
- total recipes found, by source post id
- per recipe: fields found / fields empty (title OK, ingredients N, steps N, image Y/N, servings present? times present?)
- category-resolution table: recipe → category set (with the evidence that placed it)
- legacy-permalink table: old URL → intended new slug
- collision list: any slug that would collide with an existing `recipes.slug` or with another recipe in this run

**Idempotency (D5):** on a non-dry-run write, for each recipe compute the `(source_system='wordpress', source_id=<wp post id>)` pair; if a matching row already exists in `recipes`, skip it and record in `migration_recipe_log` with `outcome='skipped'`. Re-runs after partial failure continue from where the log left off — a "resume" run is just a re-run.

**Slug generation:** from the title, lowercased, non-alphanumeric → `-`, deduped. On collision with an *existing different* recipe (detected by dry-run), the new slug gets a `-2` suffix **and** the old URL is *always* recorded in `recipe_url_aliases` → the new `recipe.id`. Slug collision resolution is a cosmetic concern; identity is the UUID (D3, D6).

**Image migration:** for each recipe image URL in the WXR (canonical/original, per the spike), download to `MEDIA_DIR` under the recipe's UUID with the original extension (e.g. `media/<uuid>.jpg`). Record each in `migration_recipe_log` (`outcome='imported'`, detail = local path) — failures go to `images_failed` **without** blocking the recipe write (D8). `image_path` is set on success, left NULL on failure (same rule as ingest, §4.4).

**Legacy URL aliases (D6):** for every imported recipe, write a `recipe_url_aliases` row with `path =` the full legacy WordPress URL (observed pattern from EVIDENCE: `/?p=1234`; the spike records the exact form in the WXR / from the site) and `recipe_id =` the new UUID. For each of the eight category pages, `path = /?page_id=<wp page id>` and `category_id` = the matching category. The app-side middleware converts these 301s (D17).

**Audit trail (D8):** `migration_runs` + `migration_recipe_log` (§3.6) record everything. A `--resume` flag re-runs the file and skips recipes whose `(source_system, source_id)` is already present per the log — the log is the source of truth for "already migrated," not the live DB alone.

### 5.3 Verification gate (must pass before the site is pointed at the new app)

1. **Count parity**: recipes in the WXR with `post_type = post` = `recipes_imported + recipes_skipped` in the run.
2. **Field spot-check**: the plan's spike sample (5 recipes) re-parsed and diffed field-by-field by the owner against the live WordPress page.
3. **Every recipe has ≥1 category** (per D12/D23 — a recipe with no category is not migrated; the report lists these and the owner decides).
4. **Every recipe has `raw_text` ingredients** (D4 is non-negotiable; any recipe with empty ingredients is flagged).
5. **Every legacy URL in the WXR resolves** through the new app (the alias table covers them; a sample is hand-verified by the owner).
6. **Image parity**: `images_ok` + `images_failed` account for every image URL that was present; the owner accepts (or accepts the failure list).
7. **Re-run idempotency**: a second run reports all `skipped`, zero new `imported`.

The gate is a script (`cookbook import-wp --verify --run <id>`), not a manual ritual — its output is the migration's acceptance check, committable to the repo as a report.

## 6. §4 — Authentication & Authorization

**Shape: anonymous public read, session-gated edit. No accounts exist for visitors** (D9, D10).

### 6.1 Sessions

- Sessions in MariaDB (`sessions` table, §3.5): the raw token is issued once, never stored — only its SHA-256 hash is the row key. Cookie: `__Host-cookbook_session`, `HttpOnly`, `Secure`, `SameSite=Lax`.
- TTL: **sliding 12 h idle** (`SESSION_TTL_HOURS`); expiry = row delete (revocation is a row delete — no token-refresh machinery).
- `POST /logout` deletes the row. Password change invalidates **all** of that user's sessions. Disabled users' sessions die at next request.

### 6.2 Passwords

- **argon2id** (via `argon2-cffi`), params at or above OWASP minimum (m=19 MiB, t=2, p=1) — a personal app, but the hash is free.
- No email, so password reset is a CLI command: `cookbook user reset-password <name> --interactive` (or `--password-file <path>`, D20). Admin-gated in the UI as well: `POST /admin/users/{id}/reset-password`.

### 6.3 Roles (D9, D10)

| capability | public | editor | admin |
|---|---|---|---|
| read recipes / categories / tags / search | ✓ | ✓ | ✓ |
| create / update / delete any recipe (D10 — no per-recipe ownership) | — | ✓ | ✓ |
| ingest from URL | — | ✓ | ✓ |
| manage categories & tags | — | ✓ | ✓ |
| manage `recipe_url_aliases` | — | — | ✓ |
| run the WP importer | — | — | ✓ |
| user create / disable / reset / role change | — | — | ✓ (subject to 6.5) |

### 6.4 First boot (`/setup`)

- While **zero users exist**, `GET /setup` is the only auth surface reachable; `POST /setup` validates username + password + confirmation and writes the owner admin row. After that, `/setup` returns 403 forever.
- Headless alternative (D20): `cookbook setup --username owner --password-file ./boot-pass.txt` — same single-user guard (refuses if any user exists, unless `--force-owner-recreation`). `FIRST_ADMIN_PASSWORD` as a plain env var is accepted **only** in dev (warned on in prod env).
- Interactive is the normal path (D20).

### 6.5 Account lifecycle

- Admins create editors: `POST /admin/users` (username, password, display name, role).
- Disabling a user = `is_active = FALSE` + session wipe. **The last active admin cannot be disabled or demoted** (app-enforced); the check counts active admins, not rows.
- No self-service signup (D9). No self-service password change UI in v1 — that's an admin action or the CLI. (A trivial future addition; the argon2id verifier + session-invalidation path exists either way.)
- The owner's account is never "the" account in the code — there is no `is_owner` column; the rule is *role = admin* + *the last active admin can't be disabled*.

### 6.6 Login hardening (D9)

- Per-IP + username lockout: 5 failed attempts in 10 min → locked until `locked_until` (exponential backoff, cap 1 h). Counters in `users.failed_logins` / `users.locked_until`, keyed loosely by IP+username via a small `login_attempts` table (the spec keeps it to the two `users` columns + the table for accuracy).
- Constant-time: compare against a dummy hash for unknown usernames; uniform timing.
- **CSRF**: per-session `csrf_token` (§3.5) required on every `POST/PUT/DELETE`; HTMX sends `Hx-Request: true` and carries the token in the `X-CSRF-Token` header (injected via a global `htmx.config` set from the page context); browser form POSTs use a hidden field. This is the one auth hole server-rendered apps usually have — it is a **named checklist item**, not a hope.
- Public read surfaces: `Cache-Control: public, max-age=300, s-maxage=600` (proxy-friendly staleness — D18 lets the owner tune it). Everything behind login: `Cache-Control: private, no-store`.

### 6.7 Out for v1

Account self-service signup, OAuth/SSO, 2FA, API tokens, per-recipe ACLs (D10), email of any kind, MFA via TOTP. All additive later; none is implied by what was asked.

## 7. §5 — Public Read Experience

**Design mode: Read.** The visitor's task is to understand a recipe well enough to cook it and to find the right recipe quickly. Every decision below serves one reading question: *what am I actually making, and what do I do in the next ten seconds?* (Structure/behavior per D11–D16; visual direction is a separate `impeccable` direction process at build time — D16.)

### 7.1 Public page map (seven surfaces)

| path | surface | purpose |
|---|---|---|
| `/` | **Home** | "choose what to cook." A working index, not a marketing hero. Lanes, all first-class: *featured picks* (a handful of `home_featured` rows in `home_position` order — light curation), *by category* (the real eight, thumbnail + name + count), *by tag* (top semantic tags), and *search*, one action away (D13: search is not hidden). |
| `/c/<category-slug>` | **Category** | Recipes in the category, ordered by `updated_at` desc (default). Each card: thumbnail (or a neutral typographic placeholder if no image — recipes render beautifully without one, D11), title, one-line `description` hook, `total_minutes` + `servings_text` if present. In-category narrowing: time predicate (structured — D13) and tag chips (semantic — D13) shown as two different UI groups, never mixed into one list. |
| `/t/<tag-slug>` | **Tag** | Same list engine as categories; tag chips cross-link. |
| `/search?q=` | **Search** | MariaDB FULLTEXT baseline (D14); ranking behavior committed: title matches strongest, then tag/category/ingredient matches, then description/steps; useful partial matching where practical. Result list + the same time/tag narrowing as categories. **Zero-results state** says exactly what it did (query, category scope) and offers close tags/categories — never a blank void. Tokenizer choice (default word parser vs. ngram) is an implementation decision, benchmarked against the real migrated corpus (D14). |
| `/r/<slug>` | **Recipe** | The product's heart (below). |
| legacy `/?p=…`, `/?page_id=…` | **301 middleware** | App-side lookups in `recipe_url_aliases` → `301` to the canonical path (D6, D17). Readers never see a "moved" page. |
| `/404`, `/403` | error states | honest, on-brand, one action (search / home) — no "you should go elsewhere" |

### 7.2 The recipe page — reading contract (D11)

Top-to-bottom, desktop:

1. **Header band** — one compact block: title, `servings_text` (or `servings_number` + unit if present), `prep_minutes` / `cook_minutes` / `total_minutes` shown as the two or three numbers a cook checks (not a table). Breadcrumb (category, if any) above; no logo-bar duplication of the title.
2. **Hero image** — one image, full-width of the text column, or **absent**; when absent, the page composes typographically (a quiet rule, not a placeholder box), so an imageless recipe is first-class, not a fallback state (D11).
3. **Two columns** — **ingredients** (left, ~38%) and **steps** (right, ~62%), the classic cookbook layout. On mobile they stack ingredients-first (hands-on reading order: read ingredients, then steps, then back up — D11). Ingredient rendering: parse columns (quantity/unit/item/preparation) when present, **falling back to `raw_text` invisibly** — the reader never sees parse seams (D4). Sub-sections inside ingredients (e.g. "Filling:") render as a quiet sub-heading within the column.
4. **Notes** — `description` as an intro above the columns and/or a "Notes" section below the steps; whichever reads better by template, one rendering, no duplication.
5. **Provenance (quiet)** — ingested recipes carry a small "From <source_name>" line with the source link at the foot, above the tag chips — attribution done quietly (D11), which is also the honest thing for externally-sourced recipes. WordPress-migrated recipes carry **no** provenance line (they are the collection, not borrowed content).
6. **Tag chips + category chips** at the foot — both link to their respective pages.
7. **Print** — a clean `@media print` stylesheet: single column, no nav, no chips-as-graphics, image at top, legible. The browser print path is the only printing (D24: no "print view" button, no "copy recipe" widget).

**Sticky-on-scroll:** on desktop, if the page is long, a thin sticky strip appears after the first viewport with title + current section (Ingredients / Steps) — a jump aid, not a takeover. Mobile: a minimal "top ↑" affordance. (Both are behavior, not decoration; the `impeccable` direction process will size them.)

### 7.3 What's deliberately NOT on the public side

No accounts, no favorites, no ratings, no comments, no newsletter bar, no "related recipes" widget, no ad space, no share-button row, no download-CV button (D11, principles 1–5, EVIDENCE-rejected looks). Related-recipe discovery, if wanted, is the search page — always one action away (D11/D13), not a widget.

### 7.4 Editor-side surfaces (design mode: Operate)

The same design system, lower visual ambition, higher scanability — the rubric the `impeccable` skill applies to Operate surfaces (dense, legible, low-color):

- **Dashboard** (`/edit`): recipe list — title, category chips, image thumb (or none), `updated_at`, `total_minutes`, `extraction_method` badge, `source_name` when present — sortable by any, filterable by category / tag / text. Bulk actions out of v1 (D24).
- **Recipe form** (`/recipes/new`, `/recipes/{id}/edit`) — the one form for create + update + ingest-landing (D15's "one type, zero translation"). Field order mirrors the public page so the editor sees what the reader sees: basics (title, description, category chips, tag chips) → image (upload / pick from URL at ingest) → servings / times → ingredients (ordered, `raw_text` primary, parse columns advanced) → steps (ordered) → provenance (read-only display of the stamped fields, editable in a "details" collapse). Save validation per §8: `title` required, ≥1 category required (D12), ≥1 ingredient required; `steps` optional-but-recommended (warned, not blocked — a recipe is allowed to have no steps in v1, e.g., a list of fermentation starters; the plan re-visits if the migration evidence says all recipes have steps).
- **Ingest launcher** — a single box on the dashboard ("paste a URL or start a new recipe"), the marquee interaction of the Operate side (D7). Submits `POST /recipes/ingest`, lands on the same form pre-filled (§4.1), with the four failure states (§4.3) rendered as distinct banners on that form.
- **Admin** (`/admin/…`, admin-only): `users`, `categories`, `tags`, `aliases` (CRUD on `recipe_url_aliases`), `import` (WP import runs + the §5.3 verification gate + report download), `sessions` (list + kill). Each is a list + a create/edit + an archive, not a settings page.

### 7.5 Standings

Everything in §7.1–7.4 is structural and behavioral — the palette, type, spacing, and composition are decided in the `impeccable` direction process (candidate worlds → user picks → direction contract → build, per D16). A detector run over the finished UI (`impeccable detect`, EVIDENCE/principle 5) is the final quality gate before the cutover, not a per-commit ritual.

## 8. Save-time validation (the only place structural rules bite)

Enforced at the API/save layer, **not** as DB constraints (D12, principle 2 — one edit away from valid, no lock):

| rule | where |
|---|---|
| `title` non-empty (whitespace-trimmed) | save |
| ≥1 category (D12) | save — 400 with a field error, form re-renders |
| ≥1 ingredient with `raw_text` (D4) | save — 400 with a field error |
| slug uniqueness — collision → auto-suffix `-2` (cosmetic, D3) | save |
| `total_minutes` = prep+cook (or explicit) — recomputed server-side from the submitted values | save |
| image file ≤ 10 MB, type in `{jpg, png, webp, avif}`, uploaded to `MEDIA_DIR` | save |
| user is active + role ≥ editor (D9) | middleware |
| CSRF token (D9) | middleware |
| last-active-admin can't be disabled/demoted (§6.5) | admin endpoint |

`extraction_method`, `source_*`, `imported_at` are **read-only on update** (provenance is a historical fact, not an editable field — changing them is a data-integrity event, handled as a future admin action, not a form field).

## 9. §6 — Deployment & Operations

**One image, one MariaDB, two environments.** A single Docker image builds once and runs in dev and prod. The only difference is *where the database and the edge point* — image, dialect, and behavior are identical (D1, D2, D22, D24).

### 9.1 Dev

`docker-compose.dev.yml` (in-repo), two services:

- **app** — the cookbook image; `0.0.0.0:8000`; `depends_on: db (condition: service_healthy)`; env: `DATABASE_URL` → the compose `db`, `MEDIA_DIR=/media` bound to **a host directory** (not an anonymous volume — dev media is preservable per D22, and the same bind-mount discipline holds in prod).
- **db** — `mariadb:11`, named volume for data (disposable per D22 — dropping the volume is the dev reset), `healthcheck` = `mariadb-admin ping`.

Dev media (upload tests, fixtures dropped there during development) survives `docker compose down -v` because it's a host path, not a volume (D22). Repo-internal test fixtures live under `tests/fixtures/` and are code, not media.

### 9.2 Prod

Same image; no `db` service. Config:

- `DATABASE_URL` → the owner's **existing MariaDB on the LAN** (D2, D21); the app creates no database and owns no schema other than Alembic-managed migrations (D23).
- `MEDIA_DIR` → host-mount path the owner chooses; the owner snapshots/restores it like any directory (D21, D22).
- Edge — the owner's **existing reverse proxy** terminates TLS at `cookbook.maynardfolks.com` and forwards to the app (D17, D18). The app is proxy-agnostic: it reads the forwarded client IP (`X-Forwarded-For` — first untrusted hop ignored) and forwarded proto (`X-Forwarded-Proto`) only when `TRUSTED_PROXY_CIDRS` (default: the proxy's address) says the header is authoritative; `Secure` cookie + HSTS-ish redirect behavior follows the forwarded proto. No app-level TLS, no app-level domain config.
- Domain takeover (D17): DNS already points at the owner's box; once the app is serving, the WordPress container is stopped. **Cutover is an owner decision, made after the §5.3 gate passes and the owner has browsed the new site** — the old container is not touched before then.
- Tailscale is **optional, private, management-only** access to the box — never on the anonymous-reader path (D18).

### 9.3 Secrets (D20, D21)

File-first, env-fallback. The app reads, for each secret, `_FILE` first (first line, trailing newline stripped), then the plain env var:

| secret | file | env (dev fallback) |
|---|---|---|
| app signing key (sessions + CSRF) | `SECRET_KEY_FILE` | `SECRET_KEY` |
| LLM key | `LLM_API_KEY_FILE` | `LLM_API_KEY` |
| first-boot owner password | `FIRST_ADMIN_PASSWORD_FILE` | `FIRST_ADMIN_PASSWORD` (dev only) |

Plain env vars are accepted in dev (compose) and refused at boot in a prod-looking config (a warning is printed, not a failure — D21's "boring" principle: the app should never take the box down because a secret was mis-placed; it should fail loudly in logs and the owner fixes the file). `SECRET_KEY` is generated once (`cookbook init-secret --out secrets/COOKIE_SECRET_FILE`) and kept on the box; rotation = new file + all sessions invalidated (expected, documented).

### 9.4 Config surface (complete list)

`DATABASE_URL`, `SECRET_KEY`/`SECRET_KEY_FILE`, `MEDIA_DIR`, `COOKIE_SECURE` (auto from forwarded proto; overridable), `SESSION_TTL_HOURS` (default 12), `TRUSTED_PROXY_CIDRS`, `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY(_FILE)`, `LLM_TIMEOUT_SECONDS` (default 10), `INGEST_FETCH_TIMEOUT_SECONDS` (default 20), `INGEST_MAX_BYTES` (default 5 MiB), `IMAGE_MAX_BYTES` (default 10 MiB), `FIRST_ADMIN_PASSWORD(_FILE)` (bootstrap only). Nothing else is tunable; the rest is code. (Keep the list boring and short — D24.)

### 9.5 Health (D19)

- `GET /healthz` — **liveness**: 200 if the process can serve; **no dependency checks**. Used to know "is it up."
- `GET /readyz` — **readiness**: 200 only if (a) a MariaDB round-trip succeeds (`SELECT 1`) and (b) `MEDIA_DIR` exists and is writable (touch + delete a temp file). Used by the proxy / owner's stack check to know "can it take traffic." A failing `/readyz` is a loud log line, not a self-shutdown (D24).

### 9.6 Migrations (D23)

Alembic, forward-only, applied at image entrypoint (`alembic upgrade head` then exec the server). A boot with a down-migration or a drift is a logged error and the entrypoint exits non-zero (the container visibly fails; D24 — the owner fixes the env, there's no silent fallback). Migration files are committed with the code; the schema version is what the app declares to `/readyz` on failure (a diagnostic, not a gate).

### 9.7 Backup & restore (D21, D22)

- **Backup** (owner's concern, not the app's): nightly, per D24, via the owner's existing jobs. The app supplies a **deterministic export** — `cookbook backup --out <dir>` writes `cookbook-<date>.sql.gz` (a `mysqldump` of the `cookbook` schema — the **app runs the SQL, not Docker**, D21) and `media-<date>.tar.gz` of `MEDIA_DIR`. Retention tiers are the owner's; the spec fixes the tool, not the schedule (boring, D24).
- **Restore** (D21): `cookbook restore --sql <file> --media <dir> --db-url <...>` restores into **explicitly supplied targets** and then runs the §5.3-style self-check (counts against a known-good dump) and a `/readyz`-equivalent assert. The app **never** touches the Docker socket, `docker` CLI, or any orchestrator (D21). The host-side drill (disposable MariaDB, point the app at it, restore, browse) is a host script owned by the owner — the spec's only requirement is that the restore command's targets are explicit flags, so that drill is trivial and idempotent.
- **Dev media** is preservable (D22, §9.1 bind mount); **dev DB data is not** (disposable volume) — the distinction is deliberate (D22).
- A backup is valid only if it has been restored into a throwaway environment once — the spec **requires** the restore drill as a gate before first production cutover, alongside the §5.3 WP verification gate (D8, D21).

### 9.8 Ops surface & out-of-scope (D24)

In v1: `/healthz`, `/readyz`, a structured JSON log line per request (owner's log collector reads it), no metrics endpoint, no admin API beyond the HTMX admin pages, no separate worker (the ingest pipeline is in-process under a bounded concurrent pool — `INGEST_MAX_CONCURRENT`, default 4 — D24), no queue, no cache tier beyond the proxy (D18), no separate image registry (the owner's build), **no CI/CD, no HA, no load balancer, no monitoring stack, no multi-node** (D24). Anything on this list that later becomes needed is additive; none is on the critical path.

## 10. Testing Strategy (spec — the plan carries the cases)

- **Pipeline** — fixtures for: JSON-LD single, JSON-LD `@graph`, JSON-LD array, microdata only, no recipe, ambiguous ingredient (raw_text preserved, no invented parse), LLM-absent, LLM-present, 30 s-cap (a mock fetch that overruns the budget → stage is failed, not hung). LLM via an in-process `httpx` mock server (same OpenAI-compatible shape), **no real internet** in the CI (principle 5, EVIDENCE).
- **Idempotency** — WXR import run twice; second run's `recipes_imported = 0` and `recipes_skipped = N` (D5, D8, §5.3 gate 7).
- **Legacy URLs** — every alias in the test fixture WXR → 301 → 200 on the canonical path (D6, D17, §5.3 gate 5).
- **Auth** — login success/failure, lockout after threshold, CSRF absent → 403, CSRF present → 200, session expiry, role gate (editor can't touch `/admin/*`), last-admin self-disable blocked (D9, §6.5, §6.6).
- **Search** — seeded corpus (a handful of fixtures), assert the committed ranking: an exact-title hit outranks an ingredient-only hit, which outranks a description-only hit (D14, §7.1). Tokenizer is an implementation choice, benchmarked at integration, not asserted by this suite.
- **Save-time validation** — the §8 table, row by row, including the "last active admin can't be disabled" rule.
- **Restore drill** — a `test_restore.py` that runs `restore --sql <fixture> --media <fixture>` into a fresh MariaDB and asserts counts + a rendered recipe page. (D21, §9.7, §5.3.)
- **No-key mode** — the entire public read side + ingest (deterministic path) + save runs with no `LLM_*` set at all (principle 5, D7, D20).

## 11. Out of Scope — v1 (explicit, per the register)

Favorites, ratings, comments, servings scaling (D15), per-recipe ACLs (D10), print-CV / share-button / copy-recipe widgets (D11, §7.3), accounts for the public (D9, D11), OAuth/SSO/2FA/API tokens (D9, §6.7), multi-node / HA / LB / queue / cache tier / CI-CD / monitoring (D18, D24), Tailscale on the reader path (D18), in-app TLS (D18), in-app domain config (D18), Docker socket / orchestrator access (D21), in-app secret rotation UI (D20), SQLite path (D2, principle 4). Each is additive later; none is implied by what was asked.

## 12. Open Items (blocking none of the above; flagged for the review)

1. **LLM provider & key** — the spec assumes an OpenAI-compatible chat endpoint behind `LLM_BASE_URL` / `LLM_MODEL` / `LLM_API_KEY(_FILE)` and that the app **fully functions without one** (D7, D20, principle 5). Confirm the provider at spec review (PRODUCT.md carries the same flag).
2. **Comments on the WordPress source** — the EVIDENCE section records that comments exist on the live posts. The spec's position: **out of v1 scope** (D11, §7.3, principle 5), but the §5.1 discovery spike records whether they're present in the WXR and whether the owner wants them, so the decision is informed, not default (EVIDENCE).
3. **Yield/times on the WordPress source** — not present in the rendered bodies (EVIDENCE). If they live in `postmeta`, the spike records the mapping into `RecipeDraft` (D4, §4.1); if they don't exist, the migration marks the fields **empty rather than inventing them** (EVIDENCE) and the public recipe page renders the servings/times line from whichever columns are present (D11, §7.2). Either way, §7.2's reading contract is unchanged.
