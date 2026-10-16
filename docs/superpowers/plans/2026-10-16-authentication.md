# Cookbook Authentication Implementation Plan — revised (draft v2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement slices one at a time. This document is a **revised draft**, not an approved implementation contract. It replaced the first draft after Codex review on 2026-10-16. Stop after drafting; no implementation, push, or merge.

**Goal:** Deliver spec §4 authentication & authorization — server-side MariaDB sessions (raw token never stored), argon2id passwords, `editor`/`admin` roles, one-time `/setup` + headless CLI bootstrap, CSRF on every mutation, per-IP+username lockout — against an isolated MariaDB, without advancing the running dev stack.

**Spec:** `docs/superpowers/specs/2026-09-20-cookbook-design.md` — D9/D10 (auth/roles, §6.1–6.7), D18 (proxy-agnostic forwarded headers, §9.2), D20 (file-first secrets, §9.3), D23 (forward-only Alembic).

**Baseline (verified 2026-10-16):** branch `task/7-auth-plan` @ `f96eea1` (draft v1 of this plan); `main` @ `57d252d` with the completed Task 6 data model; `alembic` head `b7a2f9c4d1e8` (v5); last independently verified suite: **89 passing tests**; `argon2-cffi` already a dependency; no `sessions`/`login_attempts` tables, no auth routes, no CLI entry point. uvicorn **0.54.0** installed; `.venv\Scripts\` exposes `alembic.exe`, `pytest.exe`, `uvicorn.exe`.

## Shared contract (applies to every slice)

### Isolated test database (binding, reused from the data-model plan)

**Do not run any migration or test against the running dev `db` container, the dev `cookbook` schema, or the dev `app` stack while a slice is unmerged.** Every MariaDB step runs against the throwaway container `cookbook-test-db` on `127.0.0.1:33061`.

PowerShell (this repo, Windows):

```powershell
# 1) Pre-flight: record dev stack state (proof of "untouched" comes at tear-down)
docker compose ps --format '{{.Name}} {{.Status}}' | Tee-Object /tmp/devstack-before.txt

# 2) Start the disposable test DB only if the name is free (explicit tmpfs, no anonymous volume)
docker run --rm -d --name cookbook-test-db --tmpfs /var/lib/mysql:size=1g `
  --health-cmd="healthcheck.sh --connect --innodb_initialized" `
  --health-interval=3s --health-timeout=3s --health-retries=24 `
  -e MARIADB_ROOT_PASSWORD=devrootpassword -e MARIADB_DATABASE=cookbook `
  -e MARIADB_USER=cookbook -e MARIADB_PASSWORD=devpassword `
  -p 127.0.0.1:33061:3306 mariadb:11

# 3) Wait for healthy (loop exits when the status is 'healthy'; max ~60 s)
for ($i = 0; $i -lt 30; $i++) {
  (docker inspect cookbook-test-db --format '{{.State.Health.Status}}') -match 'healthy' | Out-Null
  if ($LastMatch) { break }
  Start-Sleep -Seconds 2
}
docker inspect cookbook-test-db --format '{{.State.Health.Status}}'   # must print: healthy
```

Env for alembic/pytest (explicit env vars beat the local `.env` in pydantic-settings precedence, so dev secrets can never leak into the test connection):

```powershell
$env:DEBUG="false"; $env:SECRET_KEY="devsecretkey"
$env:DB_HOST="127.0.0.1"; $env:DB_PORT="33061"; $env:DB_NAME="cookbook"
$env:DB_USER="cookbook"; $env:DB_PASSWORD="devpassword"

& .venv\Scripts\alembic.exe upgrade head
& .venv\Scripts\pytest.exe tests -v
```

Tear-down (end of every slice, before commit):

```powershell
docker stop cookbook-test-db
docker compose ps --format '{{.Name}} {{.Status}}' | Tee-Object /tmp/devstack-after.txt
Compare-Object (Get-Content /tmp/devstack-before.txt) (Get-Content /tmp/devstack-after.txt)
if ($null -eq $diff) { "dev stack untouched" }   # no diff output = pass
```

The dev `app`/`db` containers may be running or stopped — either state is fine; the diff must show whatever it was, still the same. Never `docker compose down`.

### Assertion, slicing, and verification conventions

- **Types asserted exactly as MariaDB 11 reports** in `information_schema.columns.column_type` (`char(64)`, `char(32)`, `varchar(36)`, `datetime`, …). Engine pinned to `mariadb:11`. If an assertion fails, the migration diverged: fix the migration, never the test.
- **Indexes as complete ordered column lists** via `information_schema.statistics` (pattern in `tests/test_schema_inventory.py`).
- **Async DB tests are sync test functions wrapping `asyncio.run(run())`** (no pytest-asyncio dependency — existing convention), with cleanup in `finally` and pool `dispose()` exactly once per test.
- **Route tests** use `httpx` ASGI transport against `create_app()` (in-process, no server); controlled addresses `192.0.2.1` (trusted) and `198.51.100.1` (untrusted) stand in for real proxy/client IPs — no deployment CIDR needed for tests.
- **Slice size:** each slice is one fresh-session-sized unit: read this plan + the named spec sections → implement → test → report. Slices land on their own task branch, are reviewed, and merge only on explicit owner approval. Never push or merge without direction.
- **No placeholder recipe CRUD.** Until real application surfaces exist, permission-gate tests attach a *controlled test router* (see S8) to the app instance in the test module. Nothing for gate-verification is added under `src/`.

