# Fresh read-only review: Task 7.6 lockout

Review ONLY src/cookbook/auth/lockout.py, new lockout integration blocks in
routes.py and create_session in sessions.py, and tests/test_lockout*.py.
No edits, planning/todos, shell commands, runtime imports/API checks, tests,
git, Docker, dependency commands, environment/.env access, or other docs.
Do not inspect unchanged templates or password/CSRF implementations. They
were verified in Task 7.5 and are outside this review. Stop after report.

Contract: normalized username lower/strip and canonical IPv4/IPv6/mapped IP
pair. Empty peer uses explicit empty bucket, invalid/empty/overlong username
generic401 without DB writes. Five failures in10min lock; fifth401, next429
with positive Retry-After before any credential lookup/hash work. Escalation
10/20/40/60min cap; window/lock expiry equality free. Active locked failures
change no fields and don't extend lock. Expired window preserves tier; success
DELETE resets tier. Atomic absent-row and existing-row failure concurrency.
No schema change, DB NOW(), secrets in logs/URLs, or dev stack changes.
Whole-second clock precision matches DATETIME columns (intentional).

Success reset is inside SAME session-issuance transaction after user-active
and verified-hash guards. Refused/rolled-back session must not clear history.
Later stale failure starts fresh attempts1/tier0 and cannot invalidate session.
CSRF rejection precedes all lock queries/writes. Unknown/inactive/wrong count
failures after password work; operational errors propagate without counters.
SessionIssueRefused counts failed login. Canonical username drives User lookup.
Trusted proxy identity drives IP; forged untrusted headers cannot evade lock.
429 uses retained validated preauth token/cookie and private,no-store headers.
All earlier auth cookie/CSRF behavior remains.

Look for concrete security/correctness bugs and missing meaningful tests.
Return severity, file:line, trigger/impact, fix for each; if none, state no
blocking findings. Be concise. Setup/bootstrap/roles/global gates are later
slices, so their absence is not a finding. No further task after this review.
