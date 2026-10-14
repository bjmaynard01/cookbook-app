# Cookbook project status — 2026-10-13

This is the current cross-machine handoff. Read it before the older September
`SESSION-HANDOFF` files, which describe completed foundation work. Confirm the
live Git state with `git status --short --branch` and `git log -5 --oneline`;
the branch/commit below are a snapshot, not a substitute for those commands.

## Repository and workflow

- Remote: `https://github.com/bjmaynard01/cookbook-app.git`.
- `main` HEAD: **Task 6.2 merged at `d65dcb3`** (v2 ordered body —
  `recipe_sections` + `recipe_items`, DL-3). Task 6.1 recipes and its
  data-model plan are already on `main` beneath it.
- **Task 6.3 organization is on branch `task/6.3-organization` and is PENDING
  CODEX REVIEW — not merged.** It builds `categories` / `tags` + both join
  tables + the DL-4 eight-category seed, and records DL-7. It has NOT been
  committed yet in this session; the implementer will commit the six scoped
  files only after verification, then stop for Codex review. The owner
  approves merges.
- The owner and project manager make design and merge decisions jointly. A
  fresh-session local model implements on dedicated task branches; Codex
  reviews; the owner approves merges. Do not auto-prompt, push, or merge task
  branches without the owner's explicit direction.

## What exists

- FastAPI foundation with `/healthz` and `/readyz`, configuration including
  file-first secrets, Docker image, dev Compose app and MariaDB, and Alembic.
- v0 `users`, v1 `recipes`, and v2 `recipe_sections` + `recipe_items`
  migrations and ORM models (v2 = Task 6.2, now merged at `d65dcb3`). `Recipe`
  includes `UNIQUE(source_system, source_id)` for importer idempotency.
- Tests for configuration, app health, users, recipes, and the ordered body.
  No recipe CRUD/API, reader UI, authentication, import, or the Task 6.4 /
  6.5 / 6.6 tables yet.
- Approved design spec:
  `docs/superpowers/specs/2026-09-20-cookbook-design.md`. Task 6 plan:
  `docs/superpowers/plans/2026-10-03-data-model.md`.

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
- **Next work:** Task 6.3 organization (on `task/6.3-organization`, pending
  Codex review) implements the `categories` / `tags` + join tables + DL-4 seed
  against the DL-7 rule above. After it is reviewed and merged, proceed to
  Task 6.4 aliases, 6.5 migration audit, and 6.6 chain verification, with
  owner review at each merge gate.
- The alias CHECK and RESTRICT behavior was probed on disposable MariaDB 11
  on 2026-10-11: one-target rows inserted, zero/two-target rows failed the
  CHECK, referenced-target deletes failed, and reassignment allowed the old
  recipe's deletion while retaining the alias. Task 6.4 must still test its
  actual Alembic migration and ORM mapping against isolated MariaDB.

## Moving to desktop or cloud

Clone/fetch the remote and check out the branch under review. Read `README.md`
for local setup. Git carries code and docs, but not `.env`, host-mounted DB
data, media uploads, WordPress exports, or `.venv`. Recreate secrets safely on
each machine; never copy them into a commit or a Qwen prompt. The existing
Compose file is dev-only and requires `/mnt/container/db` and
`/mnt/container/app` on the host. A cloud deployment needs its own deployment
configuration and persistent data/backup plan; none has been approved yet.
