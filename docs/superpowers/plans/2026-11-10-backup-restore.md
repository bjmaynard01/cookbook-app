# Backup, Restore, and Cutover Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add explicit-target MariaDB and media backup/restore commands, and prove recovery in an isolated disposable MariaDB drill.

**Architecture:** Keep CLI parsing in `cli.py` and implement backup/restore orchestration in a focused maintenance module. Use packaged MariaDB client subprocesses for SQL dump/import, Python standard-library archive/hash operations for media and manifests, SQLAlchemy metadata plus Alembic history to constrain replacement, and disposable-database integration tests.

**Tech Stack:** Python 3.12, argparse, SQLAlchemy/Alembic, MariaDB client tools, tarfile, gzip, hashlib, pytest, Docker Compose for isolated integration verification.

**Spec:** `docs/superpowers/specs/2026-11-09-backup-restore-design.md`; product requirements in `docs/superpowers/specs/2026-09-20-cookbook-design.md` §9.7 and §10.

## Global Constraints

- Back up MariaDB and `MEDIA_DIR`; never back up `.env`, signing keys, LLM keys, or other secrets.
- The application must not use the Docker socket or Docker CLI.
- Restore source archives and database/media destinations must be explicit.
- Use `cookbook-<UTC-date>.sql.gz` and `media-<UTC-date>.tar.gz`; also write `manifest-<UTC-date>.json` last.
- Existing backup artifact paths are never overwritten.
- A non-empty database or media destination requires `--replace`; the same flag authorizes replacement of both explicit targets.
- Restore only supported cookbook schema objects; unknown tables or unsupported schema objects block replacement even with `--replace`.
- Restore accepts only a known current/ancestor Alembic revision, never downgrades or migrates; ordinary app startup may migrate forward.
- Keep DB verification on disposable MariaDB. Never run task tests/migrations against the development database.
- Do not expose credentials in subprocess arguments, logs, or errors; use a short-lived private MariaDB defaults file and invoke child processes without a shell.
- Backups run during an owner-managed quiet window with editor writes stopped; no application-wide lock, scheduler, retention, off-site replication, CI/CD, or production cutover is added.

## Review Focus

- Malicious media archive paths, symlinks, hardlinks, and duplicate entries must not escape or ambiguously overwrite the target; test the archive inspector before extraction.
- Partial subprocess failure must not publish an apparently complete backup; test temporary-file cleanup and manifest-last publication.
- DB replacement when the backup omits a currently supported table must clear that table to prevent stale rows; assert this with an older-schema fixture.
- User-created/unknown schema objects must not be dropped by `--replace`; test both unknown table and unsupported object refusal.
- Credentials containing shell metacharacters or whitespace must remain private and work through the defaults file; test subprocess argv and captured output.

---

## File Map

- Modify `src/cookbook/cli.py` to register `backup` and `restore` subcommands and lazily dispatch them.
- Create `src/cookbook/cli_backup.py` for backup/restore orchestration, manifests, target inventory checks, safe media archive inspection/extraction, counts, and MariaDB subprocess calls.
- Modify `Dockerfile` to install MariaDB client tools in the runtime image.
- Modify `tests/test_cli.py` for argument parsing and dispatch.
- Create `tests/test_cli_backup.py` for unit/CLI tests with subprocess and filesystem fixtures.
- Create `tests/test_backup_restore_integration.py` for opt-in disposable MariaDB and app restore-drill coverage.
- Modify `README.md` with container invocation and isolated drill instructions; do not edit `.env` or handoff/checkpoint notes.

## Interfaces

- `cli.py` dispatches backup/restore namespaces to `run_backup_command(args)` and `run_restore_command(args)` in `cli_backup.py`.
- `backup` accepts required `--out PATH`; DB and media configuration comes from existing `cli_config()`.
- `restore` accepts required `--sql PATH --media-archive PATH --manifest PATH --db-url URL --media-dir PATH`, plus optional `--replace`.
- Backup manifest fields are versioned and include artifact basenames and SHA-256 digests, Alembic revision, per-supported-table row counts, and media-file count.
- Integration tests use a dedicated disposable MariaDB URL supplied by an explicit test environment variable; absence skips only those integration cases.

## Task 1: CLI and MariaDB Runtime Support

**Files:** `src/cookbook/cli.py`, `Dockerfile`, `tests/test_cli.py`

- [x] Add `backup --out PATH` and `restore --sql PATH --media-archive PATH --manifest PATH --db-url URL --media-dir PATH [--replace]` parser tests, including required-argument validation.
- [x] Add lazy CLI dispatch to `cookbook.cli_backup` without loading DB settings for unrelated commands.
- [x] Install the MariaDB client package in the final runtime image and verify `mariadb` and `mariadb-dump` are available in that image.
- [x] Run focused CLI tests and build the runtime image.

