"""Global unsafe-method CSRF and response caching acceptance."""
import importlib

import httpx
import pytest
from fastapi import Request
from starlette.responses import Response
from sqlalchemy import func, select

from cookbook.auth import bootstrap, sessions
from cookbook.db import session_scope
from cookbook.models import Session, User
from bootstrap_support import PASSWORD
from test_admin_routes import sign_in
from test_roles import attach_gates
from test_setup_routes import web_probe
from test_login_routes import cookies, token


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_global_csrf_rejects_before_unguarded_handler(monkeypatch, method):
    async def body(app, client):
        calls = []
        @app.api_route("/_unsafe", methods=[method])
        async def unsafe(request: Request):
            calls.append(await request.body())
            return {"ok": True}
        assert (await client.request(method, "/_unsafe")).status_code == 403
        owner = await bootstrap.create_first_admin("owner", PASSWORD)
        headers = await sign_in(client, owner)
        assert (await client.request(method, "/_unsafe")).status_code == 403
        assert (await client.request(method, "/_unsafe", headers={"X-CSRF-Token": "wrong"},
            data={"csrf_token": headers["X-CSRF-Token"]})).status_code == 403
        assert calls == []
        response = await client.request(method, "/_unsafe", headers=headers, json={"value": "preserved"})
        assert response.status_code == 200 and b'preserved' in calls[-1]
        assert response.headers["cache-control"] == "private, no-store"
        assert (await client.request(method, "/_unsafe", data={"csrf_token": headers["X-CSRF-Token"],
            "value": "classic-form"})).status_code == 200
        assert b'classic-form' in calls[-1]
        assert (await client.request(method, "/_unsafe", files={"csrf_token": (None, headers["X-CSRF-Token"]),
            "upload": ("example.txt", b"file-preserved")})).status_code == 200
        assert b'file-preserved' in calls[-1]
    web_probe(monkeypatch, body)


def test_cache_split_and_cookie_renewal(monkeypatch):
    async def body(app, client):
        attach_gates(app)
        public = await client.get("/_gate/public")
        assert public.headers["cache-control"] == "public, max-age=300, s-maxage=600"
        assert "set-cookie" not in public.headers
        assert (await client.get("/_gate/editor")).headers["cache-control"] == "private, no-store"
        owner = await bootstrap.create_first_admin("owner", PASSWORD)
        await sign_in(client, owner)
        response = await client.get("/_gate/editor")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
        assert "set-cookie" in response.headers
        assert (await client.get("/_gate/public")).headers["cache-control"] == "private, no-store"
    web_probe(monkeypatch, body)


@pytest.mark.parametrize("path", ["/login", "/setup"])
@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE"])
def test_preauth_paths_do_not_exempt_other_unsafe_methods(monkeypatch, path, method):
    async def body(app, client):
        assert (await client.request(method, path)).status_code == 403
    web_probe(monkeypatch, body)


def test_cache_helper_contract():
    cache = importlib.import_module("cookbook.auth.cache")
    assert cache.cache_headers_for("/r/example", behind_login=False) == {
        "Cache-Control": "public, max-age=300, s-maxage=600"}
    for path in ("/login", "/setup", "/logout", "/admin/users", "/edit", "/recipes/new", "/recipes/abc/edit"):
        assert cache.cache_headers_for(path, behind_login=False)["Cache-Control"] == "private, no-store"
    assert cache.cache_headers_for("/r/example", behind_login=True)["Cache-Control"] == "private, no-store"
    assert cache.cache_headers_for("/editorial", behind_login=False)["Cache-Control"].startswith("public,")


def test_health_probe_never_loads_auth_session(monkeypatch):
    async def body(app, client):
        async def forbidden(*args, **kwargs):
            pytest.fail("liveness probe queried authentication")
        monkeypatch.setattr(sessions, "load_session", forbidden)
        client.cookies.set(sessions.DEV_COOKIE_NAME, "a" * 43)
        assert (await client.get("/healthz")).status_code == 200
    web_probe(monkeypatch, body)


def test_middleware_and_role_dependency_load_session_once(monkeypatch):
    async def body(app, client):
        attach_gates(app)
        owner = await bootstrap.create_first_admin("owner", PASSWORD)
        await sign_in(client, owner)
        calls = []
        original = sessions.load_token
        async def counted(*args, **kwargs):
            calls.append(1)
            return await original(*args, **kwargs)
        monkeypatch.setattr(sessions, "load_token", counted)
        assert (await client.get("/_gate/editor")).status_code == 200
        assert len(calls) == 1
    web_probe(monkeypatch, body)


