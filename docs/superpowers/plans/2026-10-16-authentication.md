# Cookbook Authentication Implementation Plan — revised (draft v3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement slices one at a time. This document is a **revised draft**, not an approved implementation contract. It is now at **draft v3**: the first draft (`f96eea1`) was revised after a Codex review, and this v3 pass corrects the Codex findings logged 2026-10-16 (browser-bound CSRF, inventory arithmetic, complete schema shapes, the lockout state machine, proxy/cookie consistency, concurrency contracts, working PowerShell verification, and the draft/approval status labels). The remaining proposals below (O-a…O-f) are **recommendations, not approvals**: the owner has NOT yet approved any of them. Stop after drafting; no implementation, push, or merge.

**Goal:** Deliver spec §4 authentication & authorization — server-side MariaDB sessions (raw token never stored), argon2id passwords, `editor`/`admin` roles, one-time `/setup` + headless CLI bootstrap, CSRF on every mutation, per-IP+username lockout — against an isolated MariaDB, without advancing the running dev stack.

**Spec:** `docs/superpowers/specs/2026-09-20-cookbook-design.md` — D9/D10 (auth/roles, §6.1–6.7), D18 (proxy-agnostic forwarded headers, §9.2), D20 (file-first secrets, §9.3), D23 (forward-only Alembic).

**Baseline (verified 2026-10-16):** branch `task/7-auth-plan` @ `8f7e8ce` (this draft v3, following draft v2 `f96eea1`); `main` @ `57d252d` with the completed Task 6 data model; `alembic` head `b7a2f9c4d1e8` (v5); last independently verified suite: **89 passing tests**; `argon2-cffi` already a dependency; no `sessions`/`login_attempts` tables, no auth routes, no CLI entry point. uvicorn **0.54.0** installed; `.venv\Scripts\` exposes `alembic.exe`, `pytest.exe`, `uvicorn.exe`.

## Shared contract (applies to every slice)

### Isolated test database (binding, reused from the data-model plan)

**Do not run any migration or test against the running dev `db` container, the dev `cookbook` schema, or the dev `app` stack while a slice is unmerged.** Every MariaDB step runs against the throwaway container `cookbook-test-db` on `127.0.0.1:33061`.

PowerShell (this repo, Windows). Notes on the environment this actually runs in:

- This desktop **does not have the Compose `.env`** the `docker compose …` snippets need, so **do not snapshot the dev stack with `docker compose ps`**. Instead snapshot each dev container by name with `docker ps` / `docker inspect` (stable identity/running/start-time data — **not** the human-readable `Up N hours` strings that change every second and cannot be compared textually).
- All temp files go under `$env:TEMP` (or `.venv`), never `/tmp` (there is no `/tmp` on this Windows host).
- The verification process gets **fresh throwaway credentials** (`devrootpassword`/`devpassword` below). It must **not** read `.env` and must **not** carry the dev `SECRET_KEY`/`DB_PASSWORD` into the test environment: clear `SECRET_KEY_FILE` and `DB_PASSWORD_FILE` so pydantic-settings cannot pull a real secret in via a file.
- Every command's **exit code is checked** before the next step; a failed `docker run`/`alembic`/`pytest` throws instead of cascading.

```powershell
# 0) Temp workspace (Windows) — never /tmp
$tmp = Join-Path $env:TEMP "cookbook-auth-verify"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
$before = Join-Path $tmp "devstack-before.json"
$after  = Join-Path $tmp "devstack-after.json"

# 1) Pre-flight: snapshot the dev containers that SHOULD exist, by stable fields.
#    (docker ps / inspect — NOT `docker compose ps`, whose .env this desktop lacks.)
#    A container is identified by name; we record running state + start epoch,
#    which are stable, rather than the per-second "Up N hours" text.
$devContainers = @("cookbook_app_1", "cookbook_db_1")   # adjust to the live dev names
foreach ($name in $devContainers) {
  $st = docker inspect $name --format '{{.Name}} {{.State.Status}} {{.State.StartedAt}}'
  if ($LASTEXITCODE -ne 0) { $st = "$name <absent>" }
  $st
} | Set-Content -Encoding utf8 $before

# 2) Start the disposable test DB only if the name is free (explicit tmpfs, no anonymous volume)
if (docker ps -a --format '{{.Names}}' | Select-String -SimpleMatch 'cookbook-test-db') {
  throw "cookbook-test-db already exists — stop it first (only THIS task's container is ours)"
}
docker run --rm -d --name cookbook-test-db --tmpfs /var/lib/mysql:size=1g `
  --health-cmd="healthcheck.sh --connect --innodb_initialized" `
  --health-interval=3s --health-timeout=3s --health-retries=24 `
  -e MARIADB_ROOT_PASSWORD=devrootpassword -e MARIADB_DATABASE=cookbook `
  -e MARIADB_USER=cookbook -e MARIADB_PASSWORD=devpassword `
  -p 127.0.0.1:33061:3306 mariadb:11
if ($LASTEXITCODE -ne 0) { throw "docker run cookbook-test-db failed" }

# 3) Wait for healthy: capture the status explicitly, compare with -eq 'healthy',
#    and THROW on timeout/unhealthy (no undefined $LastMatch, no silent fall-through).
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
  $status = docker inspect cookbook-test-db --format '{{.State.Health.Status}}'
  if ($LASTEXITCODE -ne 0) { throw "docker inspect failed while waiting for health" }
  if ($status -eq 'healthy') { $healthy = $true; break }
  Start-Sleep -Seconds 2
}
if (-not $healthy) {
  $status = docker inspect cookbook-test-db --format '{{.State.Health.Status}}'
  throw "cookbook-test-db did not become healthy; last status = '$status'"
}
```

Env for alembic/pytest (fresh throwaway values; clear the `*_FILE` overrides so no real secret can be inherited; explicit env vars beat the local `.env` in pydantic-settings precedence):

```powershell
Remove-Item Env:\SECRET_KEY_FILE   -ErrorAction SilentlyContinue
Remove-Item Env:\DB_PASSWORD_FILE  -ErrorAction SilentlyContinue
$env:DEBUG="false"; $env:SECRET_KEY="devsecretkey"
$env:DB_HOST="127.0.0.1"; $env:DB_PORT="33061"; $env:DB_NAME="cookbook"
$env:DB_USER="cookbook"; $env:DB_PASSWORD="devpassword"

& .venv\Scripts\alembic.exe upgrade head
if ($LASTEXITCODE -ne 0) { throw "alembic upgrade head failed" }
& .venv\Scripts\pytest.exe tests -v
if ($LASTEXITCODE -ne 0) { throw "pytest failed" }
```

Tear-down (end of every slice, before commit) — stop **only** the container this task created, then re-snapshot and compare the **stable** dev-container fields:

```powershell
docker stop cookbook-test-db            # ONLY this task's throwaway container
if ($LASTEXITCODE -ne 0) { throw "docker stop cookbook-test-db failed" }
foreach ($name in $devContainers) {
  $st = docker inspect $name --format '{{.Name}} {{.State.Status}} {{.State.StartedAt}}'
  if ($LASTEXITCODE -ne 0) { $st = "$name <absent>" }
  $st
} | Set-Content -Encoding utf8 $after

# Assign the comparison result, THEN check it (no undefined $diff).
$devDiff = Compare-Object (Get-Content $before) (Get-Content $after)
if ($null -eq $devDiff) { "dev stack untouched" }
else { throw "dev stack CHANGED:$($devDiff | Out-String)" }
```

The dev `app`/`db` containers may be running or stopped — either state is fine; the comparison must show whatever it was, still the same (same name, status, and start time). Never `docker compose down`.

### Assertion, slicing, and verification conventions

- **Types asserted exactly as MariaDB 11 reports** in `information_schema.columns.column_type` (`char(64)`, `char(32)`, `varchar(36)`, `datetime`, …). Engine pinned to `mariadb:11`. If an assertion fails, the migration diverged: fix the migration, never the test.
- **Indexes as complete ordered column lists** via `information_schema.statistics` (pattern in `tests/test_schema_inventory.py`).
- **Async DB tests are sync test functions wrapping `asyncio.run(run())`** (no pytest-asyncio dependency — existing convention), with cleanup in `finally` and pool `dispose()` exactly once per test.
- **Route tests** use `httpx` ASGI transport against `create_app()` (in-process, no server); controlled addresses `192.0.2.1` (trusted) and `198.51.100.1` (untrusted) stand in for real proxy/client IPs — no deployment CIDR needed for tests.
- **Slice size:** each slice is one fresh-session-sized unit: read this plan + the named spec sections → implement → test → report. Slices land on their own task branch, are reviewed, and merge only on explicit owner approval. Never push or merge without direction.
- **No placeholder recipe CRUD.** Until real application surfaces exist, permission-gate tests attach a *controlled test router* (see S8) to the app instance in the test module. Nothing for gate-verification is added under `src/`.

## Requirements (carried from draft v1, corrected where noted)

> Draft-status note: the *behavioral* requirements below are carried from draft v1 and unchanged. The **schema shapes** are made fully explicit and self-consistent in the **v3 complete-contract** sections that follow (finding 3) — `sessions` in **C0.1**, `auth_locks` in **C0.2**, `login_attempts` in **C0.3/C1.2** — and those sections are the source of truth for the S1/S6/S7 implementations. Where a contract was *partially* specified (e.g. `sessions`) in v1/v2, it is now **fully** specified before any slice is allowed to treat it as locked.

- **R1 Sessions (§6.1, §3.5):** raw token issued once, never stored; only its SHA-256 hash (`id_hash CHAR(64)` PK) is the row key — the full `sessions` shape is in **C0.1** (complete columns, nullability, defaults, FK, indexes, FK name, timestamp semantics), not re-derivable from prose alone. Cookie `__Host-cookbook_session` (see §Cookie contract below). Sliding `SESSION_TTL_HOURS` (default 12) idle TTL; expiry and revocation are row deletes. `POST /logout` deletes the row; password change invalidates all of the user's sessions; disabled users' sessions die at next request.
- **R2 Passwords (§6.2):** argon2id via `argon2-cffi` at or above OWASP minimum (m=19 MiB, t=2, p=1). Constant-time verify against a dummy hash for unknown usernames.
- **R3 Roles (§6.3, D9/D10):** public = read only; editor = recipe/category/tag/ingest; admin = +aliases, importer, user management. No per-recipe ownership.
- **R4 Bootstrap (§6.4):** while zero users exist, `/setup` is the only reachable auth surface; after first creation `GET/POST /setup` return 403 forever. Headless `cookbook setup` shares the single-user guard.
- **R5 Account lifecycle (§6.5):** admin creates editors; disable = `is_active FALSE` + session wipe; reset via CLI and admin route; the last active admin cannot be disabled/demoted (count active admins, not rows); no self-signup.
- **R6 Hardening (§6.6):** per-IP+username lockout backed by `login_attempts` (design below); CSRF required on every mutation (design below); public read `Cache-Control: public, max-age=300, s-maxage=600`, everything behind login `private, no-store`.
- **R7 Proxy-agnostic (§9.2, D18):** client IP + scheme from forwarded headers **only when the direct peer is trusted** (design below).

## Contracts (the six concrete designs)

> **Complete-contract convention (finding 3).** A contract may be referenced as *locked* by a slice only when **every** column is explicitly pinned: type (exactly as MariaDB 11 reports it in `information_schema.columns.column_type`), nullability, default, and — for identity columns — the FK name, the index/column-list, and the timestamp semantics. Below, `sessions`, `login_attempts`, and `auth_locks` are each given in that fully-explicit form. Anything that is a shape detail (as opposed to a *policy* detail) is therefore not left for an implementer to invent; the policy items that remain open are the flagged proposals in the owner-decisions table (O-a…O-f), which are **recommendations, not approvals**.

