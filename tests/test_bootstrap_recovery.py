"""Password writes and local existing-owner recovery are transactional."""
import hashlib
import importlib

import pytest
from sqlalchemy import delete, event, func, select

from cookbook.auth import sessions
from cookbook.auth.passwords import verify_password
from cookbook.db import get_engine, session_scope
from cookbook.models import AuthLock, Session, User
from bootstrap_support import PASSWORD, probe


def service():
    return importlib.import_module("cookbook.auth.bootstrap")


def test_password_reset_revokes_all_sessions_without_role_changes():
    async def body():
        s = service()
        user = await s.create_first_admin("owner", PASSWORD)
        raw, _ = await sessions.create_session(user.id)
        await sessions.create_session(user.id)
        await s.reset_password(" OWNER ", "replacement-password")
        async with session_scope() as db:
            actual = await db.get(User, user.id)
            assert actual.role == "admin" and actual.is_active
            assert verify_password("replacement-password", actual.password_hash)
            assert not verify_password(PASSWORD, actual.password_hash)
            assert (await db.execute(select(func.count()).select_from(Session))).scalar_one() == 0
        assert await sessions.load_token(raw) is None
    probe(body)


def test_recovery_restores_named_existing_account_and_never_creates_one():
    async def body():
        s = service()
        user = await s.create_first_admin("owner", PASSWORD)
        async with session_scope() as db:
            actual = await db.get(User, user.id)
            actual.role = "editor"
            actual.is_active = False
        await s.reset_password("owner", "replacement-password", restore_admin=True)
        async with session_scope() as db:
            actual = await db.get(User, user.id)
            assert actual.role == "admin" and actual.is_active
            assert verify_password("replacement-password", actual.password_hash)
        with pytest.raises(s.UserNotFound):
            await s.reset_password("nonexistent", PASSWORD, restore_admin=True)
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 1
    probe(body)


def test_missing_recovery_lock_fails_without_password_or_session_changes():
    async def body():
        s = service()
        user = await s.create_first_admin("owner", PASSWORD)
        raw, _ = await sessions.create_session(user.id)
        async with session_scope() as db:
            await db.execute(delete(AuthLock).where(AuthLock.name == "last_admin"))
        with pytest.raises(s.LockRowMissing):
            await s.reset_password("owner", "replacement-password", restore_admin=True)
        async with session_scope() as db:
            assert verify_password(PASSWORD, (await db.get(User, user.id)).password_hash)
            assert await db.get(Session, hashlib.sha256(raw.encode()).hexdigest()) is not None
    probe(body)


def test_password_policy_rejection_preserves_hash_and_sessions():
    async def body():
        s = service()
        user = await s.create_first_admin("owner", PASSWORD)
        raw, _ = await sessions.create_session(user.id)
        with pytest.raises(ValueError):
            await s.reset_password("owner", "short")
        async with session_scope() as db:
            assert verify_password(PASSWORD, (await db.get(User, user.id)).password_hash)
            assert await db.get(Session, hashlib.sha256(raw.encode()).hexdigest()) is not None
    probe(body)


def test_session_delete_failure_rolls_back_password_change():
    async def body():
        s = service()
        user = await s.create_first_admin("owner", PASSWORD)
        raw, _ = await sessions.create_session(user.id)
        engine = get_engine().sync_engine

        def fail_delete(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("DELETE FROM SESSIONS"):
                raise RuntimeError("injected session deletion failure")

        event.listen(engine, "before_cursor_execute", fail_delete)
        try:
            with pytest.raises(RuntimeError, match="injected session deletion failure"):
                await s.reset_password("owner", "replacement-password")
        finally:
            event.remove(engine, "before_cursor_execute", fail_delete)
        async with session_scope() as db:
            assert verify_password(PASSWORD, (await db.get(User, user.id)).password_hash)
            assert await db.get(Session, hashlib.sha256(raw.encode()).hexdigest()) is not None
    probe(body)
