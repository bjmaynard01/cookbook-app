"""Session service persistence tests against isolated MariaDB."""
from __future__ import annotations

import asyncio
import hashlib
import importlib
import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from cookbook.config import Settings
from cookbook.db import get_engine, session_scope
from cookbook.models import Session, User

NOW = datetime(2026, 10, 21, 12, 0, 0, 123456)


def service():
    # Lazy import lets missing implementation fail during execution, not collection.
    return importlib.import_module("cookbook.auth.sessions")


def settings(ttl=12):
    return Settings(_env_file=None, DEBUG=False, SESSION_TTL_HOURS=ttl)


async def add_user(uid, *, active=True):
    async with session_scope() as db:
        db.add(User(id=uid, username=f"session-{uid}", password_hash="test",
                    is_active=active, role="editor"))


async def stored(raw):
    async with session_scope() as db:
        return await db.get(Session, hashlib.sha256(raw.encode()).hexdigest())


def run_probe(body):
    async def run():
        uids = [str(uuid.uuid4()) for _ in range(3)]
        try:
            await body(uids)
        finally:
            try:
                async with session_scope() as db:
                    for uid in uids:
                        user = await db.get(User, uid)
                        if user:
                            await db.delete(user)
            finally:
                await get_engine().dispose()
    asyncio.run(run())


def test_issue_unique_hash_only_sessions_and_custom_ttl():
    async def body(uids):
        s = service()
        await add_user(uids[0])
        raw, row = await s.create_session(uids[0], settings=settings(2),
                                          created_ip="2001:db8::1", now=NOW)
        raw2, row2 = await s.create_session(uids[0], settings=settings(2), now=NOW)
        assert raw != raw2 and len(raw) == 43
        assert row.id_hash == hashlib.sha256(raw.encode()).hexdigest()
        assert len(row.csrf_token) == 32 and row.csrf_token != row2.csrf_token
        assert row.created_at == row.last_seen_at == NOW
        assert row.expires_at == NOW + timedelta(hours=2)
        actual = await stored(raw)
        assert actual.created_ip == "2001:db8::1"
        assert actual.user_id == uids[0]
        assert raw not in repr(actual)
        assert raw not in vars(actual).values()
    run_probe(body)


def test_load_slides_without_changing_issue_or_csrf_fields():
    async def body(uids):
        s = service()
        await add_user(uids[0])
        raw, row = await s.create_session(uids[0], settings=settings(), now=NOW)
        later = NOW + timedelta(hours=1)
        loaded = await s.load_token(raw, settings=settings(), now=later)
        assert loaded.user.id == uids[0]
        assert loaded.session.id_hash == row.id_hash
        assert loaded.session.created_at == NOW
        assert loaded.session.csrf_token == row.csrf_token
        assert loaded.session.last_seen_at == later
        assert loaded.session.expires_at == later + timedelta(hours=12)
        actual = await stored(raw)
        assert actual.last_seen_at == later
    run_probe(body)


def test_older_request_timestamp_cannot_shorten_already_slid_session():
    """Concurrent requests may reach the row lock in reverse start-time order."""
    async def body(uids):
        s = service()
        await add_user(uids[0])
        raw, _ = await s.create_session(uids[0], settings=settings(), now=NOW)
        newest = NOW + timedelta(hours=2)
        await s.load_token(raw, settings=settings(), now=newest)
        result = await s.load_token(raw, settings=settings(), now=NOW + timedelta(hours=1))
        assert result.session.last_seen_at == newest
        assert result.session.expires_at == newest + timedelta(hours=12)
    run_probe(body)


@pytest.mark.parametrize("ttl", [0, -1])
def test_issue_requires_positive_ttl(ttl):
    async def run():
        with pytest.raises(ValueError, match="TTL"):
            await service().create_session(str(uuid.uuid4()), settings=settings(ttl), now=NOW)
    asyncio.run(run())


@pytest.mark.parametrize("offset,valid", [(timedelta(0), True),
                                         (timedelta(microseconds=1), False)])
def test_expiry_boundary(offset, valid):
    async def body(uids):
        s = service()
        await add_user(uids[0])
        raw, row = await s.create_session(uids[0], settings=settings(), now=NOW)
        result = await s.load_token(raw, settings=settings(), now=row.expires_at + offset)
        assert (result is not None) is valid
        assert ((await stored(raw)) is not None) is valid
    run_probe(body)


