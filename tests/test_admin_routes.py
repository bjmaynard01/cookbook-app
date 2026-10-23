"""Admin HTTP writes require a current admin and a session CSRF token."""
from sqlalchemy import func, select

from cookbook.auth import bootstrap, sessions
from cookbook.db import session_scope
from cookbook.models import Session, User
from bootstrap_support import PASSWORD
from test_setup_routes import web_probe


async def sign_in(client, user):
    raw, row = await sessions.create_session(user.id)
    client.cookies.set(sessions.DEV_COOKIE_NAME, raw)
    return {"X-CSRF-Token": row.csrf_token}


def test_admin_creation_and_all_mutations(monkeypatch):
    async def body(app, client):
        owner = await bootstrap.create_first_admin("owner", PASSWORD)
        headers = await sign_in(client, owner)
        response = await client.post("/admin/users", headers=headers,
            data={"username": "editor", "password": PASSWORD, "display_name": "Family Cook"})
        assert response.status_code == 201
        assert response.headers["cache-control"] == "private, no-store"
        assert set(response.json()) == {"id", "username", "role", "is_active"}
        uid = response.json()["id"]
        assert response.json()["role"] == "editor"
        assert (await client.post("/admin/users", headers=headers,
            data={"username": "editor", "password": PASSWORD})).status_code == 409
        async with session_scope() as db:
            editor = await db.get(User, uid)
            assert editor.display_name == "Family Cook"
        raw, _ = await sessions.create_session(editor.id)
        response = await client.post(f"/admin/users/{uid}/active", headers=headers, data={"active": "false"})
        assert response.status_code == 200 and response.json()["is_active"] is False
        assert await sessions.load_token(raw) is None
        response = await client.post(f"/admin/users/{uid}/role", headers=headers, data={"role": "admin"})
        assert response.status_code == 200 and response.json()["role"] == "admin"
        assert (await client.post(f"/admin/users/{uid}/active", headers=headers, data={"active": "true"})).status_code == 200
        raw, _ = await sessions.create_session(uid)
        assert (await client.post(f"/admin/users/{uid}/password", headers=headers,
            data={"password": "replacement-password"})).status_code == 200
        assert await sessions.load_token(raw) is None
        assert (await client.post("/admin/users/missing/active", headers=headers,
            data={"active": "false"})).status_code == 404
    web_probe(monkeypatch, body)


def test_admin_rejects_anonymous_editor_and_bad_csrf_without_mutation(monkeypatch):
    async def body(app, client):
        owner = await bootstrap.create_first_admin("owner", PASSWORD)
        payload = {"username": "forbidden", "password": PASSWORD}
        assert (await client.post("/admin/users", data=payload)).status_code in (401, 403)
        headers = await sign_in(client, owner)
        for attempted in ({}, {"X-CSRF-Token": "wrong"}):
            response = await client.post("/admin/users", headers=attempted,
                data={**payload, "csrf_token": headers["X-CSRF-Token"]} if attempted else payload)
            assert response.status_code == 403
            assert response.headers["cache-control"] == "private, no-store"
        from cookbook.auth import admin
        editor = await admin.create_user("editor", PASSWORD)
        editor_headers = await sign_in(client, editor)
        assert (await client.post("/admin/users", headers=editor_headers, data=payload)).status_code == 403
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 2
    web_probe(monkeypatch, body)


def test_last_admin_http_denial_and_validation(monkeypatch):
    async def body(app, client):
        owner = await bootstrap.create_first_admin("owner", PASSWORD)
        headers = await sign_in(client, owner)
        for field, value, suffix, status in (("active", "false", "active", 403),
                                            ("role", "editor", "role", 403),
                                            ("active", "0", "active", 400),
                                            ("role", "root", "role", 400),
                                            ("password", "short", "password", 400)):
            response = await client.post(f"/admin/users/{owner.id}/{suffix}", headers=headers, data={field: value})
            assert response.status_code == status
            assert response.headers["cache-control"] == "private, no-store"
        async with session_scope() as db:
            actual = await db.get(User, owner.id)
            assert actual.role == "admin" and actual.is_active
    web_probe(monkeypatch, body)
