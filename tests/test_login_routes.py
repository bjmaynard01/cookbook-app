"""Authentication route acceptance against disposable MariaDB."""
import asyncio
import hashlib
import logging
import html
import json
import re
import uuid
from pathlib import Path
from datetime import datetime, timedelta
from http.cookies import SimpleCookie

import httpx
import pytest
from sqlalchemy import delete, func, select
from starlette.templating import Jinja2Templates

from cookbook.app import create_app
from cookbook.config import get_settings
from cookbook.db import get_engine, session_scope
from cookbook.models import LoginAttempt, Session, User
from cookbook.auth.passwords import hash_password

PASSWORD = "test-password"


def token(response):
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match, response.text
    return match.group(1)


def cookies(response):
    result = SimpleCookie()
    for value in response.headers.get_list("set-cookie"):
        result.load(value)
    return result


def probe(monkeypatch, body):
    monkeypatch.setenv("DEBUG", "false")
    get_settings.cache_clear()
    async def run():
        uid = str(uuid.uuid4())
        username = "login-" + uid
        try:
            async with session_scope() as db:
                db.add(User(id=uid, username=username, password_hash=hash_password(PASSWORD),
                            role="editor", is_active=True))
            app = create_app()
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                await body(app, client, username, uid)
        finally:
            try:
                async with session_scope() as db:
                    await db.execute(delete(User).where(User.id == uid))
                    await db.execute(delete(LoginAttempt).where(
                        LoginAttempt.username.in_([username, "unknown-" + uid])))
            finally:
                await get_engine().dispose()
                get_settings.cache_clear()
    asyncio.run(run())


def test_login_success_logout_form_and_header(monkeypatch):
    async def body(app, client, username, uid):
        page = await client.get("/login")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "private, no-store"
        pre = cookies(page)["cookbook_csrf_pre"]
        assert pre["httponly"] and pre["samesite"].lower() == "strict"
        assert pre["max-age"] == "900" and not pre["secure"]
        assert 'autocomplete="current-password"' in page.text
        response = await client.post("/login", data={"username": username,
                                    "password": PASSWORD, "csrf_token": token(page)})
        assert response.status_code == 302 and response.headers["location"] == "/"
        issued = cookies(response)
        assert issued["cookbook_csrf_pre"]["max-age"] == "0"
        raw = issued["cookbook_session"].value
        async with session_scope() as db:
            row = await db.get(Session, hashlib.sha256(raw.encode()).hexdigest())
            assert row and row.user_id == uid
            csrf_token = row.csrf_token
        denied = await client.post("/logout")
        assert denied.status_code == 403
        denied = await client.post("/logout", headers={"X-CSRF-Token": "wrong"},
                                   data={"csrf_token": csrf_token})
        assert denied.status_code == 403
        response = await client.post("/logout", data={"csrf_token": csrf_token})
        assert response.status_code == 302
        cleared = cookies(response)
        for name in ("cookbook_session", "__Host-cookbook_session", "cookbook_csrf_pre"):
            assert cleared[name]["max-age"] == "0"
        async with session_scope() as db:
            assert await db.get(Session, hashlib.sha256(raw.encode()).hexdigest()) is None
        page = await client.get("/login")
        await client.post("/login", data={"username": username, "password": PASSWORD,
                                         "csrf_token": token(page)})
        async with session_scope() as db:
            csrf_token = (await db.execute(select(Session.csrf_token).where(Session.user_id == uid))).scalar_one()
        response = await client.post("/logout", headers={"X-CSRF-Token": csrf_token})
        assert response.status_code == 302
    probe(monkeypatch, body)


@pytest.mark.parametrize("case", ["cross-browser", "missing-cookie", "missing-token",
                                   "tampered", "expired", "boundary", "purpose", "header-only",
                                   "malformed-form"])
