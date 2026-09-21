# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Stack

Python 3 + FastAPI + Jinja2 + HTMX (server-rendered, single process, single Docker image). **MariaDB in both dev and prod** — identical dialect and FULLTEXT behavior in every environment; dev gets a throwaway MariaDB container via docker-compose. No SQLite path. Frontend craft is governed by the `impeccable` skill — modern, sleek, unobtrusive. User-decided 2026-09-20.

## Users

- **Editors:** multiple named, trusted accounts (owner plus family/friends). Everyone who can log in can create, update, and delete recipes.
- **Public visitors:** anonymous, read-only. Browse recipe pages and category pages (Breakfast, Fermentation, Appetizers, Drinks, Main Dishes, Side Dishes, Baking, Desserts — the site's real eight).

The app replaces a WordPress cookbook that the public already visits; the replacement must be at least as easy to read as it, and easier to keep up to date.

## Product Purpose

A local, self-hosted cookbook: store and organize a personal recipe collection, replace the WordPress container, and eliminate the core pain — recipes currently require manual entry. The killer feature is **URL ingestion**: paste a link to a recipe on any site, get a clean, editable draft, save it.

Success looks like: adding a new recipe from a link takes under a minute including review; migrated WordPress recipes are complete (no lost photos, ingredients, steps, or permalinks); the public site keeps working with working old URLs.

## Positioning

A local-first personal cookbook with one-click ingestion from the open web (deterministic recipe JSON-LD parsing, LLM fallback for messy pages) — not a public recipe-sharing platform, not a WordPress replacement with the same manual-entry burden.

## Operating Context

- Runs as a single Docker image on the owner's machine; served publicly read-only via a reverse proxy or Tailscale.
- LLM fallback parsing: assumed OpenAI-compatible hosted API, key supplied via env config; the app must fully function with no key configured (ingest falls back to deterministic parsing only, and the UI says so). — *Assumed 2026-09-20; confirm at spec review.*
- Migration source: an existing WordPress site whose recipes include photos, full ingredient/step text, servings/yield, times, categories/tags, and permalinks (slugs).
- Frontend iteration: `impeccable` skill owns design quality; the mechanical detector runs once over finished UI targets (`impeccable detect`).

## Capabilities and Constraints

- CRUD for recipes: create, update, delete — editors only.
- URL ingestion: paste URL → fetch → parse (JSON-LD `Recipe` first, LLM fallback for non-conformant pages) → editable draft → save.
- Organization: categories with dedicated, browsable pages (Breakfast, Dinner, Main Courses, Sides, and others), plus tags, plus full-text search over recipes.
- WordPress migration importer: photos, title, ingredient and step text, servings/yield, prep/cook/total times, categories, tags, permalinks/slugs must carry over; old public URLs must keep resolving (slug-permalink parity).
- Auth: password-gated edit side with multiple accounts; public anonymous read.
- Database: MariaDB everywhere (dev and prod).
- Constraints: single app image; no SPA build pipeline; app must not require outbound internet for core use (LLM calls only during fallback parsing of ingested pages, and only if a key is configured).

## Brand Commitments

All confirmed by the user on 2026-09-20 (impeccable `init` round).

- Fresh visual identity — no obligation to resemble the WordPress site (it's a replacement).
- **Rejected looks** (must not appear in the design): generic food-blog (stock-photo vibe, ad space, comment sections, newsletter bars); busy/flashy treatment (excess motion, gradients, effects that distract from the recipe).
- **Information-architecture expectations**: category nav always visible; search always one action away; a home/start page that helps choose what to cook. The WordPress site's working IA is kept as reference.

## Evidence on Hand

Inspected live (2026-09-20) at `https://cookbook.maynardfolks.com/` (theme `recipes-blog`; plugins: recent-posts-widget-with-thumbnails, subscribe-to-comments-reloaded). All findings below are observed, not guessed:

- **Site title:** "Maynard Family Cookbook — Sharing Family Recipes the World Over."
- **Recipe storage: plain WordPress posts, no recipe plugin.** No Recipe JSON-LD, no structured ingredient markup (only breadcrumb microdata). Recipe body is post content text with literal markers: `Ingredients :` … `Directions :` (observed on posts 1869 and 1250). Some recipes have sub-sections inside ingredients (e.g. "Filling:").
- **Categories are static WP pages**, not a taxonomy: `?page_id=` — Breakfast (16), Fermentation (1557), Appetizers (18), Drinks (10), Main Dishes (8), Side Dishes (14), Baking (20), Desserts (12). A recipe's category membership is implied by which page lists it (and inline breadcrumb text in the body, e.g. "Baking Breakfast"). Not a clean 2-column relationship — the importer must resolve membership per the method established in the discovery spike (WXR page→post relation).
- **Permalinks are plain `?p=1234` / `?page_id=123` URLs** (observed in all hrefs), not pretty slugs. Legacy redirects must map these query-URLs → new recipe UUIDs.
- **Yield/servings and prep/cook times are NOT present in the rendered bodies** of the sampled recipes. If they exist they are in `postmeta` or custom post fields — must be confirmed in the WXR export during the discovery spike; if absent, the migration must mark those fields empty rather than inventing them.
- **Comments exist on posts** (comment forms + "N Comments" visible). The `comments` table in WXR can be imported on the same basis as recipes (editor-moderated, not a public forum) — pending user decision (was out of v1 scope in §3; re-open if the user wants them).
- Ingredient lines use free-form quantities throughout ("1lb", "2c", "1 1/2c", "6g (1tsp)", "5½ cups") — mixed imperial/metric, no unit vocabulary. Confirms the `raw_text`-first ingredient model in §1 is mandatory, not a fallback.

## Product Principles

1. Ingest is a first-class path, not a convenience — the paste-URL flow is the primary way recipes enter the collection.
2. Everything an editor saves is one edit away from being a draft — no lock, no approval, no publish step.
3. Public readers are guests: the read experience has no login wall and nothing about it says "personal admin tool."
4. The database is a library, not a website — content and structure survive any future frontend.
5. Works offline-first: no feature dies just because the LLM key is missing or the internet is down.
