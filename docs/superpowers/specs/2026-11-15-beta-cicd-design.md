# Beta CI/CD Pipeline Design

## Intent

Let external authors test the cookbook app at `cookbook-dev.maynardfolks.com` while the existing WordPress site remains live at its current public address. Automate code checks and beta delivery without giving CI a path to the live WordPress deployment or future cookbook production environment.

## Goals

- Develop and test locally, then merge approved work into the permanent `dev` branch.
- Run the full automated test suite and build the runtime image for changes on `dev`; run CI checks on `main` without deploying it for now.
- Deploy a successful `dev` commit to a persistent beta stack beside WordPress through the existing reverse proxy, using the same GitHub self-hosted development runner as Ganteva.
- Preserve beta recipes, media, accounts, and author edits across application deployments.
- Keep each environment's database, media, signing key, and deployment identity separate.
- Keep a verified image identifiable by immutable commit SHA so a future production promotion can use the exact artifact tested on beta.
- Use the Task 20 backup/restore commands to create a recovery point before beta schema migrations.
- Leave WordPress serving production traffic; do not deploy Cookbook to production or automate production cutover until the owner explicitly starts production releases.

## Non-goals

- Deploying the cookbook to production, changing `cookbook.maynardfolks.com`, or stopping/reconfiguring WordPress.
- Automating DNS or reverse-proxy configuration. The owner will publish the beta DNS record and route HTTPS traffic to the beta app.
- Replacing the existing local Docker Compose development workflow.
- Adding per-PR preview environments, high availability, a load balancer, a monitoring stack, email/account invitations, or a new user-management UI.
- Syncing tester edits to WordPress, local development, or future production.

## Owner decisions and proposed implementation calls

**Owner decisions recorded:**

- The beta hostname is `cookbook-dev.maynardfolks.com` and will share the existing reverse proxy with the live WordPress site.
- The beta database and media are independent from WordPress and any future production app.
- Beta starts from a separate copy of the imported recipe collection. Tester edits persist between application deployments.
- `dev` is the permanent development branch; `main` remains the stable integration branch.

**Proposed calls for owner review:**

- Use GitHub Actions, following the established `ganteva-gear-rental` pipeline.
- Run tests, security checks, and image build/smoke checks on GitHub-hosted runners. The deploy job runs on Ganteva's existing `[self-hosted, dev-workstation]` runner; confirm that runner can safely host the separate Cookbook beta Compose project and has the required disk, network, and secrets before enabling deployment.
- Publish the successfully checked image as a private GHCR package with an immutable full-commit-SHA tag. The dev runner pulls and deploys that exact image; it does not rebuild on the host or resolve a floating tag. This follows Ganteva's build-on-hosted-runner, deploy-on-self-hosted-runner, and immutable image promotion pattern.
- Merge day-to-day work into `dev`. A successful push to `dev` runs required checks, builds and smoke-tests the image, publishes it, and deploys beta on the shared Ganteva dev runner. PRs to `dev`/`main` run checks but receive no deployment credentials. CI on `main` does not deploy.
- Reserve Ganteva's `[self-hosted, prod-fedora]` runner for a future Cookbook production environment. There is no active Cookbook production deploy trigger now: merging to `main` must not deploy, and the production workflow must remain disabled or manual-only behind an explicit future owner decision. When production begins, it should promote the exact immutable image previously tested on beta, rather than rebuilding it.
- Keep the initial beta backup before each deployment that may run migrations. Stop the beta app before backup so application writes are quiet; place each backup set in a unique deployment-specific directory. If migration or readiness fails, keep beta offline, retain the previous image and backup, and require owner-directed recovery rather than automatically replacing tester data.
- Retain beta backups until the owner sets a storage/retention policy; do not silently delete recovery sets in the first version of the deploy workflow.

The deploy job uses the existing `[self-hosted, dev-workstation]` runner as Ganteva does. It may run a fixed deployment entry point that accepts only a full commit SHA and performs the backup/pull/restart/readiness sequence; avoid accepting arbitrary shell commands or Compose arguments from workflow inputs. Confirm the runner's current isolation, repository access, and credentials before adding Cookbook deployment steps. If a deployment fails, the operator will keep beta offline, restore the recorded pre-deploy database/media set with explicit replacement targets, restart the recorded prior image digest, and verify readiness before reopening beta. No automatic destructive restore is performed.

## Environments and flow

| Environment | Purpose | Data | Trigger |
|---|---|---|---|
| Local | Owner development through current Compose workflow | Local disposable MariaDB and persistent local media | Manual |
| CI | Test changes and build a candidate image | Fresh MariaDB service/container; no production or beta data | Pull request to `dev`/`main`, push to `dev`/`main` |
| Beta | External author testing at `cookbook-dev.maynardfolks.com` | Persistent copied recipe dataset and beta-only author changes | Successful `dev` pipeline, deployed on `[self-hosted, dev-workstation]` |
| Production | Future Cookbook production environment, currently inactive | Separate production DB/media and secrets; never WordPress data | No current trigger; future deployment reserved for `[self-hosted, prod-fedora]` after explicit owner decision |

CI must run without deployment credentials. It installs the locked dependencies, runs the full test suite against MariaDB 11, builds the runtime image, and runs the packaged-client backup/restore integration drill against disposable schemas. Keep integration URLs restricted to the documented `cookbook_task20_` test schema guard.