## Requirements (carried from draft v1, unchanged unless noted)

- **R1 Sessions (§6.1, §3.5):** raw token issued once, never stored; only its SHA-256 hash (`id_hash CHAR(64)` PK) is the row key. Cookie `__Host-cookbook_session` (see §Cookie contract below). Sliding `SESSION_TTL_HOURS` (default 12) idle TTL; expiry and revocation are row deletes. `POST /logout` deletes the row; password change invalidates all of the user's sessions; disabled users' sessions die at next request.
- **R2 Passwords (§6.2):** argon2id via `argon2-cffi` at or above OWASP minimum (m=19 MiB, t=2, p=1). Constant-time verify against a dummy hash for unknown usernames.
- **R3 Roles (§6.3, D9/D10):** public = read only; editor = recipe/category/tag/ingest; admin = +aliases, importer, user management. No per-recipe ownership.
- **R4 Bootstrap (§6.4):** while zero users exist, `/setup` is the only reachable auth surface; after first creation `GET/POST /setup` return 403 forever. Headless `cookbook setup` shares the single-user guard.
- **R5 Account lifecycle (§6.5):** admin creates editors; disable = `is_active FALSE` + session wipe; reset via CLI and admin route; the last active admin cannot be disabled/demoted (count active admins, not rows); no self-signup.
- **R6 Hardening (§6.6):** per-IP+username lockout backed by `login_attempts` (design below); CSRF required on every mutation (design below); public read `Cache-Control: public, max-age=300, s-maxage=600`, everything behind login `private, no-store`.
- **R7 Proxy-agnostic (§9.2, D18):** client IP + scheme from forwarded headers **only when the direct peer is trusted** (design below).

## Contracts (the six concrete designs)

### C1 — Lockout: `login_attempts` schema and state machine (finding 1)

**Spec conflict, resolved explicitly.** §6.6 says "Counters in `users.failed_logins` / `users.locked_until`, keyed loosely by IP+username via a small `login_attempts` table (the spec keeps it to the two `users` columns + the table...)". §3.5 lists `login_attempts` as the lockout store and says "The `users.failed_logins`/`locked_until` columns are dropped; this table is the lockout store." **Resolution: §3.5 wins** — it is the specific, self-consistent table definition, and the v0 `users` migration (already on `main`) contains neither column. This plan adds **no** `users.failed_logins` / `users.locked_until`; the §6.6 parenthetical is read as stale wording. No `users` schema change in v6.

**Locked `login_attempts` shape (one row per normalized (username, ip) pair):**

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

**State machine (all times UTC server time; single row per pair, updated atomically):**

On each failed login for pair (u, ip), let `now` = server time:

1. **Unlocked, no row, or window expired** (`first_failed_at + 10 min < now`): start a new window — `attempts = 1`, `first_failed_at = last_failed_at = now`, `locked_until = NULL`.
2. **Unlocked, window still running**: `attempts += 1`, `last_failed_at = now`. **If `attempts` reaches 5** → `locked_count += 1`, `locked_until = now + duration(lock_count)`.
3. **Locked** (`locked_until >= now`): no field changes (a locked pair does not extend its own lock while locked). Login is refused before password checking (C5 status).

On **successful login** for the pair: the row's state is cleared — `attempts = 0`, `first_failed_at = last_failed_at = NULL` is not expressible (NOT NULL), so the defined behavior is a **row `DELETE`** (simplest, matches the "revocation is a row delete" idiom). `lock_count` resets with the row.

**Lock duration (backoff):** `duration(k) = min(60 min, 10 min * 2^(k-1))` for episode `k` = `lock_count`: 10 min, 20 min, 40 min, then capped at 60 min for every later episode.

**Expiry:** locks are time-based (`locked_until`); no background purge in v1 (rows are tiny and self-contained; a purge job is an additive later concern).