def test_unexpected_errors_are_private_and_do_not_expose_details(monkeypatch):
    async def body(app, client):
        @app.get("/_gate/fault")
        async def fault():
            raise RuntimeError("sensitive-internal-detail")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                                     base_url="http://test") as browser:
            response = await browser.get("/_gate/fault")
            assert response.status_code == 500
            assert response.headers["cache-control"] == "private, no-store"
            assert "sensitive-internal-detail" not in response.text
    web_probe(monkeypatch, body)


def test_cookie_setting_response_is_never_shared_cacheable(monkeypatch):
    async def body(app, client):
        @app.get("/_cookie")
        async def cookie_page():
            response = Response("personalized")
            response.set_cookie("preference", "private-value")
            return response
        response = await client.get("/_cookie")
        assert response.headers["cache-control"] == "private, no-store"
    web_probe(monkeypatch, body)


def test_integrated_auth_lifecycle(monkeypatch):
    async def body(app, client):
        attach_gates(app)
        page = await client.get("/setup")
        response = await client.post("/setup", data={"username": "owner", "password": PASSWORD,
                                                     "csrf_token": token(page)})
        assert response.status_code == 302
        assert (await client.get("/setup")).status_code == 403
        assert (await client.post("/setup")).status_code == 403
        page = await client.get("/login")
        assert (await client.post("/login", data={"username": "owner", "password": PASSWORD,
                                                 "csrf_token": token(page)})).status_code == 302
        raw = client.cookies.get(sessions.DEV_COOKIE_NAME)
        loaded = await sessions.load_token(raw)
        headers = {"X-CSRF-Token": loaded.session.csrf_token}
        for path in ("/_gate/editor", "/_gate/admin"):
            assert (await client.post(path, headers=headers)).status_code == 200
        created = await client.post("/admin/users", headers=headers,
            data={"username": "editor", "password": PASSWORD})
        assert created.status_code == 201
        uid = created.json()["id"]
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as editor:
            page = await editor.get("/login")
            assert (await editor.post("/login", data={"username": "editor", "password": PASSWORD,
                "csrf_token": token(page)})).status_code == 302
            editor_raw = editor.cookies.get(sessions.DEV_COOKIE_NAME)
            editor_loaded = await sessions.load_token(editor_raw)
            editor_headers = {"X-CSRF-Token": editor_loaded.session.csrf_token}
            assert (await editor.post("/_gate/editor", headers=editor_headers)).status_code == 200
            assert (await editor.post("/_gate/admin", headers=editor_headers)).status_code == 403
            assert (await client.post(f"/admin/users/{uid}/active", headers=headers,
                data={"active": "false"})).status_code == 200
            assert await sessions.load_token(editor_raw) is None
            assert (await editor.get("/_gate/editor")).status_code == 401
            page = await editor.get("/login")
            payload = {"username": "owner", "password": "wrong-password", "csrf_token": token(page)}
            for _ in range(5):
                assert (await editor.post("/login", data=payload)).status_code == 401
            from cookbook.auth import passwords, lockout
            with monkeypatch.context() as patch:
                def forbidden(*args, **kwargs):
                    pytest.fail("locked request did password work")
                patch.setattr(passwords, "verify_password", forbidden)
                locked = await editor.post("/login", data=payload)
                assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0
            async with session_scope() as db:
                before = (await db.execute(select(func.count()).select_from(Session))).scalar_one()
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as other:
                await other.get("/login")
                assert (await other.post("/login", data=payload)).status_code == 403
                assert (await other.get("/_gate/public")).headers["cache-control"] == "public, max-age=300, s-maxage=600"
            async with session_scope() as db:
                assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 2
                assert (await db.execute(select(func.count()).select_from(Session))).scalar_one() == before
            await lockout.clear_pair("owner", "127.0.0.1")
            payload["password"] = PASSWORD
            assert (await editor.post("/login", data=payload)).status_code == 302
        response = await client.post("/logout", headers=headers)
        assert response.status_code == 302
        assert response.headers["cache-control"] == "private, no-store"
        assert await sessions.load_token(raw) is None
        assert cookies(response)[sessions.DEV_COOKIE_NAME]["max-age"] == "0"
    web_probe(monkeypatch, body)


@pytest.mark.parametrize("scheme,override,secure", [("https", None, True), ("https", False, False), ("http", True, True)])
def test_setup_cookie_security_modes(monkeypatch, scheme, override, secure):
    async def body(app, client):
        app.state.settings.cookie_secure = override
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=f"{scheme}://test") as browser:
            page = await browser.get("/setup")
            response = await browser.post("/setup", data={"username": "owner", "password": PASSWORD,
                                                          "csrf_token": token(page)})
            assert response.status_code == 302
            name = sessions.session_cookie_name(secure=secure)
            issued = cookies(response)[name]
            assert bool(issued["secure"]) is secure
            assert issued["httponly"] and issued["path"] == "/" and not issued["domain"]
            assert bool(name.startswith("__Host-")) is secure
    web_probe(monkeypatch, body)
