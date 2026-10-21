# Qwen sequence 2 — cookie/request adapter

Fresh-session handoff. Codex manages Task 7.4; no commits/pushes/merges.
Branch task/7.4-session-core, baseline f18d2c6, uncommitted sequence 1 work present.
Read docs/task-7.4-progress.md and current sessions.py/test_session_core.py FIRST.
Preserve the existing persistence API and tests; do not redo sequence 1.

Edit ONLY src/cookbook/auth/sessions.py and tests/test_session_core.py.
No schema/config/dependency/app-factory changes or actual routes/middleware.
No environment inspection, .env reads, credentials, Docker, or live DB tests.
Codex independently executes DB checks. Compile/collect, run pure cookie tests only.

Add request/cookie helpers using Starlette Request/Response (already dependencies):

- `session_cookie_name(*, secure: bool) -> str`: __Host-cookbook_session if true,
  otherwise cookbook_session. Path=/, HttpOnly, SameSite=Lax, no Domain.
- `cookie_set(raw_token, *, secure: bool, ttl_hours: int = 12) -> str`: compatibility
  helper for ONE active cookie header using Starlette Response.set_cookie, Max-Age
  equal ttl_hours*3600. Validate token format and positive TTL; no injection.
- `set_session_cookie(response, raw_token, *, secure: bool, ttl_hours: int = 12)`:
  append active cookie plus deletion of OTHER cookie name. Preserve existing
  unrelated Set-Cookie headers. Use response.set_cookie/delete_cookie, not string
  concatenation. Clearing __Host- always uses Secure; clearing dev name omits Secure.
  Response Cache-Control private, no-store. Return None.
- `clear_session_cookies(response)` clears BOTH session names with matching
  Path=/ and no Domain, correct secure flags; no DB/session work. Set private,no-store.
- `load_session(request, *, settings=None, now=None) -> User|None` async:
  use existing cookie_secure(request,settings) to choose the ONLY cookie accepted
  for current security mode. Never fall back to dev cookie in secure mode; ignore
  stale alternate name. Call persistence load_token. Store result (LoadedSession
  or None) on request.state.auth_session; return result.user or None. Cache within
  this request so multiple dependency consumers don't slide TTL repeatedly.
  Do not swallow DB errors as anonymous. No raw token in state/logs/URLs.
- `renew_session_cookie(response, request, *, settings=None)`:
  only if request.state.auth_session contains successful loaded result; reissue
  selected current cookie with TTL and clear alternate. No session lookup/DB call
  in this helper. Missing/anonymous state -> no cookies added. Server-side slide
  already occurred in load_session. Cache-Control private,no-store when renewed.

Rulings from Codex:
- A single string cannot represent two Set-Cookie headers. cookie_set is ONE header;
  response helpers implement active+alternate clearing safely.
- HttpOnly browser cannot migrate an unavailable Secure token over HTTP. Do not
  accept insecure cookie fallback in HTTPS mode just to make a transition test pass.
  Tests verify explicit cookie emission/clearing and current-mode lookup. A real
  browser may require fresh login after mode changes; do not promise otherwise.
- Cookie policy uses existing COOKIE_SECURE override and client_identity; do not
  reparse headers or introduce another proxy layer.
- __Host- deletion requires Secure. HTTP responses may emit correct deletion
  headers but cannot force a browser to accept them over insecure transport.

Add small tests marked `@pytest.mark.parametrize` or named cookie/adapter tests:
1. cookie attributes incl Max-Age, flags, no Domain; inspect parsed attributes,
   not brittle full header ordering. Both secure/dev names and invalid tokens/TTL.
2. Active+alternate deletion headers emitted separately; unrelated cookies kept.
3. clear both headers, __Host- deletion Secure; cache controls private/no-store.
4. Request load with monkeypatched async load_token: mode-selected cookie only,
   stale alternate ignored, trusted/untrusted proto and explicit override use
   existing identity helper; cache same request only, new requests reload.
5. Loaded user/CSRF context accessible to later routes via request.state.
6. No token/CSRF leakage in logs, Location, or error messages.
7. renew only authenticated requests, retains TTL, no extra DB call, anonymous no-op.

No wall-clock tests, real server, app factory wiring, browser UI, CSRF validation,
or login/logout route implementation. Future routes will call these helpers.
Use existing persistence load/revoke APIs; don't rename them gratuitously.

Write tests then code, compile+collect. Pure adapter tests should need no DB, use
monkeypatch for load_token. Run only pure tests with explicit selection, leave all
real DB tests for Codex. Use -p no:cacheprovider and --basetemp under .venv.
Run git diff --check. Stop and report completion, public API, collection/pure-test
results. Do not commit or start another slice. Avoid long research/narration loops.
