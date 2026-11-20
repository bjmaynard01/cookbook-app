# Cookbook beta deployment runbook

Operator runbook for the beta stack and delivery pipeline as implemented in
`.github/workflows/deploy-dev.yml`, `docker-compose.beta.yml`, and
`bin/deploy-beta.ps1`.

This describes the **current** implementation. It does **not** claim beta is
deployed, and it never assumes DNS or reverse-proxy changes are already made.
The owner records the actual reverse-proxy network name, upstream port, HTTPS
route, and trusted proxy CIDRs before beta goes public.

## Contents

- [Deployment gate (closed by default)](#deployment-gate-closed-by-default)
- [Pipeline behavior](#pipeline-behavior)
- [GitHub repository variables](#github-repository-variables)
- [Protected host env-file keys](#protected-host-env-file-keys)
- [Beta Compose project and isolation](#beta-compose-project-and-isolation)
- [Prerequisites checklist](#prerequisites-checklist)
- [Initial beta data seed (Task 20 restore)](#initial-beta-data-seed-task-20-restore)
- [Owner account and signing-key recreation](#owner-account-and-signing-key-recreation)
- [Validating the resolved Compose config](#validating-the-resolved-compose-config)
- [Performing a deployment](#performing-a-deployment)
- [Recovering a failed deployment](#recovering-a-failed-deployment)
- [Backup retention](#backup-retention)
- [Final smoke checks](#final-smoke-checks)
- [What this does not do](#what-this-does-not-do)

## Deployment gate (closed by default)

The `deploy` job runs only when **all** of these hold at once:

- `github.event_name == 'push'` (not manual dispatch, not pull request), and
- `github.ref == 'refs/heads/dev'` (exactly the `dev` branch), and
- the repository variable `COOKBOOK_BETA_ENABLED` equals the literal string `true`.

**`COOKBOOK_BETA_ENABLED` must remain unset (or left not equal to `true`) until
every item in the prerequisites checklist below has been verified on the real
`[self-hosted, dev-workstation]` runner.** Treat `true` as a deliberate,
owner-made change. Setting it to `true` starts allowing the pipeline to touch
the beta host on trusted `dev` pushes; unsetting it re-closes the gate without
re-triggering anything.

Until that gate is opened, a push to `dev` still runs the hosted `tests` and
`smoke` jobs (building and publishing the immutable SHA image to GHCR), but the
`deploy` job is skipped. Nothing is deployed and nothing on the beta host
changes.

## Pipeline behavior

`.github/workflows/deploy-dev.yml` triggers on:

- `push` to `dev`, and
- `workflow_dispatch` (manual).

Concurrent runs are serialized per repository via
`concurrency.group: beta-${{ github.workflow }}` with
`cancel-in-progress: false`, so two rapid `dev` pushes cannot overlap a
migration.

Three jobs:

| Job | When it runs | What it does |
|---|---|---|
| `tests` | both triggers | Runs the reusable `.github/workflows/tests.yml` hosted checks: the suite against disposable MariaDB 11 plus a separate read-only audit of `uv.lock`. `contents:read` only. |
| `smoke` | trusted push to `dev` only | Runs the reusable `.github/workflows/smoke-test.yml` with `push_image: true`; builds, migration/readiness-tests, and publishes the immutable `sha-<full-sha>` image. Alone holds `packages:write`. |
| `deploy` | trusted push to `dev` **and** `COOKBOOK_BETA_ENABLED == 'true'` | Runs on `[self-hosted, dev-workstation]`. Sparse-checks out only `bin/deploy-beta.ps1` and `docker-compose.beta.yml` at the exact commit, then runs `bin\deploy-beta.ps1 <github.sha>`. |

Key guarantees from the workflow:

- `main`, pull requests (including forks), and manual dispatch never select the
  self-hosted runner, never publish, and never deploy. `main` is checks-only.
- The `deploy` job needs `smoke`, so it deploys the exact immutable SHA image
  that the hosted smoke step already validated. No floating tag is used.
- The deploy step consumes only repository variables (never `secrets.*`), and
  the runner is never given arbitrary shell or Compose arguments — the fixed
  entrypoint receives only the full commit SHA.
- Success and failure are reported through the **native GitHub Actions job
  conclusion**. No external notification webhook, secret, or status endpoint is
  configured.

## GitHub repository variables

These are GitHub **repository variables** (not secrets) that the `deploy` step
reads into the environment. They hold non-secret wiring values; all credentials
live in the protected host env-file described next.

| Variable | Purpose (as consumed by the entrypoint/Compose) |
|---|---|
| `COOKBOOK_BETA_ENABLED` | Deployment gate. Must be exactly `true` to allow the `deploy` job; leave unset otherwise. |
| `COOKBOOK_BETA_ENV_FILE` | Absolute host path to the protected beta env file. Must be named `cookbook-beta.env` or `.env.beta`. |
| `COOKBOOK_BETA_DATA_DIR` | Absolute beta-only host data directory. Must be named `cookbook-beta-data` and must **not** live inside the repository checkout. |
| `COOKBOOK_BETA_PUBLIC_URL` | The configured HTTPS beta route. The entrypoint requires it to start with `https://`. |
| `COOKBOOK_BETA_PORT` | The loopback TCP port the beta app is published on (single numeric port in range). |
| `COOKBOOK_PROXY_NETWORK` | The name of an existing external reverse-proxy Docker network the beta app joins. |

`COOKBOOK_BETA_ENV_FILE`, `COOKBOOK_BETA_DATA_DIR`, `COOKBOOK_BETA_PUBLIC_URL`,
`COOKBOOK_BETA_PORT`, and `COOKBOOK_PROXY_NETWORK` are passed to the deploy
step only; `COOKBOOK_BETA_ENABLED` gates the job and is not passed into the step.

The host data directory layout the entrypoint expects under
`COOKBOOK_BETA_DATA_DIR`:

- `db/` — MariaDB data (bound to `/var/lib/mysql`)
- `app/` — media (bound to `/media`)
- `backups/` — paired backup sets (bound to `/backups`)
- `.deploy-beta.lock` — exclusive deployment lock created by the entrypoint

## Protected host env-file keys

`docker-compose.beta.yml` requires these keys to be present in the protected
env file passed with `docker compose --env-file` (the file named above as
`COOKBOOK_BETA_ENV_FILE`). Names only — no values are stored or documented here.
Protect this file the same way as any credential; never commit it, and never
paste it into logs or this runbook.

| Env-file key | Consumed by |
|---|---|
| `BETA_DB_ROOT_PASSWORD` | `db` MariaDB root password |
| `BETA_DB_NAME` | `db` database name and `app` `DB_NAME` |
| `BETA_DB_USER` | `db` app user and `app` `DB_USER` |
| `BETA_DB_PASSWORD` | `db` app password and `app` `DB_PASSWORD` |
| `BETA_SECRET_KEY` | `app` session/token signing key (`SECRET_KEY`) |
| `BETA_TRUSTED_PROXY_CIDRS` | `app` trusted proxy CIDRs (`TRUSTED_PROXY_CIDRS`) |

Note the split of responsibility:

- **Credentials** above come from the protected **env file**.
- **Host wiring** values — `COOKBOOK_BETA_DATA_DIR`, `COOKBOOK_BETA_PORT`,
  `COOKBOOK_PROXY_NETWORK` — come from repository variables (process
  environment), **not** from the env file. The entrypoint re-asserts these
  process values after validation so the lock, backup location, and readiness
  port all agree with the resolved stack.
- `COOKBOOK_IMAGE` is set by the entrypoint to the exact immutable image
  `ghcr.io/bjmaynard01/cookbook:sha-<full-sha>` and is never read from a file.

## Beta Compose project and isolation

- Fixed project name `cookbook-beta`, set by the entrypoint with
  `--project-name cookbook-beta`. The Compose file defines **no** fixed
  `container_name`, so names cannot collide with local Cookbook, Ganteva, or
  WordPress containers.
- Beta-only services `db` and `app`, a private `internal: true` backend
  network, and attachment to one external proxy network named by
  `COOKBOOK_PROXY_NETWORK`.
- App port is published to loopback only:
  `127.0.0.1:${COOKBOOK_BETA_PORT}:8000`. It is not published to the public
  interface; the reverse proxy reaches the app over the shared proxy network.
- Binds are scoped to `COOKBOOK_BETA_DATA_DIR/{db,app,backups}` — never to local
  development defaults.
- The `app` image is required and pinned to an immutable SHA reference
  (`COOKBOOK_IMAGE`); it must not resolve to a floating tag.

Because `COOKBOOK_IMAGE` is required and the required data/network variables use
`:?` guards, `docker compose config` fails fast if any is missing.

## Prerequisites checklist

Complete and verify each item **before** considering
`COOKBOOK_BETA_ENABLED=true`. Record the actual values (proxy network, port,
route, CIDRs) in a private location, not in this file.

- [ ] **Runner isolation confirmed.** The `[self-hosted, dev-workstation]`
      runner can host a separate `cookbook-beta` Compose project alongside
      Ganteva and WordPress without shared container names, ports, data paths,
      databases, or networks. Verify the runner's repository access and that
      untrusted pull-request code cannot execute on it (branch protection on
      `dev`; only trusted pushes reach the deploy job).
- [ ] **GHCR read credential configured.** A dedicated read-only GHCR pull
      credential is already configured on the host so it can pull the private
      `ghcr.io/bjmaynard01/cookbook` package. No repository secret supplies
      pull credentials at deploy time.
- [ ] **Docker + Compose present.** `docker` and the Compose plugin run on the
      runner, and `mariadb:11` is pullable.
- [ ] **Beta data directory in place.** `COOKBOOK_BETA_DATA_DIR` is an absolute
      path named `cookbook-beta-data`, outside the repository checkout, with its
      `db/`, `app/`, and `backups/` subdirectories with ownership and
      permissions that the Docker services can read/write.
- [ ] **Protected env file in place.** `COOKBOOK_BETA_ENV_FILE` exists at a
      protected path, is named `cookbook-beta.env` or `.env.beta`, contains the
      six `BETA_*` keys, and is not committed or readable by other services.
- [ ] **Loopback port chosen.** `COOKBOOK_BETA_PORT` is a unique port not used by
      local Cookbook, Ganteva, WordPress, or anything else on the host, and any
      host firewall leaves it bound to loopback only.
- [ ] **External proxy network present.** `COOKBOOK_PROXY_NETWORK` names an
      existing Docker network that the reverse proxy already uses.
- [ ] **HTTPS route recorded.** `COOKBOOK_BETA_PUBLIC_URL` is the beta HTTPS
      route the reverse proxy will serve, and the proxy is configured to reach
      the `app` service on port `8000` over `COOKBOOK_PROXY_NETWORK`. The
      loopback-published host port is for host-local checks only.
- [ ] **Trusted proxy CIDRs recorded.** `BETA_TRUSTED_PROXY_CIDRS` lists the
      reverse proxy's egress CIDR(s) so the app trusts only that proxy for
      forwarded headers.
- [ ] **Initial seed complete** (next section) — the stack has been brought up
      once, seeded, and the owner account and signing key recreated.

Do **not** enable the gate, and do not claim beta is live, until DNS and the
reverse-proxy route are actually verified end to end.

## Initial beta data seed (Task 20 restore)

Beta starts empty. Seed it **once** from a protected copy of the imported
recipe collection using the Task 20 `backup`/`restore` commands. Work inside
the built image (it ships the MariaDB client tools) so the packaged clients are
used, and keep the seed set on persistent storage outside the container.

The requirement is an **existing, already-provisioned beta `app` and `db`**
before you seed: bring the `cookbook-beta` project up with a known-good image
first, confirm `db` is healthy and migrations have run against an empty schema,
and keep the app able to reach `db`. Restoring into a half-provisioned stack is
not supported.

Steps (names and shapes only; substitute the real protected paths/credentials):

1. Obtain a protected Task 20 backup set of the source recipe collection —
   a **paired** set of `cookbook-*.sql.gz`, `media-*.tar.gz`, and
   `manifest-*.json` (manifest records artifact names, SHA-256 digests, the
   Alembic revision, and row/media counts).
2. Place that set under the beta `backups/` directory.
3. Restore into the **beta** database and **beta** media directory. The targets
   are the beta `DB_NAME`/`DB_USER` and the beta `app/` media directory bound at
   `/media` — never WordPress, local development, or any production target.

   Restoring into a populated database or media directory is refused unless
   `--replace` is supplied. **Use explicit `--replace` only for these beta
   targets.** The same flag authorizes replacement of both the named database
   and the named media directory. Restore never drops the database itself and
   refuses unsupported schema objects even with `--replace`.

   Restore stops at the Alembic revision recorded in the manifest; normal app
   startup afterward applies any forward migrations. Keep the destination app
   offline until restore checks pass.

Do not read `.env`, and do not source or reuse the source collection's local
credentials or signing key for beta.

## Owner account and signing-key recreation

After the seed restore, beta has a copy of the source's data but must not reuse
the source's sessions or credentials:

- **Recreate the owner account.** Create a new owner using the app's first-owner
  path (or a password reset) with a **fresh** owner password that has no
  relation to the local/source owner password. Beta account management stays
  owner-controlled through the app; the pipeline never creates tester accounts.
- **Rotate the beta signing key.** Generate a **unique** beta `SECRET_KEY`
  (for example via `cookbook init-secret`) and put it in the beta env file as
  `BETA_SECRET_KEY`. Do not copy the local/source `SECRET_KEY`. Rotating the
  key invalidates any sessions signed under the old key, which is the intent:
  existing local sessions and tokens must not carry into beta.

Verify the new owner can sign in over the beta HTTPS route before inviting
anyone.

## Validating the resolved Compose config

Validate that the resolved beta configuration is complete **without** printing
expanded secrets. `config --quiet` resolves the file against the env file and
the required variables but emits only a success/failure status — it does not
dump the expanded secret values, so it is safe to run in a shell.

From the repository root, with the real host wiring exported for the check:

```powershell
$env:COOKBOOK_BETA_DATA_DIR = "<beta-data-dir>"
$env:COOKBOOK_BETA_PORT     = "<port>"
$env:COOKBOOK_PROXY_NETWORK = "<proxy-network>"
docker compose --project-name cookbook-beta `
  --env-file "<beta-env-file>" `
  -f docker-compose.beta.yml config --quiet
```

A successful (zero) exit means all required variables — the `BETA_*` env-file
keys plus `COOKBOOK_BETA_DATA_DIR`, `COOKBOOK_BETA_PORT`, and
`COOKBOOK_PROXY_NETWORK` — resolved and the external proxy network name was
accepted. Do **not** drop `--quiet` and capture the expanded config in this
runbook; the expanded form contains credentials.

As the collision check, confirm the resolved project owns only beta resources:
project name `cookbook-beta`, services `db` and `app`, the private backend
network, one external proxy network, a loopback-only app port, and bind paths
under the beta data directory. Nothing should overlap local Cookbook, Ganteva,
WordPress names, ports, paths, or databases.

## Performing a deployment

A deployment is triggered by pushing a verified commit to `dev` (with the gate
open). On the runner the entrypoint `bin/deploy-beta.ps1 <40-char-sha>`:

1. Validates the single 40-hex-character SHA and every host setting before any
   side effect (arity/format, env file, data dir name/path, HTTPS public URL,
   valid port, proxy network, Compose file present).
2. Takes an exclusive lock (`.deploy-beta.lock`, `FileShare.None`) so two
   deployments cannot overlap. Releases it on exit, even on crash.
3. Sets `COOKBOOK_IMAGE` to the exact `sha-<sha>` image and runs
   `docker compose ... config --quiet` to confirm the resolved stack.
4. Confirms the beta `app` and `db` services exist, `db` is healthy, and the
   current app health check is healthy. An unhealthy existing app fails before
   stop or backup, so an operator must recover it first. The script then records
   the current image reference (printed as `Current beta image: ...`).
5. Stops the beta app to quiet writes.
6. Uses the **old** image (already running) to produce a paired backup into a
   new unique directory under `backups/`, then verifies exactly one
   `manifest-*.json`, one `cookbook-*.sql.gz`, and one `media-*.tar.gz` —
   printing `Verified paired backup: <path>`.
7. Pulls the `sha-<sha>` image, starts the app against it (normal entrypoint
   applies forward migrations), and waits for readiness.
8. On success prints `Beta deployment succeeded: <sha>` and releases the lock.

The image deployed is always the immutable SHA image the hosted smoke step
already migration/readiness-tested.

## Recovering a failed deployment

If any step **after** the app is stopped fails (backup, artifact verification,
image pull, app start, migration, `/readyz`, `/healthz`, or the public route),
the entrypoint:

- keeps the beta app **stopped/offline** (it re-runs `stop app` if needed),
- does **not** restore, does **not** delete images or backups, and never
  auto-runs `restore --replace`, and
- reports the prior image and backup location:
  `Beta deployment failed. Previous image: <oldImage>; backup: <backupPath>`.

Recovery is **owner-directed and manual**, in this order:

1. Leave beta offline. Do not invite testers back until the previous image is
   back up.
2. Decide the restore target. Rolling the image back alone is **not** safe after
   a forward schema migration — the database may already be migrated. Restore
   the recorded **paired** pre-deploy backup set (the `manifest-*.json`,
   `cookbook-*.sql.gz`, `media-*.tar.gz` in `<backupPath>`) into the beta
   database and beta media directory, using explicit `--replace` **only** for
   those beta targets.
3. Bring the app back up against the **previously recorded** image (the
   `Previous image` value), not the failed one.
4. Re-verify readiness and the public route (see final smoke checks) before
   letting anyone in.
5. Investigate the failed SHA; re-deploy a good SHA when ready.

The failed image and the backup are retained so you can inspect or retry
either way. Nothing is pruned automatically.

## Backup retention

Retention is **manual**. The entrypoint creates a fresh unique backup directory
for every deployment and never deletes prior backup sets, never prunes images,
and never schedules cleanup. The owner reviews `COOKBOOK_BETA_DATA_DIR/backups`
periodically and deletes superseded sets only when storage demands it and the
owner is confident the beta data is current and tested. Treat every backup set
as private family data.

## Final smoke checks

After seeding, recovery, or any deploy, confirm all of these:

- `db` is `healthy` and the beta `app` is running on the expected image SHA.
- Loopback `GET http://127.0.0.1:<COOKBOOK_BETA_PORT>/readyz` returns 2xx–3xx.
- Loopback `GET .../healthz` returns 2xx–3xx.
- The configured `COOKBOOK_BETA_PUBLIC_URL` HTTPS route responds successfully
  through the reverse proxy.
- As the recreated owner, sign in over HTTPS and load a public recipe page whose
  image renders from beta media.
- The deployment lock file is not held (no stray `.deploy-beta.lock` holder) and
  the last backup set in `backups/` is present and intact.

## What this does not do

- `main` and pull requests run checks only; neither ever selects the
  self-hosted runner, publishes, or deploys.
- There is **no active Cookbook production workflow**, no production runner
  label, and no production secrets. The future `[self-hosted, prod-fedora]`
  target stays disabled behind an explicit future owner decision.
- Nothing here mutates DNS or the reverse proxy. The owner publishes the beta
  record and routes HTTPS traffic; the repository does not identify, create, or
  change proxy configuration.
- No secret, database dump, media archive, signing key, or test-account
  password is stored in Git, in GitHub artifacts, or in this runbook. Do not
  read `.env` or WordPress export/import artifacts while following these steps.
- Deployment status is reported through the native GitHub Actions job
  conclusion only — no external webhook, notification secret, or status API.
