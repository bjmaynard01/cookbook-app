"""CSRF contracts independent of database and application settings."""
import asyncio
import importlib
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
from types import SimpleNamespace

import pytest
from starlette.requests import Request

NOW = datetime(2026, 10, 22, 12)
SECRET = "A" * 22
KEY = "test-only-signing-key"


def service():
    return importlib.import_module("cookbook.auth.csrf")


def issue(purpose="login", **kwargs):
    return service().issue_preauth(SECRET, purpose, now=NOW, key=KEY, **kwargs)


def verify(token, secret=SECRET, purpose="login", now=NOW, key=KEY):
    return service().verify_preauth(token, secret, purpose, now=now, key=key)


def test_valid_same_browser_replay_and_purpose_separation():
    token = issue()
    assert verify(token) is True
    assert verify(token) is True
    assert verify(token, purpose="setup") is False
    assert verify(issue("setup"), purpose="setup") is True
    assert verify(issue("setup")) is False


@pytest.mark.parametrize("seconds,expected", [(899, True), (900, False), (901, False)])
def test_expiry_equality_boundary(seconds, expected):
    assert verify(issue(), now=NOW + timedelta(seconds=seconds)) is expected


def test_browser_key_and_signature_binding():
    token = issue()
    assert verify(token, secret="B" * 22) is False
    assert verify(token, key="other-key") is False
    assert verify(token[:-1] + ("A" if token[-1] != "A" else "B")) is False


@pytest.mark.parametrize("bad", [None, "", "x", "." * 5, "☃", "x" * 10000],
                         ids=["none", "empty", "short", "segments", "unicode", "oversized"])
def test_malformed_token_rejected_without_exception(bad):
    assert verify(bad) is False


@pytest.mark.parametrize("secret", [None, "", "B" * 22, "☃", "\ud800", "B" * 10000],
                         ids=["none", "empty", "wrong", "unicode", "surrogate", "oversized"])
def test_missing_or_wrong_cookie_rejected(secret):
    assert verify(issue(), secret=secret) is False


def test_invalid_issuance_rejected():
    with pytest.raises(ValueError):
        issue("unrecognized")
    with pytest.raises(ValueError):
        issue(ttl=timedelta(seconds=0))
    with pytest.raises(ValueError):
        service().issue_preauth("", "login", now=NOW, key=KEY)
    with pytest.raises(ValueError):
        service().issue_preauth(SECRET, "login", now=NOW, key="")


@pytest.mark.parametrize("secure", [True, False])
def test_browser_cookie_contract(secure):
    cookies = SimpleCookie()
    cookies.load(service().browser_secret_set_cookie(SECRET, secure=secure))
    assert list(cookies) == ["cookbook_csrf_pre"]
    cookie = cookies["cookbook_csrf_pre"]
    assert cookie.value == SECRET
    assert cookie["path"] == "/" and not cookie["domain"]
    assert cookie["max-age"] == "900"
    assert cookie["httponly"] and cookie["samesite"].lower() == "strict"
    assert bool(cookie["secure"]) is secure
    cookies.load(service().browser_secret_clear(secure=secure))
    assert cookies["cookbook_csrf_pre"]["max-age"] == "0"


def test_session_tokens_are_random_hex():
    tokens = {service().issue_session_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(len(t) == 32 and int(t, 16) >= 0 for t in tokens)


def request(*, header=None, form=None):
    body = ("csrf_token=" + form).encode() if form is not None else b""
    headers = [(b"content-type", b"application/x-www-form-urlencoded")]
    if header is not None:
        headers.append((b"x-csrf-token", header.encode()))
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    req = Request({"type": "http", "method": "POST", "path": "/",
                   "headers": headers}, receive)
    req.state.auth_session = SimpleNamespace(session=SimpleNamespace(csrf_token="a" * 32))
    return req


@pytest.mark.parametrize("header,form,expected", [
    ("a" * 32, None, True), (None, "a" * 32, True),
    (None, None, False), ("bad", "a" * 32, False),
    ("a" * 32, "bad", True), ("", "a" * 32, False),
])
def test_session_header_precedence_and_form_fallback(header, form, expected):
    assert asyncio.run(service().validate_csrf(request(header=header, form=form))) is expected


def test_session_missing_context_fails_closed():
    req = request(header="a" * 32)
    req.state.auth_session = None
    assert asyncio.run(service().validate_csrf(req)) is False


def test_non_ascii_session_token_fails_closed():
    assert asyncio.run(service().validate_csrf(request(form="☃"))) is False


def test_aware_utc_and_explicit_ttl():
    aware = NOW.replace(tzinfo=timezone.utc)
    value = service().issue_preauth(SECRET, "login", now=aware, key=KEY,
                                    ttl=timedelta(seconds=60))
    assert verify(value, now=NOW + timedelta(seconds=59)) is True
    assert verify(value, now=aware + timedelta(seconds=60)) is False


def test_json_header_validation_never_reads_body():
    async def receive():
        pytest.fail("header CSRF validation read the body")
    req = Request({"type": "http", "method": "POST", "path": "/",
                   "headers": [(b"content-type", b"application/json"),
                               (b"x-csrf-token", b"a" * 32)]}, receive)
    req.state.auth_session = SimpleNamespace(session=SimpleNamespace(csrf_token="a" * 32))
    assert asyncio.run(service().validate_csrf(req)) is True


def test_multipart_form_session_validation():
    body = ("--boundary\r\nContent-Disposition: form-data; name=\"csrf_token\"\r\n\r\n"
            + "a" * 32 + "\r\n--boundary--\r\n").encode()
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    req = Request({"type": "http", "method": "POST", "path": "/",
                   "headers": [(b"content-type", b"multipart/form-data; boundary=boundary")]}, receive)
    req.state.auth_session = SimpleNamespace(session=SimpleNamespace(csrf_token="a" * 32))
    assert asyncio.run(service().validate_csrf(req)) is True


def test_unexpected_body_reader_failure_propagates():
    async def receive():
        raise RuntimeError("body reader fault")
    req = Request({"type": "http", "method": "POST", "path": "/",
                   "headers": [(b"content-type", b"application/x-www-form-urlencoded")]}, receive)
    req.state.auth_session = SimpleNamespace(session=SimpleNamespace(csrf_token="a" * 32))
    with pytest.raises(RuntimeError, match="body reader fault"):
        asyncio.run(service().validate_csrf(req))
