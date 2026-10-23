Read-only Task 7.7 review. No edits, planning, environment/.env access, git,
Docker, subprocess tests or extra exploration. Read ONLY auth/bootstrap.py,
auth/routes.py, templates/setup.html and tests/test_bootstrap.py,
test_bootstrap_recovery.py, test_setup_routes.py under src/cookbook or tests.
Prior routes/session/CSRF helpers are reviewed baseline; focus new setup flow.
Requirements: shared fresh-own-transaction first_admin FOR UPDATE before count
ALL users, exactly one active admin, missing sentinel loud. Reset user lock,
hash+delete ALL sessions atomically; recovery named existing only, last_admin
lock before user; never creates. GET/POSTsetup closed403 after anyuser,
preauth clear; zero-user GETlogin302setup, POSTlogin403 before password/counters.
Setup browser-cookie+form signed token purpose setup, validates before hashing
or mutation, no header-only bypass, safe error render, success session302root.
Min8 shared policy. No deployment/global CSRF/roles scope (later tasks).
Find concrete correctness/security bugs or important missing acceptance checks.
Report file:line, severity, trigger/impact and smallest fix; distinguish actual
bugs from optional suggestions. If none, state no blocking findings. Stop.
