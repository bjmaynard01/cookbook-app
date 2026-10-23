"""Global authentication, CSRF, and cache middleware for every request.

Enforced before the downstream handler runs:
* /login and /setup (unsafe): the FORM preauth token bound to the
  preauth browser cookie (a header token is never valid). Failures are a
  403 with no Set-Cookie; a closed /setup additionally clears the
  browser secret, preserving the closed-setup contract.
* every other unsafe request (POST/PUT/PATCH/DELETE): a validated session
  plus the session CSRF token (X-CSRF-Token header or form fallback).
* safe requests (except the auth endpoints and health probes): the session
  is loaded so downstream role dependencies reuse it from
  request.state.auth_session instead of re-sliding the TTL.

After the handler runs: responses that do not already carry a
private, no-store policy get Cache-Control from cache_headers_for (route
policy is never weakened), and the session cookie is renewed on
authenticated responses unless the route already set its own session
cookie (logout clearing / new login). The loaded user is exposed as
request.state.user. The body is never logged (nor any token or secret).
"""
from __future__ import annotations

from starlette.datastructures import FormData
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import Response
from starlette.status import HTTP_403_FORBIDDEN
from starlette.types import ASGIApp
from starlette.middleware.base import BaseHTTPMiddleware

from . import bootstrap, cache, csrf, sessions
from . import routes as auth_routes
from .identity import client_identity

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
PREAUTH_PATHS = frozenset({"/login", "/setup"})
NO_LOAD_PATHS = frozenset({"/login", "/setup", "/logout", "/healthz", "/readyz"})
NO_RENEW_PATHS = frozenset({"/login", "/setup", "/logout"})
_NO_STORE = "private, no-store"
_SESSION_COOKIE_NAMES = frozenset(
    {sessions.HOST_COOKIE_NAME.lower(), sessions.DEV_COOKIE_NAME.lower()}
)


def _denied() -> Response:
    """The one fixed 403 shape for every failed check (no Set-Cookie)."""
    return Response(
        status_code=HTTP_403_FORBIDDEN, headers={"Cache-Control": _NO_STORE}
    )


def _preauth_ok(request: Request, path: str, form: FormData) -> bool:
    """FORM preauth token bound to the browser secret cookie.

    The token must be a string form value AND the cookie must be present;
    a header token is never valid. Malformed/missing inputs fail closed.
    """
    token = form.get("csrf_token")
    secret = request.cookies.get(csrf.PREAUTH_COOKIE_NAME)
    if not isinstance(token, str) or not secret:
        return False
    settings = request.app.state.settings
    return csrf.verify_preauth(
        token,
        secret,
        path.lstrip("/"),
        now=auth_routes.utc_now(),
        key=settings.secret_key,
    )


def _has_session_set_cookie(response: Response) -> bool:
    for header in response.headers.getlist("set-cookie"):
        name = header.split("=", 1)[0].strip().lower()
        if name in _SESSION_COOKIE_NAMES:
            return True
    return False


class AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method
        settings = request.app.state.settings

        user = None
        if method in UNSAFE_METHODS:
            if path in PREAUTH_PATHS:
                # Preauth flows never authenticate from a session cookie.
                if path == "/setup" and await bootstrap.is_bootstrapped():
                    # Preserve the closed-setup contract: refuse and clear
                    # the browser binding so nothing survives the rejection.
                    denied = Response(
                        status_code=HTTP_403_FORBIDDEN,
                        headers={"Cache-Control": _NO_STORE},
                    )
                    # Clear with the effective scheme from client_identity;
                    # COOKIE_SECURE governs the session cookie only.
                    _ip, scheme = client_identity(request, settings)
                    denied.headers.append(
                        "set-cookie",
                        csrf.browser_secret_clear(secure=(scheme == "https")),
                    )
                    return denied
                # Read the body (and form) up front: Starlette caches the
                # body, so it stays available for the downstream handler.
                await request.body()
                try:
                    form = await request.form()
                except (HTTPException, MultiPartException, ValueError):
                    return _denied()
                if not _preauth_ok(request, path, form):
                    return _denied()
            else:
                # One load_session call (also primes request.state.auth_session
                # for the downstream role dependencies); a missing session or
                # a bad CSRF token is a cookie-free 403. DB faults propagate.
                user = await sessions.load_session(request, settings=settings)
                if user is None:
                    # Reject before touching the body: a rejected request must
                    # not drain the stream the downstream handler would replay.
                    return _denied()
                if request.headers.get("x-csrf-token") is None:
                    # No header token, so validate_csrf falls back to the form:
                    # pre-cache the body so the form parser can't consume the
                    # downstream stream.
                    await request.body()
                if not await csrf.validate_csrf(request):
                    return _denied()
        elif path not in NO_LOAD_PATHS:
            # Anonymous safe requests never hit the DB (no cookie -> None),
            # but an authenticated one slides its TTL exactly once.
            user = await sessions.load_session(request, settings=settings)

        if user is not None:
            request.state.user = user
        response = await call_next(request)

        existing = (response.headers.get("cache-control") or "").lower()
        if (
            user is not None
            or method in UNSAFE_METHODS
            or response.status_code >= 400
            or response.headers.getlist("set-cookie")
            or "private" in existing
            or "no-store" in existing
        ):
            # Authenticated, unsafe, error, cookie-setting, and already
            # private/no-store responses are never shareable. Never weaken a
            # route's existing policy.
            response.headers["cache-control"] = _NO_STORE
        else:
            for name, value in cache.cache_headers_for(
                path, behind_login=False
            ).items():
                response.headers[name] = value

        if user is not None and path not in NO_RENEW_PATHS:
            # Respect cookies the route already set (logout clears the session,
            # a fresh login owns its own); leave other Set-Cookie entries alone.
            if not _has_session_set_cookie(response):
                sessions.renew_session_cookie(response, request, settings=settings)
        return response