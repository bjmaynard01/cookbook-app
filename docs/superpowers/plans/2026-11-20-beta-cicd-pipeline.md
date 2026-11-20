# Beta CI/CD Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans (recommended when running inline) to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build GitHub Actions checks and an immutable-image delivery flow that deploys Cookbook beta from `dev` to the existing Ganteva dev runner while keeping Cookbook production inactive.

**Architecture:** Hosted GitHub Actions jobs run unit/integration checks and build and smoke-test a runtime image against disposable MariaDB and media. Only trusted `dev` commits publish an immutable GHCR image and reach a restricted beta deploy entry point on Ganteva’s `[self-hosted, dev-workstation]` runner. A separate beta Compose project owns persistent data and migration backups; `main` runs checks only, and no Cookbook job targets Ganteva’s production runner.

**Tech Stack:** GitHub Actions YAML, Docker Buildx/Compose, GHCR, Python 3.12/uv, MariaDB 11, Cookbook backup/restore CLI, existing Ganteva runner.

**Spec:** `docs/superpowers/specs/2026-11-15-beta-cicd-design.md`

## Global Constraints

- Workflow checks use MariaDB 11 and never connect to local development, beta, WordPress, or production databases.
- Integration URLs remain restricted to the documented `cookbook_task20_` test schema guard.
- Only trusted `dev` publishing/deploy jobs receive GHCR write or beta host credentials; pull requests receive neither.
- The beta stack uses separate Compose project, database, media, signing key, configuration, port, and persistent data path.
- Every potentially migrating deploy creates a unique paired DB/media backup before starting the app.
- Failed migrations/readiness checks leave beta offline and retain prior image and backup; never auto-run `restore --replace`.
- `main` runs checks only. No active Cookbook workflow uses `[self-hosted, prod-fedora]` or production secrets.
- Never commit environment files, credentials, database dumps, media archives, WordPress export/import artifacts, or test passwords.
- Keep existing local Docker Compose development behavior intact.
- Follow Ganteva’s hosted check/build then self-hosted deploy pattern and deploy the exact SHA-tagged image that passed smoke tests.

## Review Focus

- PR/fork code cannot obtain registry write, beta host credentials, or execute on the self-hosted runner; test workflow permissions and runner labels.
- Forked or untrusted pull request workflow changes cannot cause a privileged deploy; test event/branch conditions and job dependencies.
- Two rapid `dev` pushes cannot overlap migrations or replace the image selected by an in-flight deployment; test concurrency and immutable SHA selection.
- A failed backup, pull, migration, health check, or smoke check does not remove the prior image or restart beta against partially migrated data; test each deployment failure boundary.
- Compose project, container names, ports, bind paths, env files, and networks cannot collide with local Cookbook, Ganteva, WordPress, or future production; verify the resolved Compose config and static guardrails.

---

## Files and Responsibilities

- Create `.github/workflows/tests.yml`: reusable hosted test suite for PRs, branch pushes, and workflow reuse.
- Create `.github/workflows/smoke-test.yml`: build runtime image, launch against disposable MariaDB, exercise migration/readiness and backup/restore drill, optionally publish immutable SHA image only on trusted dev run.
- Create `.github/workflows/deploy-dev.yml`: orchestrate tests → image smoke test/publish → beta deploy on `[self-hosted, dev-workstation]`; ensure checks fail closed and announce deployed SHA.
- Do not create an active production workflow. Document the future `[self-hosted, prod-fedora]` promotion as a later owner-authorized phase.
- Create `docker-compose.beta.yml`: isolated beta DB/app services, project-specific persistent paths, dedicated env file, private app port and health dependencies; no fixed container names.
- Create `bin/deploy-beta.ps1`: fixed Windows-host entry point matching Ganteva's `dev-workstation` runner. It validates a full 40-character SHA, serializes deployment with an exclusive file handle, records old image and unique backup path, backs up paired DB/media, pulls the exact image, starts/migrates it, checks readiness/public route, and leaves app offline on failure without destructive restore.
- Create or update `docs/deployment-beta.md`: prerequisites and operator runbook for host provisioning, initial data seed, secrets, Compose config validation, deploy, backup, failure recovery, proxy/DNS inputs, and enabling workflow only after target verification.
- Create focused static/config tests under `tests/deployment/` for Compose isolation and deploy input/failure safety where project test conventions support them.
- Modify `.gitignore` only if needed to exclude beta env/data artifacts without weakening current local protections.

## Task 1: Add Hosted Test Workflow

**Files:**
- Create: `.github/workflows/tests.yml`
- Modify: `pyproject.toml` only if the existing test invocation requires it (avoid dependency changes)

