# Fresh Qwen sequence 2b — implement cookie adapter only

Task 7.4 on task/7.4-session-core, baseline f18d2c6, all changes UNCOMMITTED.
Persistence portion in sessions.py passes 16 real MariaDB tests, verified by Codex.
Preserve it, including user-before-session lock order and positive TTL guard.

Read docs/task-7.4-qwen-sequence-2.md for helper API contract and rulings.
Read src/cookbook/auth/sessions.py, identity.py and tests/test_session_cookies.py.
Do not read the whole auth plan or redo investigation.

Create/modify ONLY src/cookbook/auth/sessions.py by ADDING cookie/request helpers.
Do not edit tests or docs: Codex already wrote the acceptance tests.
Tests are lazy-imported, so missing helpers fail during execution, not collection.
Use direct imports of existing cookie_secure / Settings helpers, no new frameworks.
Use Starlette Response.set_cookie/delete_cookie for headers.
Cookie-setting helper signature is set_session_cookie(response, raw_token,
*, secure: bool, ttl_hours=12), and cookie_set returns ONE active header only.
clear_session_cookies deletes both names. load_session caches LoadedSession or None
on request.state.auth_session and returns User|None. renew_session_cookie needs no
DB calls and no cookies for anonymous requests. See sequence2 doc/tests for details.

No source edits outside sessions.py, no config/dependency/schema/app changes,
no environment/secret/.env reads, no Docker or DB tests, no commit/push/merge.

Run PURE tests only:
.venv/Scripts/python.exe -m pytest tests/test_session_cookies.py -q -p no:cacheprovider
--basetemp .venv/qwen74-seq2-tmp
Fix until green. Compile/collect first if necessary. No wholesale test rewrites.
No long plan or repeated research. Implement promptly (roughly 100 lines).
Report pure test count, changed API, git diff check and stop.
Codex runs DB regression independently. If blocked, report exact error promptly.
