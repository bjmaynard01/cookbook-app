# Project status — 2026-10-22

## Current checkpoint

- Repository: `https://github.com/bjmaynard01/cookbook-app`.
- `main` and local `origin/main`: `f18d2c6` (Task 7.3 password-service correction).
- Active task branch: `task/7.5-login-routes`, based on `5bdec30` (Task 7.4 plus Windows runtime fixes). Owner approved committing and pushing Task 7.5. Neither Task 7.4 nor Task 7.5 is merged to `main`.
- Check live Git state before acting; this status file is a maintained snapshot, not a replacement for Git.
- Alembic head: `e4c7a91d2f30` (v6 authentication schema).

## What the app can do today

The backend foundation and full Task 6 data model are in place. Authentication includes trusted client identity, Argon2id passwords, database-backed sessions, cookie helpers, and now uncommitted login/logout routes with browser-bound pre-auth CSRF and minimal login templates. There is no bootstrap/account-creation screen, cookbook UI, recipe CRUD, search, or import yet. The running Docker development image still exposes only health endpoints; Task 7.5 has been verified in-process against isolated MariaDB, not deployed to that stack.

The local Windows Docker Compose stack was healthy on 2026-10-22. Task 7.5 full isolated verification passed **273 tests**, with the existing Starlette/httpx deprecation warning. Alembic found no new upgrade operations; head remains v6. Fresh Qwen review identified two account-change races; both were corrected and regression-tested. The disposable database was removed; development container states and start times were unchanged.

## Completed work

- Task 6.1–6.6: data-model plan and migrations, including recipes, ordered recipe body, categories/tags, URL aliases, migration audit, and full-chain inventory/cascade verification. On `main` at `57d252d` for the final Task 6 verification.
- Authentication plan: `docs/superpowers/plans/2026-10-16-authentication.md`. Owner approved O-a through O-f on 2026-10-16.
- Task 7.1: v6 auth schema — `sessions`, `login_attempts`, `auth_locks` — commit `6dfbbeb`.
- Task 7.2: trusted client identity and cookie-security configuration — `b984e7a`.
- Task 7.3: Argon2id password service and operational-error handling — final commit `f18d2c6`.
- Task 7.4: session create/load/revoke lifecycle and cookie helpers — `aba0eb4`; 220 tests passed.
- Windows local runtime support: Compose data path is configurable with `COOKBOOK_DATA_DIR`; the Docker image normalizes the entrypoint shell script to LF so Windows checkouts start correctly.
- Task 7.5 (uncommitted): CSRF helpers, GET/POST login, POST logout, Jinja2 templates and dependencies. Session issuance checks the verified credential hash under the user lock to refuse stale logins after account changes.

## Approved auth decisions

- Password minimum: 8 characters, with no complexity rules.
- Lockout: five failures in ten minutes, escalating 10/20/40/60-minute waits; successful login resets lockout state.
- Pre-auth CSRF: browser-bound signed token, 15-minute lifetime; replay is allowed in the same browser.
- Cookies: `__Host-` name with Secure; plain HTTP development uses the non-`__Host-` cookie name.
- Local owner recovery can restore a named existing account; it cannot create another owner.
- Locked login returns 429 with `Retry-After`.

Use the approved plan as the detailed source of truth for all implementation contracts.

## Next work

Finish owner review/integration of Task 7.5 before the next slice. Task 7.6 is lockout service and login integration; Task 7.7 adds setup/bootstrap and CLI. Login redirects to `/` per contract, but the root cookbook UI is a later task. Use a fresh Qwen session for each tiny subtask; templates, GET login, POST login, and logout worked better as separate prompts than a combined route prompt. Notify the owner with access instructions when a usable app is available.

## Local Windows development data

The current machine's ignored `.env` points `COOKBOOK_DATA_DIR` to `C:/Users/bryanna/AppData/Local/cookbook-app`, outside the Nextcloud-synced repository. Do not copy or commit `.env`. On Windows, do not use the Linux default `/mnt/container` from PowerShell: inspection found its Docker-side filesystem was only 126 MB and full. Two partial initialization directories were preserved there:

- `/mnt/container/db.init-failed-20261022-1323`
- `/mnt/container/db.no-space-failed-20261022-1324`

The working database is the fresh instance under Local AppData. Do not delete the preserved partial directories without checking with the owner.

## Working agreement

The owner and Codex decide product direction. Qwen, launched through OpenCode using the owner's configured `ollama-remote/qwen3.8:27b-128k` endpoint, implements small scoped sequences; every prompt must stand alone because each sequence uses a fresh session. Provide periodic progress reports and save a concise progress/handoff note in `docs/` after each successful sequence. Do not commit, push, or merge future work without explicit owner approval.

See [README.md](../README.md) for setup and the [authentication plan](superpowers/plans/2026-10-16-authentication.md) for implementation details.
