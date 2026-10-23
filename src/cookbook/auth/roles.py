"""FastAPI role-gating and CSRF dependencies for authenticated routes.

Both dependencies resolve the session through ``sessions.load_session``,
the single owner of request-local session caching, and never read raw
cookies or cached claims themselves. Session/DB errors always propagate
loud -- they are never downgraded to anonymous access. No cookies are
emitted here; response-side renewal belongs to the route layer (S9).
"""
from __future__ import annotations

from typing import Callable, Literal

from fastapi import Request
from starlette.exceptions import HTTPException

from ..models import User
from . import csrf, sessions

# Higher value means more privilege; admin outranks editor.
_ROLE_RANKS = {"editor": 0, "admin": 1}


def require_role(minimum: Literal["editor", "admin"]) -> Callable:
    """Build a dependency admitting only users ranked at ``minimum`` or above."""
    if minimum not in _ROLE_RANKS:
        raise ValueError(f"unknown minimum role: {minimum!r}")

    async def dependency(request: Request) -> User:
        user = await sessions.load_session(
            request, settings=request.app.state.settings
        )
        if user is None or not user.is_active:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if user.role not in _ROLE_RANKS:
            # A row with an unrecognized role is invalid, not just "too low".
            raise HTTPException(status_code=401, detail="Invalid user role")
        if _ROLE_RANKS[user.role] < _ROLE_RANKS[minimum]:
            raise HTTPException(status_code=403, detail="Insufficient role")
        return user

    return dependency


async def require_csrf(request: Request) -> None:
    """Reject an authenticated mutation whose CSRF token is missing or invalid."""
    user = await sessions.load_session(request, settings=request.app.state.settings)
    if user is None:
        raise HTTPException(status_code=403, detail="Not authenticated")
    # load_session populated request.state.auth_session, which validate_csrf
    # reads; no token value is ever logged or echoed.
    if not await csrf.validate_csrf(request):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
