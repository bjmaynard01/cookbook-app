# Cookbook project status — 2026-10-11

This is the current cross-machine handoff. Read it before the older September
`SESSION-HANDOFF` files, which describe completed foundation work. Confirm the
live Git state with `git status --short --branch` and `git log -5 --oneline`;
the branch/commit below are a snapshot, not a substitute for those commands.

## Repository and workflow

- Remote: `https://github.com/bjmaynard01/cookbook-app.git`.
- Last merged `main` baseline at the start of this documentation branch:
  `a4e9d28` (Task 6.1 recipes plus the data-model plan). Its migration applied
  on an isolated MariaDB 11, `alembic check` found no drift, and the full suite
  passed 16 tests on 2026-10-10. That temporary database was stopped.
- Working branch: `task/6-plan-decisions` (created from `a4e9d28`). It records
  the approved deletion rules and the current handoff. Check whether it has
  been merged before creating the next implementation branch.
- The owner and project manager make design and merge decisions jointly. A
  fresh-session local Qwen model does implementation work on dedicated task
  branches. Do not auto-prompt Qwen or merge task branches without the owner's
  explicit direction. Each Qwen prompt must include repository context because
  sessions do not share memory.

## What exists

- FastAPI foundation with `/healthz` and `/readyz`, configuration including
  file-first secrets, Docker image, dev Compose app and MariaDB, and Alembic.
- v0 `users` and v1 `recipes` migrations and ORM models. `Recipe` includes
  `UNIQUE(source_system, source_id)` for importer idempotency.
- Tests for configuration, app health, users, and recipes. No recipe CRUD/API,
  reader UI, authentication, import, or remaining Task 6 tables yet.
- Approved design spec:
  `docs/superpowers/specs/2026-09-20-cookbook-design.md`. Task 6 plan:
  `docs/superpowers/plans/2026-10-03-data-model.md`.

## Decisions and next work

- DL-6 (owner-approved 2026-10-10): an alias always retains exactly one valid
  recipe or category target. Both alias target FKs use `ON DELETE RESTRICT`;
  reassign aliases before deleting a target. An in-use category is protected
  by `recipe_categories.category_id ON DELETE RESTRICT`; memberships must be
  reassigned or deliberately removed first. Future admin/API flows must
  explain blocked deletions. This approval does not decide tag deletion.
- O6 remains open: decide whether deleting an in-use tag is blocked or drops
  its recipe memberships before Task 6.3 creates `recipe_tags`.
- Next implementation slice after this documentation branch is reviewed and
  merged: `task/6.2-recipe-body` from updated `main`, using the plan's ordered
  `recipe_sections` and `recipe_items` work. Then proceed through Task 6.3
  organization, 6.4 aliases, 6.5 migration audit, and 6.6 chain verification,
  with owner review at each merge gate.
- The data-model plan's executable MariaDB examples for future tables still
  require validation during their implementation slices. In particular, test
  the alias CHECK and RESTRICT behavior against isolated MariaDB 11 before
  creating the 6.4 migration.

## Moving to desktop or cloud

Clone/fetch the remote and check out the branch under review. Read `README.md`
for local setup. Git carries code and docs, but not `.env`, host-mounted DB
data, media uploads, WordPress exports, or `.venv`. Recreate secrets safely on
each machine; never copy them into a commit or a Qwen prompt. The existing
Compose file is dev-only and requires `/mnt/container/db` and
`/mnt/container/app` on the host. A cloud deployment needs its own deployment
configuration and persistent data/backup plan; none has been approved yet.
