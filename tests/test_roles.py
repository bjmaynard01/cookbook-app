"""Real database roles are resolved anew on each request."""
import importlib

import pytest
from fastapi import Depends

from cookbook.auth import bootstrap, sessions
from cookbook.db import session_scope
from cookbook.models import User
from bootstrap_support import PASSWORD
from test_setup_routes import web_probe


def attach_gates(app):
    roles = importlib.import_module("cookbook.auth.roles")
    @app.get("/_gate/public")
    async def public():
        return {"ok": True}
    @app.api_route("/_gate/editor", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def editor(user=Depends(roles.require_role("editor"))):
        return {"username": user.username}
    @app.api_route("/_gate/admin", methods=["GET", "POST"])
    async def admin(user=Depends(roles.require_role("admin"))):
        return {"username": user.username}


def test_current_roles_anonymous_editor_admin_and_revocation(monkeypatch):
    async def body(app, client):
        attach_gates(app)
        assert (await client.get("/_gate/public")).status_code == 200
        assert (await client.get("/_gate/editor")).status_code == 401
        owner = await bootstrap.create_first_admin("owner", PASSWORD)
        raw, _ = await sessions.create_session(owner.id)
        client.cookies.set(sessions.DEV_COOKIE_NAME, raw)
        assert (await client.get("/_gate/admin")).status_code == 200
        async with session_scope() as db:
            (await db.get(User, owner.id)).role = "editor"
        assert (await client.get("/_gate/editor")).status_code == 200
        assert (await client.get("/_gate/admin")).status_code == 403
        async with session_scope() as db:
            (await db.get(User, owner.id)).is_active = False
        assert (await client.get("/_gate/editor")).status_code == 401
    web_probe(monkeypatch, body)


def test_invalid_minimum_role_is_configuration_error():
    roles = importlib.import_module("cookbook.auth.roles")
    with pytest.raises(ValueError):
        roles.require_role("superuser")