def test_inactive_or_missing_user_cannot_issue_and_disabled_session_dies():
    async def body(uids):
        s = service()
        await add_user(uids[0], active=False)
        for uid in (uids[0], uids[1]):
            with pytest.raises(ValueError):
                await s.create_session(uid, settings=settings(), now=NOW)
        await add_user(uids[1])
        raw, _ = await s.create_session(uids[1], settings=settings(), now=NOW)
        async with session_scope() as db:
            user = await db.get(User, uids[1])
            user.is_active = False
        assert await s.load_token(raw, settings=settings(), now=NOW) is None
        assert await stored(raw) is None
        async with session_scope() as db:
            assert not (await db.execute(select(Session).where(
                Session.user_id == uids[0]))).scalars().all()
    run_probe(body)


def test_revoke_one_and_all_never_resurrect_or_affect_other_user():
    async def body(uids):
        s = service()
        for uid in uids[:2]:
            await add_user(uid)
        a, _ = await s.create_session(uids[0], settings=settings(), now=NOW)
        b, _ = await s.create_session(uids[0], settings=settings(), now=NOW)
        c, _ = await s.create_session(uids[1], settings=settings(), now=NOW)
        await s.revoke_session(a)
        await s.revoke_session(a)
        assert await s.load_token(a, settings=settings(), now=NOW) is None
        assert await stored(b) is not None
        await s.revoke_user_sessions(uids[0])
        await s.revoke_user_sessions(uids[0])
        assert await stored(b) is None
        assert await stored(c) is not None
    run_probe(body)


def test_unknown_well_formed_token_returns_none():
    async def body(_uids):
        assert await service().load_token(secrets.token_urlsafe(32),
                                         settings=settings(), now=NOW) is None
    run_probe(body)


@pytest.mark.parametrize("token", [None, "", "short", "!" * 43, "a" * 10000,
                                  "é" * 43, b"a" * 43],
                         ids=["none", "empty", "short", "punctuation", "oversize",
                              "unicode", "bytes"])
def test_malformed_tokens_skip_database(monkeypatch, token):
    s = service()
    def forbidden():
        raise AssertionError("Malformed token touched database")
    monkeypatch.setattr(s, "session_scope", forbidden)
    async def run():
        assert await s.load_token(token, settings=settings(), now=NOW) is None
        await s.revoke_session(token)
    asyncio.run(run())


def test_aware_time_rejected_before_database():
    async def run():
        s = service()
        with pytest.raises(ValueError):
            await s.create_session(str(uuid.uuid4()), settings=settings(),
                                   now=NOW.replace(tzinfo=timezone.utc))
    asyncio.run(run())


def test_default_clock_and_settings_are_used_without_storing_raw_token(monkeypatch, caplog):
    async def body(uids):
        s = service()
        class FixedClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return NOW.replace(tzinfo=timezone.utc) if tz else NOW
        monkeypatch.setattr(s, "datetime", FixedClock)
        monkeypatch.setattr(s, "get_settings", lambda: settings(3))
        await add_user(uids[0])
        raw, row = await s.create_session(uids[0])
        assert row.created_at == NOW
        assert row.expires_at == NOW + timedelta(hours=3)
        assert (await s.load_token(raw)).user.id == uids[0]
        assert raw not in caplog.text
    run_probe(body)


def test_database_errors_propagate_from_token_lookup(monkeypatch):
    s = service()
    def failed():
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(s, "session_scope", failed)
    async def run():
        with pytest.raises(RuntimeError, match="database unavailable"):
            await s.load_token(secrets.token_urlsafe(32), settings=settings(), now=NOW)
    asyncio.run(run())


def test_load_waiting_for_user_does_not_lock_session_first(monkeypatch):
    """A disable holding the user lock can still wipe sessions without deadlock."""
    async def body(uids):
        s = service()
        await add_user(uids[0])
        raw, row = await s.create_session(uids[0], settings=settings(), now=NOW)
        about_to_lock_user = asyncio.Event()
        original_scope = s.session_scope
        @asynccontextmanager
        async def observed_scope():
            async with original_scope() as db:
                execute = db.execute
                async def observed_execute(statement, *args, **kwargs):
                    sql = str(statement)
                    if "FROM users" in sql and "FOR UPDATE" in sql:
                        about_to_lock_user.set()
                    return await execute(statement, *args, **kwargs)
                db.execute = observed_execute
                yield db
        monkeypatch.setattr(s, "session_scope", observed_scope)
        task = None
        try:
            async with session_scope() as db:
                await db.execute(select(User).where(User.id == uids[0]).with_for_update())
                task = asyncio.create_task(s.load_token(raw, settings=settings(), now=NOW))
                await asyncio.wait_for(about_to_lock_user.wait(), timeout=5)
                # Fails immediately if the loader owns the session while waiting
                # for our user lock: exactly the opposite-order deadlock pattern.
                await db.execute(select(Session).where(Session.id_hash == row.id_hash)
                                 .with_for_update(nowait=True))
        finally:
            if task is not None:
                await asyncio.wait_for(task, timeout=5)
    run_probe(body)
