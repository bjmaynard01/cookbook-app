"""Lockout state-machine contracts against disposable MariaDB."""
import asyncio
import importlib
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from cookbook.db import get_engine, session_scope
from cookbook.models import LoginAttempt

NOW = datetime(2026, 10, 22, 12)
IP = "198.51.100.10"


def service():
    return importlib.import_module("cookbook.auth.lockout")


def probe(body):
    async def run():
        username = "lock-" + str(uuid.uuid4())
        try:
            await body(username)
        finally:
            try:
                async with session_scope() as db:
                    await db.execute(delete(LoginAttempt).where(LoginAttempt.username == username))
            finally:
                await get_engine().dispose()
    asyncio.run(run())


async def row(username, ip=IP):
    async with session_scope() as db:
        return (await db.execute(select(LoginAttempt).where(
            LoginAttempt.username == username, LoginAttempt.ip == ip))).scalar_one_or_none()


def test_failure_count_and_fifth_failure_lock():
    async def body(username):
        s = service()
        for n in range(1, 6):
            state = await s.record_failure(username, IP, now=NOW + timedelta(seconds=n))
            assert state.attempts == n
            assert state.first_failed_at == NOW + timedelta(seconds=1)
            assert state.last_failed_at == NOW + timedelta(seconds=n)
            assert state.lock_count == (1 if n == 5 else 0)
            assert state.locked_until == (NOW + timedelta(minutes=10, seconds=5) if n == 5 else None)
        actual = await row(username)
        assert actual.attempts == 5 and actual.lock_count == 1
    probe(body)


def test_lock_does_not_extend_or_change_fields():
    async def body(username):
        s = service()
        for _ in range(5):
            await s.record_failure(username, IP, now=NOW)
        old = await row(username)
        state = await s.record_failure(username, IP, now=NOW + timedelta(minutes=1))
        actual = await row(username)
        assert state.attempts == actual.attempts == old.attempts == 5
        assert actual.first_failed_at == actual.last_failed_at == NOW
        assert state.locked_until == actual.locked_until == NOW + timedelta(minutes=10)
        assert actual.lock_count == 1
    probe(body)


def test_window_equality_starts_new_window_before_fifth_failure():
    async def body(username):
        s = service()
        for n in range(4):
            await s.record_failure(username, IP, now=NOW + timedelta(seconds=n))
        state = await s.record_failure(username, IP, now=NOW + timedelta(minutes=10))
        assert state.attempts == 1 and state.locked_until is None
        assert state.first_failed_at == state.last_failed_at == NOW + timedelta(minutes=10)
        assert state.lock_count == 0
    probe(body)


def test_fifth_failure_before_window_edge_stays_locked_after_window_expires():
    async def body(username):
        s = service()
        for n in range(4):
            await s.record_failure(username, IP, now=NOW + timedelta(seconds=n))
        state = await s.record_failure(username, IP, now=NOW + timedelta(minutes=9, seconds=59))
        assert state.attempts == 5 and state.locked_until == NOW + timedelta(minutes=19, seconds=59)
        state = await s.record_failure(username, IP, now=NOW + timedelta(minutes=10, seconds=1))
        assert state.attempts == 5 and state.lock_count == 1
        assert state.locked_until == NOW + timedelta(minutes=19, seconds=59)
    probe(body)


def test_backoff_and_expired_window_preserve_tier():
    async def body(username):
        s = service()
        current = NOW
        for minutes in (10, 20, 40, 60, 60):
            for _ in range(5):
                state = await s.record_failure(username, IP, now=current)
            assert state.attempts == 5
            assert state.locked_until == current + timedelta(minutes=minutes)
            current = state.locked_until
        state = await s.record_failure(username, IP, now=current)
        assert state.attempts == 1 and state.lock_count == 5 and state.locked_until is None
        state = await s.record_failure(username, IP, now=current + timedelta(minutes=10))
        assert state.attempts == 1 and state.lock_count == 5
    probe(body)


def test_concurrent_first_failures_serialize_on_one_pair():
    async def body(username):
        s = service()
        await asyncio.gather(*(s.record_failure(username, IP, now=NOW) for _ in range(2)))
        actual = await row(username)
        assert actual.attempts == 2 and actual.locked_until is None and actual.lock_count == 0
    probe(body)


def test_concurrent_failures_create_one_lock_episode():
    async def body(username):
        s = service()
        await asyncio.gather(*(s.record_failure(username, IP, now=NOW) for _ in range(7)))
        actual = await row(username)
        assert actual.attempts == 5 and actual.lock_count == 1
        assert actual.locked_until == NOW + timedelta(minutes=10)
    probe(body)


def test_pair_coordinates_canonicalized_and_isolated():
    async def body(username):
        s = service()
        await s.record_failure("  " + username.upper() + " ", "::ffff:198.51.100.10", now=NOW)
        await s.record_failure(username, IP, now=NOW)
        assert (await row(username)).attempts == 2
        await s.record_failure(username, "2001:0DB8:0:0::1", now=NOW)
        assert (await row(username, "2001:db8::1")).attempts == 1
        await s.record_failure(username, "", now=NOW)
        assert (await row(username, "")).attempts == 1
    probe(body)


def test_ipv6_scope_alias_does_not_split_one_address_into_multiple_keys():
    async def body(username):
        s = service()
        await s.record_failure(username, "2001:db8::1%interface-a", now=NOW)
        await s.record_failure(username, "2001:db8::1%interface-b", now=NOW)
        actual = await row(username, "2001:db8::1")
        assert actual is not None and actual.attempts == 2
    probe(body)


@pytest.mark.parametrize("username,ip", [("", IP), (" " * 3, IP), ("x" * 61, IP),
                                           ("alice", "not-an-ip")])
def test_invalid_pair_is_not_silently_truncated(username, ip):
    with pytest.raises(ValueError):
        service().normalize_pair(username, ip)


def test_aware_clock_is_rejected():
    with pytest.raises(ValueError):
        asyncio.run(service().record_failure("alice", IP, now=NOW.replace(tzinfo=timezone.utc)))


def test_persisted_clock_matches_column_precision():
    async def body(username):
        state = await service().record_failure(username, IP, now=NOW.replace(microsecond=900000))
        actual = await row(username)
        assert state.first_failed_at == actual.first_failed_at == NOW
    probe(body)