| detail | status |
|---|---|
| five failures / 10-minute window / `locked_until` / exponential backoff / 1 h cap | **spec** (§6.6) |
| base duration 10 min and the exact doubling curve | **spec-silent → proposal** (O-a below; spec gives only "exponential backoff, cap 1 h") |
| full reset (row delete) on successful login | **spec-silent → proposal** |
| locked pair does not extend its lock via further failures | **proposal** (spec silent) |
| row shape (`id`, `first_failed_at`, `last_failed_at`, `lock_count`) | **proposal** (spec §3.5 gives only `id`, `username`, `ip`, `attempts`, `locked_until`; the window-start column is required to *measure* the 10-minute window — the draft's five-column shape could not implement its own rule) |
| lowercase-normalized username in the pair | **proposal** (spec requires only "per-IP+username") |
| no `users.failed_logins`/`locked_until` | **resolved spec conflict** (§3.5 over §6.6, above) |

**Atomic update behavior (contract, not pseudocode):** the state machine is executed as **one SQL round trip** — an `INSERT … ON DUPLICATE KEY UPDATE` keyed by `uq_login_attempts_pair` whose `UPDATE` side computes all five columns from the existing row plus `NOW()`. No read-modify-write across round trips; the unique key guarantees one row per pair under concurrency; concurrent failures on the same pair serialize on the key. `now` semantics: use one `NOW()` value per statement (MariaDB statement-time), so the window check and the writes see the same clock.

**S1 note:** while at it, verify-and-assert the `users.username` collation is case-insensitive (MariaDB default `utf8mb4_*_ci`) so the app's lowercase normalization and the `UNIQUE` key agree; treat any future collation change as migration-only.

### C2 — CSRF: pre-auth tokens + per-session tokens (finding 2)

Spec §6.6 requires "per-session `csrf_token` (§3.5) required on every `POST/PUT/DELETE`". That is impossible for `/login` and `/setup` by definition — no session exists yet. The spec is silent on how pre-auth mutations get a token; this plan fills the gap (no spec edit; no exemption).

**Pre-auth token (for `POST /login`, `POST /setup`) — concrete proposal:**

- **Form:** stateless signed token: `b64url(nonce) . b64url(expiry) . b64url(HMAC-SHA256(SECRET_KEY, nonce|expiry))`. No DB row; issued server-side at page render; server verifies signature (constant-time) + expiry.
- **Lifetime:** 15 minutes from issue. Expiry, not single-use, is the only revocation — replay within lifetime of a *login/setup* token is low-consequence (it can only cause the submitting browser to log in with the credentials the attacker's form already contains; cross-site replay is blocked separately by `SameSite=Lax`).
- **Ordering:** `POST /login` verifies the pre-auth token **before** any password work. `POST /setup` likewise.
- **Issued by:** `GET /login` and `GET /setup` only.
- **Consumed by:** the matching `POST` only. Never stored, so there is nothing to invalidate on logout.

**Session token (post-auth) — spec as written:** per-session `csrf_token CHAR(32)` in `sessions` (random 16 bytes hex, generated at session creation). Every mutation request after login must present it. Password change / disable / session delete removes it with the row.

**Lifecycle across transitions:**

| event | pre-auth token | session token |
|---|---|---|
| `GET /login` or `GET /setup` | fresh one rendered into the form/page context | — |
| `POST /login` success | consumed (signature+expiry checked) | new one created with the `sessions` row, exposed via render context |
| `POST /login` failure / lockout | consumed (checked); pair state updated per C1 | — |
| `POST /setup` success | consumed | new one created with the owner's session |
| any authenticated mutation | — | verified (see delivery) |
| `POST /logout` | — | session row deleted → token gone |
| password change / disable | — | all of the user's rows deleted → tokens gone |

**Delivery (both token classes, two mechanisms):**

- **Classic browser form:** hidden field `<input type="hidden" name="csrf_token" value="…">`.
- **HTMX:** `htmx.config.headers['X-CSRF-Token'] = '…'` set from the page render context (global, per spec §6.6), so HTMX sends the token in the `X-CSRF-Token` header automatically.
- **Server check order:** `X-CSRF-Token` header first, then form field `csrf_token`; if neither is present/valid → `403`. This works uniformly for `multipart/form-data`, `application/x-www-form-urlencoded`, and JSON HTMX bodies (header covers the last).

**Method set (reconciles the spec wording):** the enforcement gate is a **method-set gate, not a route list**: it applies to every route whose method is in `{POST, PUT, PATCH, DELETE}`. v1 routes use `POST` for all state changes (forms + HTMX); a future `PATCH` is automatically protected because the decision is on the method, not on a per-route annotation list. `GET/HEAD/OPTIONS` are never gated. (Spec says `POST/PUT/DELETE`; the set adds `PATCH` because the app could serve it and spec §6.6's intent is "every mutation". This is the plan's concrete proposal — flagged, not a spec change.)

**Spec ambiguity resolved:** none left — the spec's own sentence ("per-session token required on every mutation" + "setup while zero users exist") forces the pre-auth mechanism; only its form and lifetime were open, and they are proposal O-b.

### C3 — Cookie contract and HTTP-dev behavior (finding 3)

**Flags (exact string, both names):**

| flag | value |
|---|---|
| `Name` | `__Host-cookbook_session` when the Secure flag is true, else `cookbook_session` (see rule 4) |
| `Value` | the raw session token |
| `HttpOnly` | always |
| `SameSite` | `Lax` (always) |
| `Path` | `/` (always) |
| `Domain` | **never present** (a `Domain` attribute invalidates a `__Host-` cookie) |
| `Secure` | exactly when the effective secure flag is true (rule 3) |
| `Max-Age` | `SESSION_TTL_HOURS` in seconds; **the cookie is re-sent on every authenticated response** with a fresh `Max-Age`, keeping the client cookie alive in step with the server-side sliding TTL (otherwise the browser would drop the cookie at the original issuance expiry while the server session is still valid). |

1. **Production behavior (HTTPS, unchanged):** the owner's proxy terminates TLS; effective scheme is `https` (via C4) → Secure true → cookie is `__Host-cookbook_session; Secure; HttpOnly; SameSite=Lax; Path=/`, no `Domain`. Production cookies are **not** weakened by anything in this contract.
2. **`COOKIE_SECURE` (§9.4):** explicit override wins. Effective secure flag = `COOKIE_SECURE` if set, else `(effective scheme == "https")`.
3. **Non-secure local development:** over `http://localhost:8000` the effective scheme is `http` → Secure false. A `__Host-` cookie is invalid without `Secure` (browsers reject it per the `__Host-` rules), and `Secure` cookies don't ride over plain http — **these two spec requirements conflict in the HTTP-dev case**: §6.1 fixes the name as `__Host-cookbook_session` (unconditional), while §9.4 permits `COOKIE_SECURE` override and §9.1 runs dev over plain http.
4. **Coherent resolution (proposal O-c, flagged not silent):** the **name** is a function of the flag: **Secure flag true ⇒ `__Host-cookbook_session`; Secure flag false ⇒ `cookbook_session`** (same flags otherwise). The invariant — never emit a `__Host-`-prefixed cookie without `Secure` — is a named test: an integration test with Secure forced off asserts the `Set-Cookie` name is the non-prefixed one and that no `__Host-` cookie appears.

### C4 — Trusted proxy / client identity (finding 4)

**Evidence (inspected once, 2026-10-16):** installed uvicorn **0.54.0** — `--proxy-headers` defaults **on**; `--forwarded-allow-ips` defaults to `$FORWARDED_ALLOW_IPS` or `127.0.0.1,::1`. The Dockerfile `CMD` (`uvicorn cookbook.app:create_app --factory --host 0.0.0.0 --port 8000`) passes **no** proxy flags, and `bin/docker-entrypoint.sh` adds none. Consequence: in the container, Uvicorn trusts forwarded headers **only from localhost peers**; the owner's reverse proxy connects from the docker network / LAN, so by default its `X-Forwarded-For`/`X-Forwarded-Proto` would already be ignored in prod. Uvicorn's trust list is a *server* setting (per-process, not per-request config) and gives the app no way to *know* which peer was trusted — so the app cannot delegate the trust decision to it.

**Trust boundary (contract):**

- **Trusted peer** = the direct TCP source of the request (`request.client.host`) falls inside `TRUSTED_PROXY_CIDRS` (comma-separated CIDRs; parsed at boot; unset/empty = no peer is trusted).
- **Trusted peer present:** client IP = right-to-left walk of `X-Forwarded-For`, taking the **first address not in a trusted CIDR**; if every listed address is trusted (or the header is absent), client IP = the direct peer. Scheme = `X-Forwarded-Proto` (first value) if present, else the direct scheme.
- **Untrusted peer:** `X-Forwarded-For` / `X-Forwarded-Proto` are **ignored entirely** (they must not leak into lockout keys or the Secure decision); client IP = direct peer; scheme = direct scheme.
- **Single access point:** one helper `client_identity(request, settings) -> (client_ip: str, scheme: str)` in `src/cookbook/auth/identity.py`. Every consumer — lockout key (C1), cookie Secure decision (C3), request logging — calls it. No ad-hoc `request.headers["X-Forwarded-For"]` reads anywhere else. This is a shared utility, not mandatory custom middleware; the app-level decision exists because Uvicorn's is a static per-process default, not a per-request authorization (see evidence above).
- **Availability:** `client_identity` is a dependency of every later slice that needs IP/scheme. It lands in **S2, before lockout (S6)**. A concrete deployment CIDR is the owner's configuration at deploy time — it is **not** a blocker to implementation or to tests; tests use controlled addresses: trusted `192.0.2.1`, untrusted `198.51.100.1` (both TEST-NET ranges, never routeable to the owner's network).

**Acceptance (S2):** same request body from trusted vs untrusted source → different `client_ip`/`scheme` results per the table above; untrusted source with forged `X-Forwarded-For: 8.8.8.8` yields the direct peer, not `8.8.8.8`; `TRUSTED_PROXY_CIDRS` unset → untrusted always.

### C5 — Concurrency invariants (finding 5)

Two invariants must hold under concurrent writes. MariaDB/InnoDB notes that shape the design: a plain `SELECT COUNT(*) FROM users` takes **no locks** (snapshot read); with an empty or small table there is no row to lock, so a count-then-write guard is **unsafe by default** — this is exactly the unlocked "count-then-write" pattern the review forbids. The design therefore uses named lock rows acquired with `SELECT … FOR UPDATE` (a locking read that sees current committed data and blocks concurrent acquirers until the holder commits), plus row-level locking where rows exist.

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

### C6 — Bootstrap, CLI, and config (finding 6)

**CLI surface (minimal, stdlib only — `argparse`, no new framework) — `src/cookbook/cli.py`, console script `cookbook`:**

| command | modes | behavior |
|---|---|---|
| `cookbook init-secret [--out PATH] [--overwrite]` | file | generate 32 random bytes (`secrets.token_hex(32)`) → write to file (best-effort `0600` perms) → print path. Refuses to overwrite an existing file without `--overwrite` (rotation is deliberate, per spec §9.3). **Never requires `SECRET_KEY` to already exist.** |
| `cookbook setup` | interactive (default) \| `--username` + `--password-file` \| `FIRST_ADMIN_PASSWORD(_FILE)` fallback | creates the owner admin via the C5A guard. Interactive: prompt username (default `owner`), then password + confirmation via `getpass` (no echo, not on stdout). Order of password source: `--password-file` > `FIRST_ADMIN_PASSWORD_FILE` > `FIRST_ADMIN_PASSWORD` (plain env = **dev only**: a loud warning is logged when used outside a dev-looking config — spec §6.4 keeps the warning-not-failure posture, §9.3). Password rules shared with `POST /setup`: non-empty, min length 8 (proposed, O-b-adjacent flag) — the *value* policy (e.g. complexity) is spec-silent, so the plan locks a minimum-length check and calls it a proposal. `--force-owner-recreation` scope: see below. **Does not require `SECRET_KEY`** (argon2id needs no app key). |
| `cookbook user reset-password <name> (--interactive \| --password-file PATH)` | interactive \| file | hash the new password (argon2id), update the user row, **delete all of that user's sessions** (spec §6.1: password change invalidates all of the user's sessions). User missing → non-zero exit + message. **Does not require `SECRET_KEY`.** |

