# Desktop handoff — 2026-10-23

## Task 7.7 implementation checkpoint

Owner authorized beginning 7.7. Active branch `task/7.7-bootstrap`, based on
Task 7.6 commit `b2941b5` (pushed). Read `task-7.7-progress.md`. Shared guarded
bootstrap, browser setup, CLI setup/recovery/reset and secret generation are
implemented. Full isolated suite: **366 passed**, existing warning only;
Alembic no drift; wheel and installed CLI entrypoint verified. Both fresh
read-only Qwen reviews complete with no blocking findings. See
task-7.7-qwen-review-findings.md for fixes and optional suggestions.
Owner explicitly confirmed https://ollama.maynardfolks.com/v1 is their trusted
Qwen server and approved bounded source/prompts. Continue fresh sessions for
each tiny subtask/review. Disposable DB removed; development app/db states and
start times unchanged; six old note deletions untouched. Owner approved Task 7.7
commit/push on 2026-10-23. Merge/deployment remain unapproved. Task 7.8
roles/admin lifecycle is next; implementation awaits authorization.

## Task 7.6 checkpoint

- Active branch `task/7.6-lockout`, based on `d4beca8` (Task 7.5 pushed).
  Task 7.6 commit/push approved on 2026-10-23; no merge authorization.
- Added atomic failure counters, canonical username/IP keys, escalating
  10/20/40/60-minute waits, and HTTP 429/Retry-After before password work.
  Successful login resets history in the same transaction as session issuance.
- Full isolated verification: **311 passed**, existing warning only; no
  schema drift. See `task-7.6-progress.md` for final review/cleanup status.
- Fresh Qwen review complete: no blocking findings. Review coverage gap
  addressed; optional DB-fault test deferred. Test DB removed and development
  states/start times unchanged. Ready for owner review and approved integration.
- Fresh Qwen session per subtask: failure state, check/reset helpers,
  session-transaction reset, route integration, and read-only final review.
- Existing six Task 7.4 note deletions are unrelated and remain unstaged.
- Development stack is not rebuilt; port 8000 still serves health-only image.
  Next planned slice is Task 7.7 setup/bootstrap and CLI.

## Task 7.5 checkpoint (historical)

This checkpoint supersedes the earlier Task 7.4 snapshot below. Read
`PROJECT-STATUS.md` before acting.

- Active branch `task/7.5-login-routes`, based on `5bdec30`; Task 7.5 files
  are approved for commit/push. Merging remains subject to owner approval.
- Task 7.4 plus startup fixes are pushed on `task/7.4-session-core` but
  unmerged. `main` remains at `f18d2c6`.
- Implemented CSRF helpers, GET/POST login, POST logout, minimal Jinja2 forms,
  multipart dependency, template package data, and a credential-hash check
  under the session-issuance user lock to reject stale in-flight logins.
- Full isolated suite: **273 passed**, existing warning only. Alembic check
  found no schema drift; v6 head unchanged. Fresh Qwen review identified two
  account-change races, both corrected and regression-tested. The disposable
  database was removed; development container states/start times were unchanged.
- Development stack was not rebuilt: http://127.0.0.1:8000 still serves the
  health-only image. No bootstrap/account creation or cookbook UI exists yet;
  login's `/` success target belongs to a later UI task.
- Next: owner review/integration, then Task 7.6 lockout and Task 7.7 bootstrap.
- **Fresh Qwen session after every tiny subtask.** The combined route session
  was stopped after ~17 minutes of inspection/planning without source output.
  Separate templates, GET, POST-login, and logout prompts succeeded.
  Use `ollama-remote/qwen3.8:27b-128k` through OpenCode.
- Corrected C2 contract overrides stale S5 `htmx.config.headers`: serialized
  `hx-headers`; mandatory hidden field plus browser cookie for pre-auth;
  header precedence/form fallback for session CSRF. Setup belongs to S7.

## Earlier Task 7.4 snapshot (historical)

Read this file and `PROJECT-STATUS.md`, then verify the live checkout before making changes.

