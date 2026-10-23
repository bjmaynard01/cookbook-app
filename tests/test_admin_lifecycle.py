"""Guarded account changes on isolated MariaDB."""
import asyncio
import importlib

import pytest
from sqlalchemy import delete, event, func, select

from cookbook.auth import bootstrap, sessions
from cookbook.auth.passwords import verify_password
from cookbook.db import get_engine, get_session, session_scope
from cookbook.models import AuthLock, Session, User
from bootstrap_support import PASSWORD, probe


def service():
    return importlib.import_module("cookbook.auth.admin")


def test_create_user_normalizes_and_defaults_editor():
    async def body():
        admin = service()
        user = await admin.create_user(" EDITOR ", " 123456 ")
        assert user.role == "editor" and user.is_active and user.username == "editor"
        assert verify_password(" 123456 ", user.password_hash)
        with pytest.raises(admin.UsernameTaken):
            await admin.create_user("editor", PASSWORD)
        for role in ("root", "Admin", ""):
            with pytest.raises(ValueError):
                await admin.create_user("bad", PASSWORD, role=role)
        with pytest.raises(ValueError):
            await admin.create_user("short", "short")
    probe(body)


def test_optional_display_name_is_preserved_and_bounded():
    async def body():
        admin = service()
        user = await admin.create_user("editor", PASSWORD, display_name="Family Cook")
        assert user.display_name == "Family Cook"
        with pytest.raises(ValueError):
            await admin.create_user("long-name", PASSWORD, display_name="x" * 101)
    probe(body)


def test_last_active_admin_protection_and_noop():
    async def body():
        admin = service()
        owner = await bootstrap.create_first_admin("owner", PASSWORD)
        await admin.change_role(owner.id, "admin")
        await admin.set_user_active(owner.id, True)
        for operation in (admin.change_role(owner.id, "editor"), admin.set_user_active(owner.id, False)):
            with pytest.raises(admin.LastAdminProtected):
                await operation
        async with session_scope() as db:
            actual = await db.get(User, owner.id)
            assert actual.is_active and actual.role == "admin"
        other = await admin.create_user("other", PASSWORD, role="admin")
        await admin.set_user_active(other.id, False)
        with pytest.raises(admin.LastAdminProtected):
            await admin.change_role(owner.id, "editor")
    probe(body)


def test_disable_and_reset_wipe_sessions_atomically():
    async def body():
        admin = service()
        user = await admin.create_user("editor", PASSWORD)
        raw, _ = await sessions.create_session(user.id)
        await admin.set_user_active(user.id, False)
        assert await sessions.load_token(raw) is None
        await admin.set_user_active(user.id, True)
        raw, _ = await sessions.create_session(user.id)
        await sessions.create_session(user.id)
        await admin.reset_user_password(user.id, "replacement-password")
        async with session_scope() as db:
            assert verify_password("replacement-password", (await db.get(User, user.id)).password_hash)
            assert (await db.execute(select(func.count()).select_from(Session))).scalar_one() == 0
        assert await sessions.load_token(raw) is None
    probe(body)


def test_parallel_admin_removal_has_one_winner():
    async def body():
        admin = service()
        a = await bootstrap.create_first_admin("a", PASSWORD)
        b = await admin.create_user("b", PASSWORD, role="admin")
        results = await asyncio.gather(admin.set_user_active(a.id, False),
                                       admin.change_role(b.id, "editor"), return_exceptions=True)
        assert sum(isinstance(r, User) for r in results) == 1
        assert sum(isinstance(r, admin.LastAdminProtected) for r in results) == 1
        async with session_scope() as db:
            count = (await db.execute(select(func.count()).select_from(User).where(
                User.role == "admin", User.is_active.is_(True)))).scalar_one()
            assert count == 1
    probe(body)


def test_guard_ignores_stale_snapshot_and_missing_lock_is_loud():
    async def body():
        admin = service()
        a = await bootstrap.create_first_admin("a", PASSWORD)
        b = await admin.create_user("b", PASSWORD, role="admin")
        async with get_session()() as stale:
            assert len((await stale.execute(select(User))).scalars().all()) == 2
            await admin.set_user_active(a.id, False)
            with pytest.raises(admin.LastAdminProtected):
                await admin.change_role(b.id, "editor")
        raw, _ = await sessions.create_session(b.id)
        async with session_scope() as db:
            await db.execute(delete(AuthLock).where(AuthLock.name == "last_admin"))
        with pytest.raises(bootstrap.LockRowMissing):
            await admin.change_role(b.id, "editor")
        assert await sessions.load_token(raw) is not None
        async with session_scope() as db:
            assert (await db.get(User, b.id)).role == "admin"
    probe(body)


def test_invalid_change_and_missing_target_leave_state_unchanged():
    async def body():
        admin = service()
        user = await admin.create_user("editor", PASSWORD)
        raw, _ = await sessions.create_session(user.id)
        with pytest.raises(ValueError):
            await admin.set_user_active(user.id, "false")
        with pytest.raises(ValueError):
            await admin.change_role(user.id, "root")
        with pytest.raises(ValueError):
            await admin.reset_user_password(user.id, "short")
        with pytest.raises(bootstrap.UserNotFound):
            await admin.set_user_active("missing", False)
        assert await sessions.load_token(raw) is not None
    probe(body)


@pytest.mark.parametrize("operation", ["disable", "demote", "password"])
def test_session_wipe_failure_rolls_back_account_change(operation):
    async def body():
        admin = service()
        await bootstrap.create_first_admin("owner", PASSWORD)
        user = await admin.create_user("other", PASSWORD, role="admin")
        raw, _ = await sessions.create_session(user.id)
        engine = get_engine().sync_engine
        def fail_delete(conn, cursor, statement, parameters, context, executemany):
            if statement.lstrip().upper().startswith("DELETE FROM SESSIONS"):
                raise RuntimeError("injected session wipe failure")
        event.listen(engine, "before_cursor_execute", fail_delete)
        try:
            with pytest.raises(RuntimeError, match="injected session wipe failure"):
                if operation == "disable":
                    await admin.set_user_active(user.id, False)
                elif operation == "demote":
                    await admin.change_role(user.id, "editor")
                else:
                    await admin.reset_user_password(user.id, "replacement-password")
        finally:
            event.remove(engine, "before_cursor_execute", fail_delete)
        async with session_scope() as db:
            actual = await db.get(User, user.id)
            assert actual.role == "admin" and actual.is_active
            assert verify_password(PASSWORD, actual.password_hash)
        assert await sessions.load_token(raw) is not None
    probe(body)
