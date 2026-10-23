"""Admin account management helpers (auth plan C5/C6 slice).

Every public helper opens its OWN fresh session and transaction (an optional
``factory`` keyword overrides ``db.get_session()``), so it never rides on the
caller's snapshot. Guarded mutations serialize on the
``auth_locks.last_admin`` row BEFORE touching the target user, which makes
"the last active admin cannot be demoted/deactivated" atomic. Password
hashing happens via ``asyncio.to_thread`` BEFORE the transaction starts.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..db import get_session
from ..models import AuthLock, Session, User
from .bootstrap import LockRowMissing, UserNotFound, normalize_username
from .passwords import hash_password

__all__ = [
    "LastAdminProtected", "LockRowMissing", "UserNotFound", "UsernameTaken",
    "change_role", "create_user", "modify_admin", "reset_user_password",
    "set_user_active",
]


class LastAdminProtected(RuntimeError):
    """Raised when a mutation would leave the app with zero active admins."""


class UsernameTaken(ValueError):
    """Raised when the requested account name already exists."""


def _check_role(role: str) -> str:
    if role not in ("editor", "admin"):
        raise ValueError("role must be exactly 'editor' or 'admin'")
    return role


def _check_active(active: object) -> bool:
    if not isinstance(active, bool):
        raise ValueError("active must be a real bool (True/False); no coercion")
    return active


def _is_duplicate_key(error: IntegrityError) -> bool:
    # MySQL 1062 = ER_DUP_ENTRY, carried in the underlying DBAPI args.
    orig = error.orig
    return orig is not None and getattr(orig, "args", (None,))[0] == 1062


async def create_user(
    username: str,
    password: str,
    *,
    role: str = "editor",
    display_name: str | None = None,
    factory: async_sessionmaker | None = None,
) -> User:
    """Insert a new ACTIVE account; a duplicate username raises UsernameTaken."""
    normalized = normalize_username(username)
    _check_role(role)
    if display_name is not None and (not isinstance(display_name, str) or len(display_name) > 100):
        raise ValueError("display name must be at most 100 characters")
    # Hash before any DB work: hashing is pure CPU and needs no transaction.
    password_hash = await asyncio.to_thread(hash_password, password)
    user = User(
        username=normalized,
        password_hash=password_hash,
        role=role,
        is_active=True,
        display_name=display_name,
    )
    session_factory = factory if factory is not None else get_session()
    async with session_factory() as db:
        try:
            async with db.begin():
                db.add(user)
                await db.flush()
        except IntegrityError as exc:
            # db.begin() already rolled back; only the unique-key case is ours.
            if not _is_duplicate_key(exc):
                raise
            raise UsernameTaken(normalized) from exc
    return user


async def modify_admin(
    target_user_id: str,
    *,
    role: str | None = None,
    is_active: bool | None = None,
    factory: async_sessionmaker | None = None,
) -> User:
    """Atomically change a user's role and/or active flag with last-admin guard.

    Updates only the requested fields. When the resulting state is a role
    change or an inactive account, all of the user's Session rows are deleted
    in the SAME transaction.
    """
    if role is not None:
        _check_role(role)
    if is_active is not None:
        _check_active(is_active)
    session_factory = factory if factory is not None else get_session()
    async with session_factory() as db:
        async with db.begin():
            lock = (await db.execute(
                select(AuthLock).where(AuthLock.name == "last_admin")
                .with_for_update()
            )).scalar_one_or_none()
            if lock is None:
                raise LockRowMissing("last_admin")
            user = (await db.execute(
                select(User).where(User.id == target_user_id).with_for_update()
            )).scalar_one_or_none()
            if user is None:
                raise UserNotFound(target_user_id)
            # No-op requests (both fields None) still lock but mutate nothing.
            if role is not None or is_active is not None:
                new_role = role if role is not None else user.role
                new_active = is_active if is_active is not None else user.is_active
                was_last_active_admin = (
                    user.role == "admin" and user.is_active
                    and not (new_role == "admin" and new_active is True)
                    and (await db.execute(
                        select(func.count()).select_from(User).where(
                            User.role == "admin", User.is_active.is_(True)
                        )
                    )).scalar_one() <= 1
                )
                if was_last_active_admin:
                    raise LastAdminProtected(
                        "refusing: this would remove the last active admin"
                    )
                original_role = user.role
                if role is not None:
                    user.role = role
                if is_active is not None:
                    user.is_active = is_active
                # Kick sessions out when the account changed roles or ends
                # up inactive — either way its granted access must not linger.
                if user.role != original_role or user.is_active is False:
                    await db.execute(
                        delete(Session).where(Session.user_id == user.id)
                    )
            await db.flush()
    return user


async def set_user_active(
    target_user_id: str,
    active: bool,
    *,
    factory: async_sessionmaker | None = None,
) -> User:
    """Activate/deactivate an account; always goes through modify_admin."""
    _check_active(active)
    return await modify_admin(
        target_user_id, is_active=active, factory=factory
    )


async def change_role(
    target_user_id: str,
    role: str,
    *,
    factory: async_sessionmaker | None = None,
) -> User:
    """Promote/demote an account; always goes through modify_admin."""
    _check_role(role)
    return await modify_admin(
        target_user_id, role=role, factory=factory
    )


async def reset_user_password(
    target_user_id: str,
    password: str,
    *,
    factory: async_sessionmaker | None = None,
) -> User:
    """Replace a stored hash and drop all sessions; no last-admin guard here."""
    password_hash = await asyncio.to_thread(hash_password, password)
    session_factory = factory if factory is not None else get_session()
    async with session_factory() as db:
        async with db.begin():
            user = (await db.execute(
                select(User).where(User.id == target_user_id).with_for_update()
            )).scalar_one_or_none()
            if user is None:
                raise UserNotFound(target_user_id)
            user.password_hash = password_hash
            await db.execute(
                delete(Session).where(Session.user_id == user.id)
            )
            await db.flush()
    return user
