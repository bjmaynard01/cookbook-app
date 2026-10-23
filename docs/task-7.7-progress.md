# Task 7.7 — bootstrap execution record

Branch `task/7.7-bootstrap`, baseline `b2941b5`. Owner authorized local work;
no commits/pushes/merges. Six old Task 7.4 note deletions remain untouched.
Approved auth plan C2/C5A/C6/S7 governs; stale pending O-d/O-f wording is
superseded by the recorded approval of all O-a–O-f.

## Bounded sequence (fresh Qwen session each)

1. Standalone first-admin guard and is_bootstrapped.
2. Password reset / existing-owner recovery service.
3. Lean CLI configuration and file-first bootstrap password settings.
4. Setup template and GET route.
5. POST setup and zero-user auth entry behavior.
6. CLI parser/init-secret.
7. CLI setup, reset and recovery commands.
8. Isolated integration/regression, fresh read-only review, cleanup.

## Rulings

- Guard public API owns its transaction; optional async session factory lets
  CLI use DB-only configuration without SECRET_KEY. No already-open session
  is passed into the first-admin guard (C5 overrides stale S7 signature).
- Bootstrap username lower/strip and 1..60 chars agrees with login/lockout.
- Recovery promotes/reactivates an existing named account only; serialize
  role/active restoration on last_admin before user locking. Generic last-admin
  demotion service remains S8; recovery never removes an active admin.
- Password writes and session revocation share one user-locked transaction.
- Password-file readers preserve spaces, stripping line endings only; no
  password, hash, browser secret or session token in logs/CLI arguments.
- No running dev-stack changes. All DB verification is disposable port 33061.

## Status

Branch and disposable DB created; development state snapshot saved separately.

Baseline verified: **311 passed**, existing warning only. First-owner guard
tests observed failing for missing bootstrap module before implementation.
Recovery and CLI configuration acceptance tests/prompts prepared separately.

## Delegate authorization pending

Automatic approval review rejected the first OpenCode launch: provider name
ollama-remote could send project source to an untrusted remote endpoint.
Read-only config inspection found https://ollama.maynardfolks.com/v1. User was
asked to confirm this is their trusted server and authorize bounded source/
prompt transfer, or choose direct Codex implementation. No launch retried,
no project source sent by this Task 7.7 sequence. Local test preparation can
continue while this required approval is pending.

Local red runs: 9 guard, 4 recovery, 5 CLI-config cases fail for missing 7.7
implementations. No production implementation changed. Disposable DB removed
while waiting; development states/start times unchanged. No commits/pushes.

Endpoint approval received: owner explicitly confirmed
https://ollama.maynardfolks.com/v1 is their trusted Qwen server and approved
sending bounded project prompts/source. The rejected launch can now be retried
within that scope. No secrets or .env are included.

## Sequence 1 — complete

Fresh trusted-endpoint Qwen session implemented standalone first-admin guard.
**9 tests pass**, including parallel one-winner behavior, stale caller snapshot,
missing lock row, normalization, password minimum and preserved password spaces.
Setup route acceptance tests observed missing-route failures before coding.
Next: fresh password reset/recovery service session.

## Sequence 2 — complete

Fresh Qwen added reset_password with optional existing-account restoration.
**13 service tests pass**. Recovery serializes through last_admin, missing
account is refused, missing guard fails loudly, short passwords preserve
hash/session state, and valid password changes wipe all sessions atomically.
Next: fresh CLI configuration session; no SECRET_KEY requirement in CLI.

## Sequence 3 — complete

Fresh Qwen added CLISettings/cli_config, lazy bootstrap file paths and file-first
web bootstrap password resolution. **17 config tests pass** (existing+new).
CLI ignores unreadable web SECRET_KEY_FILE; password-file spaces preserved;
explicit CLI file can override a bad fallback; DB URL special chars encode.
Ruling: Qwen also used SQLAlchemy URL builder for the existing web DB URL.
Accept this delimiter-encoding consistency correction, with existing tests
passing. Its report said the legacy secret reader was changed, but diff
inspection showed it was untouched; reports are not verification evidence.
Next: fresh setup-template/GET-only session.

Codex reproduced web dotenv secret-file paths being ignored, then changed
get_settings to resolve the parsed file fields (with exact first-line reads).
This makes .env.example file-first guidance work with dotenv as well as OS
environment variables. Existing direct legacy-helper tests remain unchanged.

