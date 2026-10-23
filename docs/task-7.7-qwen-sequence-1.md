# Tiny subtask: first-owner service only

Implement immediately, no planning/todos. Read ONLY tests/test_bootstrap.py,
db.py, User/AuthLock model definitions, and passwords.hash_password signature.
Create ONLY src/cookbook/auth/bootstrap.py. No tests, environment/.env, git,
Docker, dependency commands, routes/CLI or other docs.

Public AlreadyBootstrapped(RuntimeError), LockRowMissing(RuntimeError),
normalize_username(str)->lowercase stripped nonempty1..60 chars else ValueError.
async create_first_admin(username,password,*,factory=None)->User.
async is_bootstrapped(*,factory=None)->bool.
factory is async_sessionmaker (NOT open session); default get_session().
Every helper owns a new session and transaction, no inherited snapshot.

create: normalize name, hash with await asyncio.to_thread(passwords.hash_password,
password) (existing min8/no complexity), then async with factory() as db:
async with db.begin(): SELECT AuthLock first_admin FOR UPDATE; missing raises
LockRowMissing fixed message. ONLY AFTER lock SELECT COUNT(*) users; nonzero
AlreadyBootstrapped. Add User(username=normalized,password_hash=hash,role=admin,
is_active=True), await db.flush(); return detached usable User after commit.
Caller factories have expire_on_commit=False. No extra sessions, no session
token creation in this helper, no schema changes/logs/catches/DB NOW().
is_bootstrapped: fresh read session COUNT users >0, no writes.
Keep below90 lines. Compileall source only, report path and stop.