**`--force-owner-recreation` — exact scope (concrete proposal to be confirmed at merge review, O-d):**

- **Scope:** it is a *named-account recovery*, not "delete users and re-create". With `--username <name>`: if a user with that username exists → reset its password (from the sources above) and set `role='admin'`, `is_active=TRUE` (self-lockout recovery after a forgotten password). If **no user exists** → identical to plain `setup` (normal bootstrap). If users exist and `--username` names **no existing user** → **refuse** (it will not mint a second admin around the single-user guard).
- **Safeguards:** requires an explicit `--username`; requires a typed-confirmation prompt even in `--password-file` mode (`yes/<username>`); logs a warning line; refuses when it would create a *additional* admin while other users exist (the second-admin case is covered by the ordinary admin UI, which is the sanctioned path after bootstrap).
- **Owner decision remaining (O-d):** whether that scope is the intended recovery semantic, or whether the flag should be narrower (password-only, no role/active restoration) or removed from v1. Impact: it is the only path that changes an existing user's role without the admin UI; a mis-scope is a backdoor-shaped bug.

**File-first settings (already in code, extended):** `config.py` already implements `_from_secret_file` for `SECRET_KEY` / `DB_PASSWORD`. v6 adds file-first handling for `FIRST_ADMIN_PASSWORD` (same first-line/newline-strip rule, `_FILE` wins over plain). `SECRET_KEY_FILE`/`LLM_API_KEY_FILE` are spec-enumerated and land when their consumers do (not in this plan's slices).

**`.env.example` changes (spec §9.4 surface, to be applied in S7):** add `SECRET_KEY_FILE` (example path, commented real-file guidance), `SESSION_TTL_HOURS` (default 12, commented), `TRUSTED_PROXY_CIDRS` (commented, empty by default), `COOKIE_SECURE` (commented, "auto from effective scheme unless set"), `FIRST_ADMIN_PASSWORD_FILE` (commented, bootstrap-only) + a comment that `FIRST_ADMIN_PASSWORD` (plain) is dev-only and warned. Keep the list boring and short (D24): no new non-secret settings beyond C3/C4.

**CLI-before-`SECRET_KEY`:** the CLI path never calls `get_settings()` (which today *raises* on missing `SECRET_KEY` — correct for the web, wrong for the CLI). The CLI builds a lean settings view (DB parts + `MEDIA_DIR` only) — a small `cli_config()` in `config.py`. `init-secret` trivially works pre-`SECRET_KEY`; `setup`/`reset-password` work pre- and post. `uvicorn` (web) keeps the hard fail-fast on missing key (D20).

## Slices (finding 7 — rewritten, each small enough for one fresh session)

Ordering is fixed; later slices consume earlier contracts. S2 is *before* any IP-dependent behavior so lockout and cookies have the identity primitive, per C4.

### S1 — v6 schema: `sessions`, `login_attempts`, `auth_locks` + regression-inventory update

- **Branch:** `task/7.1-auth-schema` (from `main` after `task/7-auth-plan` merges).
- **Goal:** the three tables of C1/C5 exist, typed exactly as locked, and the full-chain inventory test still passes with its **exact-set** assertions (updated, not disabled).
- **Files:** `src/cookbook/models.py` (add `Session`, `LoginAttempt`, `AuthLock`); `alembic/versions/<new>_v6_auth.py` (down_revision `b7a2f9c4d1e8`; seed the two `auth_locks` rows in the revision); `tests/test_auth_schema.py` (new); `tests/test_schema_inventory.py` (expected-set updates — see Regression updates below).
- **Locked schema additions:** exactly C1 (`sessions`, `login_attempts`) and C5 (`auth_locks`) shapes. `users` is **untouched** (no `failed_logins`/`locked_until` — C1).
- **Acceptance & tests:** full-chain `upgrade head` from v5 is clean and single-head; per-table column type/nullability/index/FK assertions (`sessions.id_hash char(64)` PK; `csrf_token char(32)`; `user_id varchar(36)` FK→`users.id` `ON DELETE CASCADE`; `login_attempts.uq_login_attempts_pair` unique exactly `[username, ip]`; `auth_locks` exactly two locked rows); the updated inventory exact-set tests pass unchanged in *style* (reverse inclusions still catch unlisted columns); the 89-test baseline suite still green; dev-stack diff clean.
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
- **Locked details:** argon2id via `argon2-cffi` at OWASP minimum (m=19 MiB, t=2, p=1) — *at or above* allowed by §6.2; raising params is an owner choice (O-b). Constant-time verify incl. unknown-user path: a committed dummy hash constant; both paths execute exactly one `argon2.PasswordHasher.verify`.
- **Acceptance & tests:** round-trip; wrong-password rejection; unknown-user path executes and takes comparable timing to a known-user failure (assert only that the dummy path *runs* — no wall-clock flakiness); hash output parses as argon2id.
- **Verification:** `& .venv\Scripts\pytest.exe tests/test_passwords.py -v`.

### S4 — Session service: issue/verify/revoke + cookie contract (C3)

- **Branch:** `task/7.4-session-core`. **Depends on:** S1, S2, S3.
- **Files:** `src/cookbook/auth/sessions.py` (public: `create_session(...)->(raw_token, row)`, `load_session(request)->User|None`, `revoke_session(raw_token)`, `revoke_user_sessions(user_id)`, `cookie_set(raw_token, *, secure: bool) -> str` producing the exact `Set-Cookie` per C3); `tests/test_session_core.py`.
- **Locked details:** raw token = `secrets.token_urlsafe(32)`; stored key = `SHA-256(raw)` as `id_hash`; per-session `csrf_token` = `secrets.token_hex(16)` (32 chars); sliding TTL = `SESSION_TTL_HOURS` from `created/last_seen`; cookie name/flags are exactly C3 including the re-send-on-every-authenticated-response rule.
- **Acceptance & tests:** create → row shape; load with raw token against a seeded hash; expiry (clock-injected) → row deleted + `None`; revoke_user_sessions deletes all of a user's rows; `cookie_set(secure=True/False)` emits exactly the two C3 name/flag sets (assert the full `Set-Cookie` header string; assert **no** `Domain`, `Path=/`, `HttpOnly`, `SameSite=Lax`; and the negative invariant: Secure false never produces a `__Host-` name).
- **Verification:** isolated-DB PowerShell block; `& .venv\Scripts\pytest.exe tests/test_session_core.py -v`.

### S5 — Login/logout routes + pre-auth CSRF (C2)

- **Branch:** `task/7.5-login-routes`. **Depends on:** S4.
- **Files:** `src/cookbook/auth/csrf.py` (public: `preauth_token_*` issue/verify, `validate_csrf(request) -> bool`); `src/cookbook/auth/routes.py` (`GET /login`, `POST /login`, `POST /logout`, template render context for `htmx.config` + hidden field); `src/cookbook/templates/` minimal `base.html`/`login.html` (Jinja2 already implied by D1 but not yet a dep — **implementation dependency note, not an owner decision:** add `jinja2` + `python-multipart` to `pyproject.toml` in this slice); `tests/test_login_routes.py`.
- **Acceptance & tests (integration, httpx ASGI):** successful form login (pre-auth token present) → session row + `Set-Cookie` + 302 to `/`; login with missing/invalid/expired pre-auth token → `403` *before* password check (assert no `login_attempts` row was written); unknown-user path → same `401/403` shape as wrong-password and the dummy-hash path ran (S3 contract); valid pre-auth token + valid password → `200`/redirect; `POST /logout` deletes the session row and clears the cookie; the rendered page sets `htmx.config.headers['X-CSRF-Token']` and contains a hidden field whose values match; a mutation POST with `X-CSRF-Token` header and with hidden field both accepted, neither present → `403`.
- **Verification:** isolated-DB block + full suite regression (S1–S4 tests still green) + dev-stack diff.

### S6 — Lockout service + login integration (C1, C5 invariant A's lock row is already present)

- **Branch:** `task/7.6-lockout`. **Depends on:** S5 (login route exists to hook into), S2 (client identity), S1 (table).
- **Files:** `src/cookbook/auth/lockout.py` (public: `record_failure(username, ip) -> LockState`, `assert_not_locked(username, ip) -> None` (raises `Locked` with remaining time), `clear_pair(username, ip)`), and its wiring into `POST /login`; `tests/test_lockout.py`.
- **Locked details:** the C1 state machine, exactly; one `INSERT … ON DUPLICATE KEY UPDATE` per failure (no read-then-write); login refused while locked with `423` (status-code choice is a minor proposal, O-e) and **no** password verification (locked state is already public information); success path calls `clear_pair` with the *identity* pair from `client_identity`.
- **Acceptance & tests & concurrency:** 1st–4th failures within 10 min → `attempts` climbs, `locked_until` NULL; 5th → `locked_until ≈ now + 10 min`, 6th attempt → `423` with no hash work; window expiry (clock-injected) resets the window and allows a fresh 5; backoff tiers 10/20/40/60-min-cap asserted across reset cycles (clock-injected); successful login deletes the pair row; two concurrent failures for the same pair land as `attempts = 2` (atomicity — the unique key serializes); lockout is keyed by `client_identity` output (forged-header case stays untrusted-keyed).
- **Verification:** isolated-DB block; `& .venv\Scripts\pytest.exe tests/test_lockout.py -v`.

### S7 — Bootstrap: `/setup` route + CLI (C5A, C6)

- **Branch:** `task/7.7-bootstrap`. **Depends on:** S5 (login/session), S1 (`auth_locks`).
- **Files:** `src/cookbook/auth/bootstrap.py` (public: `create_first_admin(session, username, password) -> User` (C5A transaction exactly); `is_bootstrapped(session) -> bool`); `src/cookbook/auth/routes.py` (+`GET/POST /setup`); `src/cookbook/cli.py` (`init-secret`, `setup`, `user reset-password` — C6 table, `argparse` only, `cli_config()` that does not require `SECRET_KEY`); `pyproject.toml` (add `[project.scripts] cookbook = "cookbook.cli:main"` — **implementation wiring, not an owner decision**); `.env.example` (C6 additions); `tests/test_bootstrap.py`, `tests/test_cli.py`.
- **Acceptance & tests:** zero-user → `GET /setup` 200, `POST /setup` (valid pre-auth token + rules) → exactly one `admin` row + 1 owner session; `GET/POST /setup` thereafter → `403`; CLI `setup` interactive (monkeypatched `getpass`) and `--password-file` modes both create the owner; `init-secret` writes the file, refuses overwrite without `--overwrite`, and the produced value satisfies `verify_password`-free `Settings` acceptance; `user reset-password` changes the hash and **wipes that user's sessions**; `FIRST_ADMIN_PASSWORD` plain-env path logs the dev warning; `--force-owner-recreation` behaves per the O-d proposal (named reset + role/active restore; unknown username refused when users exist); **concurrency:** two parallel `create_first_admin` (independent sessions) → one success, one `AlreadyBootstrapped`, `COUNT(*) = 1`; CLI-vs-HTTP race test (same guard, two transports); **CLI-before-SECRET_KEY:** run the CLI in an env with `SECRET_KEY` unset → `init-secret` + `setup` succeed (guards fail-loud only inside the web `get_settings`).
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
- **Acceptance (the final integrated auth verification gate — one ordered flow on an isolated DB):** zero users → `GET /setup` 200 → `POST /setup` → one admin + session → `GET/POST /setup` 403 → `POST /login` (admin, valid CSRF) → `_gate/editor`+`_gate/admin` both allowed → `POST /admin/users` creates editor → editor login → editor allowed on `_gate/editor`, 403 on `_gate/admin` → admin disables editor (sessions wiped, editor cookie dead) → 5 login failures for a pair → 6th is `423` and no hash work → valid pre-auth CSRF absent on `/login` → `403` before password check, present → proceeds → `POST /logout` deletes the session row → `Cache-Control` asserted per surface (public recipe-read route vs an authenticated `_gate` response; `private, no-store` behind login, `public, max-age=300, s-maxage=600` on the public GET) → cookie assertions: `COOKIE_SECURE=true` ⇒ `__Host-cookbook_session; Secure` and `COOKIE_SECURE=false` ⇒ `cookbook_session` with **no** `__Host-` and **no** `Secure`; `X-Forwarded-For`/`Proto` honored from `192.0.2.1` (trusted, CIDR `192.0.2.0/24`) and ignored from `198.51.100.1` → teardown: pool dispose, `docker stop`, dev-stack diff clean, `egg-info` absent from `git ls-files`.

## Regression-test updates (finding 8) — exactly what changes and why

`tests/test_schema_inventory.py` carries four **exact-set** invariants. With v6 merged, these are updated (not relaxed):

- **`APP_TABLES`** gains `sessions`, `login_attempts`, `auth_locks` → the assertion reads "the THIRTEEN application tables + `alembic_version`" (was eleven). The exact-set style is *preserved*: a stray new table still fails.
- **`EXPECTED_HEAD`** changes from `b7a2f9c4d1e8` to the v6 revision id generated in S1 (the slice pins the literal in the test at implementation time; the head assertion still requires exactly one `alembic_version` row).
- **`EXPECTED_UUID_COLUMNS`** gains `("sessions","user_id")` and `("login_attempts","id")` only (both `varchar(36)`) — nothing else: `sessions.id_hash` is `char(64)` (not UUID), `sessions.csrf_token` is `char(32)`, `auth_locks.name` is `varchar(40)` (not UUID), `login_attempts.username`/`ip` are not identity columns.
- **Key-discovery assertion** (the `key_column_usage` "PRIMARY or FK" set == `EXPECTED_UUID_COLUMNS`): discovered keys now *include* non-UUID keys — `sessions.id_hash` (`char(64)` PK), `auth_locks.name` (`varchar(40)` PK), and `login_attempts.username`/`ip` (non-unique `uq_login_attempts_pair` is *not* in `key_column_usage` as a "PRIMARY or referenced" key, so it is not discovered — the unique pair key is instead asserted directly in the S1 shape test). **Fix:** the invariant filters discovered key columns to *those whose live `column_type` is `varchar(36)`* before comparing to `EXPECTED_UUID_COLUMNS`, and separately asserts the documented CHAR/other exceptions exist: `char(64)` set = `{("sessions","id_hash"), ("migration_runs","file_sha256")}`; the unique-char(32) column = `("sessions","csrf_token")`; `("auth_locks","name")` `varchar(40)` PK present. The exact-set checks are thus preserved (reverse inclusion still catches an unlisted `varchar(36)` column), with the CHAR token/hash exceptions made explicit rather than disabled.
- **DL-5 exceptions ledger (new constant, asserted):** `CHAR64 = {(sessions,id_hash),(migration_runs,file_sha256)}`, `CHAR32 = {(sessions,csrf_token)}` — replacing the old single-exception assumption with an explicit, still-exact set.

No other test file changes. The 89-test baseline remains the regression floor for slices S2–S9 (each adds its own file; none deletes or weakens an existing assertion).

## Dependencies and config (updated from draft v1)

- **Already present:** `argon2-cffi`, `fastapi`, `uvicorn[standard] 0.54.0`, `sqlalchemy[asyncio]` + `asyncmy`, `alembic`, `pydantic`/`pydantic-settings`, `httpx` (tests).
- **New dependencies (implementation decisions, not owner decisions — spec D1 already names Jinja2+HTMX):** `jinja2`, `python-multipart` (S5).
- **New config keys (spec §9.4):** `TRUSTED_PROXY_CIDRS` (default empty = no trusted peer), `COOKIE_SECURE` (default unset = follow effective scheme), `FIRST_ADMIN_PASSWORD(_FILE)` (bootstrap only; existing `SECRET_KEY(_FILE)`, `SESSION_TTL_HOURS` remain).
- **CLI wiring:** `[project.scripts]` in `pyproject.toml` (S7). Implementation detail.
- **Not a blocker, recorded for deploy:** the owner's concrete `TRUSTED_PROXY_CIDRS` value for the box's reverse proxy (spec §9.2 default is "the proxy's address"). Tests never need it (C4 controlled addresses).

## Owner decisions and flagged proposals (finding 9)

**Deliberately NOT owner decisions (implementation within approved scope, flagged for the merge review as such):** adding `jinja2`/`python-multipart`; the CLI console-script wiring (`argparse`, no framework); a concrete `TRUSTED_PROXY_CIDRS` deployment value (config, not code); choosing argon2id parameters *at* the OWASP minimum (spec says "at or above" — the minimum is the lowest approved value); the `login_attempts.id`/`auth_locks` types and the CHAR/UUID ledger (locked schema, tested); the `423` lockout status code (O-e below is behavior, not mechanism); the stateless pre-auth token *mechanism*.

**Genuine owner decisions / spec-ambiguities (recommended resolution + impact; a recommendation is not an approval):**

| # | question | recommended resolution | impact if left / changed later |
|---|---|---|---|
| **O-a** | base + curve of the lockout backoff — spec gives only "exponential backoff, cap 1 h" and is silent on the base duration and on what resets a pair | **base = 10 min, doubling (10/20/40, cap 60 min); pair state deleted on a successful login; no extension while locked** (C1) | changing the curve later is a one-constant edit (no schema migration — the columns are already there); a different reset policy (e.g. "keep `lock_count` across a successful login") would change the feel of the lockout for the owner |
| **O-b** | pre-auth CSRF token lifetime & replay policy (spec silent: it only mandates per-*session* tokens) | **15 min, signature-verified, replay within lifetime tolerated** (C2) — cross-site replay already blocked by `SameSite=Lax` | tightening (per-form single-use, 5 min) is a small code change and needs a row or a nonce store — that is the cost of tightening |
| **O-c** | §6.1 fixes the cookie name as `__Host-cookbook_session` unconditionally; §9.1 dev runs over plain http (Secure cookie impossible; `__Host-` without Secure is invalid) | **name follows the flag:** `__Host-cookbook_session` ⇔ Secure true, else `cookbook_session` (C3) — production (https) is invariant; never emit `__Host-` without `Secure` (named negative test) | forcing a single fixed name in dev would break login over http (cookie rejected) or weaken prod if the name is dropped; the split-name is the only reading of the spec that keeps both §6.1's prod posture and §9.1's dev path working |
| **O-d** | `--force-owner-recreation` scope — spec §6.4 names the flag but never says what it may change | **named-account recovery:** reset password for the `--username` account + restore it to active `admin`; refuse it for a username that names no existing user; never create a *second* admin around the guard (C6) — owner confirms or narrows | a broader scope (arbitrary role/active flip, multi-user) is a standing backdoor-shaped surface; a narrower one (password only) loses the self-lockout recovery use case |
| **O-e** | status code for a locked login attempt (spec doesn't specify) | **`423 Locked`**, distinguishable from `403` CSRF failure and `401` unknown/anonymous, so templates and HTMX can tell the owner "wait" apart from "wrong" | purely cosmetic for v1; changing later requires only a constant + a template tweak |

Anything in C1–C6 marked **proposal** is locked in this plan and tested, and the owner overrides it by amendment, not silently at implementation time.
