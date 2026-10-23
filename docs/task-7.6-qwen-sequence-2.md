# Tiny subtask: lock checking and clearing

Implement immediately. Read ONLY auth/lockout.py and tests/test_lockout_checks.py.
Edit lockout.py only. No planning/todos, other files/docs, git, Docker, tests,
environment/.env, or dependencies. Prior failure service must remain intact.

Add Locked(Exception) with public retry_after positive integer (ceiling of
remaining seconds), fixed generic exception message without username/IP.
Add async assert_not_locked(username,ip,*,now=None)->None: canonicalize through
normalize_pair and same clock helper as record_failure. Read LoginAttempt in
session_scope, no writes/locks needed. locked_until>now raises Locked; equality
or later free. Missing row must stay missing; expired history is not deleted.
No password work, DB NOW(), logs, exception swallowing, or HTTP dependencies.

Add async clear_pair(username,ip,*,db=None)->None: canonicalize, execute DELETE
matching pair. If db supplied (AsyncSession), execute within caller transaction
and never commit/rollback/close it. Otherwise use own session_scope. Idempotent.
This transaction-owned deletion is required for atomic login session+reset.
Keep additions under70 lines. Compileall source only, report path and stop.
