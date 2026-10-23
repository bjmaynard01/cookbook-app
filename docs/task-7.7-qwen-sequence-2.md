# Tiny subtask: password reset / existing-account recovery service

Read ONLY bootstrap.py and tests/test_bootstrap_recovery.py. Edit bootstrap.py
only. No planning/todos, tests, git, Docker, environment/.env, deps or other docs.
Add UserNotFound(RuntimeError), async reset_password(username,password,
*,restore_admin=False,factory=None)->User. Own fresh transaction like guard.
Normalize username and hash with asyncio.to_thread before transaction.
If restore_admin, FIRST lock AuthLock last_admin FOR UPDATE, missing raises
LockRowMissing. Then lock matching User FOR UPDATE. Missing UserNotFound,
Allow LockRowMissing(name="first_admin") and name last_admin in recovery so
its diagnostic identifies the actual missing row; preserve default API.
never create an account. Set password_hash; only when restore_admin set
role=admin,is_active=True. DELETE ALL Session rows for user.id in SAME db txn
(do not call sessions.revoke_user_sessions: it opens another transaction).
Plain reset leaves role/is_active unchanged. Returned User after commit.
No CLI confirmations here (CLI owns them), no logging/catches/DDL/schema.
Keep additions below60 lines. Compileall source only, report path and stop.
