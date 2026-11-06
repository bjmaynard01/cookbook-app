# Cookbook App Build Roadmap

**Source of truth:** `PRODUCT.md` and the approved design in `docs/superpowers/specs/2026-09-20-cookbook-design.md`.
**Updated:** 2026-11-06
**Planned scope:** 20 delivery tasks. Task boundaries can be revised if the owner changes product scope; this count covers the current v1 specification.

This roadmap gives the project one ordered task sheet. Each task may have a separate design and implementation plan. Tasks 1–19 are complete. Task 19's real corpus has been imported and verified; the owner accepted the one unavailable original image as a logged exception after the safe fetcher rejected its redirect. No task adds features explicitly excluded from the approved v1 spec.

## Branch workflow

- `dev` is the permanent development branch. Implement and push task work there.
- `main` is the stable integration branch. Merge tested task milestones from `dev` into `main`.
- Do not create one-off task branches or develop directly on `main`.

| Task | Name | Status | Main outcome |
|---|---|---|---|
| 1 | Repository foundation | Complete | Project scaffold, packaging, and development baseline. |
| 2 | Runtime configuration and secrets | Complete | Validated settings and file-first secret handling. |
| 3 | Health and readiness | Complete | `/healthz` and `/readyz` with DB and media checks. |
| 4 | Core model baseline | Complete | Stable application and model shape. |
| 5 | Container and database foundation | Complete | Reproducible Docker development stack and migration boot. |
| 6 | Recipe data model and migration audit | Complete | Recipes, ordered body, categories, tags, aliases, and migration audit schema. |
| 7 | Authentication and account security | Complete | Setup, sessions, login, lockout, roles, CSRF, and admin lifecycle. |
| 8 | Shared recipe draft and validation | Complete | One draft contract and save-readiness validation. |
| 9 | Recipe persistence services | Complete | Slugs plus caller-managed create, read, update, rename, and delete services. |
| 10 | Editor dashboard and recipe CRUD | Complete | Authenticated server-rendered recipe listing and manual create/edit/delete forms. [Plan](superpowers/plans/2026-10-30-editor-crud.md) |
| 11 | Recipe image storage | Complete | Validated local JPEG/PNG/WebP uploads, safe serving, replacement/removal, and post-commit cleanup. [Design](superpowers/specs/2026-10-30-recipe-image-storage-design.md) |
| 12 | Safe URL fetch and page extraction | Complete | Public-URL validation, DNS-pinned SSRF defenses, bounded HTTP fetches, and HTML/JSON-LD extraction. [Plan](superpowers/plans/2026-10-31-safe-url-fetch.md) |
| 13 | Deterministic recipe parsing | Complete | JSON-LD candidate selection and microdata fallback into the shared `RecipeDraft`. [Plan](superpowers/plans/2026-10-31-deterministic-parsing.md) |
| 14 | Optional LLM parsing fallback | Complete | Optional OpenAI-compatible fallback with strict `RecipeDraft` validation, a 10-second cap, and safe no-key behavior. [Plan](superpowers/plans/2026-11-02-llm-parsing-fallback.md) |
| 15 | Ingest review and save flow | Complete | Dashboard URL launcher, four review states in the shared form, provenance, duplicate notice, bounded image save, and a four-job ingest pool. [Plan](superpowers/plans/2026-11-05-ingest-review-save.md) |
| 16 | Public recipe page and legacy redirects | Complete | Anonymous responsive `/r/<slug>` reading pages and exact stored-alias 301 redirects. [Plan](superpowers/plans/2026-11-05-public-recipe-page.md) |
| 17 | Home, category, and tag browsing | Complete | Featured home index, ordered category/tag lanes, and public recipe lists with category time/tag narrowing. [Plan](superpowers/plans/2026-11-05-public-browsing.md) |
| 18 | Search and result filtering | Complete | MariaDB FULLTEXT search with title/semantic/body ranking, partial matching, category/time/tag filters, and scoped zero-result suggestions. The real-corpus tokenizer benchmark is recorded in the [plan](superpowers/plans/2026-11-05-search-filtering.md). |
| 19 | WordPress migration importer | Complete; one accepted image exception | 391 published posts, category pages, aliases, and 181 validated original images imported. Four ingredient-less posts are recorded as owner-approved exceptions; post 1957's unsafe image redirect is recorded as an owner-accepted exception. [Plan](superpowers/plans/2026-11-06-wordpress-importer.md) |
| 20 | Backup, restore, and production cutover gate | Planned | Explicit-target backup/restore tooling and the required isolated restore and migration verification. |

## Order and dependencies

Tasks 1–9 establish the schema, security, draft contract, and persistence layer. Task 10 uses those services to create the first editor workflow. Task 11 supplies image storage to both manual editing and later ingestion/import. Tasks 12–15 build ingestion in bounded stages. Task 12 safely fetches public HTML and extracts metadata, visible text, and raw JSON-LD blocks; Task 13 interprets recipe JSON-LD and microdata; Task 14 adds the optional 10-second LLM fallback; and Task 15 connects ingestion to the review and save form. Tasks 16–18 implement the public reading and discovery experience. Task 19 migrated and verified the current WXR collection. Task 20 verifies restore and cutover requirements; production cutover waits for the backup/restore gate.

## Completion gates

- Keep database tests on disposable MariaDB. Do not point tests or migrations at the running development database while work is unmerged.
- Keep public reading routes anonymous and all editor mutations behind the existing editor role and CSRF checks.
- Preserve ingredient source text, recipe provenance, category/tag meanings, and WordPress URL/image parity as required by the design spec.
- Run the full test suite for integration milestones. Before first production cutover, pass the importer verification and a restore drill into an isolated environment.
