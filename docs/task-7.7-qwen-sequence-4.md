# Tiny subtask: setup template and GET only

Read ONLY auth/routes.py GET login and templates/login.html. Edit routes.py
and create templates/setup.html only. No other source/docs, tests, planning,
git, Docker, environment/.env or dependency tools. Keep below90 new lines.

Import bootstrap module in routes. Add GET /setup. settings from app.state.
If await bootstrap.is_bootstrapped(): Response403 private,no-store, append
csrf.browser_secret_clear(secure=client_identity(request,settings)[1]=="https").
Otherwise fresh secrets.token_urlsafe(16), csrf.issue_preauth(secret,"setup",
now=utc_now(),key=settings.secret_key). Render setup.html with csrf_token,error
None. Append cookbook_csrf_pre cookie through existing helper; Strict/HttpOnly,
900sec,Secure follows effective scheme. Cache-Control exactly private,no-store.

setup.html extends base; heading Create owner account; error escaped role alert;
POST /setup; hidden input name="csrf_token" value="{{ csrf_token }}"; whole dict
hx-headers serialized via tojson (same safe pattern as login). Label username
name=username autocomplete=username default owner, required,maxlength60.
Password name=password,type=password,autocomplete=new-password,minlength8,
required. Explain minimum8/no complexity; never echo password. No scripts/CDN.
Do not add POST setup or change GET/POST login yet. Compile routes, report/stop.
