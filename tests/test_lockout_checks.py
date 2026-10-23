"""Lock enforcement and transaction-owned reset contracts."""
import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from cookbook.db import session_scope
from cookbook.models import LoginAttempt
from test_lockout import IP, NOW, probe, row, service


async def locked(username):
    for _ in range(5):
        await service().record_failure(username, IP, now=NOW)


def test_read_missing_pair_does_not_create_history():
    async def body(username):
        assert await service().assert_not_locked(username, IP, now=NOW) is None
        assert await row(username) is None
    probe(body)


def test_lock_expiry_boundary_and_retry_after():
    async def body(username):
        s = service()
        await locked(username)
        with pytest.raises(s.Locked) as error:
            await s.assert_not_locked(username.upper(), "::ffff:198.51.100.10",
                                      now=NOW + timedelta(minutes=9, seconds=59))
        assert error.value.retry_after == 1
        assert await s.assert_not_locked(username, IP, now=NOW + timedelta(minutes=10)) is None
        assert await s.assert_not_locked(username, IP, now=NOW + timedelta(minutes=10, seconds=1)) is None
        actual = await row(username)
        assert actual.attempts == 5 and actual.lock_count == 1
        assert actual.locked_until == NOW + timedelta(minutes=10)
    probe(body)


def test_clear_deletes_pair_and_resets_tier_for_later_failure():
    async def body(username):
        s = service()
        await locked(username)
        await s.clear_pair(" " + username.upper() + " ", "::ffff:198.51.100.10")
        assert await row(username) is None
        state = await s.record_failure(username, IP, now=NOW + timedelta(minutes=1))
        assert state.attempts == 1 and state.lock_count == 0 and state.locked_until is None
        await s.clear_pair(username, IP)
        await s.clear_pair(username, IP)
        assert await row(username) is None
    probe(body)


def test_clear_inside_caller_transaction_rolls_back_with_caller():
    async def body(username):
        s = service()
        await locked(username)
        with pytest.raises(RuntimeError, match="rollback marker"):
            async with session_scope() as db:
                await s.clear_pair(username, IP, db=db)
                assert (await db.execute(select(LoginAttempt).where(
                    LoginAttempt.username == username, LoginAttempt.ip == IP))).scalar_one_or_none() is None
                raise RuntimeError("rollback marker")
        assert (await row(username)).attempts == 5
        async with session_scope() as db:
            await s.clear_pair(username, IP, db=db)
        assert await row(username) is None
    probe(body)


def test_reset_racing_failure_never_restores_old_lock():
    async def body(username):
        s = service()
        await locked(username)
        await asyncio.gather(s.clear_pair(username, IP), s.record_failure(username, IP, now=NOW))
        actual = await row(username)
        assert actual is None or (actual.attempts == 1 and actual.lock_count == 0 and actual.locked_until is None)
    probe(body)
