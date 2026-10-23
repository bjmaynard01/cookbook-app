# Task 7.7 fresh Qwen reviews

## Setup and shared services

Fresh read-only review found no blocking correctness/security issues. It
identified a low-severity audit-IP validation edge: non-IP ASGI peers could
raise after the first owner was committed. Codex reproduced it with an actual
ASGI request, moved identity validation before creation, and verified all
**18 setup tests pass**. Added the suggested SessionIssueRefused acceptance
case (403, preauth cleared, no session, owner can reach login), and explicit
zero-user login counter/session assertions.

Optional suggestions deferred: serialized hx-headers remain consistent with
the approved template contract; hidden form token is mandatory and header-only
is explicitly rejected. Cross-scheme Secure-cookie deletion follows baseline
cookie semantics and does not affect the supported same-scheme flow.

## CLI and configuration

Separate fresh read-only review complete: **no blocking findings**. Codex
reproduced and corrected SQLAlchemyError safe reporting and same-loop engine
disposal around factory creation before this review; both fault tests pass.
The review repeated the old factory-outside-try concern despite the corrected
source: rejected as stale, confirmed by source and the factory-failure test.

Optional cleanup deferred: legacy secret reader retains existing compatibility
tests; CLI config sentinel import is harmless; I/O error wording and handling
could be friendlier but failures are nonzero and do not disclose secrets;
whitespace-only interactive names remain invalid by shared username policy.

## Final verification

**366 passed**, one existing Starlette/httpx deprecation warning. Alembic head
e4c7a91d2f30 with no schema drift. Locked dependency check, installed CLI,
wheel source/template/entrypoint checks and whitespace check pass. Disposable
database removed; development container states/start times unchanged. No
commit, push, merge or deployment performed.
