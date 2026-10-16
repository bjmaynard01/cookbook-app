# Cookbook project status — 2026-10-16

This is the current cross-machine handoff. Read it before the older September
`SESSION-HANDOFF` files, which describe completed foundation work. Confirm the
live Git state with `git status --short --branch` and `git log -5 --oneline`;
the branch/commit below are a snapshot, not a substitute for those commands.

## Repository and workflow

- Remote: `https://github.com/bjmaynard01/cookbook-app.git`.
- `main` HEAD: the data-model chain is complete — Task 6.1 recipes,
  6.2 ordered body (`recipe_sections` + `recipe_items`, DL-3), 6.3
  organization, 6.4 `recipe_url_aliases`, and the v5 migration-audit + full
  chain-verification probes are all on `main` (see `git log --oneline`;
  snapshot `57d252d`, migration head `b7a2f9c4d1e8`). Confirm live Git state
  with `git log -5 --oneline` before acting. The data-model plan is **closed**.
- Next plan drafted (2026-10-16): **authentication** —
  `docs/superpowers/plans/2026-10-16-authentication.md`. It is now a **revised
  draft (v2)** after Codex review of the first draft (commit `f96eea1`):
  concrete contracts C1–C6 (lockout schema + state machine; CSRF incl.
  pre-auth tokens; cookie/HTTP-dev rules; trusted-proxy identity; concurrency
  invariants with an `auth_locks` lock-row table; bootstrap/CLI/config),
  nine implementation slices S1–S9, exact regression-inventory updates, and a
  flagged-proposals/owner-decisions table (O-a…O-e). It has **not** been
  owner-approved. No auth code exists yet: no `sessions` /
  `login_attempts` / `auth_locks` tables, no auth routes, no CLI entry point.
  It is the "auth plan" the data-model plan deferred.
- The owner and project manager make design and merge decisions jointly. A
  fresh-session local model implements on dedicated task branches; Codex
  reviews; the owner approves merges. Do not auto-prompt, push, or merge task
  branches without the owner's explicit direction.

## What exists

- FastAPI foundation with `/healthz` and `/readyz`, configuration including
  file-first secrets, Docker image, dev Compose app and MariaDB, and Alembic.
- v0–v5 migrations and matching ORM models: `users`, `recipes`, ordered body
  (`recipe_sections` + `recipe_items`), organization (`categories`/`tags` +
  joins, DL-4 seed), `recipe_url_aliases` (exactly-one-target CHECK), and the
  v5 migration-audit tables. `Recipe` includes
  `UNIQUE(source_system, source_id)` for importer idempotency. Migration head
  is `b7a2f9c4d1e8` (v5).
- Tests for configuration, app health, users, recipes, the ordered body,
  organization, aliases, and the full schema/cascade/seed probe set.
  No recipe CRUD/API, reader UI, search, authentication, or import yet.
- Approved design spec:
  `docs/superpowers/specs/2026-09-20-cookbook-design.md`. Plans:
  `docs/superpowers/plans/2026-10-03-data-model.md` (complete) and
  `docs/superpowers/plans/2026-10-16-authentication.md` (revised draft v2,
  pending owner approval).

## Decisions and next work

- DL-6 (owner-approved 2026-10-10): an alias always retains exactly one valid
  recipe or category target. Both alias target FKs use `ON DELETE RESTRICT`;
  reassign aliases before deleting a target. An in-use category is protected
  by `recipe_categories.category_id ON DELETE RESTRICT`; memberships must be
  reassigned or deliberately removed first. Future admin/API flows must
  explain blocked deletions.
- DL-7 (owner-approved 2026-10-13): tag deletion is **allowed** and removes
  only its recipe memberships. `recipe_tags.tag_id` uses `ON DELETE CASCADE`;
  the recipes, their body, their category memberships, and every other
  tag/membership survive. A tag rename is a plain `slug`/`name` UPDATE — the
  immutable `id` PK and every `recipe_tags` row stay intact. Category and
  alias `RESTRICT` policies are unchanged (DL-6). **This resolves O6** and
  overrides the earlier draft `ON DELETE RESTRICT` rule for
  `recipe_tags.tag_id`.
- O6 is now **resolved** by DL-7 (was: "decide whether deleting an in-use tag
  is blocked or drops its recipe memberships").
- **Next work:** the data-model plan (Tasks 6.1–6.6) is complete and on
  `main`. The next active plan is **authentication**
  (`docs/superpowers/plans/2026-10-16-authentication.md`), drafted 2026-10-16
  and revised (v2) after the first draft's Codex review. It awaits owner
  approval of the flagged proposals (O-a lockout backoff base/reset, O-b
  pre-auth CSRF lifetime, O-c `__Host-` cookie name vs http-dev, O-d
  `--force-owner-recreation` scope, O-e locked-login status code) before any
  slice is implemented; the draft's recommendations are not approvals.
  Implementation runs on task branches; owner approves each merge.
- The alias CHECK and RESTRICT behavior was probed on disposable MariaDB 11
  on 2026-10-11: one-target rows inserted, zero/two-target rows failed the
  CHECK, referenced-target deletes failed, and reassignment allowed the old
  recipe's deletion while retaining the alias. Task 6.4 shipped with the
  migration + ORM mapping tested against isolated MariaDB.

## Moving to desktop or cloud

Clone/fetch the remote and check out the branch under review. Read `README.md`
for local setup. Git carries code and docs, but not `.env`, host-mounted DB
data, media uploads, WordPress exports, or `.venv`. Recreate secrets safely on
each machine; never copy them into a commit or a Qwen prompt. The existing
Compose file is dev-only and requires `/mnt/container/db` and
`/mnt/container/app` on the host. A cloud deployment needs its own deployment
configuration and persistent data/backup plan; none has been approved yet.
