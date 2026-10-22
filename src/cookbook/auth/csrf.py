"""Browser-bound pre-auth CSRF + per-session CSRF helpers (auth plan C2).

Two separate token families, kept distinct by name, helper, and value:

* *Pre-auth* (``POST /login`` / ``POST /setup``): a per-browser random
  secret carried in the ``cookbook_csrf_pre`` cookie, plus a server-signed
  form token over ``(secret, purpose, expiry)``. The binding is what stops
  cross-browser replay — a stolen token is useless without the issuing
  browser's secret cookie.
* *Session*: the per-session ``csrf_token`` (16 random hex bytes, issued at
  session creation, see ``sessions.py``) for all authenticated mutations.

Pre-auth token layout: ``<expiry-epoch-seconds>.<urlsafe-b64 HMAC-SHA256>``.
The HMAC key is domain-separated from the caller's explicit key so other
HMAC users of the same key cannot forge tokens. Verification is constant-
time, bounded, and fail-closed on any malformed input (including Unicode).

This module never reads settings, never logs tokens or secrets, and never
puts them in URLs. No routes are registered here.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import string
from datetime import datetime, timedelta

from starlette.exceptions import HTTPException
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import Response

# Pre-auth cookie family (C2). The session cookie family lives in
# sessions.py; the two must never share a name, helper, or deletion rule.
PREAUTH_COOKIE_NAME = "cookbook_csrf_pre"
PREAUTH_COOKIE_MAX_AGE = 15 * 60  # matches the default pre-auth token ttl

# The only purposes a pre-auth token may be issued for. Purpose is part of
# the signed payload, so a login token fails on /setup and vice versa.
SUPPORTED_PURPOSES = frozenset({"login", "setup"})

# Domain-separation label for the HMAC subkey. Without it, any other
# consumer of the same raw key could forge pre-auth tokens.
_DOMAIN_LABEL = b"cookbook-auth/csrf-preauth/v1"

# Token bounds: expiry is an epoch-seconds integer (<= 11 digits covers
# any plausible date), the signature is exactly 32 bytes of HMAC-SHA256,
# i.e. 44 urlsafe-b64 characters with padding.
_MAX_EXP_DIGITS = 11
_SIG_B64_LEN = 44
_SIG_BYTES = 32
_MAX_TOKEN_LEN = _MAX_EXP_DIGITS + 1 + _SIG_B64_LEN

_DIGITS = "0123456789"
# urlsafe-b64 alphabet (base64 with -_ instead of +/, optional = padding).
# Checked explicitly because urlsafe_b64decode has no strict/validate mode.
_URLSAFE_B64 = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_="
)
_EPOCH = datetime(1970, 1, 1)
_SECRET_CHARS = frozenset(string.ascii_letters + string.digits + "-_")


def _valid_secret(secret: object) -> bool:
    return (isinstance(secret, str) and len(secret) == 22
            and all(char in _SECRET_CHARS for char in secret))


def _as_naive_utc(value: datetime) -> datetime:
    """Normalize aware-UTC input to naive UTC; naive input passes through.

    Raises ValueError for non-datetime or non-UTC input (fail closed rather
    than silently mixing offset semantics into expiry math).
    """
    if not isinstance(value, datetime):
        raise ValueError("now must be a datetime")
    if value.tzinfo is None:
        return value
    if value.utcoffset() != timedelta(0):
        raise ValueError("now must be UTC")
    return value.replace(tzinfo=None)


def _to_epoch(naive_utc: datetime) -> int:
    return int((naive_utc - _EPOCH).total_seconds())


def _canonical(secret: str, purpose: str, expiry_epoch: int) -> bytes:
    """One unambiguous byte form of (secret, purpose, expiry).

    The secret is length-prefixed so a secret containing the ``.`` field
    separator can never be re-split into a different purpose/expiry reading
    (canonical encoding, C2).
    """
    secret_bytes = secret.encode("utf-8")
    return (
        len(secret_bytes).to_bytes(4, "big")
        + secret_bytes
        + b"."
        + purpose.encode("ascii")
        + b"."
        + str(expiry_epoch).encode("ascii")
    )


def _signing_key(key: str) -> bytes:
    return hmac.new(key.encode("utf-8"), _DOMAIN_LABEL, hashlib.sha256).digest()


def _sign(secret: str, purpose: str, expiry_epoch: int, key: str) -> bytes:
    return hmac.new(
        _signing_key(key), _canonical(secret, purpose, expiry_epoch), hashlib.sha256
    ).digest()


def issue_preauth(
    secret: str,
    purpose: str,
    *,
    now: datetime,
    key: str,
    ttl: timedelta = timedelta(minutes=15),
) -> str:
    """Issue a pre-auth form token bound to (secret, purpose, now + ttl).

    Raises ValueError on any invalid issuance input (empty secret/key,
    unsupported purpose, non-positive TTL, non-datetime now) so a rejected
    call produces no token at all.
    """
    if not _valid_secret(secret):
        raise ValueError("pre-auth secret must be a 16-byte URL-safe nonce")
    if not isinstance(key, str) or not key:
        raise ValueError("pre-auth signing key must be a non-empty string")
    if purpose not in SUPPORTED_PURPOSES:
        raise ValueError(f"unsupported pre-auth purpose: {purpose!r}")
    if not isinstance(ttl, timedelta) or ttl.total_seconds() <= 0:
        raise ValueError("pre-auth TTL must be a positive timedelta")

    issued = _as_naive_utc(now)
    expiry_epoch = _to_epoch(issued + ttl)
    signature = base64.urlsafe_b64encode(
        _sign(secret, purpose, expiry_epoch, key)
    ).decode("ascii")
    return f"{expiry_epoch}.{signature}"


def _parse_token(token: str) -> tuple[int, bytes] | None:
    """Bounded, fail-closed parse of an untrusted token.

    Returns (expiry_epoch, signature_bytes) or None. No exception escapes.
    """
    if len(token) > _MAX_TOKEN_LEN:
        return None
    exp_part, sep, sig_part = token.partition(".")
    if not sep:
        return None
    if (
        not exp_part
        or len(exp_part) > _MAX_EXP_DIGITS
        or any(char not in _DIGITS for char in exp_part)
    ):
        return None
    if len(sig_part) != _SIG_B64_LEN:
        return None
    # urlsafe_b64decode is not strict, so bound the alphabet first; then a
    # 44-char payload must decode to exactly 32 signature bytes.
    if any(char not in _URLSAFE_B64 for char in sig_part):
        return None
    try:
        sig_bytes = base64.urlsafe_b64decode(sig_part.encode("ascii"))
    except (ValueError, TypeError):
        return None
    if len(sig_bytes) != _SIG_BYTES:
        return None
    return (int(exp_part), sig_bytes)


def verify_preauth(
    token: object,
    secret: object,
    purpose: str,
    *,
    now: datetime,
    key: str,
) -> bool:
    """Verify a pre-auth token; any invalid input or check failure -> False.

    The signature covers (secret, purpose, expiry), so the constant-time
    signature check alone proves purpose binding *and* browser binding: it
    only matches when the embedded secret equals the cookie-provided one,
    the purpose equals the expected one, and the expiry is the signed one.
    Expiry is strict: ``now == expiry`` is rejected. Same-browser replay of
    an in-lifetime token is permitted by design (no single-use store).
    """
    if not isinstance(token, str) or not _valid_secret(secret):
        return False
    if not isinstance(key, str) or not key:
        return False
    if purpose not in SUPPORTED_PURPOSES:
        return False
    try:
        now_epoch = _to_epoch(_as_naive_utc(now))
    except ValueError:
        return False

    parsed = _parse_token(token)
    if parsed is None:
        return False
    expiry_epoch, sig_bytes = parsed

    # One constant-time compare covers signature + purpose + browser binding.
    if not hmac.compare_digest(_sign(secret, purpose, expiry_epoch, key), sig_bytes):
        return False
    return now_epoch < expiry_epoch


def _first_set_cookie(carrier: Response) -> str:
    """Extract the single Set-Cookie header Starlette just emitted."""
    for header_name, header_value in reversed(carrier.raw_headers):
        if header_name == b"set-cookie":
            return header_value.decode("latin-1")
    raise RuntimeError("set_cookie emitted no Set-Cookie header")  # unreachable


def browser_secret_set_cookie(secret: str, *, secure: bool) -> str:
    """Render ONE Set-Cookie header for the pre-auth browser secret.

    HttpOnly (no JS ever reads it), SameSite=Strict, Path=/, no Domain,
    Max-Age=900, Secure per argument. ValueError on an empty secret so a
    rejected call leaves no partial headers behind.
    """
    if not _valid_secret(secret):
        raise ValueError("browser secret must be a 16-byte URL-safe nonce")
    carrier = Response()
    carrier.set_cookie(
        PREAUTH_COOKIE_NAME,
        secret,
        max_age=PREAUTH_COOKIE_MAX_AGE,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    return _first_set_cookie(carrier)


def browser_secret_clear(*, secure: bool = False) -> str:
    """Render ONE Set-Cookie header deleting the pre-auth browser secret."""
    carrier = Response()
    carrier.delete_cookie(
        PREAUTH_COOKIE_NAME,
        path="/",
        secure=secure,
        httponly=True,
        samesite="strict",
    )
    return _first_set_cookie(carrier)


def issue_session_token() -> str:
    """A fresh per-session CSRF token: random 16 bytes as 32 hex chars."""
    return secrets.token_hex(16)


def _constant_time_equal(candidate: object, expected: str) -> bool:
    # str type guard first: compare_digest raises on mixed types/encodings,
    # and an unhashable/odd form value (e.g. multipart UploadFile) must
    # fail closed, not crash the request.
    if not isinstance(candidate, str) or not candidate.isascii():
        return False
    return hmac.compare_digest(candidate, expected)


async def validate_csrf(request: Request) -> bool:
    """Check an authenticated mutation's CSRF token (C2 delivery contract).

    The ``X-CSRF-Token`` header takes precedence even when it is present
    but empty or invalid; only an *absent* header falls back to the
    ``csrf_token`` form field (covers urlencoded and multipart). The form
    body is never read when the header is present, so JSON/HTMX requests
    can authenticate via header alone. Missing loaded session context
    (``request.state.auth_session`` absent or None) fails closed.
    """
    loaded = getattr(request.state, "auth_session", None)
    session = getattr(loaded, "session", None)
    expected = getattr(session, "csrf_token", None)
    if not isinstance(expected, str) or not expected:
        return False

    presented = request.headers.get("x-csrf-token")
    if presented is not None:
        return _constant_time_equal(presented, expected)

    try:
        form = await request.form()
    except (HTTPException, MultiPartException, ValueError):
        # Reject malformed forms while preserving unexpected reader faults.
        return False
    return _constant_time_equal(form.get("csrf_token"), expected)
