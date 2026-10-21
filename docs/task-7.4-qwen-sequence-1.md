# Qwen sequence 1 — session persistence

Implement only the persistence portion of Task 7.4. Codex manages this task.
Branch `task/7.4-session-core`, baseline `f18d2c6`. Preserve existing docs.
No commits/pushes/merges. No Docker operations. No secrets or .env reads.

Read auth plan C0.1, C3, S4, src/cookbook/db.py, models.py Session/User, config.py,
and tests/test_auth_schema.py. You may edit ONLY new files
`src/cookbook/auth/sessions.py` and `tests/test_session_core.py`.

Implement concise persistence API (cookies/request adapter are a SECOND session):

- `create_session(user_id, *, settings=None, created_ip=None, now=None)` async:
  returns `(raw_token, Session row)` after committing. Settings defaults via get_settings.
  `raw_token=secrets.token_urlsafe(32)`, only SHA256 hex persisted; fresh token_hex(16)
  CSRF token. Set created_at=last_seen_at=one UTC naive now, expires_at=now+TTL.
  Reject missing/inactive users with ValueError, no session inserted. Lock/read user
  in transaction so later disable/session-wipe writers can coordinate.
- `LoadedSession` frozen dataclass with `user: User`, `session: Session`.
- `load_token(raw_token, *, settings=None, now=None)` async -> LoadedSession|None.
  Missing/empty/malformed token -> None cheaply. Generated token format 43 ASCII
  URL-safe chars; bound length before hashing/database calls.
  Load session by SHA256 hash, lock existing row during validation/update. No INSERT
  in load path, never resurrect revoked data. Unknown -> None. Expired iff expires_at
  < now (equality valid per approved contract): DELETE expired row, return None.
  Missing/inactive user -> delete session, None. Active -> slide last_seen_at/expires_at,
  leave created_at/IP/hash/CSRF unchanged, commit, return detached usable result.
- `revoke_session(raw_token)` async: idempotent delete by hash; malformed no-op.
- `revoke_user_sessions(user_id)` async: idempotent delete all user's sessions only.

Use existing session_scope for owning transactions, no dependencies/schema changes.
No request/cookie helpers, routes, logging raw tokens, middleware or login behavior.
Accept explicit naive UTC datetime now for deterministic tests; reject aware datetime
clearly rather than silently mixing aware and naive DB values. UTC default should use
datetime.now(timezone.utc).replace(tzinfo=None). Use consistent helper.

Tests first, then minimal code. Real MariaDB tests for row data, token uniqueness,
raw token absence, defaults/custom TTL, valid load/slide, equality/expired boundaries,
unknown/malformed input, inactive user rejection/cleanup, revoke one/all isolation,
and preservation of created fields/CSRF. No wall-clock assertions/sleeps. Dispose
engine in owning loop's finally even if cleanup fails. No changes to previous tests.
Do not invent extensive framework or hundreds of lines of commentary.

Codex owns the test database and will execute live database tests independently.
Do not inspect environment variables or request database credentials.
Compile using compile(source, filename, 'exec') and collect tests only.
Report that live red/green database checks await Codex, not pretend they passed.

Write code now; no long plan or repeated research. If blocked, report promptly.
When code and tests compile/collect, report public API, test count, git diff check,
and stop. Do not start cookies, commit or run full suite. Codex does independent review.
