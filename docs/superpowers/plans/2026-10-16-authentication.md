# Cookbook Authentication Implementation Plan — first draft

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver spec §4 authentication & authorization — server-side MariaDB sessions (raw token never stored), argon2id passwords, `editor`/`admin` roles, one-time `/setup` + headless CLI bootstrap, CSRF on every mutation, and per-IP+username lockout — against isolated MariaDB, without advancing the running dev stack.

**Spec:** `docs/superpowers/specs/2026-09-20-cookbook-design.md` — D9/D10 (auth/roles, §6.1–6.7), D18 (proxy-agnostic forwarded headers, §9.2), D20 (file-first secrets, §9.3), D23 (forward-only Alembic). This is the "auth plan" the data-model plan defers (`sessions`, `login_attempts`, CHAR token/hash types).

**Baseline (already on `main`):** v0 `users` table (shape below), `argon2-cffi` already in dependencies, FastAPI `create_app()` with `/healthz`+`/readyz`, `get_engine()`/`session_scope()`, Alembic head `b7a2f9c4d1e8` (v5). No `sessions`/`login_attempts` tables, no auth routes, no CLI entry point yet.

## Requirements

- **R1 Sessions (§6.1, §3.5):** raw session token issued once and never stored; only its SHA-256 hash is the row key (`id_hash CHAR(64)` PK). Cookie `__Host-cookbook_session`, `HttpOnly`, `Secure`, `SameSite=Lax`. Sliding 12 h idle TTL (`SESSION_TTL_HOURS`); expiry and revocation are row deletes (no refresh machinery). `POST /logout` deletes the row; password change invalidates all of the user's sessions; disabled users' sessions die at next request.
- **R2 Passwords (§6.2):** argon2id via `argon2-cffi` at or above OWASP minimum (m=19 MiB, t=2, p=1). Constant-time verify against a dummy hash for unknown usernames (R6).
- **R3 Roles (§6.3, D9/D10):** public = read only; editor = recipe/category/tag/ingest; admin = +aliases, importer, user management. No per-recipe ownership; `created_by`/`last_modified_by` attribution only.
- **R4 Bootstrap (§6.4):** while zero users exist, `GET /setup` is the only reachable auth surface; `POST /setup` writes the owner admin row and then returns 403 forever. Headless alternative `cookbook setup --username … --password-file …` shares the single-user guard.
- **R5 Account lifecycle (§6.5):** admin creates editors (`POST /admin/users`); disable = `is_active FALSE` + session wipe; reset via CLI `cookbook user reset-password <name>` and admin `POST /admin/users/{id}/reset-password`; the last active admin cannot be disabled/demoted (check counts active admins, not rows); no self-signup.
- **R6 Hardening (§6.6):** per-IP+username lockout (5 failures / 10 min → `locked_until`, exponential backoff, cap 1 h) backed by `login_attempts`; CSRF token required on every `POST/PUT/DELETE`; public read `Cache-Control: public, max-age=300, s-maxage=600`, everything behind login `private, no-store`.
- **R7 Proxy-agnostic (§9.2, D18):** consume `X-Forwarded-For` (first untrusted hop ignored) and `X-Forwarded-Proto` only when `TRUSTED_PROXY_CIDRS` authorizes them; `Secure`-cookie/redirect behavior follows the forwarded proto. No app-level TLS or domain config.
- **Out of v1 (§6.7):** self-signup, OAuth/SSO, 2FA/TOTP, API tokens, email. Additive later.

## Ordered implementation slices

