# Project status — 2026-10-31

## Repository and task state

- Repository: `https://github.com/bjmaynard01/cookbook-app`.
- Work on the permanent `dev` branch and push to `origin/dev`; `main` is the stable integration branch. Merge tested milestones from `dev` into `main`.
- Tasks 10–13 are implemented and integrated on `main`; new work proceeds on `dev`.
- The full v1 task list and current statuses are in [`BUILD-ROADMAP.md`](BUILD-ROADMAP.md).
- Check live Git state before acting; this file is a maintained snapshot, not a replacement for Git.

## What the app can do

The backend foundation includes the recipe data model, browser and CLI owner setup, Argon2id passwords, database-backed sessions, login/logout, lockout, role checks, guarded admin account operations, CSRF protection, and private response cache policy.

The shared `RecipeDraft` contract and save-readiness validation preserve ordered sections and source ingredient text. The editor dashboard at `/edit` supports text, category, and tag filters and sorting. Editors can create, edit, and delete recipes using the shared server-rendered form. Recipe aliases protect existing URLs from deletion, and private editor responses use `private, no-store`.

Editors can upload, replace, and remove JPEG, PNG, or WebP recipe images. Pillow validates image contents; originals are stored under generated names on `MEDIA_DIR`, safely served from `/media/`, and removed after the database update commits. Uploads default to a 10 MiB limit.

Recipe source URLs are fetched only over HTTP(S) after rejecting non-public DNS answers; connections use a validated, pinned IP and each of up to five redirects is checked again. Fetches have a 20-second deadline and 5 MiB compressed and decoded body caps. HTML extraction returns page metadata, visible text, and raw JSON-LD blocks. Deterministic parsing checks all JSON-LD blocks first, selects the richest supported Recipe candidate, then falls back to schema.org microdata. It preserves ingredient source text and emits sparse drafts with conservative best-effort splits and parse notes.

Task 13 verification passed **523 tests** against disposable tmpfs MariaDB, and the Docker image built successfully. The focused parser suite passed **19 tests**. One existing Starlette/httpx deprecation warning remains. No schema migration was added for Tasks 10–13.

## Remaining product work

Optional LLM parsing fallback is next, followed by ingest review/save, public recipe pages, browse/search, WordPress migration, and backup/restore/cutover verification. See the roadmap for task boundaries and dependencies.

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
