"""First-owner HTTP setup and pre-auth CSRF acceptance."""
import asyncio
from datetime import datetime, timedelta

import httpx
import pytest
from sqlalchemy import delete, func, select

from cookbook.app import create_app
from cookbook.config import get_settings
from cookbook.db import session_scope
from cookbook.models import AuthLock, LoginAttempt, Session, User
from bootstrap_support import PASSWORD, probe
from test_login_routes import cookies, token


def web_probe(monkeypatch, body):
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "setup-test-key")
    monkeypatch.delenv("SECRET_KEY_FILE", raising=False)
    monkeypatch.delenv("FIRST_ADMIN_PASSWORD_FILE", raising=False)
    get_settings.cache_clear()
    async def wrapped():
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await body(app, client)
    try:
        probe(wrapped)
    finally:
        get_settings.cache_clear()


async def submit(client, value, **extra):
    data = {"username": "owner", "password": PASSWORD, "csrf_token": value}
    data.update(extra)
    return await client.post("/setup", data=data)


def test_setup_page_rotates_browser_binding_and_closes_after_cli_creation(monkeypatch):
    async def body(app, client):
        from cookbook.auth import bootstrap
        first, second = await client.get("/setup"), await client.get("/setup")
        assert first.status_code == second.status_code == 200
        assert cookies(first)["cookbook_csrf_pre"].value != cookies(second)["cookbook_csrf_pre"].value
        assert first.headers["cache-control"] == second.headers["cache-control"] == "private, no-store"
        assert token(first) and token(second)
        assert 'autocomplete="new-password"' in second.text
        await bootstrap.create_first_admin("owner", PASSWORD)
        closed = await client.get("/setup")
        assert closed.status_code == 403
        assert cookies(closed)["cookbook_csrf_pre"]["max-age"] == "0"
    web_probe(monkeypatch, body)


def test_zero_user_setup_creates_one_owner_session_then_closes(monkeypatch):
    async def body(app, client):
        response = await client.get("/login")
        assert response.status_code == 302 and response.headers["location"] == "/setup"
        assert response.headers["cache-control"] == "private, no-store"
        assert (await client.post("/login", data={"username": "owner", "password": PASSWORD})).status_code == 403
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(LoginAttempt))).scalar_one() == 0
            assert (await db.execute(select(func.count()).select_from(Session))).scalar_one() == 0
        page = await client.get("/setup")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "private, no-store"
        secret = cookies(page)["cookbook_csrf_pre"]
        assert secret["httponly"] and secret["samesite"].lower() == "strict"
        assert secret.value not in page.text
        response = await submit(client, token(page))
        assert response.status_code == 302 and response.headers["location"] == "/"
        assert cookies(response)["cookbook_csrf_pre"]["max-age"] == "0"
        assert cookies(response)["cookbook_session"].value
        async with session_scope() as db:
            user = (await db.execute(select(User))).scalar_one()
            assert user.username == "owner" and user.role == "admin" and user.is_active
            assert (await db.execute(select(Session))).scalar_one().user_id == user.id
        for method in (client.get, client.post):
            closed = await method("/setup")
            assert closed.status_code == 403
            assert closed.headers["cache-control"] == "private, no-store"
            assert cookies(closed)["cookbook_csrf_pre"]["max-age"] == "0"
    web_probe(monkeypatch, body)


def test_non_ip_peer_is_rejected_before_creating_owner(monkeypatch):
    async def body(app, client):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("local-peer", 123)),
                                     base_url="http://test") as local:
            page = await local.get("/setup")
            response = await submit(local, token(page))
            assert response.status_code == 400
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 0
    web_probe(monkeypatch, body)


def test_session_issuance_refusal_closes_setup_without_authentication(monkeypatch):
    from cookbook.auth import sessions
    async def refuse(*args, **kwargs):
        raise sessions.SessionIssueRefused()
    monkeypatch.setattr(sessions, "create_session", refuse)
    async def body(app, client):
        response = await submit(client, token(await client.get("/setup")))
        assert response.status_code == 403
        assert cookies(response)["cookbook_csrf_pre"]["max-age"] == "0"
        assert "cookbook_session" not in cookies(response)
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 1
            assert (await db.execute(select(func.count()).select_from(Session))).scalar_one() == 0
        assert (await client.get("/login")).status_code == 200
    web_probe(monkeypatch, body)