def test_preauth_rejection_before_password_work(monkeypatch, case):
    async def body(app, client, username, uid):
        from cookbook.auth import routes, csrf
        async with session_scope() as db:
            before = {model: (await db.execute(select(func.count()).select_from(model))).scalar_one()
                      for model in (Session, LoginAttempt, User)}
        def forbidden(*args, **kwargs):
            pytest.fail("password work ran before CSRF rejection")
        monkeypatch.setattr(routes.passwords, "verify_password", forbidden)
        monkeypatch.setattr(routes.passwords, "dummy_verify", forbidden)
        page = await client.get("/login")
        value = token(page)
        secret = client.cookies.get("cookbook_csrf_pre")
        data = {"username": username, "password": PASSWORD, "csrf_token": value}
        headers = {}
        if case == "cross-browser":
            await client.get("/login")  # rotates binding, old form remains signed
        elif case == "missing-cookie":
            client.cookies.clear()
        elif case == "missing-token":
            data.pop("csrf_token")
        elif case == "header-only":
            data.pop("csrf_token")
            headers["X-CSRF-Token"] = value
        elif case == "malformed-form":
            headers["Content-Type"] = "multipart/form-data"  # no boundary
        elif case == "tampered":
            data["csrf_token"] = "invalid"
        elif case in {"expired", "boundary"}:
            now = datetime(2026, 10, 22, 12)
            monkeypatch.setattr(routes, "utc_now", lambda: now)
            issued = now - timedelta(minutes=15, seconds=1 if case == "expired" else 0)
            data["csrf_token"] = csrf.issue_preauth(secret, "login", now=issued,
                                                   key=app.state.settings.secret_key)
        elif case == "purpose":
            data["csrf_token"] = csrf.issue_preauth(secret, "setup", now=routes.utc_now(),
                                                   key=app.state.settings.secret_key)
        response = await client.post("/login", data=data, headers=headers)
        assert response.status_code == 403
        assert response.headers["cache-control"] == "private, no-store"
        assert not response.headers.get_list("set-cookie")
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(Session).where(Session.user_id == uid))).scalar_one() == 0
            after = {model: (await db.execute(select(func.count()).select_from(model))).scalar_one()
                     for model in (Session, LoginAttempt, User)}
            assert after == before
            assert (await db.get(User, uid)).username == username
    probe(monkeypatch, body)


def test_wrong_unknown_inactive_login_share_failure_and_allow_retry(monkeypatch):
    async def body(app, client, username, uid):
        from cookbook.auth import routes
        original = routes.passwords.dummy_verify
        original_verify = routes.passwords.verify_password
        seen = []
        verified = []
        def dummy(password):
            seen.append(True)
            return original(password)
        monkeypatch.setattr(routes.passwords, "dummy_verify", dummy)
        def verify(password, stored_hash):
            verified.append(True)
            return original_verify(password, stored_hash)
        monkeypatch.setattr(routes.passwords, "verify_password", verify)
        page = await client.get("/login")
        value = token(page)
        responses = []
        for name in (username, "unknown-" + uid):
            response = await client.post("/login", data={"username": name,
                "password": "incorrect", "csrf_token": value})
            assert response.status_code == 401
            assert response.headers["cache-control"] == "private, no-store"
            assert PASSWORD not in response.text and "incorrect" not in response.text
            assert not response.headers.get_list("set-cookie")
            responses.append(response)
        assert seen == [True]
        assert responses[0].text == responses[1].text
        async with session_scope() as db:
            user = await db.get(User, uid)
            user.is_active = False
        response = await client.post("/login", data={"username": username,
            "password": PASSWORD, "csrf_token": value})
        assert response.status_code == 401
        assert verified == [True, True]
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(Session).where(Session.user_id == uid))).scalar_one() == 0
    probe(monkeypatch, body)


def test_secure_cookie_mode_and_no_secrets_in_logs(monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    async def body(app, client, username, uid):
        app.state.settings.cookie_secure = True
        # HTTPS client allows the Secure pre-auth cookie to be sent.
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://test") as secure:
            page = await secure.get("/login")
            secret = cookies(page)["cookbook_csrf_pre"].value
            assert cookies(page)["cookbook_csrf_pre"]["secure"]
            value = token(page)
            response = await secure.post("/login", data={"username": username,
                "password": PASSWORD, "csrf_token": value})
            assert response.status_code == 302
            issued = cookies(response)
            assert issued["__Host-cookbook_session"]["secure"]
            assert issued["cookbook_session"]["max-age"] == "0"
            for sensitive in (secret, value, PASSWORD, issued["__Host-cookbook_session"].value):
                assert sensitive not in caplog.text
                assert sensitive not in response.headers["location"]
    probe(monkeypatch, body)


def test_password_operational_errors_propagate(monkeypatch):
    async def body(app, client, username, uid):
        from cookbook.auth import routes
        def broken(*args):
            raise RuntimeError("test password service failure")
        monkeypatch.setattr(routes.passwords, "verify_password", broken)
        page = await client.get("/login")
        with pytest.raises(RuntimeError, match="test password service failure"):
            await client.post("/login", data={"username": username, "password": PASSWORD,
                                             "csrf_token": token(page)})
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(LoginAttempt).where(
                LoginAttempt.username == username))).scalar_one() == 0
    probe(monkeypatch, body)


