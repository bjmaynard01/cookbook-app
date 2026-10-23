"""Utilities for zero-user bootstrap probes on the disposable DB only."""
import asyncio
from sqlalchemy import delete, select
from cookbook.db import get_engine, session_scope
from cookbook.models import AuthLock, LoginAttempt, User

PASSWORD = "bootstrap-password"


def probe(body):
    async def run():
        try:
            async with session_scope() as db:
                await db.execute(delete(User))
            await body()
        finally:
            try:
                async with session_scope() as db:
                    await db.execute(delete(User))
                    await db.execute(delete(LoginAttempt))
                    for name in ("first_admin", "last_admin"):
                        if await db.get(AuthLock, name) is None:
                            db.add(AuthLock(name=name))
            finally:
                await get_engine().dispose()
    asyncio.run(run())