**Interfaces:**
- Produces reusable `workflow_call` plus PR checks for targets `dev` and `main`, push checks for `dev` and `main`, and a manual dispatch.
- Runs Python 3.12 using `uv.lock` and project dev dependency group; runs against disposable MariaDB 11.
- No secrets, package write permission, or self-hosted runner.

- [ ] Inspect existing test markers and MariaDB integration fixtures; identify the supported DB env vars and Task 20 schema guard without reading `.env`.
- [ ] Add workflow triggers for PRs into `dev`/`main`, pushes to `dev`/`main`, manual dispatch, and reusable workflow calls.
- [ ] Add a clean `ubuntu-latest` job with least-privilege `contents: read`, uv setup from lock file, MariaDB 11 service health check, project test suite, and concurrency cancellation by ref.
- [ ] Validate workflow syntax and confirm no secrets/self-hosted labels/package write access appear in this workflow.
- [ ] Run local equivalent: `uv run --locked --group dev pytest`; expected: all repository tests pass.

## Task 2: Add Runtime Image Smoke Test and Immutable Publishing

**Files:**
- Create: `.github/workflows/smoke-test.yml`
- Modify: `Dockerfile` only if image cannot run its existing health/backup commands.

**Interfaces:**
- Reusable input: boolean `push_image` (default false); publishes `ghcr.io/<owner>/cookbook:sha-<full-github-sha>` only when explicitly enabled from trusted `dev` flow.
- Builds one runtime image; uses disposable MariaDB 11 and isolated media; waits on `/readyz`, tests `/healthz` and a public route, and executes Task 20 backup/restore drill under `cookbook_task20_` DB guard.
- Workflow call exposes image reference/digest to dependent deploy job; no floating tag used for deploy.

- [ ] Write smoke-test workflow or script-level checks proving the image applies migrations and reaches ready state on a fresh disposable DB.
- [ ] Run backup, restore-to-new-target, and restored public recipe request using disposable schema/media only; assert seeded recipe and media survive.
- [ ] Run failure cases for invalid schema name and failed readiness; expected: test fails before publishing.
- [ ] Build image in GitHub-hosted runner; grant package write only to publish-capable invocation and use GitHub token package permissions if repository settings support GHCR publishing.
- [ ] Publish only immutable full-SHA tag on trusted dev pipeline; do not publish or promote from PR/fork or `main`.
- [ ] Verify a non-publishing PR invocation builds/smokes without registry login or write permission.

## Task 3: Add Isolated Beta Compose Project and Deployment Entrypoint

**Files:**
- Create: `docker-compose.beta.yml`
- Create: `bin/deploy-beta.ps1`
- Create: `tests/deployment/test_beta_compose.py`
- Create: `tests/deployment/test_deploy_beta.py`

**Interfaces:**
- Compose project: `cookbook-beta`; beta-only database/app service names; no `container_name`; explicit beta env file and beta-only binds; app image supplied as immutable `COOKBOOK_IMAGE`.
- Host command: `bin/deploy-beta.ps1 <40-char-hex-sha>`; rejects all other arity/format before side effects.
- Deployment uses a lock file held with an exclusive .NET file handle, private GHCR pull auth already configured on host, explicit backup directory, and Compose project `cookbook-beta`.
- Backup invokes `cookbook backup --out <unique-dir>` inside the current beta app image with beta DB/media configuration. Restore remains manual and requires explicit `--replace` and matching paired artifacts.

- [x] Add Compose with separate MariaDB 11 service, app dependency on healthy DB, no fixed container names, beta env file, beta-only binds and app port, and required immutable image override.
- [x] Add static tests: resolved service set is beta-only; no local/Ganteva container names; persistent paths do not resolve to local defaults; no port 8000/default collision; image is required/pinned; secrets are env-file sourced not committed.
- [x] Add deploy entrypoint argument tests: wrong count and short/nonhex SHA rejected before invoking Docker; valid SHA selects only its matching GHCR image.
- [x] Implement serialized deployment: confirm beta stack exists and is healthy; record current image reference; stop app; make unique paired DB/media backup; verify backup manifest; pull SHA image; recreate beta app with image SHA; wait for `/readyz`, `/healthz`, and configured public route.
- [x] On any failure after stopping app, keep app stopped and retain backup plus prior image reference; do not restore, delete images/backups, alter proxy, touch WordPress, or deploy production.
- [x] Test backup failure, image-pull failure, app migration failure, and readiness failure each preserve backup/prior image and leave beta app stopped.
- [x] Run static safety tests; expected: all path/name/image/entrypoint and failure-contract assertions pass.

## Task 4: Wire Dev Pipeline to the Existing Ganteva Dev Runner