## First commands

Run in PowerShell from the repository root:

```powershell
git status --short --branch
git log -5 --oneline
git branch -vv
docker compose ps
```

Expected after the approved handoff commit is pushed: branch `task/7.4-session-core`, clean working tree, current branch pushed to `origin`, and local `db` and `app` services healthy. `main` remains at the Task 7.3 baseline until the owner separately approves merging Task 7.4.

## Project checkpoint

This is a local-first family cookbook using FastAPI, SQLAlchemy, Alembic, and MariaDB. Tasks 6.1–6.6 completed the data model and verification. Authentication plan: `docs/superpowers/plans/2026-10-16-authentication.md`; owner-approved policies are recorded in that plan and summarized in `PROJECT-STATUS.md`.

Tasks 7.1–7.4 are complete:

- 7.1: v6 auth schema (`sessions`, `login_attempts`, `auth_locks`) — `6dfbbeb`.
- 7.2: trusted client identity and cookie-security configuration — `b984e7a`.
- 7.3: Argon2id password service and operational error handling — `f18d2c6`.
- 7.4: session lifecycle and secure-cookie helpers — `aba0eb4`, 220 tests passed.

The next planned implementation slice is Task 7.5: login/logout routes, templates, and browser-bound pre-auth CSRF. There is no user-facing login or cookbook UI yet. The app currently exposes health endpoints only. Do not start manual feature testing until the owner-facing app is available; send access instructions when a usable screen is ready.

## Current local runtime

The Windows Compose stack was smoke-checked on 2026-10-22:

- `db` and `app` healthy.
- `/healthz` returned `200 {"status":"ok",...}`.
- `/readyz` returned `200 {"status":"ready",...}`.
- Alembic reached `e4c7a91d2f30` (v6).

Open http://127.0.0.1:8000/healthz and http://127.0.0.1:8000/readyz to repeat the checks. They are health probes, not application screens.

The current ignored `.env` stores persistent data under `C:/Users/bryanna/AppData/Local/cookbook-app`, outside the synced repository. Never print, paste, or commit `.env`. Compose now uses configurable bind mounts through `COOKBOOK_DATA_DIR`; on Windows, set it to a local unsynced directory and avoid the Linux default `/mnt/container`. The Dockerfile normalizes the entrypoint shell script so CRLF checkouts work in Linux containers.

The failed initializations were preserved under Docker's `/mnt/container` path as `db.init-failed-20261022-1323` and `db.no-space-failed-20261022-1324`. The original path's Docker-side filesystem was only 126 MB and full. The working database is in Local AppData. Do not delete the preserved directories without owner approval.

## Collaboration and Git rules

The owner and Codex decide product direction; Qwen through OpenCode implements small bounded pieces. Use the configured `ollama-remote/qwen3.8:27b-128k` model. Each Qwen prompt must be self-contained and run in a fresh session because it has a 128k context. Provide periodic progress updates and save one concise note under `docs/` after each successful sequence.

Review the implementation and verification before asking the owner to approve integration. No future commits, pushes, or merges without explicit owner approval. The commit/push authorization in the prior session applied to the Task 7.4/startup-handoff changes only.

## Suggested first message for the new Codex session

```text
Continue the cookbook-app project from the current checkout. First read docs/PROJECT-STATUS.md, docs/DESKTOP-HANDOFF.md, and docs/superpowers/plans/2026-10-16-authentication.md, then inspect live Git state. Task 7.4 and the Windows development startup fixes are committed and pushed on task/7.4-session-core; do not merge without my approval. The app runs locally at http://127.0.0.1:8000, but only health endpoints are usable. Next planned slice is Task 7.5 (login/logout routes, templates, browser-bound pre-auth CSRF). Coordinate direction with me, then delegate small, standalone implementation prompts to Qwen through OpenCode, one fresh session per prompt. Send periodic progress updates and save a docs progress note after each successful sequence. Do not commit, push, or merge future work without explicit approval.
```
