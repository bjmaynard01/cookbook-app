"""Lockout behavior through the real login route and disposable database."""
import asyncio
import hashlib
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import delete, func, select

from cookbook.auth import passwords
from cookbook.config import Settings
from cookbook.db import session_scope
from cookbook.models import LoginAttempt, Session, User
from test_login_routes import PASSWORD, cookies, probe as login_probe, token
from test_lockout import NOW, row, service

IP = "127.0.0.1"


def probe(monkeypatch, body):
    async def wrapped(app, client, username, uid):
        try:
            await body(app, client, username, uid)
        finally:
            async with session_scope() as db:
                await db.execute(delete(LoginAttempt).where(
                    LoginAttempt.username.in_([username, username + "-unknown"])))
    login_probe(monkeypatch, wrapped)


async def submit(client, username, csrf_token, password="incorrect", **kwargs):
    return await client.post("/login", data={"username": username, "password": password,
                                            "csrf_token": csrf_token}, **kwargs)


def test_sixth_attempt_is_429_before_password_work(monkeypatch):
    async def body(app, client, username, uid):
        from cookbook.auth import routes
        monkeypatch.setattr(routes, "utc_now", lambda: NOW)
        page = await client.get("/login")
        value = token(page)
        secret = client.cookies.get("cookbook_csrf_pre")
        for _ in range(5):
            response = await submit(client, username, value)
            assert response.status_code == 401
        def forbidden(*args):
            pytest.fail("locked request ran password work")
        monkeypatch.setattr(passwords, "verify_password", forbidden)
        monkeypatch.setattr(passwords, "dummy_verify", forbidden)
        response = await submit(client, username, value, PASSWORD)
        assert response.status_code == 429 and response.headers["retry-after"] == "600"
        assert response.headers["cache-control"] == "private, no-store"
        assert not response.headers.get_list("set-cookie")
        assert username not in response.text and PASSWORD not in response.text
        assert client.cookies.get("cookbook_csrf_pre") == secret
        actual = await row(username, IP)
        assert actual.attempts == 5 and actual.locked_until == NOW + timedelta(minutes=10)
    probe(monkeypatch, body)


def test_expired_lock_allows_success_and_later_stale_failure_is_fresh(monkeypatch):
    async def body(app, client, username, uid):
        from cookbook.auth import routes
        clock = [NOW]
        monkeypatch.setattr(routes, "utc_now", lambda: clock[0])
        page = await client.get("/login")
        for _ in range(5):
            assert (await submit(client, username, token(page))).status_code == 401
        assert (await row(username, IP)).locked_until == NOW + timedelta(minutes=10)
        clock[0] = NOW + timedelta(minutes=10)
        response = await submit(client, username, token(page), PASSWORD)
        assert response.status_code == 302
        assert await row(username, IP) is None
        raw = cookies(response)["cookbook_session"].value
        state = await service().record_failure(username, IP, now=clock[0])
        assert state.attempts == 1 and state.lock_count == 0 and state.locked_until is None
        async with session_scope() as db:
            assert await db.get(Session, hashlib.sha256(raw.encode()).hexdigest()) is not None
    probe(monkeypatch, body)


def test_success_reset_wins_when_failures_lock_pair_after_password_check(monkeypatch):
    async def body(app, client, username, uid):
        from cookbook.auth import routes
        monkeypatch.setattr(routes, "utc_now", lambda: NOW)
        original_create = routes.sessions.create_session
        async def failures_before_commit(user_id, **kwargs):
            for _ in range(5):
                await service().record_failure(username, IP, now=NOW)
            assert (await row(username, IP)).locked_until == NOW + timedelta(minutes=10)
            return await original_create(user_id, **kwargs)
        monkeypatch.setattr(routes.sessions, "create_session", failures_before_commit)
        page = await client.get("/login")
        response = await submit(client, username, token(page), PASSWORD)
        assert response.status_code == 302
        assert await row(username, IP) is None
        raw = cookies(response)["cookbook_session"].value
        async with session_scope() as db:
            assert await db.get(Session, hashlib.sha256(raw.encode()).hexdigest()) is not None
    probe(monkeypatch, body)


def test_canonical_username_used_for_both_lookup_and_counter_reset(monkeypatch):
    async def body(app, client, username, uid):
        page = await client.get("/login")
        value = token(page)
        assert (await submit(client, " " + username.upper() + " ", value)).status_code == 401
        assert (await submit(client, username, value)).status_code == 401
        assert (await row(username, IP)).attempts == 2
        assert (await submit(client, " " + username.upper() + " ", value, PASSWORD)).status_code == 302
        assert await row(username, IP) is None
    probe(monkeypatch, body)