Slice order is fixed; each slice lands on its own task branch, reviewed, and merged only on explicit owner approval. All MariaDB steps run against the isolated `cookbook-test-db` tmpfs container (reuse the data-model plan's shared contract: snapshot dev stack, `p 127.0.0.1:33061`, `env CT=…` overrides, tear down + `diff` proof before commit).

### S1 — `sessions` + `login_attempts` tables (model, migration, shape tests)
- **Files:** modify `src/cookbook/models.py`; create `alembic/versions/<r6>_v6_auth.py` (down_revision `b7a2f9c4d1e8`); create `tests/test_auth_schema.py`.
- **Locked shapes (spec §3.5):**
  - `sessions`: `id_hash CHAR(64)` PK (SHA-256 of raw token); `user_id VARCHAR(36)` NOT NULL FK → `users.id` ON DELETE CASCADE; `csrf_token CHAR(32)` NOT NULL; `created_ip VARCHAR(45)`; `created_at`, `last_seen_at`, `expires_at` DATETIME. DL-5: fixed-length hashes/tokens keep CHAR; `user_id` is a UUID → `varchar(36)`.
  - `login_attempts`: `id` (VARCHAR(36) PK or a small int — pick one, lock in tests), `username VARCHAR(60)`, `ip VARCHAR(45)`, `attempts INT NOT NULL`, `locked_until DATETIME NULL`.
- **User schema:** preserve the existing v0 shape. Do **not** assert it is immutable — verify the collation that makes `username` UNIQUE effectively case-insensitive and treat any change as a migration only if an approved requirement demonstrably needs it (spec §3.5 lists no new `users` columns; `failed_logins`/`locked_until` were never added here).
- **Acceptance:** `information_schema` shape asserts (types exactly as MariaDB 11 reports, including `char(64)`/`char(32)` for the token/hash and `varchar(36)` for `user_id`); FK `user_id → users.id` CASCADE; `alembic upgrade head` clean v5→v6; teardown diff proves dev stack untouched.

### S2 — Session core + login/logout
- **Scope:** argon2id verify (dummy-hash on unknown user), issue raw token → `sha256` → INSERT `sessions`, set cookie; validate/refresh `last_seen_at`/`expires_at` on request; `POST /logout` deletes the row; password-change and disable paths wipe the user's sessions.
- **Dependency note (implementation, not an owner decision):** add `python-multipart` for FastAPI form parsing of `POST /login` (username+password as form fields) — it is not currently in `pyproject.toml`.
- **Acceptance:** integration test (httpx/ASGI transport) covering successful login, unknown-user constant-time path, expiry/revocation via row delete, logout.

### S3 — Bootstrap `/setup` + headless CLI
- **Scope:** `GET /setup` reachable only while `SELECT COUNT(*) FROM users = 0`; `POST /setup` validates username+password+confirmation, writes the owner admin, then 403 forever. Headless `cookbook setup` shares the same single-user guard.
- **Dependency note (implementation proposal, not an owner decision):** introduce a minimal CLI entry point (console script on the existing `cookbook` package) exposing `setup` and `user reset-password`; this is the mechanism, not a spec choice.
- **Acceptance:** zero-user → `/setup` reachable, first POST creates exactly one admin, second access 403; CLI guard refuses when any user exists (absent `--force-owner-recreation`).

### S4 — Roles & permission gate
- **Scope:** `require_editor` / `require_admin` dependencies reading the role from the session's user; apply to each capability in the §6.3 table; public read stays open.
- **Acceptance:** editor blocked from `/admin/*`; admin allowed; unauthenticated mutation → 401/403; read surfaces open.

### S5 — Account lifecycle (admin)
- **Scope:** `POST /admin/users`, disable (session wipe), `POST /admin/users/{id}/reset-password`, role change; last-active-admin rule enforced by counting active admins (not rows).
- **Acceptance:** editor cannot self-disable last admin; disable wipes that user's sessions; reset invalidates all of that user's sessions.

### S6 — Login hardening (lockout + CSRF + cache)
- **Scope:** `login_attempts` counter (5/10 min → `locked_until`, backoff cap 1 h); per-session `csrf_token` (CHAR(32)) required on `POST/PUT/DELETE` from `X-CSRF-Token` header or hidden field; `Cache-Control` split per §6.6.
- **Acceptance:** 6th failure in window locked until `locked_until`; CSRF absent → 403, present → 200; cache headers asserted per surface.

### S7 — Proxy-agnostic forwarded headers
- **Scope:** honor `X-Forwarded-For`/`X-Forwarded-Proto` only when `TRUSTED_PROXY_CIDRS` authorizes; drive `Secure`-cookie/redirect from forwarded proto.
- **Note (per scope correction):** do **not** claim custom middleware is mandatory because Starlette lacks a `TrustedProxyMiddleware`. The slice first inspects **Uvicorn's existing** proxy support (`--proxy-headers` / `--forwarded-allow-ips`) and only adds in-app handling if that does not satisfy R7. The mechanism is resolved by inspection in this slice, not pre-decided here.
- **Acceptance:** with a trusted source IP, forwarded IP/proto are consumed (lockout keyed on the real client, `Secure` set on https); from an untrusted source the headers are ignored.

## Dependencies

- **Already present:** `argon2-cffi`, `fastapi`, `uvicorn[standard]`, `sqlalchemy[asyncio]` + `asyncmy`, `alembic`, `pydantic`/`pydantic-settings`, `httpx` (tests).
- **Proposed dependency addition (implementation, not an owner decision):** `python-multipart` for form parsing (S2).
- **Proposed CLI entry point (implementation proposal, not an owner decision):** a minimal console script on the `cookbook` package for `setup` and `user reset-password` (S3/S5).
- **Config additions (spec §9.4, already enumerated):** `SESSION_TTL_HOURS` (default 12, already in `Settings`), `TRUSTED_PROXY_CIDRS`, `COOKIE_SECURE` (auto from forwardable proto). `TRUSTED_PROXY_CIDRS` needs a concrete value before S7's real-edge acceptance passes (see Unresolved).
- **Testing:** same isolated-`cookbook-test-db` + `env CT=…` + no-`pytest-asyncio`-for-DB pattern as the data-model plan; httpx/ASGI transport for route tests.

## Acceptance checks (end-to-end)

1. `alembic upgrade head` from v5 applies a single new revision cleanly; shape tests green against isolated MariaDB.
2. Full flow: zero-user → bootstrap one admin → login → editor CRUD allowed, `/admin/*` denied to editor → admin creates/disables an editor (last-admin rule holds) → lockout trips after 5 failures → CSRF absent 403 / present 200 → logout deletes the row.
3. `Secure` cookie and forwarded-proto behavior assert correctly for a trusted vs untrusted source.
4. Dev stack unchanged (snapshot diff) and `git ls-files | grep -c egg-info` returns 0 after each slice.

## Unresolved questions / reserved owner decisions

Reserve these for the owner; they are ambiguous requirements or changed behavior, not implementation details:

- **O7 (ambiguous/environment-specific):** the concrete `TRUSTED_PROXY_CIDRS` value for the owner's reverse proxy (spec §9.2 leaves it "default: the proxy's address"). Needed to pass S7's real-edge acceptance. Blocker: none for drafting.
- **O8 (changed behavior):** whether to set argon2id params **above** the OWASP-minimum default (m=19 MiB, t=2, p=1). Spec §6.2 allows "at or above"; defaulting to the OWASP minimum is the implementation's position, and raising it is an owner choice.
- **Open (not an owner decision — resolve in-slice):** the exact `login_attempts.id` type (UUID vs int) and whether R7 is satisfied by Uvicorn flags alone vs supplemental in-app handling (S1, S7). Implementation detail, chosen by the implementer and locked by tests.
