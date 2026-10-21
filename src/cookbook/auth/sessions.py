"""Server-side session token persistence (auth plan S4, persistence half).

Issues session tokens, validates/slides existing ones, and revokes them.
The raw token is generated once and never stored — only its SHA-256 hash
identifies the row (models.Session contract). Timestamps are always naive
UTC; callers may pass an explicit ``now`` for deterministic behavior.
Cookie/request helpers adapt this service to future routes; no routes are
registered here.
"""
from __future__ import annotations

import hashlib
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from starlette.requests import Request
from starlette.responses import Response

from ..config import Settings, get_settings
from ..db import session_scope
from ..models import Session, User
from .identity import cookie_secure

# secrets.token_urlsafe(32) always yields exactly 43 URL-safe chars;
# bound length + alphabet before touching the database.
_TOKEN_LEN = 43
_TOKEN_CHARS = set(string.ascii_letters + string.digits + "-_")


@dataclass(frozen=True)
class LoadedSession:
    """A validated session and its user, returned after commit."""

    user: User
    session: Session


def _is_well_formed(raw_token: object) -> bool:
    """True iff the token matches the exact format we issue."""
    return (
        isinstance(raw_token, str)
        and len(raw_token) == _TOKEN_LEN
        and all(char in _TOKEN_CHARS for char in raw_token)
    )


