"""Standalone first-owner guard contracts against disposable MariaDB."""
import asyncio
import importlib

import pytest
from sqlalchemy import delete, func, select

from cookbook.auth.passwords import verify_password
from cookbook.db import get_session, session_scope
from cookbook.models import AuthLock, User
from bootstrap_support import PASSWORD, probe


def service():
    return importlib.import_module("cookbook.auth.bootstrap")


def test_first_owner_is_active_admin_with_normalized_username():
    async def body():
        s = service()
        assert await s.is_bootstrapped() is False
        user = await s.create_first_admin(" Owner ", PASSWORD)
        assert user.id and user.username == "owner" and user.role == "admin" and user.is_active
        assert verify_password(PASSWORD, user.password_hash)
        assert await s.is_bootstrapped() is True
        with pytest.raises(s.AlreadyBootstrapped):
            await s.create_first_admin("second", PASSWORD)
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 1
    probe(body)


def test_parallel_bootstrap_has_one_winner():
    async def body():
        s = service()
        results = await asyncio.gather(s.create_first_admin("owner-a", PASSWORD),
                                       s.create_first_admin("owner-b", PASSWORD), return_exceptions=True)
        assert sum(isinstance(r, User) for r in results) == 1
        assert sum(isinstance(r, s.AlreadyBootstrapped) for r in results) == 1
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 1
    probe(body)


def test_missing_lock_is_loud_and_creates_no_user():
    async def body():
        s = service()
        async with session_scope() as db:
            await db.execute(delete(AuthLock).where(AuthLock.name == "first_admin"))
        with pytest.raises(s.LockRowMissing):
            await s.create_first_admin("owner", PASSWORD)
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 0
    probe(body)


def test_guard_does_not_inherit_a_callers_old_user_snapshot():
    async def body():
        s = service()
        async with get_session()() as stale:
            assert (await stale.execute(select(func.count()).select_from(User))).scalar_one() == 0
            await s.create_first_admin("owner", PASSWORD)
            assert (await stale.execute(select(func.count()).select_from(User))).scalar_one() == 0
            with pytest.raises(s.AlreadyBootstrapped):
                await s.create_first_admin("second", PASSWORD)
    probe(body)


@pytest.mark.parametrize("name", ["", " " * 3, "x" * 61, "İ" * 60])
def test_invalid_username_never_creates_owner(name):
    async def body():
        with pytest.raises(ValueError):
            await service().create_first_admin(name, PASSWORD)
        assert await service().is_bootstrapped() is False
    probe(body)


def test_password_policy_and_spaces_preserved():
    async def body():
        s = service()
        with pytest.raises(ValueError):
            await s.create_first_admin("owner", "short")
        assert await s.is_bootstrapped() is False
        user = await s.create_first_admin("owner", " 123456 ")
        assert verify_password(" 123456 ", user.password_hash)
        assert not verify_password("123456", user.password_hash)
    probe(body)