### C0.1 — `sessions` (complete, explicit contract) — the R1 store

All times are **server UTC** (the DB server clock, not `NOW()` string arithmetic; the app passes one `DateTime` per statement so a single write transaction is internally consistent). Types as `column_type`. `user_id` references `users.id` (a v0 column that already exists — no `users` change).

| column | type | nullability / default | notes |
|---|---|---|---|
| `id_hash` | CHAR(64) | NOT NULL, **PK** | SHA-256 of the raw token, hex (64 chars). The raw token is issued once and **never** stored; this hash is the only row key. |
| `user_id` | VARCHAR(36) | NOT NULL | FK `fk_sessions_user` → `users.id`, **`ON DELETE CASCADE`**. (The `users.id` UUID is app-generated, so the CASCADE target is a real app row.) |
| `csrf_token` | CHAR(32) | NOT NULL | per-session CSRF token (random 16 bytes hex). Generated at session creation; deleted with the row. |
| `created_at` | DATETIME(6) | NOT NULL | set at insert (app-provided). Session born. |
| `last_seen_at` | DATETIME(6) | NULL | last authenticated request that re-validated the session; set at insert = `created_at`, then updated to the request's `now` on each slide. |

- **Sliding TTL semantics:** a session is *valid* iff `last_seen_at + SESSION_TTL_HOURS ≥ now`. On load: if expired → **row DELETE** (revocation is a row delete, per R1) and the request is anonymous. Otherwise `last_seen_at = now` (UPDATE) and the session proceeds. `SESSION_TTL_HOURS` default 12 (spec §9.4; config).
- **Keys/indexes:** `PRIMARY KEY (id_hash)`; `CONSTRAINT fk_sessions_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE`; `INDEX ix_sessions_user_id (user_id)` (support `revoke_user_sessions(user_id)`).
- **Engine / charset:** InnoDB, `utf8mb4` (the repo-wide default).
- **`users` is untouched** (no `failed_logins`/`locked_until` — see C1).

### C0.2 — `auth_locks` (complete, explicit contract) — the C5 lock rows

| column | type | nullability / default | notes |
|---|---|---|---|
| `name` | VARCHAR(40) | NOT NULL, **PK** | logical lock name; no other columns. |

- **Keys:** `PRIMARY KEY (name)`.
- **Seeded rows (in the v6 revision, fixed set):** `('first_admin')`, `('last_admin')`. The table exists only as something for `SELECT … FOR UPDATE` to lock when the guarded condition is about *zero or one* `users` rows (see C5); no other purpose.
- **Engine / charset:** InnoDB, `utf8mb4`.

### C0.3 — `login_attempts` (complete, explicit contract)

The authoritative `login_attempts` shape is the **shape table at the top of C1** (immediately below): `id VARCHAR(36)` PK, `username VARCHAR(60)`, `ip VARCHAR(45)`, `attempts INT`, `first_failed_at`/`last_failed_at` DATETIME, `lock_count INT`, `locked_until DATETIME NULL`, `PRIMARY KEY (id)`, `UNIQUE KEY uq_login_attempts_pair (username, ip)`, `INDEX ix_login_attempts_locked (locked_until)`, InnoDB/utf8mb4. C1.2 below re-states the *exact same* shape as the locked contract for S1/S6 to implement against. Nothing in C0 adds to or removes from that shape.

### C1 — Lockout: `login_attempts` schema and state machine

**Spec conflict, resolved explicitly.** §6.6 says "Counters in `users.failed_logins` / `users.locked_until`, keyed loosely by IP+username via a small `login_attempts` table (the spec keeps it to the two `users` columns + the table...)". §3.5 lists `login_attempts` as the lockout store and says "The `users.failed_logins`/`locked_until` columns are dropped; this table is the lockout store." **Resolution: §3.5 wins** — it is the specific, self-consistent table definition, and the v0 `users` migration (already on `main`) contains neither column. This plan adds **no** `users.failed_logins` / `users.locked_until`; the §6.6 parenthetical is read as stale wording. No `users` schema change in v6.

**C1.2 — Locked `login_attempts` shape (one row per normalized (username, ip) pair):**

