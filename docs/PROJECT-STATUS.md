# Project status — 2026-10-30

## Current repository state

- Repository: `https://github.com/bjmaynard01/cookbook-app`.
- `main` and `origin/main` are both at `cfb19b8` (`feat(recipes): add shared draft and save validation`).
- `main` is the only local and remote branch; Tasks 7.4–7.9 and Task 8 are consolidated there.
- Alembic head is `e4c7a91d2f30` (v6 authentication schema).
- Check live Git state before acting; this file is a maintained snapshot, not a replacement for Git.

## What the app can do

The backend foundation includes the Task 6 recipe data model, browser and CLI owner setup, Argon2id passwords, database-backed sessions, login/logout, login lockout, role checks, guarded admin account operations, CSRF protection on unsafe requests, and cache policy for private responses.

Task 8 adds the shared Pydantic recipe draft and pure save-readiness validation. Partial drafts are valid; saving requires a nonblank title, at least one category, and at least one nonblank ingredient. Missing steps produce a warning. Original nonblank line text and provenance are preserved, and total time is resolved from explicit or complete prep/cook values.

Task 8 verification: **23 focused tests passed**. The last full isolated suite, during Task 7.9, passed **401 tests** with the existing Starlette/httpx deprecation warning. The full suite has not been rerun after Task 8. No database migration was required for Task 8.

There is no recipe CRUD service or UI, recipe ingest, image upload pipeline, public cookbook interface, search, or WordPress importer yet. The development app has not been updated as part of Tasks 7.4–8.

## Completed work

- Tasks 6.1–6.6: recipe schema, ordered body sections/items, categories and tags, aliases, migration audit, and full-chain schema verification.
- Tasks 7.1–7.9: authentication schema and services, trusted client identity, password handling, sessions, login/logout, lockout, first-owner setup/recovery, roles/admin lifecycle, global CSRF and cache hardening.
- Task 8: shared `RecipeDraft` contract and save-readiness validation. Plan: `docs/superpowers/plans/2026-10-30-recipe-draft.md`.

Detailed product requirements and accepted decisions are in `docs/superpowers/specs/2026-09-20-cookbook-design.md`. Authentication contracts are in `docs/superpowers/plans/2026-10-16-authentication.md`.

## Next work

Task 9 is recipe CRUD services, slug generation, and rename handling. Create and review a detailed plan before implementation. The owner resolved the slug rule: generate a slug from the title on initial creation, then allow editing it; collision suffixing and recording old-slug aliases are part of Task 9. The shared draft validator from Task 8 is the input contract.

## Approved authentication decisions

- Password minimum: 8 characters, with no complexity rules.
- Lockout: five failures in ten minutes, escalating 10/20/40/60-minute waits; successful login resets lockout state.
- Pre-auth CSRF: browser-bound signed token, 15-minute lifetime; replay is allowed in the same browser.
- Cookies: `__Host-` name with Secure; plain HTTP development uses the non-`__Host-` cookie name.
- Local owner recovery restores a named existing account; it cannot create another owner.
- Locked login returns 429 with `Retry-After`.

## Local Windows development data

The ignored `.env` points `COOKBOOK_DATA_DIR` to `C:/Users/bryanna/AppData/Local/cookbook-app`, outside the synced repository. Do not copy or commit `.env`. On Windows, do not use the Linux default `/mnt/container` from PowerShell: its Docker-side filesystem was previously found to be only 126 MB and full. These partial initialization directories are preserved and must not be deleted without checking with the owner:

- `/mnt/container/db.init-failed-20261022-1323`
- `/mnt/container/db.no-space-failed-20261022-1324`

The working database was the instance under Local AppData at the last inspection.

## Working agreements

The owner and Codex decide product direction. Use a fresh OpenCode/Qwen session for each small implementation subtask; each prompt must stand alone. The owner approved bounded source and prompt transfer to `https://ollama.maynardfolks.com/v1` using `ollama-remote/qwen3.8:27b-128k`. Do not create progress, handoff, or checkpoint notes; flag meaningful decision points. Keep database verification isolated and do not advance the running development database while a task is unmerged. Commit and push only when the owner authorizes it.

See `README.md` for setup.