## Task 2: Deterministic Backup and Manifest

**Files:** `src/cookbook/cli_backup.py`, `tests/test_cli_backup.py`

- [x] Test UTC artifact names, stable sorted relative media paths, archive contents, manifest fields/digests/counts, existing-output refusal, and no published manifest after dump/archive failure.
- [x] Implement manifest version validation, SHA-256 streaming helpers, supported table row counting, Alembic current revision lookup, and deterministic media tar creation.
- [x] Implement MariaDB dump using a temporary permission-restricted defaults file, `mariadb-dump --single-transaction --routines=false --events=false`, gzip streaming, no shell, and fixed/redacted error messages.
- [x] Write temporary artifacts and atomically publish SQL/media files, then publish the manifest last; clean temporary files on every failure.
- [x] Run focused unit tests and a backup smoke test against a disposable MariaDB with seeded media.

## Task 3: Restore Preflight and Safe Media Staging

**Files:** `src/cookbook/cli_backup.py`, `tests/test_cli_backup.py`

- [x] Test bad/mismatched manifest digests, malformed manifests, unknown/newer Alembic revisions, unsafe paths/links, duplicate paths, corrupt archives, and refusal to replace occupied targets without `--replace`.
- [x] Implement source digest validation and confirm manifest artifact basenames match the explicit source files.
- [x] Implement safe tar inspection that rejects absolute/traversal paths, links, devices, duplicate normalized paths, and non-regular entries; extract only into a new staging directory.
- [x] Implement target DB schema inventory checks using SQLAlchemy metadata plus `alembic_version`; reject unknown tables and unsupported schema objects even when `--replace` is set.
- [x] Implement media target validation and staging without modifying either destination during preflight.
- [x] Run focused restore preflight tests.

## Task 4: Restore Replacement and Verification

**Files:** `src/cookbook/cli_backup.py`, `tests/test_cli_backup.py`

- [x] Test empty-target restore, refusal of non-empty targets without `--replace`, replacement of both targets with the flag, clearing supported tables absent from the dump, row/media count mismatch, import failure, readiness failure, and credential redaction.
- [x] Implement guarded replacement: clear only supported cookbook tables in FK-safe order, including tables missing from older dumps; replace contents only inside the explicit media directory; never drop the database.
- [x] Import the gzip SQL using `mariadb` with the private defaults file; do not pass credentials in argv or invoke a shell.
- [x] Compare all manifest table counts and media count after import; check DB connectivity and media writability before reporting success.
- [x] Ensure all error messages identify the failed stage without printing passwords or full credential-bearing URLs.
- [x] Run focused restore tests against disposable MariaDB.

## Task 5: Isolated Restore Drill, Documentation, and Gate

**Files:** `tests/test_backup_restore_integration.py`, `README.md`, `docs/BUILD-ROADMAP.md`, `docs/PROJECT-STATUS.md`

- [x] Add an opt-in integration test that seeds a source database with a recipe, image, and import audit rows; backs up; restores to separate disposable DB/media targets; verifies counts/readiness; starts the app against restored targets; and requests the public recipe route.
- [x] Verify Alembic upgrade/check behavior against the restored database at an older known revision before app startup, then confirm it reaches current head without downgrade.
- [x] Document commands for invoking backup/restore inside the app image and running the disposable restore drill; state the quiet-window requirement and that production cutover is a human gate.
- [x] Update task 20 status and completion gate only after the integration drill passes; do not perform cutover.
- [x] Run relevant tests and the full suite; build and launch the local stack only through the established workflow; inspect Git diff to ensure unrelated existing changes are untouched.

**Verification outcome (2026-11-12):** The Task 20/CLI/integration set passed 146 tests, including the isolated MariaDB drill and image/runtime verification. The complete project suite then passed 707 tests with 1 skipped on Windows; the skipped case requires the packaged MariaDB client and passed in the container drill. The earlier five failures were caused by running from the app container without its expected checkout/configuration: the Dockerfile test could not see the file, the importer guard requires localhost port 33061, and the inconsistent runner environment caused the account logging and asyncmy loop errors. No product/test-code change was needed for these failures. One existing Starlette/httpx deprecation warning remains. The application image built successfully; the local Compose stack was not launched. Test databases and the named disposable MariaDB container/network were isolated and removed after verification.

## Execution Order

Tasks 1–2 establish the CLI and backup artifact contract. Tasks 3–4 implement restore in preflight and mutation stages so destructive behavior has an explicit review boundary. Task 5 proves the complete round trip and records the cutover gate. Each task should be completed and reviewed in a fresh Qwen session before moving to the next.