def _token_hash(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def _require_naive_utc(now: datetime | None) -> datetime:
    # Reject aware datetimes loudly: silently mixing aware input with the
    # naive DATETIME columns would corrupt expiry comparisons.
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if now.tzinfo is not None:
        raise ValueError("now must be a naive UTC datetime, got an aware one")
    return now


def _resolve_settings(settings: Settings | None) -> Settings:
    return settings if settings is not None else get_settings()


def _ttl_hours(settings: Settings | None) -> int:
    ttl = _resolve_settings(settings).session_ttl_hours
    if ttl <= 0:
        raise ValueError("session TTL must be positive")
    return ttl


async def create_session(
    user_id: str,
    *,
    settings: Settings | None = None,
    created_ip: str | None = None,
    now: datetime | None = None,
) -> tuple[str, Session]:
    """Issue a new session for an active user; returns (raw_token, row).

    Raises ValueError for a missing or inactive user and inserts nothing.
    """
    ts = _require_naive_utc(now)
    ttl = _ttl_hours(settings)
    raw_token = secrets.token_urlsafe(32)
    row = Session(
        id_hash=_token_hash(raw_token),
        user_id=user_id,
        csrf_token=secrets.token_hex(16),
        created_ip=created_ip,
        created_at=ts,
        last_seen_at=ts,
        expires_at=ts + timedelta(hours=ttl),
    )
    async with session_scope() as db:
        # Locked read so a concurrent disable/wipe cannot race us.
        result = await db.execute(
            select(User).where(User.id == user_id).with_for_update()
        )
        user = result.scalar_one_or_none()
        if user is None or not user.is_active:
            raise ValueError(f"cannot issue session: user {user_id!r} is missing or inactive")
        db.add(row)
    return raw_token, row


async def load_token(
    raw_token: str,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> LoadedSession | None:
    """Validate a raw token, slide it, and return LoadedSession.

    None (and a row delete when appropriate) for unknown, expired, or
    inactive-user sessions. Never inserts or resurrects anything.
    """
    if not _is_well_formed(raw_token):
        return None
    ts = _require_naive_utc(now)
    ttl = _ttl_hours(settings)
    async with session_scope() as db:
        digest = _token_hash(raw_token)
        # Discover owner without retaining a session lock. All paths which
        # need both locks acquire user first, then session (as disable does).
        owner = (await db.execute(select(Session.user_id).where(
            Session.id_hash == digest))).scalar_one_or_none()
        if owner is None:
            return None
        user = (await db.execute(select(User).where(
            User.id == owner).with_for_update())).scalar_one_or_none()
        result = await db.execute(
            select(Session)
            .where(Session.id_hash == digest)
            .with_for_update()
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        if user is None or not user.is_active or row.user_id != owner:
            await db.delete(row)
            return None
        # Contract: valid iff expires_at >= now (equality is valid).
        if row.expires_at < ts:
            await db.delete(row)
            return None
        # Requests can acquire the lock out of start-time order. An older
        # request must not move an already renewed session backwards.
        row.last_seen_at = max(ts, row.last_seen_at)
        row.expires_at = row.last_seen_at + timedelta(hours=ttl)
        return LoadedSession(user=user, session=row)


async def revoke_session(raw_token: str) -> None:
    """Idempotently delete one session by its raw token; malformed no-op."""
    if not _is_well_formed(raw_token):
        return
    async with session_scope() as db:
        result = await db.execute(
            select(Session)
            .where(Session.id_hash == _token_hash(raw_token))
            .with_for_update()
        )
        row = result.scalar_one_or_none()
        if row is not None:
            await db.delete(row)


async def revoke_user_sessions(user_id: str) -> None:
    """Idempotently delete all sessions belonging to one user only.

    Owns its transaction. Future account-change code already holding a user
    lock must wipe sessions in that same transaction, not call this helper
    through a second connection while retaining the first lock.
    """
    async with session_scope() as db:
        await db.execute(select(User).where(User.id == user_id).with_for_update())
        result = await db.execute(
            select(Session).where(Session.user_id == user_id).with_for_update()
        )
        for row in result.scalars().all():
            await db.delete(row)


# --- Cookie/request adapter (auth plan S4, cookie half) ---------------------
#
# The persistence half above stays DB-only. These helpers adapt it to
# Starlette request/response: they choose which *name* of cookie is accepted
# per the current security mode (via identity.cookie_secure), emit/clear
# cookie headers, and cache the loaded result on request.state so later
# dependencies in the same request don't re-slide the TTL. The raw token is
# only ever copied from the incoming cookie back into an outgoing header —
# never logged, stored, or placed in URLs.

HOST_COOKIE_NAME = "__Host-cookbook_session"
DEV_COOKIE_NAME = "cookbook_session"


def session_cookie_name(*, secure: bool) -> str:
    """The active session cookie name for the given Secure-flag mode."""
    return HOST_COOKIE_NAME if secure else DEV_COOKIE_NAME


def _validate_cookie_inputs(raw_token: object, ttl_hours: object) -> None:
    # Fail before touching the response so a rejected call leaves no partial
    # Set-Cookie headers behind (see cookie-adapter tests).
    if not _is_well_formed(raw_token):
        raise ValueError("session token is malformed")
    if not isinstance(ttl_hours, int) or ttl_hours <= 0:
        raise ValueError("session TTL must be positive")


def cookie_set(raw_token: str, *, secure: bool, ttl_hours: int = 12) -> str:
    """Render ONE active Set-Cookie header string (no alternate clearing).

    Built through Starlette's own set_cookie so attribute encoding stays
    identical to the response helpers (Path=/, HttpOnly, SameSite=Lax,
    no Domain, Max-Age = ttl_hours*3600). Raises ValueError on a malformed
    token or non-positive TTL.
    """
    _validate_cookie_inputs(raw_token, ttl_hours)
    carrier = Response()
    carrier.set_cookie(
        session_cookie_name(secure=secure),
        raw_token,
        max_age=ttl_hours * 3600,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    for header_name, header_value in reversed(carrier.raw_headers):
        if header_name == b"set-cookie":
            return header_value.decode("latin-1")
    raise RuntimeError("set_cookie emitted no header")  # unreachable


def set_session_cookie(
    response: Response,
    raw_token: str,
    *,
    secure: bool,
    ttl_hours: int = 12,
) -> None:
    """Emit the active session cookie and delete the other cookie name.

    Uses Starlette set_cookie/delete_cookie (never header string splicing)
    and preserves any pre-existing unrelated Set-Cookie headers. Deleting the
    __Host- name always keeps its Secure flag; deleting the dev name omits
    it. The raw token is validated before any header is appended.
    """
    _validate_cookie_inputs(raw_token, ttl_hours)
    active = session_cookie_name(secure=secure)
    alternate = DEV_COOKIE_NAME if active == HOST_COOKIE_NAME else HOST_COOKIE_NAME
    response.set_cookie(
        active,
        raw_token,
        max_age=ttl_hours * 3600,
        path="/",
        secure=secure,
        httponly=True,
        samesite="lax",
    )
    # A browser will not honor an __Host- deletion without Secure.
    response.delete_cookie(
        alternate,
        path="/",
        secure=alternate == HOST_COOKIE_NAME,
        httponly=True,
        samesite="lax",
    )
    response.headers["cache-control"] = "private, no-store"


def clear_session_cookies(response: Response) -> None:
    """Delete BOTH session cookie names; safe for logout/error paths (no DB)."""
    response.delete_cookie(
        HOST_COOKIE_NAME,
        path="/",
        secure=True,  # __Host- deletion requires the Secure attribute.
        httponly=True,
        samesite="lax",
    )
    response.delete_cookie(
        DEV_COOKIE_NAME,
        path="/",
        secure=False,  # the dev name never carries Secure.
        httponly=True,
        samesite="lax",
    )
    response.headers["cache-control"] = "private, no-store"


_SENTINEL = object()


async def load_session(
    request: Request,
    *,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> User | None:
    """Resolve the request's session, caching the result on request.state.

    Only the cookie whose name matches the current security mode is accepted
    (identity.cookie_secure decides); a stale alternate name is ignored
    rather than falling back to an insecure token. The LoadedSession — or
    None — is cached on ``request.state.auth_session`` for the life of this
    request only, so repeated dependency consumers don't slide the TTL twice.
    DB errors propagate; they are never downgraded to anonymous.
    """
    settings = _resolve_settings(settings)
    cached: object = getattr(request.state, "auth_session", _SENTINEL)
    if cached is not _SENTINEL:
        return cached.user if isinstance(cached, LoadedSession) else None

    name = session_cookie_name(secure=cookie_secure(request, settings))
    raw_token = request.cookies.get(name)
    if not raw_token:
        request.state.auth_session = None
        return None

    loaded = await load_token(raw_token, settings=settings, now=now)
    request.state.auth_session = loaded
    return loaded.user if loaded is not None else None


def renew_session_cookie(
    response: Response,
    request: Request,
    *,
    settings: Settings | None = None,
) -> None:
    """Re-issue the active cookie (and clear the alternate) when authenticated.

    Relies entirely on ``request.state.auth_session`` set by load_session —
    the server-side slide already happened there, so this helper makes no
    DB calls and re-validates nothing. The token value is read back from the
    request's own (mode-selected) cookie, without storing it in request state.
    Anonymous/missing state means no headers are added at all.
    """
    cached: object = getattr(request.state, "auth_session", _SENTINEL)
    if cached is _SENTINEL or not isinstance(cached, LoadedSession):
        return
    settings = _resolve_settings(settings)
    secure = cookie_secure(request, settings)
    raw_token = request.cookies.get(session_cookie_name(secure=secure))
    if not raw_token:
        return
    set_session_cookie(
        response,
        raw_token,
        secure=secure,
        ttl_hours=_ttl_hours(settings),
    )
