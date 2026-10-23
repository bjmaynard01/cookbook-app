"""Login forms and CSRF-protected login/logout routes.

GET renders the classic HTML login form with a per-request CSRF preauth
token; a fresh browser secret is issued on every GET and delivered as a
Set-Cookie (the cookie value is never rendered into the HTML).

POST verifies the preauth CSRF token before any database or password work,
checks credentials, and on success issues a server session and redirects to
"/". Credential failures return an identical generic 401 login render so nothing
distinguishes unknown user, wrong password, or inactive user.
Logout validates the session's CSRF token before revoking and clearing cookies.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request
from sqlalchemy import select
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.responses import RedirectResponse, Response
from starlette.templating import Jinja2Templates

from ..db import session_scope
from ..models import User
from . import csrf, lockout, passwords, sessions
from .identity import client_identity, cookie_secure

templates = Jinja2Templates(directory=Path(__file__).resolve().parent.parent / "templates")
router = APIRouter()


def utc_now() -> datetime:
    # Naive UTC: the app's timestamp convention is tz-naive UTC datetimes.
    return datetime.now(timezone.utc).replace(tzinfo=None)


@router.get("/login")
async def login_page(request: Request):
    settings = request.app.state.settings

    # A fresh secret on every GET means a replayed or cached copy of the
    # login page cannot reuse a previous browser secret (anti-replay).
    browser_secret = secrets.token_urlsafe(16)
    csrf_token = csrf.issue_preauth(
        browser_secret,
        "login",
        now=utc_now(),
        key=settings.secret_key,
    )

    # Starlette 1.7 signature: request first, then template name and context.
    # The browser secret is deliberately NOT passed into the template.
    response = templates.TemplateResponse(
        request,
        "login.html",
        {"csrf_token": csrf_token, "error": None},
        status_code=200,
    )

    # Append (not set) so any cookies already on the response survive.
    response.headers.append(
        "set-cookie",
        csrf.browser_secret_set_cookie(
            browser_secret,
            secure=client_identity(request, settings)[1] == "https",
        ),
    )
    # Login pages must never be cached or shared.
    response.headers["Cache-Control"] = "private, no-store"
    return response


def _login_error_response(request: Request, csrf_token: str, error: str) -> Response:
    """Retain the validated token and browser binding for a credential retry."""
    response = templates.TemplateResponse(
        request,
        "login.html",
        {"csrf_token": csrf_token, "error": error},
        status_code=401,
    )
    # A 401 login page is just as sensitive as a 200 one: never cache or share.
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.post("/login")
async def login_submit(request: Request):
    settings = request.app.state.settings
    try:
        form = await request.form()
    except (HTTPException, MultiPartException, ValueError):
        return Response(status_code=403, headers={"Cache-Control": "private, no-store"})

    # The preauth token travels in the form (hidden field), never a header.
    csrf_token = form.get("csrf_token")
    browser_secret = request.cookies.get(csrf.PREAUTH_COOKIE_NAME)

    # CSRF check failure is a 403 BEFORE any DB or password work: no
    # credential timing side-channel can leak from a forged cross-site form.
    if not (
        isinstance(csrf_token, str)
        and browser_secret
        and csrf.verify_preauth(
            csrf_token,
            browser_secret,
            "login",
            now=utc_now(),
            key=settings.secret_key,
        )
    ):
        denied = Response(status_code=403)
        # No Set-Cookie: the browser secret must stay untouched on 403.
        denied.headers["Cache-Control"] = "private, no-store"
        return denied

    username = form.get("username")
    password = form.get("password")
    generic_401 = "Invalid username or password."
    if not isinstance(username, str) or not isinstance(password, str):
        return _login_error_response(request, csrf_token, generic_401)

    # Canonical lockout key. Invalid pairs fail with the same 401 body as a
    # credential rejection; no counter can be written for a pair we cannot
    # canonicalize, and no password work ever happens.
    try:
        pair = lockout.normalize_pair(username, client_identity(request, settings)[0])
    except ValueError:
        return _login_error_response(request, csrf_token, generic_401)

    # Locked check BEFORE any DB credential lookup or password work, so a
    # locked pair never spends Argon2 cycles. Only Locked is caught: DB and
    # other operational errors propagate instead of being miscounted.
    try:
        await lockout.assert_not_locked(*pair, now=utc_now())
    except lockout.Locked as exc:
        # Fixed message: no username, IP, token, or count that would let an
        # attacker distinguish pairs. No cookie is set, deleted, or refreshed.
        response = templates.TemplateResponse(
            request,
            "login.html",
            {
                "csrf_token": csrf_token,
                "error": "Too many failed attempts. Please wait before trying again.",
            },
            status_code=429,
        )
        response.headers["Retry-After"] = str(exc.retry_after)
        response.headers["Cache-Control"] = "private, no-store"
        return response

    # Read credentials without retaining a transaction during password work.
    async with session_scope() as db:
        # pair[0] is already stripped+lowercased; the DB match is case-insensitive.
        result = await db.execute(select(User).where(User.username == pair[0]))
        user = result.scalar_one_or_none()

    if user is None:
        # Comparable Argon2 work for an unknown account, then count it.
        await run_in_threadpool(passwords.dummy_verify, password)
        await lockout.record_failure(*pair, now=utc_now())
        return _login_error_response(request, csrf_token, generic_401)

    if not user.is_active:
        # Still burn a verify so an attacker cannot fingerprint deactivation.
        await run_in_threadpool(passwords.verify_password, password, user.password_hash)
        await lockout.record_failure(*pair, now=utc_now())
        return _login_error_response(request, csrf_token, generic_401)

    if not await run_in_threadpool(passwords.verify_password, password, user.password_hash):
        await lockout.record_failure(*pair, now=utc_now())
        return _login_error_response(request, csrf_token, generic_401)

    # Success: issue the session, set the session cookie, and clear the
    # preauth browser secret in the same response. login_pair lets
    # create_session clear this pair's failure history inside its own
    # transaction; a standalone clear_pair would race and is never called.
    try:
        raw_token, _row = await sessions.create_session(
            user.id,
            settings=settings,
            created_ip=pair[1] or None,
            expected_password_hash=user.password_hash,
            login_pair=pair,
        )
    except sessions.SessionIssueRefused:
        await lockout.record_failure(*pair, now=utc_now())
        return _login_error_response(request, csrf_token, generic_401)
    secure = client_identity(request, settings)[1] == "https"
    response = RedirectResponse("/", status_code=302)
    sessions.set_session_cookie(
        response, raw_token, secure=cookie_secure(request, settings),
        ttl_hours=settings.session_ttl_hours
    )
    # Clear the pre-auth binding after authentication succeeds.
    response.headers.append("set-cookie", csrf.browser_secret_clear(secure=secure))
    # A redirect carrying a fresh session cookie must never be cached/shared.
    response.headers["Cache-Control"] = "private, no-store"
    return response


@router.post("/logout")
async def logout(request: Request):
    settings = request.app.state.settings
    # load_session sets request.state.auth_session, which validate_csrf uses.
    user = await sessions.load_session(request, settings=settings)

    # A valid session AND a valid CSRF token are both required; failure is a
    # 403 with no cookie clearing or server-side revocation.
    if user is None or not await csrf.validate_csrf(request):
        denied = Response(status_code=403)
        denied.headers["Cache-Control"] = "private, no-store"
        return denied

    # Revoke server-side using the name of the cookie the client actually sent.
    raw = request.cookies.get(
        sessions.session_cookie_name(secure=cookie_secure(request, settings))
    )
    await sessions.revoke_session(raw)

    response = RedirectResponse("/login", status_code=302)
    # Explicitly expire BOTH session cookie names in the same response.
    sessions.clear_session_cookies(response)
    response.headers.append(
        "set-cookie",
        csrf.browser_secret_clear(secure=client_identity(request, settings)[1] == "https"),
    )
    # A logout redirect must never be cached or shared.
    response.headers["Cache-Control"] = "private, no-store"
    return response
