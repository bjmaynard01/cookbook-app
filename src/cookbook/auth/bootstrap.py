"""First-owner bootstrap guard (auth plan C5).

Creates the single initial admin exactly once, serialized by the
``auth_locks.first_admin`` row. Every public helper opens its own fresh
session and transaction — none inherits the caller's snapshot.
"""
from __future__ import annotations

import asyncio

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from ..db import get_session
from ..models import AuthLock, Session, User
from .passwords import hash_password


class AlreadyBootstrapped(RuntimeError):
    """Raised when an admin already exists and bootstrap is attempted again."""


class UserNotFound(RuntimeError):
    """Raised when a reset targets an account name that does not exist."""


class LockRowMissing(RuntimeError):
    def __init__(self, name: str = "first_admin") -> None:
        super().__init__(
            f"auth_locks row '{name}' is missing; refusing bootstrap."
        )


def normalize_username(name: str) -> str:
    """Lowercase and strip; require 1..60 chars, else raise ValueError."""
    normalized = name.strip().lower()
    if not 1 <= len(normalized) <= 60:
        raise ValueError("username must be 1-60 non-whitespace characters")
    return normalized


async def create_first_admin(
    username: str, password: str, *, factory: async_sessionmaker | None = None
) -> User:
    """Create the one and only initial admin; raise if one already exists."""
    normalized = normalize_username(username)
    password_hash = await asyncio.to_thread(hash_password, password)
    session_factory = factory if factory is not None else get_session()
    async with session_factory() as db:
        async with db.begin():
            lock = (await db.execute(
                select(AuthLock).where(AuthLock.name == "first_admin")
                .with_for_update()
            )).scalar_one_or_none()
            if lock is None:
                raise LockRowMissing()
            user_count = (await db.execute(
                select(func.count()).select_from(User)
            )).scalar_one()
            if user_count:
                raise AlreadyBootstrapped("first admin already exists")
            user = User(
                username=normalized,
                password_hash=password_hash,
                role="admin",
                is_active=True,
            )
            db.add(user)
            await db.flush()
            return user


async def is_bootstrapped(*, factory: async_sessionmaker | None = None) -> bool:
    """Whether any user row already exists (read-only probe)."""
    session_factory = factory if factory is not None else get_session()
    async with session_factory() as db:
        count = (await db.execute(
            select(func.count()).select_from(User)
        )).scalar_one()
    return count > 0


async def reset_password(
    username: str,
    password: str,
    *,
    restore_admin: bool = False,
    factory: async_sessionmaker | None = None,
) -> User:
    """Replace a password on an existing account; never creates one."""
    normalized = normalize_username(username)
    password_hash = await asyncio.to_thread(hash_password, password)
    session_factory = factory if factory is not None else get_session()
    async with session_factory() as db:
        async with db.begin():
            if restore_admin:
                lock = (await db.execute(
                    select(AuthLock).where(AuthLock.name == "last_admin")
                    .with_for_update()
                )).scalar_one_or_none()
                if lock is None:
                    raise LockRowMissing("last_admin")
            user = (await db.execute(
                select(User).where(User.username == normalized).with_for_update()
            )).scalar_one_or_none()
            if user is None:
                raise UserNotFound(normalized)
            user.password_hash = password_hash
            if restore_admin:
                user.role = "admin"
                user.is_active = True
            await db.execute(delete(Session).where(Session.user_id == user.id))
            await db.flush()
            return user
