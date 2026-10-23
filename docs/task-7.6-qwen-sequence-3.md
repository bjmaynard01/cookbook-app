# Tiny subtask: atomic session issuance plus lockout reset

Read ONLY auth/sessions.py create_session and auth/lockout.py clear_pair.
Edit sessions.py only. No planning/todos, exploration, git, Docker, tests,
environment/.env, dependencies, docs, or other edits. Keep change under15 lines.

Extend create_session signature with login_pair: tuple[str,str]|None=None.
After the existing user-active and expected_password_hash checks, but inside
the SAME existing session_scope transaction, call await lockout.clear_pair(
*login_pair, db=db) when login_pair is not None. Add from . import lockout
(local import allowed). Do not open another transaction. No other behavior
changes. db.add(row) and reset must commit or roll back together. Refused
issuance must NOT clear lockout. Existing callers with no login_pair unchanged.
Document the optional pair resets history atomically with issuance.
Compileall source only, report path and stop.