## Sequence 4 — complete

Fresh Qwen wrote setup.html and GET /setup only. Setup-page rotation/cache/
cookie and closure-after-CLI-creation test passes. **18 config tests pass**
after the dotenv file-first correction. Next: fresh POST/setup/login-entry
session, followed by all browser CSRF rejection and concurrency cases.

## Sequence 5 — complete

Fresh Qwen added POST /setup and zero-user login entry routing. **29 guard/
recovery/web tests pass**: CSRF rejections precede mutation, one concurrent web
winner gets one session, setup closes, password policy applies, and missing
lock fails loudly. Login GET redirects to setup only while no user exists.
Next: fresh CLI parser/init-secret session, then separate input/DB dispatch.

## Sequence 6 — complete

Fresh Qwen implemented CLI parser, init-secret, console entrypoint metadata,
secret.key ignore rule and env-example guidance. **2 init-secret tests pass**:
no config access, private random key, no key output, no accidental overwrite,
explicit rotation, and subprocess before web key. Corrected recovery flag help
to describe restoration rather than destructive recreation. Prior login-page
test was observed returning302 for zero users, then given its required existing
account fixture; its original assertions pass. Next: CLI credential inputs.

## Sequence 7a — review correction

Fresh Qwen wrote credential-source inputs. Acceptance run found argparse
contract mismatches: force_owner_recreation was read as force, and nested
user/reset-password was treated as a top-level command. Fresh bounded Qwen
correction requested, also honoring an explicit interactive username and
showing the exact recovery confirmation token. CLI database dispatcher remains
unimplemented; its nine expected failures were observed before sequence 7b.

Correction verified: **4 input tests pass**, including supplied username with
interactive password. **5 recovery tests pass**, including injected session-
deletion failure rolling back the password write and preserving old sessions.
Sequence 7b is running in another fresh Qwen session.

## Sequence 7b — complete; final review underway

Fresh Qwen implemented CLI database dispatch. **11 CLI acceptance tests pass**,
including pre-key subprocess, credential precedence, named recovery and CLI/
HTTP race. Codex fault injection reproduced uncaught IntegrityError (possible
SQL parameter disclosure) and cleanup outside the factory construction path.
Corrected catch to SQLAlchemyError and moved factory creation inside try/finally;
**2 lifecycle fault tests pass**, including same-event-loop disposal.

Full suite: **364 passed**, existing Starlette/httpx warning only. Alembic head
e4c7a91d2f30, no new upgrade operations. uv lock --check, uv sync --frozen and
wheel build pass; installed cookbook --help works. Wheel contains setup.html,
all CLI modules and console entrypoint. Fresh read-only web/guard review is
running; separate fresh CLI/config review follows. No commit/push/merge.

Web review complete: no blockers; one conditional audit-IP failure reproduced
and corrected before owner creation. All **18 setup cases pass**, including
failed session issuance and zero-user login no-side-effects. See review-findings
note. Fresh CLI/config review is now running. Final full-suite rerun required
after this correction.

## Final checkpoint — complete, awaiting owner review

Both fresh read-only Qwen reviews complete with no blocking findings. Review
disposition is in task-7.7-qwen-review-findings.md. Final full suite after the
web correction: **366 passed**, one pre-existing warning. Final wheel rebuilt
and source/template/console-entrypoint verified. Alembic remains v6 with no
drift; uv lock --check and installed cookbook --help pass. Git diff --check
passes; no tracked egg-info. Temporary workspace build directory removed after
absolute-path verification. Disposable cookbook-test-db stopped/removed;
development app/db states and start times match the pre-task snapshot exactly.

Branch task/7.7-bootstrap remains uncommitted. No push/merge/deployment; six
unrelated Task 7.4 note deletions remain untouched and unstaged. Next action:
owner review and explicit commit/push authorization, then Task 7.8 roles/admin
lifecycle when authorized. Root cookbook UI remains a later task.

## Commit/push approval — 2026-10-23

Owner explicitly authorized committing and pushing Task 7.7. Fresh pre-commit
verification: **366 passed**, existing warning only; Alembic check no drift.
Disposable database removed again; dev states/start times unchanged. Stage only
Task 7.7 implementation, tests and notes; exclude the six unrelated deletions.
Merge and deployment remain unapproved. Next implementation slice is Task 7.8.