@pytest.mark.parametrize("case", ["cross-browser", "missing-cookie", "missing-token", "tampered",
                                   "expired", "boundary", "login-purpose", "header-only"])
def test_setup_rejects_csrf_before_bootstrap_mutation(monkeypatch, case):
    async def body(app, client):
        from cookbook.auth import bootstrap, csrf, routes
        async def forbidden(*args, **kwargs):
            pytest.fail("rejected CSRF reached bootstrap mutation")
        monkeypatch.setattr(bootstrap, "create_first_admin", forbidden)
        page = await client.get("/setup")
        value = token(page)
        secret = client.cookies.get("cookbook_csrf_pre")
        data = {"username": "owner", "password": PASSWORD, "csrf_token": value}
        headers = {}
        if case == "cross-browser":
            await client.get("/setup")
        elif case == "missing-cookie":
            client.cookies.clear()
        elif case == "missing-token":
            data.pop("csrf_token")
        elif case == "header-only":
            data.pop("csrf_token")
            headers["X-CSRF-Token"] = value
        elif case == "tampered":
            data["csrf_token"] = "tampered"
        elif case == "login-purpose":
            data["csrf_token"] = csrf.issue_preauth(secret, "login", now=routes.utc_now(), key=app.state.settings.secret_key)
        else:
            now = datetime(2026, 10, 23, 12)
            monkeypatch.setattr(routes, "utc_now", lambda: now)
            issued = now - timedelta(minutes=15, seconds=1 if case == "expired" else 0)
            data["csrf_token"] = csrf.issue_preauth(secret, "setup", now=issued, key=app.state.settings.secret_key)
        response = await client.post("/setup", data=data, headers=headers)
        assert response.status_code == 403
        assert response.headers["cache-control"] == "private, no-store"
        assert not response.headers.get_list("set-cookie")
        async with session_scope() as db:
            for model in (User, Session, LoginAttempt):
                assert (await db.execute(select(func.count()).select_from(model))).scalar_one() == 0
    web_probe(monkeypatch, body)


@pytest.mark.parametrize("extra", [{"password": "short"}, {"username": ""}, {"username": "x" * 61}])
def test_setup_bad_input_preserves_form_binding_without_creating_owner(monkeypatch, extra):
    async def body(app, client):
        page = await client.get("/setup")
        response = await submit(client, token(page), **extra)
        assert response.status_code == 400
        assert PASSWORD not in response.text
        assert not response.headers.get_list("set-cookie")
        from cookbook.auth import bootstrap
        assert await bootstrap.is_bootstrapped() is False
    web_probe(monkeypatch, body)


def test_setup_purpose_cannot_authorize_login_after_cli_bootstrap(monkeypatch):
    async def body(app, client):
        from cookbook.auth import bootstrap
        page = await client.get("/setup")
        await bootstrap.create_first_admin("owner", PASSWORD)
        response = await client.post("/login", data={"username": "owner", "password": PASSWORD,
                                                     "csrf_token": token(page)})
        assert response.status_code == 403
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(Session))).scalar_one() == 0
    web_probe(monkeypatch, body)


def test_parallel_web_setup_has_one_owner_and_one_session(monkeypatch):
    async def body(app, client):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as other:
            a, b = await client.get("/setup"), await other.get("/setup")
            results = await asyncio.gather(submit(client, token(a)), submit(other, token(b)))
            assert sorted(r.status_code for r in results) == [302, 403]
            async with session_scope() as db:
                assert (await db.execute(select(func.count()).select_from(User))).scalar_one() == 1
                assert (await db.execute(select(func.count()).select_from(Session))).scalar_one() == 1
    web_probe(monkeypatch, body)


def test_setup_missing_lock_row_is_loud_without_owner(monkeypatch):
    async def body(app, client):
        from cookbook.auth import bootstrap
        page = await client.get("/setup")
        async with session_scope() as db:
            await db.execute(delete(AuthLock).where(AuthLock.name == "first_admin"))
        with pytest.raises(bootstrap.LockRowMissing):
            await submit(client, token(page))
        assert await bootstrap.is_bootstrapped() is False
    web_probe(monkeypatch, body)
