# Task 7.6 — lockout execution record

Branch: `task/7.6-lockout`; baseline `d4beca8`.
Owner authorized implementation, not commits/pushes/merges.
The six existing Task 7.4 note deletions are unrelated and remain untouched.

## Sequence

1. Fresh Qwen: canonical pair and atomic failure-state service.
2. Fresh Qwen: lock check and transaction-aware reset helpers.
3. Fresh Qwen: session-transaction reset integration.
4. Fresh Qwen: login lockout integration.
5. Codex isolated regression, fresh read-only review, cleanup and handoff.

## Contracts and rulings

- Auth plan C1/S6 and owner-approved O-a/O-e bind behavior. A stale S6
  sentence saying "one past locked_until still locked" is a typo: C1 says
  expiry equality and later are free; immediately before expiry is locked.
- A MariaDB insert/no-op-upsert ensures the pair exists and locks it before
  SELECT FOR UPDATE in one transaction. This handles concurrent first failures
  without relying on an absent-row gap lock or swallowing an IntegrityError.
- Success clears the pair inside session issuance's existing transaction,
  after the account/verified-hash checks. Failed issuance must not reset it.
- Username lower().strip() also drives login lookup. Empty/overlong canonical
  names cannot identify a valid account and return generic 401 without a DB
  lockout write. Never truncate keys. Nonempty IPs use ipaddress canonical form;
  missing peer uses empty string as one explicit unknown-peer bucket.
- DATETIME lockout columns have whole-second precision; normalize injected
  naive UTC time to whole seconds consistently. No schema change or DB NOW().

## Status

Branch and disposable DB created. Development stack snapshot saved separately.

## Sequence 1 — complete

Baseline **273 passed**. New failure-state tests were observed failing for
missing module before coding. Fresh Qwen wrote lockout.py only; Codex verified
**13 tests pass** against isolated MariaDB, including concurrent first failures
and exactly one lock episode under seven concurrent failures. Five lock-check/
reset tests now fail for the expected missing helpers. Next: fresh session 2.

## Sequence 2 — complete

Fresh Qwen added Locked, read-only assert_not_locked, and clear_pair with
optional caller transaction. **18 service tests pass**, including exact
expiry boundaries, no-write missing lookups, and rollback of caller reset.
Session/route acceptance failures were captured before those integrations.
Next: fresh session for the atomic session/reset transaction.

## Sequence 3 — complete

Fresh Qwen added optional login_pair to session issuance and reset in the same
transaction after account/hash guards. **23 tests pass** (failure state, lock
checks, atomic issuance/reset). Reset faults roll back both operations, and
refused issuance preserves history. Added active-lock/window-edge coverage.
Next: fresh route-integration session, preserving all prior CSRF/cookie rules.

Codex review reproduced IPv6 interface suffixes splitting one IP into alias
counters. Normalize scoped IPv6 to its numeric address (mapped IPv6 still
collapses to IPv4). The regression was observed failing, then fixed; **24
service/session tests pass**. Zone identifiers are routing metadata, not key
coordinates. No schema changes.

## Sequence 4 — complete

Fresh Qwen integrated pre-password lock checks, 429/Retry-After/no-store,
failure counters, canonical username lookup and atomic success reset. Initial
targeted integration: **35 passed**. Codex then reproduced scoped IP metadata
exceeding sessions.created_ip length; use the same canonical pair IP for
session metadata. Unknown peer is explicitly handled as empty counter key and
NULL session metadata. Both regression cases included in final verification.
Existing login test cleanup now removes its own lockout history as well.

## Initial integrated verification

Full isolated suite: **310 passed**, one existing Starlette/httpx warning.
Task 7.6 adds **37 tests** (15 failure-state, 5 check/reset, 4 session-reset,
13 route integration). Alembic check: no new upgrade operations; v6 unchanged.
Fresh read-only Qwen review is running. No commits, pushes or merges.

Additional ordering regression proves a correct login still commits and
clears history if concurrent failures lock the pair after its password check.
Operational password faults are also asserted not to create counters.
Final full suite: **311 passed**, same existing warning; **38 new tests**.
Alembic drift check remains clean. Wheel contains the new service and session
integration; changed source/tests compile and whitespace checks pass.

Cleanup complete: disposable cookbook-test-db stopped/auto-removed. Development
app/db names, states and start times match the original snapshot; both healthy.
All four implementation sessions exited successfully. Final Qwen review pending.

## Fresh review triage

Qwen found **no blocking security/correctness defects**. Its medium test gap
was that SessionIssueRefused route failures were not asserted to increment
counters. Existing real account-change race probes already test the 401 and
no-session outcomes; strengthened both to require attempts=1, unlocked.

Minor deferred: explicit lock-database fault-injection coverage. Existing
password-service operational-error coverage proves propagation without a
counter; lock-query propagation is also explicit in the implementation's
catch-only-Locked handling, but a dedicated DB-fault probe is optional follow-up.

Ruling on informational non-IP-peer note: retain generic 401 for malformed,
nonempty peer identities rather than invent a noncanonical IP key. A missing
peer explicitly uses the tested empty bucket; real IPs are canonicalized.

## Final status — ready for owner review

- Reviewed full regression on a fresh disposable DB: **311 passed**, one
  pre-existing Starlette/httpx deprecation warning. No Alembic schema drift.
- Medium review coverage gap addressed by both actual account-change route
  probes asserting failure counts. No blocking findings remain. Optional
  database-fault-injection test is the only deferred minor.
- All implementation/review Qwen sessions completed. No delegate running.
- Recreated test DB removed after final verification; development app/db
  states and start times still match the original snapshot. No dev rebuild,
  migrations, or data changes. No preserved data directories touched.
- Branch `task/7.6-lockout`, HEAD `d4beca8`; Task 7.6 changes remain uncommitted
  and unstaged. Existing six Task 7.4 note deletions remain untouched.
- No commits, pushes or merges. Next: owner review/integration, then Task 7.7
  setup/bootstrap plus CLI. The running image still exposes health probes only.

## Commit/push authorization — 2026-10-23

Owner approved committing and pushing Task 7.6. Fresh pre-commit verification:
**311 passed**, same existing warning, no Alembic drift. Disposable DB removed;
development container states/start times unchanged. Commit scope excludes the
six pre-existing Task 7.4 note deletions. No merge authorized. Task 7.7 briefing
is requested; its implementation has not started.
