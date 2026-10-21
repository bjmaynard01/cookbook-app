# Fresh read-only review — Task 7.4

You are reviewing, NOT implementing. Branch task/7.4-session-core, baseline f18d2c6.
All work uncommitted. Do not edit, commit, push, merge, inspect environment/secrets,
read .env, run Docker, or execute live DB tests. Codex owns verification/cleanup.

Read ONLY:
- src/cookbook/auth/sessions.py
- tests/test_session_core.py
- tests/test_session_cookies.py
- src/cookbook/db.py and models.py Session/User if needed.

Contract: opaque 43-char random token, only SHA256 stored; random CSRF; active users
only; naive UTC DATETIME6; TTL default12h; expires_at equality valid; sliding must
not regress from out-of-order requests; consistent user-before-session locks,
revoke idempotent and no resurrection. Request adapter caches same request only,
propagates DB failures. Mode-selected secure/dev cookie only, no alternate fallback.
Secure __Host- path=/ no Domain, HttpOnly SameSite=Lax. Cookie renewal follows DB
slide, clears stale alternate, no DB in response helper; anonymous does nothing.
No real routes or middleware in this slice.

Verified by Codex: 39 targeted tests and full suite 220 pass, schema check clean.
Look for concrete bugs/security regressions/tests missing important behavior.
Avoid hypothetical feature requests or code-style rewrites. No new research needed.
Report severity + exact function/line + reproduction if any; otherwise 'no blocking
findings'. Keep review concise. Do not make changes or plan another task.
