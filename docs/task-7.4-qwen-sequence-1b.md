# Fresh Qwen handoff — implement persistence only

Branch task/7.4-session-core, baseline f18d2c6. Codex wrote acceptance tests in
tests/test_session_core.py and captured their missing-module failure. Preserve tests.

Create ONLY src/cookbook/auth/sessions.py. Do not modify tests or docs.
No commit/push/merge, no environment/.env reads, no DB/Docker commands.
Read tests/test_session_core.py and docs/task-7.4-qwen-sequence-1.md for API contract.
Read db.py/models.py/config.py only as necessary. Do not reread the full auth plan.

Implement the specified create_session, LoadedSession, load_token, revoke_session,
revoke_user_sessions now. Aim for 100-150 lines, small comments. The tests are the
contract: exact method names/arguments/returned row attributes must match them.
Use one existing session_scope per call; locking reads and no session resurrection.
Return detached usable objects after commit; expires_at equality is valid.
Malformed token rejects before database. Time supplied must be naive UTC.
No cookies in this sequence. No schema/config/dependencies/routes changes.

Compile source with compile(...), then pytest --collect-only -q for session tests
(no DB execution). Codex runs the database tests and reviews implementation.
Write the source promptly rather than a lengthy plan or repeated research.
Stop and report once file compiles and tests collect; no follow-on task.
