# Cookbook app

A local-first family cookbook built with FastAPI, SQLAlchemy, Alembic, and
MariaDB. The repository has the complete Task 6 data model and the first
authentication services (identity, password hashing, sessions, and cookie
helpers). The app currently exposes health endpoints only; login/logout pages,
recipe browsing/editing, search, and import are still planned. See the
[project status](docs/PROJECT-STATUS.md) and [desktop handoff](docs/DESKTOP-HANDOFF.md)
before starting new work.

## Work on another machine

Clone the repository and check out the branch named in the project status file.
The source, migrations, tests, specification, and implementation plan travel
through Git. `.env`, database contents, media uploads, local virtual
environments, and WordPress exports are intentionally not committed; move or
recreate them separately.

For local development, install Docker with Compose. The dev stack uses host
directories under `COOKBOOK_DATA_DIR` for MariaDB and media. It uses bind
mounts rather than named or anonymous volumes. Copy `.env.example` to `.env`,
replace every example secret and password, and keep `.env` untracked. On
Windows, set `COOKBOOK_DATA_DIR` in `.env` to a local directory outside synced
folders, such as `C:/Users/<you>/AppData/Local/cookbook-app`, and create its
`db` and `app` subdirectories before starting. Do not use the Linux default
`/mnt/container` from PowerShell; it may resolve to a small Docker-side
filesystem. Then:

```bash
docker compose up -d --build
docker compose ps
curl -i http://127.0.0.1:8000/healthz
curl -i http://127.0.0.1:8000/readyz
```

The app entrypoint runs `alembic upgrade head` at startup. `/healthz` checks
process liveness; `/readyz` checks the database connection and media-directory
write access. The current application has no recipe HTTP routes yet.

## Development checks

Install [uv](https://docs.astral.sh/uv/) and run `uv sync --frozen` to create a
local `.venv`. Database tests require an isolated MariaDB 11 database; do not
point them at the running development database. The shared disposable-database
procedure is in the [data-model plan](docs/superpowers/plans/2026-10-03-data-model.md#isolated-test-database-binding).

With that database running and its connection supplied through environment
variables, run `.venv/bin/alembic upgrade head`, `.venv/bin/alembic check`, and
`.venv/bin/pytest -q`. The latest verified result is recorded in the project
status file. Do not commit `.env` or test credentials.

## First owner and local recovery

After installing this branch with `uv sync --frozen`, `uv run cookbook
init-secret --out secret.key` generates a private key file and prints its path.
Set `SECRET_KEY_FILE` to that path for the web app. Existing files are refused
unless `--overwrite` is supplied; rotating the key invalidates signed tokens.

Against a migrated database, run `uv run cookbook setup` for hidden interactive
password entry, or `uv run cookbook setup --username owner --password-file
/path/to/password`. Setup also accepts `FIRST_ADMIN_PASSWORD_FILE`, then
`FIRST_ADMIN_PASSWORD` as fallbacks; these require `--username`. Passwords
require at least eight characters, with spaces preserved. CLI account commands
use `DB_*` configuration and work before a web secret is configured.

The web alternative is `/setup` while the database contains no users. Both
paths share a transaction guard: only one first owner can be created. Browser
setup signs the owner in and redirects to `/`; the cookbook UI is a later task.

Use `uv run cookbook user reset-password owner --interactive` (or
`--password-file /path/to/password`) to reset an existing account and revoke all
its sessions. Local recovery with `uv run cookbook setup --username owner
--password-file /path/to/password --force-owner-recreation` additionally restores
that existing account as an active admin and requires typing `yes/owner`.
Recovery refuses nonexistent accounts. The running development image only
gains these commands and screens after a separately approved rebuild.

## Launch and trusted proxies

Authentication checks the current database role on each request. Account
creation, activation, role changes and password resets are admin-only form
endpoints under `/admin/users`; disabling an account or changing its role or
password revokes its sessions. Concurrent changes cannot remove the last active
admin. Full admin screens remain a later UI task.

All `POST`, `PUT`, `PATCH` and `DELETE` requests require CSRF. Authenticated
requests use `X-CSRF-Token` (which takes precedence) or a hidden `csrf_token`
form field. Login and setup require their signed form token plus the browser
binding cookie. Successful anonymous public reads are cacheable; authenticated,
cookie-setting, unsafe and error responses are private and not stored.

The application owns the interpretation of `X-Forwarded-For` and
`X-Forwarded-Proto` (see `src/cookbook/auth/identity.py`). It only honors
those headers when the request's **direct TCP peer** falls inside
`TRUSTED_PROXY_CIDRS`; from any other peer the forwarded headers are ignored
and the direct peer is used as the client identity.

- Set `TRUSTED_PROXY_CIDRS` to the CIDR(s) of the box's *actual* trusted
  reverse proxy only — for example the docker network range the proxy
  connects from, or a single address such as `172.17.0.1/32`. Do **not**
  add your LAN or the whole world; a peer outside the list cannot use
  forwarded headers, and the direct peer becomes identity.
  Leave it empty when running without a reverse proxy.
- Launch uvicorn with `--no-proxy-headers` so uvicorn does not rewrite the
  peer or interpret these headers before the app does (the Dockerfile's
  `CMD` already includes it). For direct local runs:
  `uvicorn cookbook.app:create_app --factory --host 127.0.0.1 --port 8000 --no-proxy-headers`.
- The app does **not** guess the external scheme at startup — the effective
  scheme comes from the request (the trusted peer's `X-Forwarded-Proto`
  when set). `COOKIE_SECURE` unset follows that effective scheme; an
  explicit `true`/`false` overrides it (see the `.env.example` comment:
  `false` on HTTPS is a deliberate weakening).

## Deployment boundary

`docker-compose.yml` is a two-service **development** configuration: it starts
its own MariaDB and uses bind mounts under `COOKBOOK_DATA_DIR`. The application
code can connect to another MariaDB through `DB_*` settings, but a separate
production/cloud Compose configuration, persistent storage, secrets, backup,
and access controls have not been built or reviewed. Cloning the repository
into the cloud does not by itself deploy a production-ready service. Keep
database and media data outside Git.

Design: [cookbook specification](docs/superpowers/specs/2026-09-20-cookbook-design.md).