| column | type | nullability / default |
|---|---|---|
| `id` | VARCHAR(36) | PK NOT NULL (UUIDv4, app-generated — keeps the DL-5 invariant clean; rejects the draft's "int?" option) |
| `username` | VARCHAR(60) | NOT NULL — **lowercased at write time** (normalized form is the stored form) |
| `ip` | VARCHAR(45) | NOT NULL — real client IP from the C4 identity helper (IPv4 or IPv6 text) |
| `attempts` | INT | NOT NULL DEFAULT 0 — failures in the current window |
| `first_failed_at` | DATETIME | NOT NULL — start of the current failure window |
| `last_failed_at` | DATETIME | NOT NULL — most recent failure in the current window |
| `lock_count` | INT | NOT NULL DEFAULT 0 — completed lock episodes for this pair (drives backoff tier) |
| `locked_until` | DATETIME | NULL — NULL = unlocked |

Keys/indexes: `PRIMARY KEY (id)`; `UNIQUE KEY uq_login_attempts_pair (username, ip)` — the pair key; `INDEX ix_login_attempts_locked (locked_until)` for optional expiry scans. Engine InnoDB / utf8mb4.

**Canonicalization (define before the state machine).** Both pair coordinates are canonicalized **at write time and at lookup time**, by one shared helper, so that "the pair" is well-defined:

- **username:** the stored form is the *lowercased* form (`unicode.lower()` of the submitted username, then trimmed). The canonical form is what the `uq_login_attempts_pair` key, the app's lowercasing, and the DB's `utf8mb4_*_ci` collation all agree on (see the S1 collation note below). **The canonical form must agree with the case-insensitive username lookup used by login** — i.e. `Login("Alice")` and `login("alice")` must hit the same row. Tests assert this explicitly (a login for `ALICE` is counted against the row created by `alice`).
- **ip:** the *canonical text* of the client IP from `client_identity` (C4) — IPv4 dotted-quad, IPv6 in compact (RFC 5952) lowercase form. No ports, no brackets, no surrounding whitespace. An IPv4-mapped IPv6 (`::ffff:1.2.3.4`) is normalized to its IPv4 dotted form. This is the value used as the lockout key, so a client behind any proxy is locked by its *real* peer, and a forged-header client (untrusted, C4) is locked by its *direct* peer.

**Window and lock expiry are equality-inclusive.** A window is *still running* iff `first_failed_at + 10 min > now` (strictly greater); it is *expired* iff `first_failed_at + 10 min <= now`. A lock is *still in effect* iff `locked_until > now` (strictly greater); it is *expired* iff `locked_until <= now`. **The boundary instant (`==`) is the *free* side of each comparison.** This is fixed so the "5th failure at exactly the window edge" and "login at exactly `locked_until`" cases are deterministic under tests' clock injection.

**State machine (all times UTC server time; single row per pair, updated atomically).** Let `now` = the statement's clock (injectable, see below). On each failed login for pair (u, ip), compute the next state as a function of the current row (or the absence of one):

- **No row**, or **`locked_until` set and `locked_until <= now`** (lock expired) **and window expired** (`first_failed_at + window <= now`) → **start a new episode / new window**: `attempts = 1`, `first_failed_at = last_failed_at = now`, **`lock_count` is PRESERVED** (it only ever changes via a *new* five-failure episode reaching the tier — see below), `locked_until = NULL`.
- **`locked_until` set and `locked_until > now`** (lock still in effect) → **no field changes**: a locked pair does *not* extend its own lock while locked, and the login is refused *before* any password work (the response is `429` + `Retry-After`, recommendation O-e; the remaining-time is available so `Retry-After` can be computed). `attempts`, timestamps, `lock_count`, `locked_until` all unchanged.
- **Unlocked, `attempts < 5`, window still running** (`first_failed_at + window > now`) → `attempts += 1`, `last_failed_at = now`; no lock change.
- **Unlocked, `attempts < 5`, window expired** (`first_failed_at + window <= now`) → **reset the window, keep the tier**: `attempts = 1`, `first_failed_at = last_failed_at = now`, `locked_until = NULL`; `lock_count` **unchanged** (a tier-2 pair that fails again after the window expires still sits at tier 2 and will lock at 5 with the tier-2 duration).
- **`attempts` reaches 5** (this is the transition **into** a lock for the current episode): `lock_count += 1`, `locked_until = now + duration(lock_count)`; `attempts`, `first_failed_at`, `last_failed_at` are left as-is until the next window/lock decision. This is the only transition that increments `lock_count`.

On **successful login** for the pair: **DELETE the row** entirely (simplest; matches the "revocation is a row delete" idiom used for sessions). `attempts`, `first_failed_at`, `last_failed_at`, `lock_count`, and `locked_until` all reset with the row. This is the **full reset on success** policy (O-a, a recommendation, not an approval).

**Lock duration (backoff).** `duration(k) = min(60 min, 10 min * 2^(k-1))` for episode `k = lock_count`: 10, 20, 40, then **capped at 60 min** for every `k ≥ 4` (episode 3's next is already 60). The curve and the cap are the O-a recommendation (spec §6.6 gives only "exponential backoff, cap 1 h").

**Expiry / purge.** Locks and windows are time-based; **no background purge in v1** (rows are tiny and self-contained; a purge job is an additive later concern). Equality boundaries above make expiry deterministic without a purge.

**Concurrency: failure recording vs. successful-login reset.** The two writes are **not** serialized by "one upsert fixes the whole race" — the review explicitly rejects that claim. The contract is:

- **Failure recording** = one `INSERT … ON DUPLICATE KEY UPDATE` keyed by `uq_login_attempts_pair`, computing every column from the current row + `now` in a single statement (no read-modify-write across round trips). Concurrent failures on the same pair serialize on the unique key (MariaDB enforces the `uq_` pair uniqueness; the loser of an insert race blocks, then sees the winner's row and re-applies its update). This makes the failure path atomic *against other failures on the same pair*.
- **Successful-login reset** = `DELETE FROM login_attempts WHERE (username, ip) = (u, ip)`, run in the *authenticated* transaction. It is a different statement on the same pair: a failure recorded *after* a successful login (a stale client that raced) is simply a new row (or a no-op upsert) — it does **not** resurrect a lock we just cleared, because the reset is a row delete and the next failure re-creates the row with `lock_count` semantics per the state machine above (a fresh `attempts=1`; `lock_count` is 0 for a brand-new row).
- **The remaining race** (the *only* real race left) is a **failure recorded between the password check and the session commit** on a *correct* login. The contract's answer is: **the reset is the source of truth** — if a failure lands after our password check but before we commit, it is an *incorrect* observation (the password was right). Because the reset happens in the commit path and the failure path is a separate write, the *winner* of the commit is the successful login; a stale failure write that lands later creates a fresh `attempts=1` row, which is the correct, safe outcome (it starts a new window, it does not lock, and it does not block the user's now-valid session). So: **a successful login is never invalidated by a later stale failure** — the row-delete reset plus the fresh-window re-create is the invariant, and the tests assert exactly that (see S6 acceptance).
- Do **not** claim "one upsert solves the entire login race." The upsert solves *failure-vs-failure* serialization. *Login-vs-failure* ordering is handled by the reset-being-source-of-truth rule above. Both are needed; neither alone is the whole story.

**Injectable clock (consistent across the service and the tests).** The state machine takes `now` as a parameter of every function that reads or writes a row (`record_failure(u, ip, now)`, `assert_not_locked(u, ip, now)`, `clear_pair(u, ip)`). **Do not** call MariaDB's `NOW()` inside the state machine: that couples correctness to the DB server clock and makes the window/lock boundaries nondeterministic in tests. Instead:

- The app obtains `now` from an injected `Clock` (a callable returning a `DateTime`; production uses the real UTC clock, tests use a fixed/advancing fake). This is the **same clock** the tests use to assert window/lock expiry and the 5-failure transitions, so tests share one source of truth for "now" with the service code.
- The **DB timestamps** (`created_at`, `last_seen_at`, `first_failed_at`, `last_failed_at`, `locked_until`) are written from the supplied `now` (app-provided), not from `NOW()`. This is what makes "the DB timestamp and the test clock agree" true: the value the test asserts against is the value the test itself supplied.
- The one place `NOW()` is still acceptable is a *diagnostic* `SELECT NOW()` in a test *assertion* (to confirm the DB is reachable) — never in the state-machine path.

| detail | status |
|---|---|
| five failures / 10-minute window / `locked_until` / exponential backoff / 1 h cap | **spec** (§6.6) |
| base duration 10 min and the exact doubling curve; lock-cap 60 min | **spec-silent → proposal O-a (recommendation, not approval)** |
| full reset (row delete) on successful login; `lock_count` preserved across a *window* expiry but not across a *successful login* | **spec-silent → proposal O-a (recommendation, not approval)** |
| locked pair does not extend its lock via further failures | **proposal (recommendation, not approval)** |
| row shape (`id`, `first_failed_at`, `last_failed_at`, `lock_count`) | **proposal** (spec §3.5 gives only `id`, `username`, `ip`, `attempts`, `locked_until`; the window-start column is required to *measure* the 10-minute window — the draft's five-column shape could not implement its own rule) |
| lowercase-normalized username + compact-IP canonicalization in the pair | **proposal (recommendation, not approval)** (spec requires only "per-IP+username") |
| equality-inclusive expiry boundaries | **proposal (recommendation, not approval)** (spec silent on `<=` vs `<`) |
| injectable-clock discipline (no `NOW()` in the state machine) | **proposal (recommendation, not approval)** (an implementation-discipline choice, not a spec item) |
| login-vs-failure ordering = reset-is-source-of-truth (not "one upsert") | **proposal (recommendation, not approval)** |
| no `users.failed_logins`/`locked_until` | **resolved spec conflict** (§3.5 over §6.6, above) |

**Atomic update behavior (contract, not pseudocode).** The *failure* path is one `INSERT … ON DUPLICATE KEY UPDATE` keyed by `uq_login_attempts_pair` that computes `attempts`, `first_failed_at`, `last_failed_at`, `lock_count`, `locked_until` from the current row + `now` (the supplied `now`, not `NOW()`). The *success/reset* path is a separate `DELETE` on the pair (C0.3 shape). The unique pair key guarantees one row per pair under concurrency; concurrent failures on the same pair serialize on the key. `now` semantics: one injected value per statement, so the window check and the writes see the same clock. (The review's "one round trip" is a *simplification we do not mandate*: a short row-locked `BEGIN … SELECT … FOR UPDATE … COMMIT` around the same update is equally correct and clearer, and is the form we will implement if the upsert-on-duplicate becomes awkward. Both satisfy the same invariants; the tests assert the invariants, not the statement shape.)

**S1 collation note:** while at it, verify-and-assert the `users.username` collation is case-insensitive (MariaDB default `utf8mb4_*_ci`) so the canonicalization rule above and the `UNIQUE` key agree; treat any future collation change as migration-only.

### C2 — Browser-bound pre-auth CSRF + per-session CSRF

Spec §6.6 requires "per-session `csrf_token` (§3.5) required on every `POST/PUT/DELETE`". That is impossible for `/login` and `/setup` by definition — no session exists yet. The spec is silent on how pre-auth mutations get a token; this plan fills the gap (no spec edit; no exemption).

> **Why the draft-v2 token is wrong (the finding).** Draft v2 issued a *stateless signed token* `b64url(nonce) . b64url(expiry) . b64url(HMAC(SECRET_KEY, nonce|expiry))` and relied on `SameSite=Lax` for cross-site replay. That token is **not bound to a browser**: an attacker (Client B) can request `GET /login`, obtain their *own* valid token, and replay it inside a forged login form posted from their site. `SameSite=Lax` does not stop an *unauthenticated* victim's browser from being the origin of a forged cross-site `POST` that carries a *genuinely valid, in-lifetime* token — Lax's cross-site `POST` blocking is a *second* line, and it is not a substitute for browser binding. The fix below binds the token to a per-browser secret, so Client B's token is useless without *Client A's* browser secret.

**Pre-auth token (for `POST /login`, `POST /setup`) — browser-bound.** Two cooperating values, both issued server-side at `GET /login` / `GET /setup` and checked server-side at the matching `POST`:

1. **Pre-auth browser secret (a random nonce), held in a scoped cookie.** A fresh 16-byte random value (`secrets.token_urlsafe(16)`) per browser, set in a cookie (below). This is the *browser binding*: only the browser that was offered the `GET` page holds the secret. It is NOT a credential, NOT a session token, and MUST NOT appear in a URL, a log line, or a `Set-Cookie` for anything but the pre-auth surface. It is scoped to `/` for `SameSite=Strict` (see cookie attributes) and is deleted on logout and on successful login.
2. **Signed form token, bound to that secret + purpose + expiry.** A server-signed string over `(secret, purpose, expiry)` where `purpose ∈ {"login", "setup"}` and `expiry = issued + 15 min`. Emitted into the rendered form (hidden field) *and* into the HTMX request header path. It is the thing the form actually submits; the secret stays in the cookie and is carried by the browser on the `POST`.

**Verification (all four must hold, else `403 *before* any password work or bootstrap mutation):**

- **Signature** over `(secret, purpose, expiry)` is valid in constant time (HMAC-SHA256, `SECRET_KEY`-derived subkey; verify with `hmac.compare_digest`).
- **Expiry:** `now < expiry` (boundary per C1 equality rule: `now == expiry` is *reject*).
- **Purpose binding:** a `login`-purpose token is rejected by `POST /setup`, and a `setup`-purpose token is rejected by `POST /login`. This is a named test.
- **Browser binding:** the secret embedded in the token MUST equal the current value of the pre-auth browser-secret cookie on the `POST`. This is the anti-replay-across-browsers rule and is the fix for the finding: Client A's valid token + Client B's browser cookie **fails** the binding check and is rejected, even though the token itself is valid, in-lifetime, and purpose-correct.

**Cookie attributes — pre-auth browser secret (define both modes, no silent substitution):**

- **HTTPS production:** `Name=cookbook_csrf_pre`, `Value=<secret>`, `Path=/`, `SameSite=Strict` (the pre-auth surface never needs cross-site navigation; Strict is the safe default for a CSRF secret), `HttpOnly=TRUE` (the browser auto-carries the cookie on the `POST`; no JS ever reads it, so it should not be JS-accessible), `Secure` **when the effective scheme is `https`** (C4), `Max-Age` = 15 min (matches the pre-auth token lifetime). **Not** `__Host-` prefixed — it is not the session cookie and does not need the `__Host-` rules; using `__Host-` here would be a scope error. The secret is never put in a URL or a log line (see the "no credentials in URLs" rule below).
- **HTTP development (explicit, not a silent fallback):** over plain `http://localhost:8000` the `Secure` flag is **omitted** (a `Secure` cookie will not be sent/received over http). `SameSite=Strict` is retained. Everything else is the same. This is the honest configuration contract: **the dev mode is not secure against cross-site `POST` from another origin on the same http host** — the owner's dev box is single-user and trusted; we document that, and production (https, `Secure`) is the only mode we claim to be secure in. We do **not** claim "HTTPS production can never be weakened" while also permitting an unrestricted `COOKIE_SECURE` override (finding 5): the C3 override is real and we state its cost honestly below.
- **No credentials / session tokens / sensitive browser secrets in URLs or logs.** The pre-auth secret and the form token are never logged, never appear in a query string, and the `Set-Cookie` for the secret is never echoed into a `Location` header. A named test asserts the secret value does not appear in any logged line or redirect target during the login flow.

**Issuance / renewal / expiry / success / logout behavior (all pre-auth surface):**

| event | pre-auth secret (cookie) | form token |
|---|---|---|
| `GET /login` or `GET /setup` | **new** secret issued + `Set-Cookie` (fresh per page render; a stale secret from a prior render is replaced) | fresh token signed over `(secret, purpose, issued+15 min)`, rendered into the form + HTMX header |
| `POST /login` success (valid) | **deleted** (cookie cleared, `Max-Age=0`) | consumed; signature/binding/purpose/expiry all checked **before** password work |
| `POST /login` failure / lockout (token valid) | **retained** (the user may retry from the same browser) | consumed by the attempt; the form page re-issues on the next `GET /login` |
| `POST /setup` success (valid) | **deleted** | consumed; checked **before** the bootstrap mutation |
| `POST /setup` refused (bootstrapped) | **deleted** (no further pre-auth surface for `setup`; it is 403 forever) | consumed |
| `POST /logout` (session) | **deleted** (clear any stale pre-auth secret too, so a later `GET /login` starts clean) | n/a |
| 15 min pass (expiry) | **deleted** if the browser re-requests (the `Set-Cookie` refresh on the next `GET` replaces it, so a stale one cannot outlive its `Max-Age`); the cookie is expired by `Max-Age` regardless | token self-expires (`now > expiry`); no server-side invalidation needed, nothing to delete |

**Authenticity of "renewal":** a `POST /login` attempt that *fails the binding* (Client-B-with-Client-A's-token) does **not** refresh the secret — only a *successful* `GET /login|/setup` (page render) issues a fresh secret. This means a stolen-token-replay attempt cannot *extend* the secret's life.

**Origin/Referer checks — defense in depth, not a substitute:** the app MAY additionally check `Origin`/`Referer` as a second layer, but the binding above is the primary control and the design does **not** silently substitute Referer (which an attacker can omit or spoof on some browsers) for the browser-binding cookie. If an Origin/Referer check is added, it is additive and must not downgrade the binding requirement.

**Session token (post-auth) — spec as written (unchanged):** per-session `csrf_token CHAR(32)` in `sessions` (random 16 bytes hex, generated at session creation). **Authenticated mutations continue to use the session's stored CSRF token** — the pre-auth mechanism is *only* for the two pre-auth `POST`s; after login, every mutation on `{POST, PUT, PATCH, DELETE}` presents the session's `csrf_token` (C0.1). The two token classes are separate; the session token is never derived from, nor replaced by, the pre-auth token.

**Caching:** pre-auth pages and the pre-auth token **must never be publicly cached**. `GET /login`, `GET /setup`, and any response that issues a pre-auth token or secret get **`Cache-Control: private, no-store`** (same treatment as behind-login responses, regardless of the public-read `public, max-age=300, s-maxage=600` rule in R6). A named test asserts the `Set-Cookie: cookbook_csrf_pre=…` response is not `Cache-Control: public` and that `GET /login` / `GET /setup` carry `no-store`.

**Delivery (session token — both mechanisms; pre-auth token — form + HTMX header):**

- **Classic browser form (session token, post-auth):** hidden field `<input type="hidden" name="csrf_token" value="…">`.
- **HTMX (session token, post-auth):** **use an HTMX-supported delivery mechanism.** HTMX reads request headers from `htmx.config.headers` (a documented, stable config property) — *verify against the intended HTMX version before locking a name*: the delivery is set as `htmx.config.headers['X-CSRF-Token'] = '<session csrf_token>'` from the page render context, so every HTMX request automatically carries the session token in the `X-CSRF-Token` header. (This is the mechanism v1 will use; it is *supported* by HTMX's documented `htmx.config.headers` API. Do **not** invent a nonexistent config property — if the intended HTMX version exposes a different, supported hook, that hook wins, and we verify it in S5 before the plan is treated as locked.)
- **Server check order (session token):** `X-CSRF-Token` header first, then form field `csrf_token`; if neither is present/valid → `403`. This works uniformly for `multipart/form-data`, `application/x-www-form-urlencoded`, and JSON HTMX bodies (the header covers the last). The *pre-auth* check (findings above) uses the **cookie + form token** pair, not the header — the header path is post-auth only.
- **Pre-auth form token delivery:** the signed token (bound to the cookie secret) is rendered as a hidden field in `login.html`/`setup.html` *and* is available in the HTMX context if the page is JS-driven. The secret itself is **not** rendered — it is the cookie.

**Method set (reconciles the spec wording; unchanged):** the enforcement gate is a **method-set gate, not a route list**: it applies to every route whose method is in `{POST, PUT, PATCH, DELETE}`. v1 routes use `POST` for all state changes (forms + HTMX); a future `PATCH` is automatically protected because the decision is on the method, not on a per-route annotation list. `GET/HEAD/OPTIONS` are never gated. (Spec says `POST/PUT/DELETE`; the set adds `PATCH` because the app could serve it and spec §6.6's intent is "every mutation". This is the plan's concrete proposal — flagged, not a spec change.)

**Acceptance tests (S5 acceptance block, binding):**

- **Cross-browser replay:** Client A obtains a valid pre-auth token (valid signature, in-lifetime, correct purpose). Client B, in a *different* browser/cookie jar, presents Client A's token **with Client B's own browser-secret cookie** → **rejected** (binding check fails) and **no password is verified, no bootstrap row is written** (assert on `login_attempts` row count and on `users` count = 0).
- **Missing browser-secret cookie** (Client A's token, no cookie) → rejected.
- **Missing form token** (Client A's cookie, no token in the form) → rejected.
- **Tampered token** (one bit flipped in the HMAC) → rejected by constant-time signature check.
- **Expired token** (clock advanced past `expiry`) → rejected; and at *exactly* `expiry` (`now == expiry`) → rejected (equality rule, C1).
- **Purpose mismatch:** `login`-purpose token submitted to `POST /setup` → rejected; `setup`-purpose token to `POST /login` → rejected.
- **Legitimate same-browser:** Client A's cookie + Client A's token + correct purpose + in-lifetime → accepted (proceeds to password check / bootstrap); the *rejection* in the cross-browser case occurs **before** the password-verify step (a no-op password check is allowed to *run for timing* but must not *authorize*; assert no `users` row and no `sessions` row are created).
- **Rejection-before-password-verified** is a named invariant: for every rejected case above, assert (a) no `login_attempts` row was written, (b) no `users` row exists, (c) no `sessions` row exists.

**Implementation shape (standard, small, established primitives — no new token framework):**

- One module `src/cookbook/auth/csrf.py` with a small set of functions: `issue_preauth(secret, purpose, now, ttl) -> token`; `verify_preauth(token, secret, purpose, now) -> bool` (does the four checks above, constant-time, and returns a *reason* for logging the rejection class but **not** the secret); `browser_secret_set_cookie(secret, secure) -> Set-Cookie header string`; `browser_secret_clear() -> Set-Cookie header string`; `issue_session_token() -> str`; `validate_session_token(request, session) -> bool`.
- **HMAC-SHA256** for the token signature (no new crypto lib — `hmac` + `hashlib` + `secrets` are stdlib).
- **One `Set-Cookie` helper** for the pre-auth secret cookie (the C3 rule for session cookies is separate and does not reuse this helper — the two cookie families are distinct, per finding 5's "cookie-name selection, lookup, renewal, and deletion must agree").
- **No new dependency** is added for this (findings 5/7 do not require one).
- The binding check is `hmac.compare_digest(secret_from_token, secret_from_cookie)` — a constant-time compare, not `==`.
- The `purpose` field is a *literal string* in the signed payload; verification rejects any `purpose` that is not the route's expected one. This is what makes "login-purpose rejected by setup and vice versa" a one-line check.
- We explicitly do **not** build a generic "token framework" with pluggable backends, JWT, or a token store. The pre-auth secret is a 16-byte random value in a cookie; the token is a 3-field HMAC string. That is the whole design. It is small, testable, and uses only stdlib.

**Configuration contract (stated honestly — finding 5):** the `COOKIE_SECURE` override (C3) *does* let the owner disable `Secure` in production, and we no longer claim that is "never a weakening." The honest statement: **production (https) is the only mode that is secure against the cross-site `POST` replay; the `COOKIE_SECURE=false` override, if set, weakens even production, and the owner must consciously set it.** The dev mode (plain http) is explicitly *not* a security boundary — it is a single-user convenience. This replaces the draft-v2 claim that HTTPS production "can never be weakened while also allowing an unrestricted `COOKIE_SECURE` override." See C3 below for the exact override behavior and the recommended "non-`__Host-` cookie in dev" split.

### C3 — Cookie contract and HTTP-dev behavior

> **This contract governs the *session* cookie only.** The pre-auth browser-secret cookie (C2) is a *separate* cookie family with its own name/flags (`cookbook_csrf_pre`, `SameSite=Strict`, `Secure` when https) and its own issuance/renewal/deletion (C2 lifecycle table). **The two families must not share a name, a `Set-Cookie` helper, or a deletion rule** — finding 5 requires that "cookie-name selection, lookup, renewal, and deletion agree" *within* each family, and the two families are kept distinct on purpose. See the "stale alternate cookie name during transitions" rule below.

**Flags (exact string, both names):**

| flag | value |
|---|---|
| `Name` | `__Host-cookbook_session` when the Secure flag is true, else `cookbook_session` (rule 4) |
| `Value` | the raw session token |
| `HttpOnly` | always |
| `SameSite` | `Lax` (always) |
| `Path` | `/` (always) |
| `Domain` | **never present** (a `Domain` attribute invalidates a `__Host-` cookie) |
| `Secure` | exactly when the effective secure flag is true (rule 2) |
| `Max-Age` | `SESSION_TTL_HOURS` in seconds; **the cookie is re-sent on every authenticated response** with a fresh `Max-Age`, keeping the client cookie alive in step with the server-side sliding TTL (otherwise the browser would drop the cookie at the original issuance expiry while the server session is still valid). |

1. **Production behavior (HTTPS):** the owner's proxy terminates TLS; effective scheme is `https` (via C4) → Secure true → cookie is `__Host-cookbook_session; Secure; HttpOnly; SameSite=Lax; Path=/`, no `Domain`. **We do NOT claim this "can never be weakened":** rule 2 shows that a deliberate `COOKIE_SECURE=false` override can drop the Secure flag even in production. That is a *real* weakening and the owner must consciously choose it. v1's posture is: **production default (no override, https) is the only configuration we treat as the security-relevant baseline**, and any override below it is the owner's explicit, logged, conscious choice.
2. **`COOKIE_SECURE` (§9.4) — the honest override contract, stated plainly:** effective secure flag = `COOKIE_SECURE` *if the owner set it explicitly* (either `true` or `false`), **else** `(effective scheme == "https")`. Consequences, stated without hedging:
   - `COOKIE_SECURE` unset + https → Secure true (production default, secure).
   - `COOKIE_SECURE` unset + http → Secure false (dev default, *not* a security boundary).
   - **`COOKIE_SECURE=true` + http → Secure true, and the name becomes `__Host-cookbook_session`. This is *broken over plain http* (a Secure cookie is not sent over http), and v1 does not treat it as a supported configuration — the name/flag combination is coherent but the transport is wrong; a loud startup warning is emitted. The owner should not use this.**
   - **`COOKIE_SECURE=false` + https → Secure false, and the name *downgrades* to `cookbook_session`. This is a *deliberate weakening of production*; v1 emits a loud warning on startup and records it in the config echo. It is *supported* (the name/flag invariant is preserved: no `__Host-` without `Secure`), but it is a security downgrade the owner is opting into.**
   The draft-v2 sentence "HTTPS production can never be weakened while also allowing an unrestricted `COOKIE_SECURE=false` override" is **retracted**; this table is the actual, honest contract.
3. **Non-secure local development:** over `http://localhost:8000` the effective scheme is `http` → Secure false (rule 2). A `__Host-` cookie is invalid without `Secure`, and a `Secure` cookie does not ride over plain http — both spec requirements conflict in the HTTP-dev case. The coherent reading is in rule 4.
4. **Name-follows-flag (the resolution, now a *recommendation*, not an approval — O-c):** the **name** is a function of the flag: **Secure flag true ⇒ `__Host-cookbook_session`; Secure flag false ⇒ `cookbook_session`** (same flags otherwise). The invariant — **never emit a `__Host-`-prefixed cookie without `Secure`** — is a named test: an integration test with Secure forced off asserts the `Set-Cookie` name is the non-prefixed one and that no `__Host-` cookie appears. This is the **Codex recommendation** for dev: *HTTP development uses a non-`__Host-` cookie; production retains the secure `__Host-` host cookie.* It is recorded as a recommendation in the owner-decisions table and is **the draft's recommended implementation**, pending owner sign-off.
5. **Stale alternate cookie names during transitions (finding 5):** if a configuration change (or a dev→prod transition) flips the Secure flag, the *other* cookie name may still exist in the client. The contract: **`POST /logout` clears BOTH names** (`__Host-cookbook_session` and `cookbook_session`) with `Max-Age=0`, and the same is true on successful `POST /login` (the pre-auth flow ends and any stale alternate session name is cleared). On every `Set-Cookie` for the *new* name, the app also emits a `Max-Age=0` clearing `Set-Cookie` for the *other* name. This is a named test: flip Secure true→false between two `GET /login` calls; the session is still coherent (the new name carries the token, the old name is cleared). Do **not** let a stale `_session` cookie survive a name change and shadow the new one.
6. **No credentials / session tokens / sensitive browser secrets in URLs or logs.** The session raw token is never logged, never in a query string, and the `Location:` of a redirect after a successful `POST /login` does not carry the token. A named test greps the logged lines and redirect targets for the raw token (random, test-chosen) and asserts absence.

### C4 — Trusted proxy / client identity

**Evidence (inspected once, 2026-10-16):** installed uvicorn **0.54.0** — `--proxy-headers` defaults **on**; `--forwarded-allow-ips` defaults to `$FORWARDED_ALLOW_IPS` or `127.0.0.1,::1`. The Dockerfile `CMD` (`uvicorn cookbook.app:create_app --factory --host 0.0.0.0 --port 8000`) passes **no** proxy flags, and `bin/docker-entrypoint.sh` adds none. Consequence: in the container, Uvicorn trusts forwarded headers **only from localhost peers**; the owner's reverse proxy connects from the docker network / LAN, so by default its `X-Forwarded-For`/`X-Forwarded-Proto` would already be ignored in prod. Uvicorn's trust list is a *server* setting (per-process, not per-request config) and gives the app no way to *know* which peer was trusted — so the app cannot delegate the trust decision to it.

**Trust boundary (contract):**

- **Trusted peer** = the direct TCP source of the request (`request.client.host`) falls inside `TRUSTED_PROXY_CIDRS` (comma-separated CIDRs; parsed at boot; unset/empty = no peer is trusted).
- **Trusted peer present:** client IP = right-to-left walk of `X-Forwarded-For`, taking the **first address not in a trusted CIDR**; if every listed address is trusted (or the header is absent), client IP = the direct peer. Scheme = `X-Forwarded-Proto` (first value) if present, else the direct scheme.
- **Untrusted peer:** `X-Forwarded-For` / `X-Forwarded-Proto` are **ignored entirely** (they must not leak into lockout keys or the Secure decision); client IP = direct peer; scheme = direct scheme.
- **Single access point:** one helper `client_identity(request, settings) -> (client_ip: str, scheme: str)` in `src/cookbook/auth/identity.py`. Every consumer — lockout key (C1), cookie Secure decision (C3), request logging — calls it. No ad-hoc `request.headers["X-Forwarded-For"]` reads anywhere else. This is a shared utility, not mandatory custom middleware; the app-level decision exists because Uvicorn's is a static per-process default, not a per-request authorization (see evidence above).
- **Availability:** `client_identity` is a dependency of every later slice that needs IP/scheme. It lands in **S2, before lockout (S6)**. A concrete deployment CIDR is the owner's configuration at deploy time — it is **not** a blocker to implementation or to tests; tests use controlled addresses: trusted `192.0.2.1`, untrusted `198.51.100.1` (both TEST-NET ranges, never routeable to the owner's network).

**Malformed-header and chain rules (contract, not left to the implementer):**

- **No double processing.** `client_identity` is the *only* component that reads `X-Forwarded-For` / `X-Forwarded-Proto`. Because Uvicorn's own `--proxy-headers` trust (see evidence) would *also* interpret these headers, the app **must** launch Uvicorn with `--no-proxy-headers` (see the launch-flags rule below) so that Uvicorn does *not* rewrite `request.client.host`/`request.scope` before `client_identity` runs. **One owner of forwarded-header processing: the app, via `client_identity`. Uvicorn's is disabled.** Do not let both run — that is the "double processing" the review forbids.
- **Malformed `X-Forwarded-For`:** tokens that are not parseable IPs (empty, non-IP text, out of range, more than a sane bound) are **dropped**, not guessed. If the entire header is malformed or the walk yields no untrusted address, fall back to the **direct peer**. Never a crash, never a guess. A named test: `X-Forwarded-For: "not-an-ip, 1.2.3.4"` → the walk treats `1.2.3.4` if untrusted, else direct peer; `X-Forwarded-For: "junk"` → direct peer.
- **Untrusted chains:** a chain of *only trusted* addresses (every hop in `TRUSTED_PROXY_CIDRS`) is treated as "all trusted" → client IP = **direct peer**. A chain with a *spoofed untrusted* address *behind* trusted ones uses the right-to-left walk's first untrusted — but only because the *peer* is trusted. The trust decision is *peer-based* (the direct TCP source), never *header-based*.
- **Allowed `X-Forwarded-Proto` values:** only `http` and `https`. Any other value (`HTTP`, `HTTP/2`, `wss`, junk) is **ignored** and the scheme falls back to the direct scheme. Scheme is a *two-value* decision, not an open string. A named test: `X-Forwarded-Proto: HTTP/2` → direct scheme.
- **`X-Forwarded-Host` / `X-Forwarded-Port`:** not read by `client_identity` (out of scope for v1's IP/scheme contract).

**Launch flags and deployment wiring (in the slice's files, not just prose):** there must be **exactly one** owner of forwarded-header processing. The app owns it via `client_identity`; to prevent Uvicorn from *also* rewriting the direct peer first, the launch configuration must **keep Uvicorn's `--forwarded-allow-ips` at "localhost only"** (i.e. the proxy's non-localhost peer is *not* in Uvicorn's server-side trust set), so Uvicorn leaves `request.client` as the real (proxy) peer and the *app* is the sole trust decider. Concretely: set `FORWARDED_ALLOW_IPS=127.0.0.1,::1` (or the empty value that disables forwarding trust for non-localhost peers) in the container env / S7 deployment note. **Verify the exact flag spelling and its current default against the installed uvicorn 0.54.0 CLI before locking it in S2** — do not assume a `--no-proxy-headers` flag exists; if the installed uvicorn exposes a different mechanism to *disable* server-side proxy-header trust, use that. The two owners (Uvicorn server-trust and the app's `client_identity`) must not both act on the same header. Whatever the exact flag is, it belongs in the Dockerfile `CMD`/env + `.env.example` comment + a startup log line that echoes the final trust decision, so a reviewer can see it. This is a **deployment** concern, not an owner *code* decision — it is recorded here for completeness and is not in the O-table.

**Acceptance (S2):** same request body from trusted vs untrusted source → different `client_ip`/`scheme` results per the table above; untrusted source with forged `X-Forwarded-For: 8.8.8.8` yields the direct peer, not `8.8.8.8`; trusted source with a valid chain yields the right untrusted hop; `TRUSTED_PROXY_CIDRS` unset → untrusted always; malformed `X-Forwarded-For` falls back to direct peer (not a crash); `X-Forwarded-Proto` other than `http`/`https` falls back to direct scheme.

### C5 — Concurrency invariants

Two invariants must hold under concurrent writes. MariaDB/InnoDB notes that shape the design: a plain `SELECT COUNT(*) FROM users` takes **no locks** (snapshot read); with an empty or small table there is no row to lock, so a count-then-write guard is **unsafe by default** — this is exactly the unlocked "count-then-write" pattern the review forbids. The design therefore uses named lock rows acquired with `SELECT … FOR UPDATE` (a locking read that sees current committed data and blocks concurrent acquirers until the holder commits), plus row-level locking where rows exist.

**Isolation / read-after-lock (contract).** MariaDB defaults to `REPEATABLE READ`. A *locking* read (`SELECT … FOR UPDATE`) always returns the latest committed row, but a subsequent *plain* `SELECT` in the same transaction reads the transaction's *snapshot* — which is established by the transaction's **first** read. Consequence for the guarded helpers: the **lock-row `… FOR UPDATE` must be the first read in the transaction**, and the count read that follows must therefore see current committed data. To make this robust rather than relying on read order, the guarded helpers **open their own dedicated transaction** (their own unit of work) and do **not** inherit an earlier snapshot from a caller that already read `users`. If the caller (route/CLI) needs to read `users` for its own purposes, it does so *before* invoking the guard, not inside it. The net contract: **after the holder acquires the lock row, every read in that guarded transaction sees current committed user state**, including the `COUNT(*)` that the guard acts on. A named test proves this: two connections, one holds the `first_admin` lock and inserts an admin, commits; the second, which had read `users` *before* the lock was released, then acquires the lock and re-counts — it must see the committed admin (count ≥ 1) and raise `AlreadyBootstrapped`, not re-insert.

**Transaction ownership (contract, single owner).** The guard helper is the **sole owner** of its transaction: it opens (`BEGIN`), locks, reads, writes, and either `COMMIT` or `ROLLBACK` itself, then returns. A caller must **not** wrap a guard call in its own already-open transaction and then commit separately — that is the "nest incompatible transactions / accidentally commit caller work" hazard the review forbids. Concretely: `create_first_admin` and `modify_admin` are called *standalone* (the route/CLI passes the inputs in; the helper returns the outcome or raises). The helper never leaves a half-committed caller transaction. If a future caller needs the guard's write and additional writes atomically, it must do so by *calling the helper inside its own single transaction as the guarded step* and owning all of it — the helper then operates within the caller's transaction and does **not** commit on its own in that mode. v1 uses the **standalone** mode (helper owns the transaction); the in-caller-transaction mode is documented but not required. Tests assert: a guard that raises `AlreadyBootstrapped`/`LastAdminProtected` leaves the transaction **rolled back** (no partial row).

**Missing lock row = loud failure, not an unlocked fallback (contract).** If the `… FOR UPDATE` on the expected lock row returns **no row** (the `auth_locks` seed row is absent), the helper MUST raise `LockRowMissing` (→ HTTP `500` / CLI non-zero exit). It must **never** proceed without the lock — an unlocked fallback would silently reintroduce the count-then-write race. A named test deletes the `first_admin` lock row, calls `create_first_admin`, and asserts a loud failure with **no** `users` row created.

**Shared-guard requirement (contract).** Every HTTP and CLI writer of account state that touches the guarded conditions uses the shared guards — do **not** re-implement the count-then-write inline:
- "Create the first admin" (HTTP `POST /setup`, CLI `cookbook setup`, CLI `--force-owner-recreation` when it creates) → `create_first_admin` (Invariant A).
- "Disable / demote / reset the role-+active-state of an admin" (admin UI routes, CLI `user reset-password` when it flips `role`/`is_active`) → `modify_admin` (Invariant B).
- A pure password reset that does **not** change `role`/`is_active` needs neither guard; it still wipes sessions per R1. (So: guard membership is *by which account-state transition*, not by which transport.)

A named test greps the S7/S8 implementation list and asserts that `POST /setup`, `cookbook setup`, and the admin disable/demote path each call the named guard (this is a code-review assertion, captured as an acceptance bullet).

**New table (locked shape, part of S1):**

| column | type | notes |
|---|---|---|
| `name` | VARCHAR(40) | PRIMARY KEY |

Seeded rows: `('first_admin')`, `('last_admin')`. The table has no other purpose; it exists so the app has something to lock when the guarded condition is about *zero or one* rows (empty-table bootstrap / one-admin bootstrap) where the protected table has nothing to lock.

**Invariant A — exactly one initial admin:**

`create_first_admin` (shared by `POST /setup` and `cookbook setup`; CLI and HTTP cannot create two admins by choosing different doors):

1. `BEGIN` (app transaction).
2. `SELECT name FROM auth_locks WHERE name = 'first_admin' FOR UPDATE` — serializes all first-admin writers, including CLI/HTTP races.
3. `SELECT COUNT(*) FROM users` (safe now: the holder owns the lock).
4. If `count == 0`: insert the owner row (`role='admin'`, `is_active=TRUE`). Otherwise: raise `AlreadyBootstrapped` → route answers the setup form's 403 / CLI exits non-zero.
5. `COMMIT`.

Concurrency test: two `asyncio` tasks (two independent sessions) call `create_first_admin` in parallel — exactly one succeeds, `COUNT(*) FROM users == 1`, loser raises. A second test runs the same race with one participant the CLI path and the other the HTTP service path (both through the same function; the test asserts the *shared* guard, not the transport).

**Invariant B — at least one active admin survives:**

Every disable/demote goes through `modify_admin(target_user_id, ...)`:

1. `BEGIN`.
2. `SELECT name FROM auth_locks WHERE name = 'last_admin' FOR UPDATE`.
3. `SELECT COUNT(*) FROM users WHERE role='admin' AND is_active = TRUE`.
4. If the target's removal would drop that count to 0 → raise `LastAdminProtected` (→ 403 in the admin UI, non-zero exit in CLI). Otherwise apply the change (`is_active=FALSE` + session wipe, or `role` change).
5. `COMMIT`.

Because the lock is held across count-check and write, two concurrent "remove the last admin" attempts (disable + demote in parallel, or disable + disable of the two admins) serialize; the second re-counts *after* the first commits and fails if removal would leave zero. Note the rule counts **active admins, not rows** (spec §6.5): an already-disabled admin row does not count as a survivor.

Concurrency test: seed two admins; fire "disable A" and "demote B" concurrently; exactly one succeeds, the other fails with the invariant error, and `COUNT(role='admin' AND is_active) >= 1`.

**Empty-table bootstrap note:** Invariant A's guard works *because* the lock row exists independently of `users`. This is the documented reason for `auth_locks`; no alternative (advisory locks, sentinel users) is used.

### C6 — Bootstrap, CLI, and config

**CLI surface (minimal, stdlib only — `argparse`, no new framework) — `src/cookbook/cli.py`, console script `cookbook`:**

| command | modes | behavior |
|---|---|---|
| `cookbook init-secret [--out PATH] [--overwrite]` | file | generate 32 random bytes (`secrets.token_hex(32)`) → write to file (best-effort `0600` perms) → print path. Refuses to overwrite an existing file without `--overwrite` (rotation is deliberate, per spec §9.3). **Never requires `SECRET_KEY` to already exist.** |
| `cookbook setup` | interactive (default) \| `--username` + `--password-file` \| `FIRST_ADMIN_PASSWORD(_FILE)` fallback | creates the owner admin via the C5A guard. Interactive: prompt username (default `owner`), then password + confirmation via `getpass` (no echo, not on stdout). Order of password source: `--password-file` > `FIRST_ADMIN_PASSWORD_FILE` > `FIRST_ADMIN_PASSWORD` (plain env = **dev only**: a loud warning is logged when used outside a dev-looking config — spec §6.4 keeps the warning-not-failure posture, §9.3). **Password policy (a product decision — O-f, a *recommendation, not an approval*):** shared by `POST /setup` and this command — non-empty and **minimum length 8**; complexity/character-class policy is *not* mandated in v1 (spec-silent, out of scope). This is surfaced here and in the O-f row deliberately so the length policy is a visible owner/product call, not hidden in CLI wording. `--force-owner-recreation` scope: see below. **Does not require `SECRET_KEY`** (argon2id needs no app key). |
| `cookbook user reset-password <name> (--interactive \| --password-file PATH)` | interactive \| file | hash the new password (argon2id), update the user row, **delete all of that user's sessions** (spec §6.1: password change invalidates all of the user's sessions). User missing → non-zero exit + message. **Does not require `SECRET_KEY`.** |

**`--force-owner-recreation` — exact scope (O-d recommendation, *pending owner approval*):**

- **Scope (the recommendation):** it is an **explicit local CLI recovery of a *named, existing* account**, not "delete users and re-create" and **not a bootstrap door**. With `--username <name>`: the named user **must exist** → reset its password (from the sources above) and set `role='admin'`, `is_active=TRUE` (self-lockout recovery after a forgotten password). If `--username` names **no existing user** → **refuse** (it will not mint a second admin around the single-user guard, and it is not the way to bootstrap — plain `cookbook setup`, with the C5A `first_admin` guard, is the only zero-user bootstrap path).
- **Safeguards:** requires an explicit `--username`; requires a typed-confirmation prompt even in `--password-file` mode (`yes/<username>`); logs a warning line; refuses when users exist and the name names no account (the second-admin / arbitrary-account case is covered by the ordinary admin UI, the sanctioned path after bootstrap).
- **Owner decision (O-d), an approval the owner has *not* yet given:** confirm that "recover a named *existing* account (password + restore active admin); refuse a nonexistent name" is the intended semantic — or narrow it (password-only, no role/active restore) or remove it from v1. It is the only path that changes an *existing* user's role/active state without the admin UI; a mis-scope is a backdoor-shaped bug, which is exactly why it is held as an unapproved recommendation.

**File-first settings (already in code, extended):** `config.py` already implements `_from_secret_file` for `SECRET_KEY` / `DB_PASSWORD`. v6 adds file-first handling for `FIRST_ADMIN_PASSWORD` (same first-line/newline-strip rule, `_FILE` wins over plain). `SECRET_KEY_FILE`/`LLM_API_KEY_FILE` are spec-enumerated and land when their consumers do (not in this plan's slices).

**`.env.example` changes (spec §9.4 surface, to be applied in S7):** add `SECRET_KEY_FILE` (example path, commented real-file guidance), `SESSION_TTL_HOURS` (default 12, commented), `TRUSTED_PROXY_CIDRS` (commented, empty by default), `COOKIE_SECURE` (commented, "auto from effective scheme unless set"), `FIRST_ADMIN_PASSWORD_FILE` (commented, bootstrap-only) + a comment that `FIRST_ADMIN_PASSWORD` (plain) is dev-only and warned. Keep the list boring and short (D24): no new non-secret settings beyond C3/C4.

**CLI-before-`SECRET_KEY`:** the CLI path never calls `get_settings()` (which today *raises* on missing `SECRET_KEY` — correct for the web, wrong for the CLI). The CLI builds a lean settings view (DB parts + `MEDIA_DIR` only) — a small `cli_config()` in `config.py`. `init-secret` trivially works pre-`SECRET_KEY`; `setup`/`reset-password` work pre- and post. `uvicorn` (web) keeps the hard fail-fast on missing key (D20).

## Slices (each small enough for one fresh session; ordering is fixed)

Ordering is fixed; later slices consume earlier contracts. S2 is *before* any IP-dependent behavior so lockout and cookies have the identity primitive, per C4.

### S1 — v6 schema: `sessions`, `login_attempts`, `auth_locks` + regression-inventory update

- **Branch:** `task/7.1-auth-schema` (from `main` after `task/7-auth-plan` merges).
- **Goal:** the three tables (`sessions`, `login_attempts`, `auth_locks`) exist and match the **complete** contracts **C0.1** (`sessions`), **C1.2** (`login_attempts`), **C0.2** (`auth_locks`) — every column type, nullability, default, FK name, and index pinned — and the full-chain inventory test is **restructured** per the Regression-updates section (fourteen tables; type-independent key discovery) and still passes.
- **Files:** `src/cookbook/models.py` (add `Session`, `LoginAttempt`, `AuthLock`); `alembic/versions/<new>_v6_auth.py` (down_revision `b7a2f9c4d1e8`; seed the two `auth_locks` rows in the revision); `tests/test_auth_schema.py` (new); `tests/test_schema_inventory.py` (expected-set updates — see Regression updates below).
- **Locked schema additions:** exactly the **C0.1** (`sessions`) / **C1.2** (`login_attempts`) / **C0.2** (`auth_locks`) complete shapes (types, nullability, defaults, FK names, indexes, timestamp semantics). `users` is **untouched** (no `failed_logins`/`locked_until` — C1).
- **Acceptance & tests:** full-chain `upgrade head` from v5 is clean and single-head; **complete per-table assertions** — `sessions`: `id_hash char(64)` NOT NULL PK, `user_id varchar(36)` NOT NULL FK `fk_sessions_user`→`users.id` `ON DELETE CASCADE`, `csrf_token char(32)` NOT NULL, `created_at`/`last_seen_at` DATETIME(6); `login_attempts`: all eight C1.2 columns, `uq_login_attempts_pair (username,ip)` unique **exactly** `[username, ip]` (asserted *separately*, as a uniqueness contract, not via key discovery), `ix_login_attempts_locked`; `auth_locks`: `name varchar(40)` PK + **exactly** the two seeded rows; the **restructured inventory** asserts **fourteen** application tables (+ `alembic_version`), a **type-independent** discovered-key set == `EXPECTED_UUID_KEYS ∪ EXPECTED_NONUUID_KEYS` (Alembic key excluded explicitly), `varchar(36)` for every UUID key, `char(64)` for `sessions.id_hash`, `varchar(40)` for `auth_locks.name`, and the exact char/external-ID ledgers (see Regression-updates). The **89-test baseline suite still green; dev-stack diff clean** (per the working PowerShell block above).
- **Verification:** shared-contract PowerShell block; `& .venv\Scripts\pytest.exe tests -v`.

### S2 — `client_identity` + config for proxy/secure (C4, C3 flags)

- **Branch:** `task/7.2-client-identity`. **Depends on:** S1.
- **Files:** `src/cookbook/auth/__init__.py`, `src/cookbook/auth/identity.py` (one public function: `client_identity(request, settings) -> (str, str)`); `src/cookbook/config.py` (add `TRUSTED_PROXY_CIDRS`, `COOKIE_SECURE`); `tests/test_identity.py`.
- **Public interface:** `client_identity` only; CIDR parsing helper private.
- **Acceptance & tests:** the C4 acceptance table (trusted/untrusted/forged/unset-CIDR) using controlled ASGI transport addresses; `COOKIE_SECURE` unset → scheme drives the secure flag; `COOKIE_SECURE` set → override wins (pure function over `(request, settings)` — no DB needed).
- **Verification:** `& .venv\Scripts\pytest.exe tests/test_identity.py -v` (no MariaDB needed; run without the container).

### S3 — Password service (C1/R2)

- **Branch:** `task/7.3-passwords`. **Depends on:** none (pure).
- **Files:** `src/cookbook/auth/passwords.py` (public: `hash_password(plain) -> str`, `verify_password(plain, stored_hash) -> bool`, `dummy_verify() -> None`); `tests/test_passwords.py`.
- **Locked details:** argon2id via `argon2-cffi` at OWASP minimum (m=19 MiB, t=2, p=1) — *at or above* allowed by §6.2; raising params is an owner choice and is **not one of the O-rows** (the OWASP minimum is the lowest approved value, chosen within scope). Constant-time verify incl. unknown-user path: a committed dummy hash constant; both paths execute exactly one `argon2.PasswordHasher.verify`.
- **Acceptance & tests:** round-trip; wrong-password rejection; unknown-user path executes and takes comparable timing to a known-user failure (assert only that the dummy path *runs* — no wall-clock flakiness); hash output parses as argon2id.
- **Verification:** `& .venv\Scripts\pytest.exe tests/test_passwords.py -v`.

### S4 — Session service: issue/verify/revoke + cookie contract (C3)

- **Branch:** `task/7.4-session-core`. **Depends on:** S1, S2, S3.
- **Files:** `src/cookbook/auth/sessions.py` (public: `create_session(...)->(raw_token, row)`, `load_session(request)->User|None`, `revoke_session(raw_token)`, `revoke_user_sessions(user_id)`, `cookie_set(raw_token, *, secure: bool) -> str` producing the exact `Set-Cookie` per C3); `tests/test_session_core.py`.
- **Locked details:** raw token = `secrets.token_urlsafe(32)`; stored key = `SHA-256(raw)` as `id_hash`; per-session `csrf_token` = `secrets.token_hex(16)` (32 chars); sliding TTL = `SESSION_TTL_HOURS` from `created/last_seen`; cookie name/flags are exactly C3 including the re-send-on-every-authenticated-response rule.
- **Acceptance & tests:** create → row shape (assert `id_hash char(64)` PK, `csrf_token char(32)`, `user_id varchar(36)` NOT NULL FK→`users.id` `ON DELETE CASCADE`, `created_at`/`last_seen_at` per C0.1); load with raw token against a seeded hash; expiry (clock-injected) → row deleted + `None`; `revoke_user_sessions` deletes all of a user's rows; `cookie_set(secure=True)` → `__Host-cookbook_session; Secure; HttpOnly; SameSite=Lax; Path=/` with **no** `Domain`; `cookie_set(secure=False)` → `cookbook_session; HttpOnly; SameSite=Lax; Path=/` with **no** `Domain` and **no** `Secure`; **negative invariant:** Secure false never produces a `__Host-` name; **stale alternate name (C3 rule 5):** every session `Set-Cookie` also emits a `Max-Age=0` clearing `Set-Cookie` for the *other* session name, and a Secure true→false flip between two requests leaves the session coherent (new name active, old name cleared).
- **Verification:** isolated-DB PowerShell block; `& .venv\Scripts\pytest.exe tests/test_session_core.py -v`.

### S5 — Login/logout routes + pre-auth CSRF (C2)

- **Branch:** `task/7.5-login-routes`. **Depends on:** S4.
- **Files:** `src/cookbook/auth/csrf.py` (public: `issue_preauth(secret, purpose, now, ttl)`, `verify_preauth(token, secret, purpose, now)`, `browser_secret_set_cookie(secret, secure)`, `browser_secret_clear()`, `issue_session_token()`, `validate_csrf(request) -> bool`; per C2's "standard, small" shape — stdlib `hmac`/`hashlib`/`secrets`, no token framework); `src/cookbook/auth/routes.py` (`GET /login`, `POST /login`, `POST /logout`; the `GET`s issue the browser-secret cookie + signed form token; the `POST`/mutation paths verify binding/purpose/expiry/signature **before** password work, and use the **session** `csrf_token` for authenticated mutations); `src/cookbook/templates/` minimal `base.html`/`login.html` (hidden field + `htmx.config.headers['X-CSRF-Token']` for the *session* token — a **supported** HTMX `htmx.config.headers` delivery, verified against the intended HTMX version in this slice, not an invented property); `tests/test_login_routes.py`. (Jinja2 already implied by D1 but not yet a dep — **implementation dependency note, not an owner decision:** add `jinja2` + `python-multipart` to `pyproject.toml` in this slice.)
- **Acceptance & tests (integration, httpx ASGI — the C2 binding suite):** **cross-browser replay** (Client A's valid token + Client B's browser-secret cookie) → **`403`, and NO `users`/`sessions`/`login_attempts` row** (rejection precedes password work and bootstrap); **missing cookie**, **missing token**, **tampered token**, **expired token** (and `now == expiry` boundary) → each `403` with the same no-row invariant; **purpose mismatch** (login token to `/setup`, setup token to `/login`) → each `403`; **legitimate same-browser** (Client A cookie + Client A token + right purpose + in-lifetime) → accepted (login proceeds to password check / bootstrap creates the owner); login success → session row + `Set-Cookie` (session cookie per C3) + 302 to `/`; `POST /logout` deletes the session row and clears **both** the session cookie names (C3 rule 5) **and** the pre-auth secret cookie; the `GET` pre-auth pages and any `Set-Cookie: cookbook_csrf_pre=…` response carry **`Cache-Control: private, no-store`** (never `public`); the rendered page sets `htmx.config.headers['X-CSRF-Token']` and a hidden field whose **session-token** values match; an authenticated mutation POST with `X-CSRF-Token` header and with hidden field are both accepted, neither present → `403`; unknown-user path → same `401` shape as wrong-password and the dummy-hash path ran (S3 contract).
- **Verification:** isolated-DB block + full suite regression (S1–S4 tests still green) + dev-stack diff.

### S6 — Lockout service + login integration (C1, C5 invariant A's lock row is already present)

- **Branch:** `task/7.6-lockout`. **Depends on:** S5 (login route exists to hook into), S2 (client identity), S1 (table).
- **Files:** `src/cookbook/auth/lockout.py` (public: `record_failure(username, ip) -> LockState`, `assert_not_locked(username, ip) -> None` (raises `Locked` with remaining time), `clear_pair(username, ip)`), and its wiring into `POST /login`; `tests/test_lockout.py`.
- **Locked details:** the C1 state machine, exactly (injectable clock — functions take `now`; window/lock expiry equality-boundary tests; `lock_count` preserved across a window expiry but reset on a successful login); one `INSERT … ON DUPLICATE KEY UPDATE` per failure **or** the equivalent row-locked `BEGIN … FOR UPDATE … COMMIT` (both satisfy the invariants); login refused while locked with **`429 Too Many Requests` + `Retry-After`** (Codex recommendation O-e, **recommendation not approval**) and **no** password verification (locked state is already public information; the `Retry-After` value = seconds until `locked_until`); success path calls `clear_pair` with the *identity* pair from `client_identity` (canonicalized per C1).
- **Acceptance & tests & concurrency:** 1st–4th failures within 10 min → `attempts` climbs, `locked_until` NULL; 5th → `locked_until ≈ now + 10 min` and the 6th attempt → **`429` with a valid `Retry-After`** and no hash work; **equality boundary:** a login at exactly `locked_until` (`now == locked_until`) → *free* (lock expired), one past `locked_until` → still locked; window expiry (clock-injected) resets the window, keeps `lock_count` (tier) and allows a fresh 5; backoff **10/20/40/60-min-cap** (Codex recommendation O-a) asserted across episodes — a *second* five-failure episode locks for 20 min, third → 40 min, fourth and later → 60 min cap; a *successful login* deletes the pair row (full reset); two concurrent failures for the same pair land as `attempts = 2` (the unique key serializes failure-vs-failure); **login-vs-failure:** a stale failure recorded *after* a successful login creates a fresh `attempts=1` row and does **not** lock or invalidate the now-valid session (the reset-is-source-of-truth rule, C1); lockout is keyed by the canonicalized `client_identity` output (forged-header case stays keyed to the *direct* peer, untrusted).
- **Verification:** isolated-DB block; `& .venv\Scripts\pytest.exe tests/test_lockout.py -v`.

### S7 — Bootstrap: `/setup` route + CLI (C5A, C6)

- **Branch:** `task/7.7-bootstrap`. **Depends on:** S5 (login/session), S1 (`auth_locks`).
- **Files:** `src/cookbook/auth/bootstrap.py` (public: `create_first_admin(session, username, password) -> User` (C5A transaction exactly); `is_bootstrapped(session) -> bool`); `src/cookbook/auth/routes.py` (+`GET/POST /setup`); `src/cookbook/cli.py` (`init-secret`, `setup`, `user reset-password` — C6 table, `argparse` only, `cli_config()` that does not require `SECRET_KEY`); `pyproject.toml` (add `[project.scripts] cookbook = "cookbook.cli:main"` — **implementation wiring, not an owner decision**); `.env.example` (C6 additions); `tests/test_bootstrap.py`, `tests/test_cli.py`.
- **Acceptance & tests:** zero-user → `GET /setup` 200 (sets the browser-secret cookie + signed `setup`-purpose token), `POST /setup` with a valid **browser-bound** token → exactly one `admin` row + 1 owner session, and the rejection suite (missing cookie/token, tampered, expired, `login`-purpose token → `403` **before the bootstrap mutation**, no `users` row) holds per C2; `GET/POST /setup` thereafter → `403` forever; a `setup`-purpose token is **refused on `/login`** and a `login`-purpose token on `/setup` (C2 purpose binding); CLI `setup` interactive (monkeypatched `getpass`) and `--password-file` modes both create the owner; **password policy per O-f (min-8, a *recommendation not approval*)** enforced on both the HTTP and CLI paths; `init-secret` writes the file, refuses overwrite without `--overwrite`, and the produced value satisfies `verify_password`-free `Settings` acceptance; `user reset-password` changes the hash and **wipes that user's sessions**; `FIRST_ADMIN_PASSWORD` plain-env path logs the dev warning; `--force-owner-recreation` behaves per the **O-d recommendation (not yet approved)** — recover a *named existing* account (password reset + restore active admin) and **refuse** a nonexistent name (it is not a bootstrap door; plain `setup` is the only zero-user path); **concurrency:** two parallel `create_first_admin` (independent sessions) → one success, one `AlreadyBootstrapped`, `COUNT(*) = 1`; CLI-vs-HTTP race test (same C5A guard, two transports; the C5 read-after-lock and transaction-ownership contracts apply); **missing lock row → `LockRowMissing` loud failure** (delete the `first_admin` row → `create_first_admin` fails loudly and creates **no** `users` row); **CLI-before-SECRET_KEY:** run the CLI in an env with `SECRET_KEY` unset → `init-secret` + `setup` succeed (guards fail-loud only inside the web `get_settings`).
- **Verification:** isolated-DB block + full suite + dev-stack diff + `git ls-files | Select-String egg-info` returns nothing.

### S8 — Roles gate + admin account lifecycle (C5B, R3/R5)

- **Branch:** `task/7.8-roles-admin`. **Depends on:** S7 (bootstrap exists to make users deterministically), S5 (CSRF on all mutations).
- **Files:** `src/cookbook/auth/roles.py` (public: `require_role(minimum: Literal["editor","admin"]) -> Callable` FastAPI dependency; reads role from the *current session's user*, 401 for anonymous, 403 for insufficient role); `src/cookbook/auth/admin.py` (public: `create_user`, `set_user_active`, `change_role`, `reset_user_password` — all through C5B's `modify_admin` guard for demote/disable of admins); `tests/test_roles.py`, `tests/test_admin_lifecycle.py`.
- **Controlled test routes (no recipe CRUD in `src/`):** inside `tests/test_roles.py`, attach a tiny router to the app under test: `POST /_gate/editor` (`require_role("editor")`) and `POST /_gate/admin` (`require_role("admin")`), plus `GET /_gate/public` (ungated). These endpoints exist **only in the test module**; they are the permission surface for v1 until the real recipe/category surfaces land in their own plan.
- **Acceptance & tests:** public read ungated (GET passes with no session); editor → `_gate/editor` 200, `_gate/admin` 403; anonymous → both gates 401; `POST /admin/users` (admin, CSRF-valid) creates an editor; disable(editor) wipes their sessions (assert rows gone) and their in-flight cookie no longer authenticates; demote last active admin → blocked by `LastAdminProtected` with the UI surfacing a 403; demote an admin when ≥2 active admins → allowed; **concurrency:** two parallel "remove last admin" operations (disable A + demote B, seeded as the only admins) → exactly one succeeds, invariant `COUNT(active admins) >= 1` holds; reset-password (admin route) invalidates the target's sessions.
- **Verification:** isolated-DB block; `& .venv\Scripts\pytest.exe tests/test_roles.py tests/test_admin_lifecycle.py -v`.

### S9 — Hardening: all-unsafe-methods CSRF + cache split + integrated gate (C2, R6, final gate)

- **Branch:** `task/7.9-hardening-gate`. **Depends on:** S7, S8.
- **Files:** `src/cookbook/auth/middleware.py` (the C2 method-set CSRF gate applied app-wide: `{POST, PUT, PATCH, DELETE}`; pre-auth token check on `/login`+`/setup`, session token on the rest); `src/cookbook/auth/cache.py` (public: `cache_headers_for(path, *, behind_login: bool) -> dict` — public read `public, max-age=300, s-maxage=600`; behind login `private, no-store`); wiring into `create_app()`; `tests/test_auth_gate.py` (the integrated end-to-end below).
- **Acceptance (the final integrated auth verification gate — one ordered flow on an isolated DB):** zero users → `GET /setup` 200 → `POST /setup` → one admin + session → `GET/POST /setup` 403 → `POST /login` (admin, valid CSRF) → `_gate/editor`+`_gate/admin` both allowed → `POST /admin/users` creates editor → editor login → editor allowed on `_gate/editor`, 403 on `_gate/admin` → admin disables editor (sessions wiped, editor cookie dead) → 5 login failures for a pair → 6th is **`429` + `Retry-After`** and no hash work → cross-browser pre-auth replay (Client A token + Client B cookie) → `403` **before** password check, no `users`/`sessions` row → legit same-browser pre-auth → proceeds → `POST /logout` deletes the session row → `Cache-Control` asserted per surface (public recipe-read route vs an authenticated `_gate` response; `private, no-store` behind login, `public, max-age=300, s-maxage=600` on the public GET) → cookie assertions: `COOKIE_SECURE=true` ⇒ `__Host-cookbook_session; Secure` and `COOKIE_SECURE=false` ⇒ `cookbook_session` with **no** `__Host-` and **no** `Secure`; `X-Forwarded-For`/`Proto` honored from `192.0.2.1` (trusted, CIDR `192.0.2.0/24`) and ignored from `198.51.100.1` → teardown: pool dispose, `docker stop`, dev-stack diff clean, `egg-info` absent from `git ls-files`.

## Regression-test updates — inventory arithmetic **and** independent key discovery

`tests/test_schema_inventory.py` carries four **exact-set** invariants. Its current shape *filters discovered PK/FK columns to `varchar(36)` before comparing*, which is the bug the review flags: that filter **hides a UUID key that accidentally changes type** (a `varchar(36)` PK that becomes `char(36)` would still be a key but would vanish from the `varchar(36)`-filtered set, and the "reverse inclusion" would see a *shrunk* set and pass). With v6 merged, the invariant is **restructured to discover keys independently of type** and to assert type separately:

- **`APP_TABLES` — FOURTEEN, not thirteen.** The v0–v5 chain has **eleven** application tables; v6 adds **three** (`sessions`, `login_attempts`, `auth_locks`) → **fourteen** application tables, plus `alembic_version`. The exact-set `tables - {alembic_version} == APP_TABLES` style is *preserved* (a stray new table still fails); the comment/wording in the test is corrected from "eleven/THIRTEEN" to **fourteen**.
- **`EXPECTED_HEAD`** changes from `b7a2f9c4d1e8` to the v6 revision id (S1 pins the literal at implementation time; still exactly one `alembic_version` row).
- **Key discovery — type-independent (the core fix).** Discover **ALL** application PK/FK columns via `information_schema.key_column_usage` where `constraint_name = 'PRIMARY' OR referenced_table_name IS NOT NULL`, **without any `varchar(36)` filter**. Then **exclude Alembic's bookkeeping key explicitly** (discard `(alembic_version, version_num)` *before* the comparison — it is a PK but not an application key). The remaining discovered set is compared to a **union of two explicitly-pinned sets**:
  - **`EXPECTED_UUID_KEYS`** = the application UUID PK/FK columns: the v0–v5 UUID keys **plus** `("sessions","user_id")` and `("login_attempts","id")` (both `varchar(36)`). Nothing else is a UUID key: `sessions.id_hash` is a non-UUID PK (`char(64)`), `auth_locks.name` is a non-UUID PK (`varchar(40)`), and `login_attempts.username`/`ip` sit in `uq_login_attempts_pair`, which is a *unique* constraint — not `PRIMARY` and not an FK (`referenced_table_name IS NULL`), so `key_column_usage` does **not** list them as keys; they are asserted by the separate unique-pair assertion below, not by key discovery.
  - **`EXPECTED_NONUUID_KEYS`** = the application non-UUID PK/FK columns v6 introduces: `("sessions","id_hash")` (`char(64)` PK) and `("auth_locks","name")` (`varchar(40)` PK).
  - **Invariant:** `discovered_keys == EXPECTED_UUID_KEYS ∪ EXPECTED_NONUUID_KEYS`. Because discovery is type-independent, a UUID key that *drifts off* `varchar(36)` is still discovered, still appears in `discovered_keys`, and — being absent from the two expected sets — **fails** as an unexpected key. This is exactly the regression the varchar-filter masked.
- **Type asserted *separately* from discovery (two independent proofs).**
  - **UUID type:** for every `(table, col) in EXPECTED_UUID_KEYS`, assert the live `column_type` is exactly `varchar(36)`. This catches a drift of a *known* UUID key to the wrong type (the reverse of the discovery invariant), and the `char(64)`/`char(32)`/`varchar(64)`/`varchar(40)` columns stay out of this set by construction.
  - **Non-UUID PK types (exact):** assert `("sessions","id_hash")` is **`char(64)`** and `("auth_locks","name")` is **`varchar(40)`** — explicitly, not by absence from the UUID type set. This is the "assert exact non-UUID PK types" requirement.
- **Char/external-ID ledgers preserved (DL-5), now the explicit exception sets:** `CHAR64 = {("sessions","id_hash"), ("migration_runs","file_sha256")}` (the *only* two `char(64)` columns), `CHAR32 = {("sessions","csrf_token")}` (the *only* `char(32)` column), and `EXTERNAL_ID_COLUMNS = {("recipes","source_id"), ("migration_recipe_log","source_post_id")}` (the two `varchar(64)` **external, non-UUID** ids, asserted to be *outside* `EXPECTED_UUID_KEYS`). These are asserted as **exact sets of live columns with the given type** (reverse inclusion included), so a new `char(64)`/`char(32)`/`varchar(64)` column that is *not* one of the listed ones still fails.
- **Unique login-attempt pair assertion kept separate (not merged into key discovery):** `uq_login_attempts_pair (username, ip)` is asserted **directly in the S1 shape test** against `information_schema.statistics` as the exact ordered column list `[username, ip]` on `login_attempts` — as a *unique-key* invariant, distinct from both the key-discovery invariant and the type invariants. Do **not** fold it into `key_column_usage`; it is a uniqueness contract, not a primary-identity one.

**Net effect:** the old single "discovered-`varchar(36)`-keys == UUID-set" invariant is split into (a) a **type-independent exact set** of *all* application keys == expected-UUID ∪ expected-non-UUID keys, (b) **per-column type assertions** for the UUID keys, (c) **exact type assertions** for the non-UUID PKs, and (d) **explicit char/external-ID exception ledgers** — with the Alembic bookkeeping key **excluded explicitly** and the unique pair key **asserted on its own**. Each of these remains an *exact-set* proof (reverse inclusion catches unlisted columns of the relevant kind), so no assertion is relaxed; the test is *restructured*, not weakened.

## Dependencies and config (updated from draft v1)

- **Already present:** `argon2-cffi`, `fastapi`, `uvicorn[standard] 0.54.0`, `sqlalchemy[asyncio]` + `asyncmy`, `alembic`, `pydantic`/`pydantic-settings`, `httpx` (tests).
- **New dependencies (implementation decisions, not owner decisions — spec D1 already names Jinja2+HTMX):** `jinja2`, `python-multipart` (S5).
- **New config keys (spec §9.4):** `TRUSTED_PROXY_CIDRS` (default empty = no trusted peer), `COOKIE_SECURE` (default unset = follow effective scheme), `FIRST_ADMIN_PASSWORD(_FILE)` (bootstrap only; existing `SECRET_KEY(_FILE)`, `SESSION_TTL_HOURS` remain).
- **CLI wiring:** `[project.scripts]` in `pyproject.toml` (S7). Implementation detail.
- **Not a blocker, recorded for deploy:** the owner's concrete `TRUSTED_PROXY_CIDRS` value for the box's reverse proxy (spec §9.2 default is "the proxy's address"). Tests never need it (C4 controlled addresses).

## Owner decisions and flagged proposals (recommendations, not approvals)

**Deliberately NOT owner decisions (implementation within approved scope, flagged for the merge review as such):** adding `jinja2`/`python-multipart`; the CLI console-script wiring (`argparse`, no framework); a concrete `TRUSTED_PROXY_CIDRS` deployment value and the single forwarded-header owner (config, not code — the app owns it via `client_identity`, C4); choosing argon2id parameters *at* the OWASP minimum (spec says "at or above" — the minimum is the lowest approved value); the `login_attempts.id`/`auth_locks`/`sessions` types and the CHAR/UUID/non-UUID-key ledger (schema, tested). Note: the lockout **status code** (O-e) and the **password minimum-length policy** (O-f) **are** owner/product decisions and are **not** in this list — see the table below.

**Genuine owner decisions / spec-ambiguities (recommended resolution + impact; a recommendation is not an approval):**

| # | question | recommended resolution | impact if left / changed later |
|---|---|---|---|
| **O-a** | base + curve of the lockout backoff, and what resets a pair — spec gives only "exponential backoff, cap 1 h" | **Codex recommendation: tiers 10/20/40/60 min (cap); a successful login resets the pair state (row delete).** `lock_count` is *preserved* across a window expiry (the next five-failure episode climbs a tier) but *reset* on a successful login. `locked_until` expiry and window expiry are equality-inclusive (boundary is free). (C1) | a *different* reset policy (e.g. keep `lock_count` across a successful login) would change the owner's lockout experience; the tiers and cap are a one-constant change (no schema migration — the columns exist) if the owner wants a different curve |
| **O-b** | pre-auth CSRF token lifetime & replay policy — spec silent (it mandates per-*session* tokens only) | **Codex recommendation: 15-minute lifetime, browser-bound (cookie secret + signed token over `(secret, purpose, expiry)`); no single-use requirement unless later justified.** The browser binding is the anti-cross-browser-replay control; `SameSite`/Origin/Referer are *additional* defense in depth, **not** a substitute for the binding (finding 1). (C2) | shortening (5 min) or adding single-use is a small code change (a nonce store for single-use, if desired). Widening increases replay window. The binding itself is *implementation within scope* — the *lifetime* is the open product call |
| **O-c** | §6.1 fixes the cookie name as `__Host-cookbook_session` unconditionally; §9.1 dev runs over plain http (a `Secure` cookie is impossible over http; `__Host-` without `Secure` is invalid; and `COOKIE_SECURE=false` can, if honored, drop the name/Secure in prod) | **Codex recommendation: name follows the flag — Secure true ⇒ `__Host-cookbook_session`, Secure false ⇒ a non-`__Host-` `cookbook_session`.** Production default (unset override + https) keeps the secure host cookie; HTTP development uses the non-`__Host-` name. Never emit `__Host-` without `Secure` (named negative test). The dev mode is explicitly *not* a security boundary; a production `COOKIE_SECURE=false` override is a deliberate, warned, logged downgrade (C3). | forcing one fixed name in dev breaks login over http (cookie rejected) or weakens prod if the name is dropped; the split-name is the reading that keeps §6.1's prod posture *and* §9.1's dev path working, and it is honest about what `COOKIE_SECURE=false` actually does |
| **O-d** | `--force-owner-recreation` scope — spec §6.4 names the flag but never says what it may change | **Codex recommendation: recovery = an explicit local CLI recovery of a *named, existing* account:** reset the password for the `--username` account (and restore it to active `admin`); **refuse** it for a username that names no existing user; never mint a *second* admin around the `first_admin` guard. (C6) | a broader scope (arbitrary role/active flip, multi-user) is a standing backdoor-shaped surface; a narrower one (password only, no role/active restore) loses the self-lockout recovery use case that the flag exists for |
| **O-e** | status code for a locked login attempt — spec doesn't specify the code | **Codex recommendation: `429 Too Many Requests` with `Retry-After`**, distinguishing "wait" from `403` CSRF failure and `401`/wrong-password, so templates and HTMX can surface the cooldown and the client can honor a wait. (S6, C1) | changing later is a constant + a small template tweak. `429`+`Retry-After` is the idiomatic, client-actionable choice (vs. `423` which carries no wait hint) |
| **O-f** | **password minimum-length policy** (newly made *visible*; it was previously hidden inside the CLI/`setup` wording) — spec is silent on the *length* policy | **Recommendation: enforce a minimum length of 8 on the `setup` path and on any password write (`reset-password`), non-empty + min-8, shared by HTTP and CLI.** Complexity/character-class policy is *not* mandated for v1 (spec-silent; out of scope). | this is a *product* policy, not an implementation detail: a different minimum (e.g. 12, or 6) is a one-constant change. Making it an O-row is deliberate — it should not sit invisibly inside CLI command wording |

**Status of the above (finding 8, corrected):** the owner **has NOT yet approved any of O-a…O-f.** Each is a **recommendation** (in this case, Codex's), recorded *as a recommendation, not as approval*. Rows in C1–C6, the contracts, and the slice acceptance blocks that are marked **proposal** reflect these *unapproved* recommendations; they are the draft's recommended implementation and are **pending owner review**, not "locked". The draft remains a **draft pending review** — no slice is implemented until the owner approves (or amends) the proposals.
