"""Admin user management routes (form-in, fixed JSON-out; templates later).

Admin role gate then CSRF on every route; mutations delegate to the
``admin`` service (no DB here). Every response — success, error, or
dependency denial — carries ``Cache-Control: private, no-store``; lock
rows and DB faults propagate as 500 (never stringified); no cookies.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from fastapi import APIRouter, Depends, Request
from starlette.exceptions import HTTPException
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

try:
    from starlette.formparsers import MultiPartException
except ImportError:  # older Starlette
    from starlette.exceptions import MultipartException as MultiPartException

from ..models import User
from . import admin
from .roles import require_csrf, require_role

_SAFE_HEADERS = {"Cache-Control": "private, no-store"}

# Only form-level failures become 400; lock rows/DB faults stay loud as 500.
_FORM_ERRORS = (MultiPartException, HTTPException, ValueError)
_DOMAIN_ERRORS = (
    admin.UserNotFound, admin.LastAdminProtected, admin.UsernameTaken, ValueError,
)


class SafeAdminRoute(APIRoute):
    """Return fixed safe JSON for errors; safe cache headers for all."""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request):
            try:
                response = await original(request)
            except HTTPException as exc:
                # Only fixed, app-authored details reach here; echoing a
                # string is safe, anything else degrades instead of str(exc).
                detail = exc.detail if isinstance(exc.detail, str) else "error"
                return JSONResponse(
                    status_code=exc.status_code,
                    content={"error": detail},
                    headers=_SAFE_HEADERS,
                )
            response.headers.setdefault("Cache-Control", "private, no-store")
            return response

        return handler


_DEPS = [Depends(require_role("admin")), Depends(require_csrf)]

router = APIRouter(route_class=SafeAdminRoute)


async def _form_fields(request: Request) -> dict[str, str]:
    """Read the form once; non-string (e.g. UploadFile) values are invalid."""
    try:
        form = await request.form()
    except _FORM_ERRORS as exc:
        raise HTTPException(status_code=400, detail="invalid form data") from exc
    fields: dict[str, str] = {}
    for key, value in form.multi_items():
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail="invalid form data")
        fields.setdefault(key, value)
    return fields


def _required(fields: dict[str, str], name: str) -> str:
    value = fields.get(name)
    if not isinstance(value, str):
        raise HTTPException(status_code=400, detail="invalid form data")
    return value


async def _service_call(coro: Awaitable[User]) -> User:
    try:
        return await coro
    except _DOMAIN_ERRORS as exc:
        if isinstance(exc, admin.UserNotFound):
            raise HTTPException(status_code=404, detail="user not found") from exc
        if isinstance(exc, admin.LastAdminProtected):
            raise HTTPException(status_code=403, detail="protected user") from exc
        if isinstance(exc, admin.UsernameTaken):
            raise HTTPException(status_code=409, detail="username taken") from exc
        # Remaining ValueError (bad role/active/username): invalid input.
        raise HTTPException(status_code=400, detail="invalid input") from exc


def _public_user(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "is_active": user.is_active,
    }


@router.post("/admin/users", status_code=201, dependencies=_DEPS)
async def create_user_route(request: Request):
    fields = await _form_fields(request)
    username = _required(fields, "username")
    password = _required(fields, "password")
    role = fields.get("role", "editor")  # optional; service validates the value
    user = await _service_call(admin.create_user(
        username, password, role=role, display_name=fields.get("display_name")
    ))
    return _public_user(user)


@router.post("/admin/users/{user_id}/active", dependencies=_DEPS)
async def set_active_route(request: Request, user_id: str):
    fields = await _form_fields(request)
    raw = fields.get("active")
    # Exactly 'true'/'false'; missing or anything else is a 400.
    if raw not in ("true", "false"):
        raise HTTPException(status_code=400, detail="invalid form data")
    user = await _service_call(admin.set_user_active(user_id, raw == "true"))
    return _public_user(user)


@router.post("/admin/users/{user_id}/role", dependencies=_DEPS)
async def change_role_route(request: Request, user_id: str):
    fields = await _form_fields(request)
    role = _required(fields, "role")  # service rejects anything else
    user = await _service_call(admin.change_role(user_id, role))
    return _public_user(user)


@router.post("/admin/users/{user_id}/password", dependencies=_DEPS)
async def reset_password_route(request: Request, user_id: str):
    fields = await _form_fields(request)
    password = _required(fields, "password")
    user = await _service_call(admin.reset_user_password(user_id, password))
    return _public_user(user)