**Files:**
- Create: `.github/workflows/deploy-dev.yml`
- Modify reusable workflows only to satisfy their contracts.

**Interfaces:**
- Trigger on trusted `push` to `dev` and optional manual dispatch with protected branch/ref check.
- Job chain: tests → image smoke/build/publish SHA → beta deploy. Deploy runs only on `[self-hosted, dev-workstation]`.
- Deploy checks out only pinned Compose/runbook files for the same commit and passes validated full SHA to fixed entrypoint; no arbitrary workflow inputs or shell.
- `main` and PR checks never deploy. No prod runner label, environment, secret, or workflow.
- Beta deployments serialize using repository-level concurrency with cancel-in-progress false.

- [x] Create pipeline with reusable test and smoke jobs, dependencies, and least-privilege permissions.
- [x] Require a trusted push to `dev` and a successful publish job before deployment; forks cannot select the self-hosted runner.
- [x] Deploy job runs on `[self-hosted, dev-workstation]`, pulls exact immutable SHA image, and invokes only `bin/deploy-beta.ps1` with the known full SHA.
- [x] Use the native GitHub Actions job conclusion for success/failure status; no external notification secret or webhook is configured.
- [x] Verify `main` push and PR events do not select deploy; verify missing package, missing host prerequisites, and invalid SHA fail closed.
- [x] Keep deployment behind the closed-by-default `COOKBOOK_BETA_ENABLED` repository variable until runner identity, host isolation, capacity, storage paths, proxy port/network, and secrets are verified; workflow tests assert the gate.

## Task 5: Beta Provisioning and Recovery Runbook

**Files:**
- Create: `docs/deployment-beta.md`
- Modify: `README.md` with a brief pointer only if useful.

**Interfaces:**
- Operator steps cover dev runner suitability/isolation; GHCR read credentials; beta paths/env; initial data seed; owner account recreation/signing key rotation; proxy and DNS setup; dry-run/first deployment; SHA and backup tracking; retention; manual recovery.
- No DNS/proxy mutation commands are assumed; owner records actual reverse proxy/network/port and trusted CIDRs before beta becomes public.

- [x] Document exact secrets/variables by name without values, required GitHub permissions and branch protections, and runner prerequisites.
- [x] Document validation of resolved beta Compose config and proof no resource/path/port collision with WordPress, Ganteva, or local Cookbook.
- [x] Document initial beta data copy using Task 20 backup/restore and explicit owner credential recreation/signing key rotation.
- [x] Document deployment and recovery, including paired DB/media restore with `--replace`, previous immutable image, and readiness verification.
- [x] Add an enablement checklist requiring runner/host and proxy/DNS verification; state `main` does not deploy and prod remains inactive.
- [x] Review runbook for secrets/dump/media artifact leakage; expected: no live values or source data.

## Task 6: End-to-End CI/CD Acceptance

**Files:**
- Modify only targeted workflow/scripts/docs/tests from prior tasks.

**Interfaces:**
- One proven PR-check to trusted `dev` SHA-image-deployment path, plus checks-only `main` path.
- Acceptance report states local simulation limits; no actual deploy until host prerequisites are confirmed.

- [x] Parse workflow YAML and inspect triggers, permissions, dependencies, runner labels, and concurrency. (`actionlint` is not installed locally; YAML parsing and focused structural tests were used.)
- [x] Run deployment static tests and full suite: deployment tests pass; full suite passes against disposable MariaDB from a clean snapshot without local `.env`.
- [x] Build image and run disposable MariaDB migration/readiness plus Task 20 backup/restore/public-recipe drill; verified during Task 2 without using local `.env` or development DB.
- [x] Verify `git diff --check`; verify no secrets, WordPress exports/import reports, DB dumps, or media archives are staged.
- [x] Prepare review summary of required GitHub/host configuration and deployment gate status in `docs/deployment-beta.md`.
- [x] Add dependency-audit-only security check to reusable hosted checks; update only vulnerable `certifi` and `opentelemetry-api` lock entries and confirm the audit is clean.

The hosted GitHub Actions path has not yet been triggered from a pushed `dev`
commit. It will run on the next authorized push. Beta deployment remains
disabled until the runbook's runner and proxy prerequisites are verified and
`COOKBOOK_BETA_ENABLED` is deliberately set to `true`.

## Handoff

- This plan authorizes design/plan preparation only. Present this plan for review before implementation.
- The owner’s established implementation method is Qwen/OpenCode in a fresh session after each completed subtask. Give Qwen one numbered task brief at a time, then inspect its diff, run checks, and proceed.
- Do not commit or push without the owner’s next instruction. Never merge to `dev` or enable the self-hosted deploy job as part of local implementation.
