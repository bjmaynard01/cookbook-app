# Backup, Restore, and Cutover Gate Design

## Intent

Make the cookbook's database and uploaded media recoverable, and prove that a backup can be restored before the owner considers production cutover.

## Goals

- Provide `cookbook backup --out <dir>` with a paired MariaDB SQL archive, media archive, and manifest.
- Provide `cookbook restore` with explicit source archives and explicit database and media destinations.
- Refuse to replace existing target data unless the owner supplies `--replace`.
- Verify archive integrity, restored table counts, and target readiness; prove the public app can read a restored recipe in an isolated drill.
- Keep Docker orchestration outside the application process.

## Non-goals

- Scheduling backups, retention policy, off-site replication, or a monitoring service.
- CI/CD, production deployment, DNS changes, stopping WordPress, or performing production cutover.
- Backing up `.env`, signing keys, LLM keys, or any other secrets.

## Constraints

- Follow the product spec's D21/D22: back up MariaDB plus `MEDIA_DIR`; the app does not use the Docker socket or Docker CLI; restore targets are explicit.
- Retain the deterministic artifact naming contract: `cookbook-<date>.sql.gz` and `media-<date>.tar.gz`.
- The current CLI uses `argparse` and the lean `CLISettings` (`DB_*`, `MEDIA_DIR`). The Docker image currently lacks MariaDB client tools, and the Windows host has no `mariadb-dump`/`mariadb` executable.
- Keep database verification on a disposable MariaDB instance. Never read `.env` or run test migrations against the development database.

## Approaches

1. **Package MariaDB client tools in the app image — selected by the owner.** Add the MariaDB client package to the runtime image; `cookbook` invokes `mariadb-dump` and `mariadb` as child processes. This matches the SQL dump contract and works for the Windows Docker-based development workflow. The host may invoke the CLI inside the container, but the application itself never calls Docker.
2. **Implement a Python SQL dump and restore.** This avoids an OS package and works directly with `uv run`, but creates a custom SQL serializer/importer with greater correctness risk and departs from the specified `mysqldump` export.
3. **Require host-installed MariaDB tools.** This keeps the image smaller, but the required tools are absent on the current Windows host and would make the supported local workflow depend on manual installation.

## Design

### Backup

`cookbook backup --out <dir>` reads the database connection from the existing CLI configuration and archives `MEDIA_DIR`. It writes a matched set of `cookbook-<UTC-date>.sql.gz`, `media-<UTC-date>.tar.gz`, and `manifest-<UTC-date>.json`. The manifest records the artifact names and SHA-256 digests, the Alembic revision, per-table row counts, and media-file count so restore can compare against the known-good backup. If any destination artifact already exists, backup fails without overwriting it. It writes temporary files first and publishes the manifest last.

Use `mariadb-dump` with a transaction-consistent InnoDB snapshot; invoke external processes without a shell and provide credentials through a short-lived private defaults file rather than command arguments or logs. Build the media archive with stable relative paths and sorted entries. The host owns schedule and retention. To avoid a database/media mismatch, run backups during a quiet window with editor writes stopped; backup does not add a new application-wide maintenance lock.

### Restore

Use explicit arguments so input archives cannot be confused with output targets:

```text
cookbook restore --sql <dump.sql.gz> --media-archive <media.tar.gz> --manifest <manifest.json> --db-url <URL> --media-dir <directory> [--replace]
```

The source flags identify the backup set; `--db-url` and `--media-dir` identify the destinations. Restore validates the manifest and archive digests, inspects the media archive for unsafe paths/links, and stages media extraction before changing either target. It refuses if the database contains tables or the media target contains files unless `--replace` is present. With `--replace`, it replaces the cookbook database contents and the contents of the explicit media directory; it does not drop the database itself or touch paths outside that directory. A target containing tables outside the supported cookbook schema is refused, including with `--replace`.

Before changing targets, restore compares the target's tables with the supported cookbook schema inventory (the current SQLAlchemy metadata plus `alembic_version`). Without `--replace`, any user table is a refusal. With `--replace`, restore may clear supported cookbook tables—including tables absent from the backup—then import; unknown tables or other unsupported schema objects are refused even with the flag. It never drops the database itself. The same flag permits replacing the contents of the explicit media directory.

The manifest's Alembic revision must exist in the current migration history and be the current head or one of its ancestors. Restore never downgrades or runs migrations: it imports the backup at its recorded revision, compares every expected table count and the media-file count with the manifest, then checks database connectivity and media-directory writability (the `/readyz` contract). Starting the app afterward applies the normal forward migrations. A revision unknown to or newer than the running code is refused. Restore reports a failing stage without printing credentials. Because SQL and filesystem changes cannot be committed atomically together, restore requires the destination app to remain offline until all checks pass; a failed `--replace` restore may require restoring the prior backup.

### Restore drill and cutover gate

Add CLI and integration tests for archive naming/no-overwrite, manifest digests and counts, unsafe archive paths, empty-target success, non-empty refusal, and explicit `--replace` across both targets. The real drill uses a fresh disposable MariaDB and isolated media directory: back up a seeded source containing a recipe, image, and import audit rows; restore to a separate database and media directory; check counts/readiness; start the app against the restored targets; and request a public recipe page. Verify migration upgrade/check behavior on the restored database before considering the drill passed.

Document the host-side commands for running the CLI inside the app image and conducting the disposable restore drill. The owner may consider production cutover only after the existing WordPress importer verification and this restore drill pass, and after browsing the restored site. No cutover action is automated.

## Decisions recorded

- A non-empty database is never replaced implicitly; `--replace` is required.
- The same `--replace` flag authorizes replacing both the explicit database and media targets.
- MariaDB client tools are packaged in the app runtime image; the app invokes them but never invokes Docker.
- The restore CLI separates media archive input (`--media-archive`) from media destination (`--media-dir`) to make both sides explicit.
- Backups from a known older Alembic revision may be restored and migrated forward by normal app startup; unknown/newer revisions are refused, and downgrade is never attempted.
- `--replace` clears all known cookbook tables before import, including any not present in an older backup; unrecognized tables/objects block replacement.
