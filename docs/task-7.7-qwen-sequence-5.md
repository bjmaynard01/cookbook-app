# Tiny subtask: POST setup and zero-user login entry

Read ONLY auth/routes.py and bootstrap.py public helper signatures. Edit
routes.py only, under110 new lines. No planning/todos, other docs/source,
tests, git, Docker, environment/.env, or dependencies.

GET /login: before rendering, if not await bootstrap.is_bootstrapped(), 302
/setup with private,no-store. POST /login: at start if not bootstrapped(),
403 private,no-store, no password/counter work. No changes for existing users.

POST /setup: if already bootstrapped return403/no-store and clear preauth cookie
(same closed GET behavior). Otherwise parse form, malformed expected form
errors =>403/no-store/no cookies. Require form csrf_token plus cookie secret,
verify_preauth(token,secret,"setup",now=utc_now(),key=settings.secret_key)
BEFORE password work/mutation. No preauth header fallback. Invalid403/no-store,
no cookies. Then require string username,password. Invalid fields or ValueError
from create_first_admin => render setup.html400, fixed generic input-policy
error, retain same csrf_token and browser cookie (no refresh), no password echo.
user=await bootstrap.create_first_admin(username,password). Catch only
bootstrap.AlreadyBootstrapped => closed403 with preauth deletion; LockRowMissing
and operational faults propagate. On success create_session(user.id,
settings=settings,created_ip=lockout.normalize_pair(user.username,
client_identity(request,settings)[0])[1] or None,
expected_password_hash=user.password_hash). Expected SessionIssueRefused ->
closed403. Success302 /, set_session_cookie mode via cookie_secure(), clear
preauth cookie Secure per effective scheme, private,no-store. No logs/tokens in
URLs. Do not change logout, lockout or schema. Compile routes, report and stop.