def test_unknown_and_inactive_credentials_record_failures(monkeypatch):
    async def body(app, client, username, uid):
        async with session_scope() as db:
            user = await db.get(User, uid)
            user.is_active = False
        page = await client.get("/login")
        responses = []
        for name in (username, username + "-unknown"):
            response = await submit(client, name, token(page), PASSWORD)
            assert response.status_code == 401
            responses.append(response.text)
            assert (await row(name, IP)).attempts == 1
        assert responses[0] == responses[1]
    probe(monkeypatch, body)


def test_forged_headers_cannot_evade_direct_peer_lockout(monkeypatch):
    async def body(app, client, username, uid):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("198.51.100.1", 1234)),
                                    base_url="http://test") as browser:
            page = await browser.get("/login")
            for i in range(5):
                response = await submit(browser, username, token(page),
                    headers={"X-Forwarded-For": f"203.0.113.{i + 1}"})
                assert response.status_code == 401
            response = await submit(browser, username, token(page), PASSWORD,
                                     headers={"X-Forwarded-For": "203.0.113.99"})
            assert response.status_code == 429
            assert (await row(username, "198.51.100.1")).attempts == 5
            assert await row(username, "203.0.113.99") is None
    probe(monkeypatch, body)


def test_trusted_proxy_uses_real_client_for_lockout(monkeypatch):
    async def body(app, client, username, uid):
        app.state.settings = Settings(_env_file=None, DEBUG=False, SECRET_KEY="proxy-test-key",
                                      TRUSTED_PROXY_CIDRS="192.0.2.0/24")
        headers = {"X-Forwarded-For": "203.0.113.42", "X-Forwarded-Proto": "https"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("192.0.2.1", 1234)),
                                    base_url="https://test") as browser:
            page = await browser.get("/login", headers=headers)
            for _ in range(5):
                assert (await submit(browser, username, token(page), headers=headers)).status_code == 401
            assert (await submit(browser, username, token(page), PASSWORD, headers=headers)).status_code == 429
            assert (await row(username, "203.0.113.42")).attempts == 5
            assert await row(username, "192.0.2.1") is None
    probe(monkeypatch, body)


def test_invalid_csrf_precedes_lock_lookup_and_never_changes_history(monkeypatch):
    async def body(app, client, username, uid):
        from cookbook.auth import routes
        s = service()
        for _ in range(5):
            await s.record_failure(username, IP, now=NOW)
        async def forbidden(*args, **kwargs):
            pytest.fail("invalid CSRF touched lock service")
        monkeypatch.setattr(s, "assert_not_locked", forbidden)
        page = await client.get("/login")
        response = await submit(client, username, "tampered")
        assert response.status_code == 403
        assert not response.headers.get_list("set-cookie")
        assert (await row(username, IP)).attempts == 5
    probe(monkeypatch, body)


@pytest.mark.parametrize("bad_name", ["", " " * 3, "x" * 61, "İ" * 60])
def test_unrepresentable_username_is_generic_failure_without_db_write(monkeypatch, bad_name):
    async def body(app, client, username, uid):
        async with session_scope() as db:
            before = (await db.execute(select(func.count()).select_from(LoginAttempt))).scalar_one()
        page = await client.get("/login")
        response = await submit(client, bad_name, token(page), PASSWORD)
        assert response.status_code == 401
        async with session_scope() as db:
            assert (await db.execute(select(func.count()).select_from(LoginAttempt))).scalar_one() == before
    probe(monkeypatch, body)


@pytest.mark.parametrize("peer,canonical_ip", [(None, ""),
    (("2001:db8::1%" + "interface-" * 6, 1234), "2001:db8::1")])
def test_session_and_lockout_share_canonical_ip_including_unknown_peer(monkeypatch, peer, canonical_ip):
    async def body(app, client, username, uid):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=peer),
                                    base_url="http://test") as browser:
            page = await browser.get("/login")
            assert (await submit(browser, username, token(page))).status_code == 401
            assert (await row(username, canonical_ip)).attempts == 1
            response = await submit(browser, username, token(page), PASSWORD)
            assert response.status_code == 302
            assert await row(username, canonical_ip) is None
            raw = cookies(response)["cookbook_session"].value
            async with session_scope() as db:
                issued = await db.get(Session, hashlib.sha256(raw.encode()).hexdigest())
                assert issued.created_ip == (canonical_ip or None)
    probe(monkeypatch, body)
