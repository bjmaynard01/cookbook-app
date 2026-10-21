"""Pure request/cookie adapter tests; no database or running server."""
from __future__ import annotations

import asyncio
import importlib
import secrets
from datetime import datetime
from http.cookies import SimpleCookie

import pytest
from starlette.requests import Request
from starlette.responses import Response

from cookbook.config import Settings
from cookbook.models import Session, User

HOST = "__Host-cookbook_session"
DEV = "cookbook_session"
TOKEN = secrets.token_urlsafe(32)
NOW = datetime(2026, 10, 21, 12)


def service():
    return importlib.import_module("cookbook.auth.sessions")


def settings(secure=None, cidrs=""):
    return Settings(_env_file=None, DEBUG=False, COOKIE_SECURE=secure,
                    TRUSTED_PROXY_CIDRS=cidrs, SESSION_TTL_HOURS=2)


def request(*, secure=False, cookies="", peer="198.51.100.1", proto=None):
    headers = [(b"cookie", cookies.encode())]
    if proto:
        headers.append((b"x-forwarded-proto", proto.encode()))
    return Request({"type": "http", "scheme": "https" if secure else "http",
                    "method": "GET", "path": "/", "headers": headers,
                    "client": (peer, 1234)})


def parsed(response):
    result = SimpleCookie()
    for header in response.headers.getlist("set-cookie"):
        result.load(header)
    return result


@pytest.mark.parametrize("secure,name,other", [(True, HOST, DEV), (False, DEV, HOST)])
def test_cookie_emission_and_alternate_deletion_preserve_other_headers(secure, name, other):
    s = service()
    response = Response()
    response.set_cookie("unrelated", "retained")
    s.set_session_cookie(response, TOKEN, secure=secure, ttl_hours=2)
    cookies = parsed(response)
    active = cookies[name]
    assert active.value == TOKEN
    assert active["max-age"] == "7200"
    assert active["path"] == "/" and not active["domain"]
    assert active["httponly"] and active["samesite"].lower() == "lax"
    assert bool(active["secure"]) is secure
    assert cookies[other]["max-age"] == "0"
    assert bool(cookies[other]["secure"]) is (other == HOST)
    assert cookies["unrelated"].value == "retained"
    assert len(response.headers.getlist("set-cookie")) == 3
    assert response.headers["cache-control"] == "private, no-store"
    one = SimpleCookie()
    one.load(s.cookie_set(TOKEN, secure=secure, ttl_hours=2))
    assert list(one) == [name] and one[name].value == TOKEN


def test_clear_both_cookie_names():
    response = Response()
    service().clear_session_cookies(response)
    cookies = parsed(response)
    assert set(cookies) == {HOST, DEV}
    for name, cookie in cookies.items():
        assert cookie["max-age"] == "0" and cookie["path"] == "/"
        assert not cookie["domain"]
        assert bool(cookie["secure"]) is (name == HOST)
    assert response.headers["cache-control"] == "private, no-store"


@pytest.mark.parametrize("bad", ["bad\r\nInjected: true", "", "!" * 43])
def test_cookie_refuses_malformed_tokens_without_partial_response(bad):
    response = Response()
    with pytest.raises(ValueError):
        service().set_session_cookie(response, bad, secure=True)
    assert not response.headers.getlist("set-cookie")


@pytest.mark.parametrize("ttl", [0, -1])
def test_cookie_refuses_nonpositive_ttl(ttl):
    with pytest.raises(ValueError):
        service().cookie_set(TOKEN, secure=True, ttl_hours=ttl)


@pytest.mark.parametrize("secure", [True, False])
def test_request_load_selects_mode_cookie_caches_user_and_context(monkeypatch, secure):
    s = service()
    user = User(id="user-id", username="editor", password_hash="private")
    row = Session(id_hash="a" * 64, user_id=user.id, csrf_token="b" * 32)
    loaded = s.LoadedSession(user=user, session=row)
    calls = []
    async def fake_load(raw, **kwargs):
        calls.append(raw)
        return loaded
    monkeypatch.setattr(s, "load_token", fake_load)
    chosen = HOST if secure else DEV
    alternate = DEV if secure else HOST
    req = request(secure=secure, cookies=f"{chosen}={TOKEN}; {alternate}=stale")
    async def run():
        for _ in range(2):
            assert await s.load_session(req, settings=settings(), now=NOW) is user
        assert req.state.auth_session is loaded
        assert req.state.auth_session.session.csrf_token == "b" * 32
        await s.load_session(request(secure=secure, cookies=f"{chosen}={TOKEN}"),
                             settings=settings(), now=NOW)
    asyncio.run(run())
    assert calls == [TOKEN, TOKEN]
    response = Response()
    s.renew_session_cookie(response, req, settings=settings())
    assert parsed(response)[chosen].value == TOKEN
    assert parsed(response)[chosen]["max-age"] == "7200"
    assert len(calls) == 2  # renewal uses loaded request state, no DB lookup


@pytest.mark.parametrize("secure", [True, False])
def test_stale_alternate_cookie_never_authenticates(monkeypatch, secure):
    s = service()
    async def none_load(raw, **kwargs):
        assert raw is None or raw == ""
        return None
    monkeypatch.setattr(s, "load_token", none_load)
    alternate = DEV if secure else HOST
    req = request(secure=secure, cookies=f"{alternate}={TOKEN}")
    async def run():
        assert await s.load_session(req, settings=settings(), now=NOW) is None
        assert req.state.auth_session is None
    asyncio.run(run())
    response = Response()
    s.renew_session_cookie(response, req, settings=settings())
    assert not response.headers.getlist("set-cookie")


@pytest.mark.parametrize("trusted,override,expected", [(True, None, HOST),
    (False, None, DEV), (True, False, DEV), (False, True, HOST)])
def test_request_cookie_selection_uses_identity_and_explicit_override(
    monkeypatch, trusted, override, expected
):
    s = service()
    async def fake_load(raw, **kwargs):
        assert raw == TOKEN
        return None
    monkeypatch.setattr(s, "load_token", fake_load)
    req = request(peer="192.0.2.1" if trusted else "198.51.100.1", proto="https",
                  cookies=f"{expected}={TOKEN}")
    asyncio.run(s.load_session(req, settings=settings(override, "192.0.2.0/24"), now=NOW))


def test_database_errors_are_not_downgraded_to_anonymous(monkeypatch):
    s = service()
    async def failed(*args, **kwargs):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(s, "load_token", failed)
    with pytest.raises(RuntimeError, match="database unavailable"):
        asyncio.run(s.load_session(request(cookies=f"{DEV}={TOKEN}"),
                                    settings=settings(), now=NOW))


def test_no_token_in_logs_location_or_anonymous_renewal(caplog):
    response = Response(status_code=302, headers={"location": "/"})
    service().set_session_cookie(response, TOKEN, secure=True)
    assert TOKEN not in response.headers["location"]
    assert TOKEN not in caplog.text
    anon = Response()
    service().renew_session_cookie(anon, request(), settings=settings())
    assert not anon.headers.getlist("set-cookie")