Only the trusted `dev` pipeline's publish/deploy jobs receive package-write or beta deployment credentials. The dev deploy job uses the established `[self-hosted, dev-workstation]` runner, and deploys the exact image identified by the full commit SHA after hosted checks and smoke tests pass. Serialize beta deployments so overlapping pushes cannot race database migrations. Pull requests, including any from forks, must not receive registry write access or host credentials. Pin third-party workflow actions to reviewed commit SHAs and give each job only the GitHub token permissions it needs. Do not grant Cookbook a production runner label or production credentials in this initial rollout.

## Beta host and data

- Run beta as a separate Compose project with a project-specific name and no fixed container names shared with local development or WordPress.
- Give beta its own MariaDB service/schema, persistent database storage, media directory, signing key, and app configuration. Keep the beta app's published upstream port distinct from other apps and limit access to the existing reverse proxy.
- Use a host data path configured specifically for beta; never inherit the local dev `COOKBOOK_DATA_DIR` by accident.
- The owner will create the DNS record and HTTPS proxy route before author invitations. Record the actual proxy software, network attachment, upstream port, and `TRUSTED_PROXY_CIDRS` during implementation; the repository does not identify them, so the workflow must not guess or mutate proxy configuration.
- Seed the beta database and media once from a protected copy of the imported cookbook data using Task 20 backup/restore. Recreate beta owner/editor accounts and rotate the beta signing key after restore so existing local sessions and credentials are not reused. Do not connect beta to the WordPress database.
- Author account creation and removal remain owner-controlled through the app's existing admin path. The pipeline does not create or provision tester accounts.

## Deployment and recovery behavior

1. CI passes for a pushed `dev` commit and publishes one immutable image to GHCR.
2. The beta deploy job runs on the existing Ganteva `[self-hosted, dev-workstation]` runner and calls the restricted `deploy-beta <40-character-commit-SHA>` entry point. It cannot submit a shell command or arbitrary compose arguments. Confirm the runner is the intended shared development host and can isolate Cookbook from Ganteva and WordPress before enabling this job.
3. The host serializes the update, stops the beta app to stop writes, writes a paired database/media backup to a unique directory, pulls the exact image, and starts beta. The normal entrypoint applies forward Alembic migrations.
4. The deploy job waits for `/readyz` and checks `/healthz` plus a small public route. It reports the deployed SHA and backup location.
5. If startup, migration, or smoke checks fail, leave the beta app offline, keep the prior image and pre-deploy backup, and notify the owner. Do not automatically run destructive `restore --replace`; the owner restores the recorded DB/media set, restarts the previous image digest, then verifies readiness before allowing testers back in.

Beta downtime during deployment is acceptable for this single-host tester environment. Beta content is durable, so the backup must be complete before a migration attempt. Rollback to an older image alone is not guaranteed to work after a forward schema migration; restore the paired pre-deploy database and media backup before restarting an older image.

## Security and operations

- Keep workflow test jobs secret-free. Scope GHCR write permission to the image publishing job and deploy credentials to the trusted dev deploy job. The dev host uses a dedicated read-only GHCR credential to pull the private package.
- The shared dev runner executes repository-controlled deployment code. Protect the `dev` branch and restrict who can alter or approve deployment workflow changes; do not allow untrusted pull request code to execute on the self-hosted runner. Public author traffic uses the existing reverse proxy and beta hostname, not runner access.
- Keep the production runner label `[self-hosted, prod-fedora]` and production credentials out of Cookbook's active workflows until production is explicitly authorized. `main` is a code integration branch only for this phase.
- Keep beta's environment variables and secret files separate from local dev, WordPress, and future production. Never put `.env`, database dumps, media archives, signing keys, or test account passwords in GitHub artifacts or the repository.
- The beta app starts migrations automatically, so a green unit suite is not enough: the integration job must exercise the runtime image and migration path before a beta deploy.
- This remains a one-host design. A host, network, or reverse-proxy outage takes beta offline; beta availability does not affect WordPress if service ports, storage, and databases are isolated.

## Acceptance criteria

- A pull request or push to `dev`/`main` runs required checks with MariaDB 11, builds the runtime image, and executes the packaged backup/restore drill without touching the dev or WordPress database.
- A green push to `dev` deploys the commit-SHA image to the beta stack on the same `[self-hosted, dev-workstation]` runner used by Ganteva and confirms readiness/public-route smoke checks.
- A push or merge to `main` runs checks only; it does not deploy to beta or production.
- Cookbook has no active production deploy workflow or production secrets; the future production target is reserved for `[self-hosted, prod-fedora]` and remains disabled until the owner opts into production releases.
- Two beta deployments never overlap; beta database/media and tester edits survive an application-only redeploy.
- A migration deployment records a unique recoverable database/media backup before it starts migrations.
- A failed beta release leaves its backup and prior image available and does not silently overwrite tester changes.
- `cookbook.maynardfolks.com`, WordPress containers/data, and future production secrets are not modified by the workflow.

## References

- [GitHub Actions secure use and self-hosted runner risks](https://docs.github.com/en/actions/reference/security/secure-use)
- [Ganteva dev deployment workflow](https://github.com/bjmaynard01/ganteva-gear-rental/blob/main/.github/workflows/deploy-dev.yml)
- [Ganteva production deployment workflow](https://github.com/bjmaynard01/ganteva-gear-rental/blob/main/.github/workflows/deploy-prod.yml)
- [Ganteva test workflow](https://github.com/bjmaynard01/ganteva-gear-rental/blob/main/.github/workflows/tests.yml)
- Repository product spec: `docs/superpowers/specs/2026-09-20-cookbook-design.md` §9
- Backup/restore contract: `docs/superpowers/specs/2026-11-09-backup-restore-design.md`
