# Cookbook app

A local-first family cookbook built with FastAPI, SQLAlchemy, Alembic, and
MariaDB. The repository currently contains the application foundation, health
endpoints, the `users` table, and the first Task 6 `recipes` table. Recipe
editing, browsing, import, authentication, and the remaining data-model tables
are planned but not yet implemented. See [project status](docs/PROJECT-STATUS.md)
and the [desktop handoff](docs/DESKTOP-HANDOFF.md) before starting new work.

## Work on another machine

Clone the repository and check out the branch named in the project status file.
The source, migrations, tests, specification, and implementation plan travel
through Git. `.env`, database contents, media uploads, local virtual
environments, and WordPress exports are intentionally not committed; move or
recreate them separately.

For local development, install Docker with Compose. The dev stack uses host
directories `/mnt/container/db` for MariaDB and `/mnt/container/app` for media;
create them with permissions suitable for the containers before starting the
stack. It uses no named or anonymous volumes. Copy `.env.example` to `.env`,
replace every example secret and password, and keep `.env` untracked. Then:

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

## Launch and trusted proxies

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
its own MariaDB and uses fixed `/mnt/container` bind mounts. The application
code can connect to another MariaDB through `DB_*` settings, but a separate
production/cloud Compose configuration, persistent storage, secrets, backup,
and access controls have not been built or reviewed. Cloning the repository
into the cloud does not by itself deploy a production-ready service. Keep
database and media data outside Git.

Design: [cookbook specification](docs/superpowers/specs/2026-09-20-cookbook-design.md).
