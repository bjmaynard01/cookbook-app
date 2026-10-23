# Tiny subtask: atomic lockout failure recording

Implement immediately, no planning/todos/exploration. Create ONLY
src/cookbook/auth/lockout.py. Read ONLY tests/test_lockout.py and the
LoginAttempt model (models.py lines 113–151). Do not edit tests. No git,
Docker, DB tests, environment/.env access, or dependency installation.

APIs: normalize_pair(username,ip)->(str,str); async record_failure(username,
ip,*,now=None)->LockState. Frozen dataclass LockState fields attempts,
first_failed_at,last_failed_at,lock_count,locked_until (copies, not ORM object).
now defaults datetime.now(timezone.utc).replace(tzinfo=None), reject aware
datetime, normalize microseconds=0 to match schema DATETIME precision.
Username lower().strip(), must length1..60 (reject, never truncate). IP strip,
ipaddress.ip_address canonical; mappedIPv6 -> IPv4. Empty IP remains empty as
explicit unknown bucket. Invalid pair raises ValueError before DB operations.

Use from ..db import session_scope; from ..models import LoginAttempt.
Transaction-safe missing-row concurrency: in one session_scope execute
sqlalchemy.dialects.mysql.insert(LoginAttempt).values(id=str(uuid.uuid4()),
username=u,ip=ip,attempts=0,first_failed_at=now,last_failed_at=now,
lock_count=0,locked_until=None).on_duplicate_key_update(id=LoginAttempt.id).
This insert/no-op upsert serializes on unique pair and creates zero-attempt
row if missing. Then SELECT matching row FOR UPDATE in SAME transaction,
apply state machine and return copied immutable snapshot. No DB NOW().

State: active locked_until>now => return unchanged, no field updates.
Otherwise if first_failed_at+10min<=now OR expired lock present: attempts=1,
first_failed_at=last_failed_at=now, locked_until=None, preserve lock_count.
Otherwise increment attempts and set last_failed_at=now.
When attempts reaches5: increment lock_count ONCE; locked_until=now+10/20/40/60
minutes at tier1/2/3/4+, cap at60 WITHOUT huge exponent arithmetic.
New zero row becomes attempts1, tier0. No logging/catches/DDL/schema changes.
Do not implement lock checking or clearing yet: next fresh session owns them.
Keep under170 lines. Compileall changed source only, report path and stop.
