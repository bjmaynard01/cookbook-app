# Tiny subtask: login route lockout integration

Read ONLY auth/routes.py and lockout.py public function signatures. Edit
routes.py only. No planning/todos, other source/docs, git, Docker, tests,
environment/.env, or dependencies. Session service now supports login_pair.

Import lockout module (routes.lockout test instrumentation needs it).
After pre-auth CSRF validates and username/password are confirmed strings,
compute pair=lockout.normalize_pair(username,client_identity(request,settings)[0]).
Invalid pair ValueError => existing generic401 with retained CSRF/cookie.
Use pair[0] for User.username lookup (lowercase+trim, DB case-insensitive).
Before DB credential lookup or ANY password work: await
lockout.assert_not_locked(*pair,now=utc_now()). Catch ONLY lockout.Locked:
render login.html with existing validated csrf_token and fixed generic wait
message, status429, Retry-After=str(exc.retry_after), private,no-store. Do not
set/delete/refresh cookies. Never include username/IP/token in wait message.
Keep CSRF check BEFORE lock lookup, so invalid CSRF never touches counters.

For each credential rejection after a valid pair (unknown user, inactive,
wrong password, SessionIssueRefused): await lockout.record_failure(*pair,
now=utc_now()) AFTER password work or refusal and BEFORE returning generic401.
Fifth failed attempt creates lock but still returns401; next request429 with
no password work. Unexpected operational errors propagate, never counted.

On session issuance pass login_pair=pair to sessions.create_session. This
resets history inside the authenticated transaction; DO NOT call standalone
clear_pair after create_session. Keep hash guard and all cookie/CSRF contracts.
No changes to GET login/logout, setup/bootstrap, roles or schema.
Compileall changed source only, report path and stop.
