# Desktop handoff — 2026-10-12

This file is a standalone starting point for a new project-management or Qwen
session on another machine. Check live Git state first; commit IDs below record
the state at handoff time.

## Get the work on the desktop

```bash
git clone https://github.com/bjmaynard01/cookbook-app.git
cd cookbook-app
git switch --track origin/task/6-plan-decisions
git status --short --branch
git log -3 --oneline
```

At handoff, `task/6-plan-decisions` is clean and pushed at `be73a80`.
`main` is at `a4e9d28`. The decision branch is **not merged**. If the remote has
moved by the time you clone, inspect the new commits before acting on this
snapshot. The older `task/6.1-recipes` and `task/6-data-model-plan` branches
are already merged into `main`; they are history, not new work to merge.

## Project and working agreement

The application is a local-first family cookbook using FastAPI, SQLAlchemy,
Alembic, and MariaDB. The owner and Codex jointly manage design decisions,
documentation, review, and merges. A smaller local Qwen model does coding on
one dedicated Git branch per task. Qwen is prompted manually by the owner;
there is no automatic prompting. Every Qwen prompt must stand alone because
each session starts without prior context. Merge to `main` only with the
owner's explicit approval.

The merged code currently provides configuration and file-first secrets,
Docker dev app/database, health/readiness endpoints, the `users` and `recipes`
tables, and their tests. There is no recipe CRUD/API, reader UI,
authentication, importer, or remaining Task 6 schema yet. The dev app and db
containers were healthy on the laptop at the last check, but their local
runtime state does not travel through Git.

## Decisions and review gate

The owner approved DL-6: `recipe_categories.category_id` and both
`recipe_url_aliases` target FKs use `ON DELETE RESTRICT`. An in-use category
cannot be deleted until memberships are deliberately reassigned or removed.
An alias must always retain exactly one valid recipe or category target; an
admin reassigns it before deleting its old target. Future admin/API flows must
explain these blocks. The [design spec](superpowers/specs/2026-09-20-cookbook-design.md)
and [Task 6 plan](superpowers/plans/2026-10-03-data-model.md) record the rule.

The decision branch updates those documents, fixes the alias CHECK example,
and corrects the isolated MariaDB test commands. A disposable MariaDB 11
probe verified the CHECK's one-target behavior, both RESTRICT blocks, and
reassignment followed by deletion. The actual future alias migration and ORM
mapping still need their own tests. `git diff --check main...task/6-plan-decisions`
was clean at handoff. The last full code-suite run, on the merged `main` tree
before these documentation-only commits, passed 16 tests and `alembic check`
found no drift against an isolated MariaDB 11 database.

**Next gate:** review `task/6-plan-decisions` and obtain the owner's merge
direction. Once merged, cut `task/6.2-recipe-body` from updated `main` for
ordered `recipe_sections` and `recipe_items`. The remaining sequence is 6.3
organization, 6.4 aliases, 6.5 migration audit, and 6.6 chain verification,
each reviewed before merging. Tag deletion is **open decision O6**: decide
whether deleting an in-use tag is blocked or removes its memberships before
Task 6.3's migration. Do not infer that DL-6 applies to tags.

## What Git does not carry

`.env`, database contents, uploaded media, WordPress exports, and `.venv` are
ignored or otherwise local. Do not commit or paste secrets into a Qwen prompt.
Use [`.env.example`](../.env.example) only as a template and set new values on
the desktop. The current [Compose file](../docker-compose.yml) is a dev stack
with host bind mounts at `/mnt/container/db` and `/mnt/container/app`; create
those directories with suitable permissions on the target machine before
starting it. No named or anonymous Docker volumes are intended. The app
entrypoint runs `alembic upgrade head`, so starting a fresh stack advances
that machine's database to the migrations present in its checked-out branch.

The repository alone is enough to continue documentation, review, and new
task-branch coding. To continue work that depends on existing recipe media,
WordPress source exports, or the laptop's database rows, transfer those data
separately with an appropriate backup; they are not in Git. See the
[README](../README.md) for local setup and [project status](PROJECT-STATUS.md)
for the shorter current-state summary.

The existing Compose file is not a cloud deployment manifest: it starts a
local MariaDB and hardcodes laptop-style `/mnt/container` paths. Cloud hosting
needs a separate deployment configuration, persistent storage and backup,
secrets, and network/access decisions before exposing the service.
