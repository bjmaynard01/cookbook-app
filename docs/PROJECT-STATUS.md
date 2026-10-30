# Project status — 2026-10-30

## Repository and task state

- Repository: `https://github.com/bjmaynard01/cookbook-app`.
- Work on the permanent `dev` branch and push to `origin/dev`; `main` is the stable integration branch. Merge tested milestones from `dev` into `main`.
- Task 10 (editor dashboard and recipe CRUD) is implemented on `main`. New work proceeds from `dev`.
- The full v1 task list and current statuses are in [`BUILD-ROADMAP.md`](BUILD-ROADMAP.md).
- Check live Git state before acting; this file is a maintained snapshot, not a replacement for Git.

## What the app can do

The backend foundation includes the recipe data model, browser and CLI owner setup, Argon2id passwords, database-backed sessions, login/logout, lockout, role checks, guarded admin account operations, CSRF protection, and private response cache policy.

The shared `RecipeDraft` contract and save-readiness validation preserve ordered sections and source ingredient text. The editor dashboard at `/edit` supports text, category, and tag filters and sorting. Editors can create, edit, and delete recipes using the shared server-rendered form. Recipe aliases protect existing URLs from deletion, and private editor responses use `private, no-store`.

Task 10 verification passed **456 tests** against disposable MariaDB, plus Python compilation and whitespace checks. One existing Starlette/httpx deprecation warning remains. No schema migration was added for Task 10.

## Remaining product work

Recipe image storage is the next task, followed by safe URL ingestion, deterministic parsing, optional LLM fallback, ingest review/save, public recipe pages, browse/search, WordPress migration, and backup/restore/cutover verification. See the roadmap for task boundaries and dependencies.

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
