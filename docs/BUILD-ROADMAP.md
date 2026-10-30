# Cookbook App Build Roadmap

**Source of truth:** `PRODUCT.md` and the approved design in `docs/superpowers/specs/2026-09-20-cookbook-design.md`.
**Updated:** 2026-10-30
**Planned scope:** 20 delivery tasks. Task boundaries can be revised if the owner changes product scope; this count covers the current v1 specification.

This roadmap gives the project one ordered task sheet. Each task may have a separate design and implementation plan. Tasks 1–9 are complete; Task 10 is active. No task adds features explicitly excluded from the approved v1 spec.

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
| 10 | Editor dashboard and recipe CRUD | Active | Authenticated server-rendered recipe listing and manual create/edit/delete forms. [Plan](superpowers/plans/2026-10-30-editor-crud.md) |
| 11 | Recipe image storage | Planned | Local media paths, upload/download validation, and non-blocking image persistence. |
| 12 | Safe URL fetch and page extraction | Planned | Public-URL validation, SSRF defenses, bounded HTTP fetches, and HTML extraction. |
| 13 | Deterministic recipe parsing | Planned | JSON-LD and microdata extraction into the shared `RecipeDraft`. |
| 14 | Optional LLM parsing fallback | Planned | Configurable OpenAI-compatible fallback with bounded time and no-key behavior. |
| 15 | Ingest review and save flow | Planned | Dashboard URL launcher, distinct ingest states, and landing in the same recipe form. |
| 16 | Public recipe page and legacy redirects | Planned | Readable `/r/<slug>` pages and alias redirects with preserved old links. |
| 17 | Home, category, and tag browsing | Planned | Public navigation and recipe lists across the specified browse surfaces. |
| 18 | Search and result filtering | Planned | Full-text search, ranking, time/tag filters, and useful empty states. |
| 19 | WordPress migration importer | Planned | Discovery spike, idempotent WXR import, media transfer, aliases, audit, and verification report. |
| 20 | Backup, restore, and production cutover gate | Planned | Explicit-target backup/restore tooling and the required isolated restore and migration verification. |

## Order and dependencies

Tasks 1–9 establish the schema, security, draft contract, and persistence layer. Task 10 uses those services to create the first editor workflow. Task 11 supplies image storage to both manual editing and later ingestion/import. Tasks 12–15 build ingestion in bounded stages. Tasks 16–18 implement the public reading and discovery experience. Task 19 migrates the existing collection only after its discovery spike. Task 20 verifies restore and cutover requirements after the product surfaces and importer are complete.

## Completion gates

- Keep database tests on disposable MariaDB. Do not point tests or migrations at the running development database while work is unmerged.
- Keep public reading routes anonymous and all editor mutations behind the existing editor role and CSRF checks.
- Preserve ingredient source text, recipe provenance, category/tag meanings, and WordPress URL/image parity as required by the design spec.
- Run the full test suite for integration milestones. Before first production cutover, pass the importer verification and a restore drill into an isolated environment.
