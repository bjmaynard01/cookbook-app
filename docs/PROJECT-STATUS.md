# Project status — 2026-11-06

## Repository and task state

- Repository: `https://github.com/bjmaynard01/cookbook-app`.
- Work on the permanent `dev` branch and push to `origin/dev`; `main` is the stable integration branch. Merge tested milestones from `dev` into `main`.
- Tasks 1–18 are complete and integrated on `main`; Task 19 is implemented on `dev` and fixture-verified. The production WordPress export is not present in this workspace, so the corpus import and parity verification remain pending.
- The full v1 task list and current statuses are in [`BUILD-ROADMAP.md`](BUILD-ROADMAP.md).
- Check live Git state before acting; this file is a maintained snapshot, not a replacement for Git.

## What the app can do

The backend foundation includes the recipe data model, browser and CLI owner setup, Argon2id passwords, database-backed sessions, login/logout, lockout, role checks, guarded admin account operations, CSRF protection, and private response cache policy.

The shared `RecipeDraft` contract and save-readiness validation preserve ordered sections and source ingredient text. The editor dashboard at `/edit` supports text, category, and tag filters and sorting. Editors can create, edit, and delete recipes using the shared server-rendered form. Recipe aliases protect existing URLs from deletion, and private editor responses use `private, no-store`.

Editors can upload, replace, and remove JPEG, PNG, or WebP recipe images. Pillow validates image contents; originals are stored under generated names on `MEDIA_DIR`, safely served from `/media/`, and removed after the database update commits. Uploads default to a 10 MiB limit.

Recipe source URLs are fetched only over HTTP(S) after rejecting non-public DNS answers; connections use a validated, pinned IP and each of up to five redirects is checked again. Fetches have a 20-second deadline and 5 MiB compressed and decoded body caps. HTML extraction returns page metadata, visible text, and raw JSON-LD blocks. Deterministic parsing checks all JSON-LD blocks first, selects the richest supported Recipe candidate, then falls back to schema.org microdata. It preserves ingredient source text and emits sparse drafts with conservative best-effort splits and parse notes.

The optional LLM fallback runs only when deterministic parsing has no ingredient or step content, or its ingredient data is obviously incomplete. It sends at most 24,000 characters of cleaned visible text to the configured OpenAI-compatible chat-completions endpoint at temperature zero, validates the response against `RecipeDraft`, and stamps source provenance locally. Missing or unreadable keys, provider failures, invalid output, and timeouts retain the deterministic draft and add a parse note. Task 15 connects the async parser to the editor's ingest/review flow.

The public app now serves recipe pages, category/tag browsing, and filtered search. Task 19 adds `cookbook import-wp`: a bounded WXR parser, dry-run JSON report, title-based slug collision handling, category hierarchy mapping, safe original-image fetch/storage, legacy aliases, per-recipe audit, resume, image retry, and `--verify`. Fixture tests cover published/draft handling, line-level plain-text parsing, category/page aliases, slug collisions, dry-run no-write behavior, import/resume idempotency, image failure isolation and retry, and the verification gate. The 2026-11-06 export contained 391 published posts, eight category pages, 182 images, and 12 deterministic slug suffixes. All 391 posts were imported with no recipe errors; 181 original images are stored. The owner approved posts 210, 331, 342, and 870 as audited exceptions without ingredient lines. One image remains unavailable because its redirect fails public-address validation. The discovery-identified “Pinto Bean Recipes” post remains included as a flagged collection record.

Task 19 verification passed **573 tests** against a fresh disposable tmpfs MariaDB, `alembic check` found no schema drift, and the refreshed Docker app is healthy. The real import run `96797932-43a5-47af-9455-cb7e20b637d9` imported 391 recipes; resume run `ea253eff-9a07-4b64-abb1-4ecb75847c60` skipped all 391. Final verification passed all integrity checks. The owner accepted post 1957's unavailable image as an exception; that approval and the rejected unsafe redirect are recorded in the recipe log and migration-run notes. One existing Starlette/httpx deprecation warning remains. No schema migration was added.

## Remaining product work

Task 20, backup/restore and cutover verification, is next. The real WXR corpus is imported and verified, and the Task 18 tokenizer benchmark is recorded in its plan. The single missing original image is an owner-accepted exception recorded in the migration audit. Production cutover still requires the isolated restore and migration verification gate. See the roadmap for task boundaries and dependencies.

## Local Windows development data

The ignored `.env` points `COOKBOOK_DATA_DIR` to `C:/Users/bryanna/AppData/Local/cookbook-app`, outside the synced repository. Do not copy or commit `.env`. On Windows, do not use the Linux default `/mnt/container` from PowerShell: its Docker-side filesystem was previously found to be only 126 MB and full. These partial initialization directories are preserved and must not be deleted without checking with the owner:

- `/mnt/container/db.init-failed-20261022-1323`
- `/mnt/container/db.no-space-failed-20261022-1324`

The working database was the instance under Local AppData at the last inspection.

## Working agreements

- Use a fresh OpenCode/Qwen session for each bounded subtask when useful. The owner approved bounded source and prompt transfer to `https://ollama.maynardfolks.com/v1` using `ollama-remote/qwen3.8:27b-128k`.
- Do not create progress, handoff, or checkpoint notes; flag meaningful decision points.
- Keep database verification isolated and do not test or migrate against the running development database. Never read or commit `.env`.

See `README.md` for setup.