def test_trusted_proxy_identity_controls_cookie_and_created_ip(monkeypatch):
    async def body(app, client, username, uid):
        from cookbook.config import Settings
        app.state.settings = Settings(_env_file=None, DEBUG=False, SECRET_KEY="proxy-test-key",
                                      TRUSTED_PROXY_CIDRS="192.0.2.0/24")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.0.2.1", 1234)),
                                    base_url="https://test") as proxy:
            headers = {"X-Forwarded-Proto": "https", "X-Forwarded-For": "198.51.100.42"}
            page = await proxy.get("/login", headers=headers)
            assert cookies(page)["cookbook_csrf_pre"]["secure"]
            response = await proxy.post("/login", headers=headers, data={"username": username,
                 "password": PASSWORD, "csrf_token": token(page)})
            assert response.status_code == 302
            raw = cookies(response)["__Host-cookbook_session"].value
            async with session_scope() as db:
                row = await db.get(Session, hashlib.sha256(raw.encode()).hexdigest())
                assert row.created_ip == "198.51.100.42"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("198.51.100.1", 1234)),
                                    base_url="http://test") as direct:
            page = await direct.get("/login", headers=headers)
            assert not cookies(page)["cookbook_csrf_pre"]["secure"]
            response = await direct.post("/login", headers=headers, data={"username": username,
                "password": PASSWORD, "csrf_token": token(page)})
            raw = cookies(response)["cookbook_session"].value
            async with session_scope() as db:
                row = await db.get(Session, hashlib.sha256(raw.encode()).hexdigest())
                assert row.created_ip == "198.51.100.1"
    probe(monkeypatch, body)


def test_login_page_rotation_and_safe_header_delivery(monkeypatch):
    monkeypatch.setenv("DEBUG", "false")
    monkeypatch.setenv("SECRET_KEY", "test-only-key")
    monkeypatch.delenv("SECRET_KEY_FILE", raising=False)
    get_settings.cache_clear()
    async def run():
        app = create_app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = await client.get("/login")
            second = await client.get("/login")
            assert first.status_code == second.status_code == 200
            assert cookies(first)["cookbook_csrf_pre"].value != cookies(second)["cookbook_csrf_pre"].value
            for page in (first, second):
                assert page.headers["cache-control"] == "private, no-store"
                secret = cookies(page)["cookbook_csrf_pre"].value
                assert secret not in page.text
                assert "htmx.config.headers" not in page.text
                headers = re.search(r"hx-headers='([^']+)'", page.text)
                assert headers
                assert json.loads(html.unescape(headers.group(1)))["X-CSRF-Token"] == token(page)
    try:
        asyncio.run(run())
    finally:
        get_settings.cache_clear()


def test_templates_escape_errors_and_deliver_session_csrf():
    templates = Jinja2Templates(directory=Path(__file__).resolve().parents[1] / "src/cookbook/templates")
    page = templates.env.get_template("login.html").render(
        csrf_token="preauth-token", session_csrf="a" * 32,
        error='<script>alert("unsafe")</script>')
    assert '<script>alert("unsafe")</script>' not in page
    assert "&lt;script&gt;" in page
    assert 'action="/logout"' in page
    assert 'name="csrf_token" value="' + "a" * 32 + '"' in page
    headers = [json.loads(html.unescape(value)) for value in re.findall(r"hx-headers='([^']+)'", page)]
    assert {item["X-CSRF-Token"] for item in headers} == {"preauth-token", "a" * 32}


@pytest.mark.parametrize("override,base_url,expected_name", [(True, "http://test", "__Host-cookbook_session"),
                                                           (False, "https://test", "cookbook_session")])
def test_session_cookie_explicit_override_is_honored(monkeypatch, override, base_url, expected_name):
    async def body(app, client, username, uid):
        app.state.settings.cookie_secure = override
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base_url) as browser:
            page = await browser.get("/login")
            response = await browser.post("/login", data={"username": username,
                "password": PASSWORD, "csrf_token": token(page)})
            assert response.status_code == 302
            active = cookies(response)[expected_name]
            assert active["max-age"] != "0"
            assert bool(active["secure"]) is override
    probe(monkeypatch, body)


@pytest.mark.parametrize("change", ["disabled", "password-reset"])
def test_account_change_between_verification_and_session_issue_refuses_login(monkeypatch, change):
    async def body(app, client, username, uid):
        from cookbook.auth import routes
        original_create = routes.sessions.create_session
        async def changed_account(user_id, **kwargs):
            async with session_scope() as db:
                user = await db.get(User, user_id)
                if change == "disabled":
                    user.is_active = False
                else:
                    user.password_hash = hash_password("replacement-password")
            return await original_create(user_id, **kwargs)
        monkeypatch.setattr(routes.sessions, "create_session", changed_account)
        page = await client.get("/login")
        response = await client.post("/login", data={"username": username,
            "password": PASSWORD, "csrf_token": token(page)})
        assert response.status_code == 401
        assert not response.headers.get_list("set-cookie")
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(Session).where(Session.user_id == uid))).scalar_one() == 0
            attempt = (await db.execute(select(LoginAttempt).where(
                LoginAttempt.username == username, LoginAttempt.ip == "127.0.0.1"))).scalar_one()
            assert attempt.attempts == 1 and attempt.locked_until is None
    probe(monkeypatch, body)
