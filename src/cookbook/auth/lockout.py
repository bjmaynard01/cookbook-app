"""Login-failure lockout state machine (auth plan S6, failure-recording half).

Records one failed login attempt for a normalized (username, ip) pair and
returns an immutable snapshot of the resulting lock state. The state machine
(10-minute window, escalating 10/20/40/60-minute locks), read-only lock checks,
and transaction-aware reset live here. The missing-row race is
resolved with an INSERT ... ON DUPLICATE KEY UPDATE no-op upsert followed by
SELECT ... FOR UPDATE in the SAME transaction, so concurrent first failures
serialize on the unique pair instead of double-creating rows.

Timestamps are always naive UTC with microsecond truncated to zero (the
schema column has no sub-second precision). No DB NOW(), no logging, no
catches — errors propagate and the session rolls back.
"""
from __future__ import annotations

import ipaddress
import math
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.dialects.mysql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..db import session_scope
from ..models import LoginAttempt

#: Failures in a 10-minute window before the account locks out.
_WINDOW = timedelta(minutes=10)

#: Lock duration per lock tier (lock_count 1, 2, 3, 4+), in minutes; the
#: last entry is the cap so tier 5+ never escalates past one hour.
_LOCK_MINUTES = (10, 20, 40, 60)

_USERNAME_MIN_LEN = 1
_USERNAME_MAX_LEN = 60


@dataclass(frozen=True)
class LockState:
    """Immutable copy of a LoginAttempt row as of a recorded failure."""

    attempts: int
    first_failed_at: datetime
    last_failed_at: datetime
    lock_count: int
    locked_until: datetime | None


def normalize_pair(username: str, ip: str) -> tuple[str, str]:
    """Canonicalize a (username, ip) pair, rejecting invalid input.

    Username: strip + lowercase, must be 1..60 chars after stripping —
    never silently truncated. IP: strip, then canonical ipaddress text
    (compact IPv6, mapped IPv6 collapsed to its IPv4). An empty IP is kept
    as an explicit "unknown client" bucket. Raises ValueError on any
    invalid component before any database work happens.
    """
    if not isinstance(username, str) or not isinstance(ip, str):
        raise ValueError("username and ip must be strings")

    normalized_username = username.strip().lower()
    if not (_USERNAME_MIN_LEN <= len(normalized_username) <= _USERNAME_MAX_LEN):
        raise ValueError("username must be 1-60 characters after stripping")

    normalized_ip = ip.strip()
    if not normalized_ip:
        return normalized_username, ""

    addr = ipaddress.ip_address(normalized_ip)
    if isinstance(addr, ipaddress.IPv6Address):
        # An interface/zone identifier is routing metadata, not another IP.
        # Drop it before keying so one address cannot acquire alias counters.
        addr = addr.ipv4_mapped or ipaddress.IPv6Address(int(addr))
    return normalized_username, str(addr)


def _coerce_now(now: datetime | None) -> datetime:
    """Resolve the failure timestamp: reject aware input, drop microseconds."""
    if now is None:
        now = datetime.now(timezone.utc).replace(tzinfo=None)
    if not isinstance(now, datetime) or now.tzinfo is not None:
        raise ValueError("now must be a naive UTC datetime, got an aware one")
    return now.replace(microsecond=0)


async def record_failure(username: str, ip: str, *, now: datetime | None = None) -> LockState:
    """Record one failed login for the pair and return the new lock state."""
    normalized_username, normalized_ip = normalize_pair(username, ip)
    ts = _coerce_now(now)

    async with session_scope() as db:
        # Upsert no-op: create the zero-attempt row if (and only if) the
        # unique pair is missing; the duplicate-key path is a self-assignment
        # that changes nothing. Concurrent first failures serialize here.
        upsert = (
            insert(LoginAttempt)
            .values(
                id=str(uuid.uuid4()),
                username=normalized_username,
                ip=normalized_ip,
                attempts=0,
                first_failed_at=ts,
                last_failed_at=ts,
                lock_count=0,
                locked_until=None,
            )
            .on_duplicate_key_update(id=LoginAttempt.id)
        )
        await db.execute(upsert)

        # Row lock for the rest of the transaction: every concurrent
        # recorder for this pair waits here and re-reads fresh state.
        row = (
            await db.execute(
                select(LoginAttempt)
                .where(
                    LoginAttempt.username == normalized_username,
                    LoginAttempt.ip == normalized_ip,
                )
                .with_for_update()
            )
        ).scalar_one()

        _apply_state_machine(row, ts)
        return LockState(
            attempts=row.attempts,
            first_failed_at=row.first_failed_at,
            last_failed_at=row.last_failed_at,
            lock_count=row.lock_count,
            locked_until=row.locked_until,
        )


class Locked(Exception):
    """Raised when a (username, ip) pair is inside its lock window."""

    def __init__(self, retry_after: int) -> None:
        super().__init__("Account temporarily locked. Try again later.")
        self.retry_after = retry_after


async def assert_not_locked(username: str, ip: str, *, now: datetime | None = None) -> None:
    """Raise Locked if the pair is inside its lock window; read-only, no writes."""
    normalized_username, normalized_ip = normalize_pair(username, ip)
    ts = _coerce_now(now)
    async with session_scope() as db:
        row = (
            await db.execute(
                select(LoginAttempt).where(
                    LoginAttempt.username == normalized_username,
                    LoginAttempt.ip == normalized_ip,
                )
            )
        ).scalar_one_or_none()
    if row is not None and row.locked_until is not None and row.locked_until > ts:
        raise Locked(math.ceil((row.locked_until - ts).total_seconds()))


async def clear_pair(username: str, ip: str, *, db: AsyncSession | None = None) -> None:
    """Delete the pair's counter row. Idempotent; no-op when no row exists.

    With a caller session, only the DELETE runs — the caller owns commit,
    rollback and closure. Without one, a self-contained session_scope is used.
    """
    normalized_username, normalized_ip = normalize_pair(username, ip)
    statement = delete(LoginAttempt).where(
        LoginAttempt.username == normalized_username,
        LoginAttempt.ip == normalized_ip,
    )
    if db is not None:
        await db.execute(statement)
        return
    async with session_scope() as own_db:
        await own_db.execute(statement)


def _lock_duration_minutes(lock_count: int) -> int:
    """Lock minutes for the tier; caps at 60 without exponent math."""
    return _LOCK_MINUTES[min(lock_count, len(_LOCK_MINUTES)) - 1]


def _apply_state_machine(row: LoginAttempt, ts: datetime) -> None:
    """Advance the (already row-locked) counter row by one failure.

    Active lock: untouched. Window expired or lock already lapsed: start a
    fresh single-attempt window while preserving the lock tier. Otherwise the
    episode continues, and the fifth attempt of an episode closes it with a
    lock exactly once (the row is serialized, so nothing passes it again).
    """
    locked = row.locked_until is not None and row.locked_until > ts
    if locked:
        return

    window_expired = row.first_failed_at + _WINDOW <= ts
    lock_lapsed = row.locked_until is not None and row.locked_until <= ts
    if window_expired or lock_lapsed or row.attempts == 0:
        row.attempts = 1
        row.first_failed_at = ts
        row.last_failed_at = ts
        row.locked_until = None
    else:
        row.attempts += 1
        row.last_failed_at = ts

    if row.attempts >= 5 and row.locked_until is None:
        row.lock_count += 1
        row.locked_until = ts + timedelta(minutes=_lock_duration_minutes(row.lock_count))
