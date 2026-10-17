"""``client_identity`` / ``cookie_secure`` behavior (auth plan S2, C4 + C3).

Controlled ASGI peers (TEST-NET addresses) stand in for real proxy/client
addresses — no database or live proxy needed. All cases verify the
*behavioral* contract: trusted vs untrusted peer, chain walk, bounds,
canonicalization, and Secure-flag resolution.
"""
from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from starlette.requests import Request

from cookbook import config as config_mod
from cookbook.auth.identity import client_identity, cookie_secure
from cookbook.config import Settings

TRUSTED_V4 = "192.0.2.0/24"
TRUSTED_V6 = "2001:db8:aaaa::/48"


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> Iterator[None]:
    """Same hermetic env as tests/test_config.py: no inherited config, no .env."""
    for var in list(os.environ):
        if var.startswith(
            ("DB_", "SECRET_", "APP_", "DEBUG", "MEDIA_", "ALLOWED_", "UPLOAD_",
             "SESSION_TTL", "TRUSTED_PROXY", "COOKIE_")
        ):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    config_mod.get_settings.cache_clear()
    yield
    config_mod.get_settings.cache_clear()


def make_request(client, scheme: str = "http", xff: str | None = None,
                 xfp: str | None = None) -> Request:
    """Pure ASGI request with a controlled direct peer and headers."""
    headers = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    if xfp is not None:
        headers.append((b"x-forwarded-proto", xfp.encode()))
    scope: dict = {"type": "http", "client": client, "scheme": scheme}
    if headers:
        scope["headers"] = headers
    return Request(scope)


def settings_for(cidrs: str = "", cookie_secure: bool | None = None) -> Settings:
    return Settings(trusted_proxy_cidrs=cidrs, cookie_secure=cookie_secure)


# --- untrusted peer: headers must be ignored entirely ----------------------

def test_unset_trust_ignores_forged_headers() -> None:
    req = make_request(("198.51.100.7", 51000),
                       xff="8.8.8.8, 9.9.9.9", xfp="https")
    assert client_identity(req, settings_for()) == ("198.51.100.7", "http")


def test_untrusted_v4_peer_ignores_both_headers() -> None:
    req = make_request(("198.51.100.7", 51000), xff="8.8.8.8", xfp="https")
    assert client_identity(req, settings_for(TRUSTED_V4)) == ("198.51.100.7", "http")


def test_untrusted_v6_peer_ignores_both_headers() -> None:
    req = make_request(("2001:db9::7", 51000), xff="2003::8", xfp="https")
    assert client_identity(req, settings_for(TRUSTED_V6)) == ("2001:db9::7", "http")


def test_untrusted_mapped_v6_peer_returns_canonical_ipv4() -> None:
    # Peer arrives as IPv4-mapped IPv6; trust list holds the plain IPv4 net.
    # Untrusted => direct peer, canonicalized to dotted form (C1 lockout key).
    req = make_request(("::ffff:10.9.8.7", 51000), xff="1.1.1.1", xfp="https")
    assert client_identity(req, settings_for("192.0.2.0/24")) == ("10.9.8.7", "http")


# --- trusted peer: headers honored -----------------------------------------

def test_trusted_v4_peer_uses_chain_and_proto() -> None:
    req = make_request(("192.0.2.3", 51000),
                       xff="203.0.113.44, 192.0.2.9", xfp="https")
    assert client_identity(req, settings_for(TRUSTED_V4)) == ("203.0.113.44", "https")


def test_trusted_v6_peer_uses_chain_and_proto() -> None:
    req = make_request(("2001:db8:aaaa::1", 51000),
                       xff="2003::44, 2001:db8:aaaa::9", xfp="https")
    assert client_identity(req, settings_for(TRUSTED_V6)) == ("2003::44", "https")


def test_mixed_proxy_chain_selects_first_untrusted_from_right() -> None:
    cidrs = f"{TRUSTED_V4}, {TRUSTED_V6}"
    req = make_request(("192.0.2.1", 51000), xff="185.220.101.1, 10.0.0.5, 192.0.2.7")
    assert client_identity(req, settings_for(cidrs)) == ("10.0.0.5", "http")


def test_spoofed_left_side_entry_does_not_override_nearest_untrusted() -> None:
    # Attacker prepends 8.8.8.8; the right-to-left walk must still pick the
    # real first-untrusted hop, 45.6.7.8.
    req = make_request(("192.0.2.1", 51000), xff="8.8.8.8, 45.6.7.8")
    assert client_identity(req, settings_for(TRUSTED_V4)) == ("45.6.7.8", "http")


# --- chain fallbacks ---------------------------------------------------------

def test_all_trusted_chain_falls_back_to_peer() -> None:
    req = make_request(("192.0.2.1", 51000), xff="192.0.2.4, 192.0.2.5")
    assert client_identity(req, settings_for(TRUSTED_V4)) == ("192.0.2.1", "http")


def test_missing_chain_falls_back_to_peer() -> None:
    req = make_request(("192.0.2.1", 51000), xfp="https")
    # No XFF at all: identity is the peer, scheme still follows trusted proto.
    assert client_identity(req, settings_for(TRUSTED_V4)) == ("192.0.2.1", "https")


def test_entirely_malformed_chain_falls_back_to_peer() -> None:
    req = make_request(("192.0.2.1", 51000), xff="not-an-ip, more junk")
    assert client_identity(req, settings_for(TRUSTED_V4)) == ("192.0.2.1", "http")


def test_mixed_malformed_and_valid_tokens_drop_bad_keep_good() -> None:
    cidrs = TRUSTED_V4
    bad_left = make_request(("192.0.2.1", 51000), xff="not-an-ip, 203.0.113.44")
    bad_right = make_request(("192.0.2.1", 51000), xff="203.0.113.44, not-an-ip")
    assert client_identity(bad_left, settings_for(cidrs)) == ("203.0.113.44", "http")
    assert client_identity(bad_right, settings_for(cidrs)) == ("203.0.113.44", "http")


def test_trusted_mapped_v6_peer_in_v4_trust_net() -> None:
    req = make_request(("::ffff:198.51.100.67", 51000),
                       xff="203.0.113.10", xfp="https")
    assert client_identity(req, settings_for("198.51.100.0/24")) == (
        "203.0.113.10", "https"
    )


# --- processing bounds -------------------------------------------------------

def _hops(n: int) -> str:
    return ", ".join(f"10.200.0.{i}" for i in range(n))


def test_hop_bound_32_ok_33_falls_back() -> None:
    ok = make_request(("192.0.2.1", 51000), xff=_hops(32))
    over = make_request(("192.0.2.1", 51000), xff=_hops(33))
    s = settings_for(TRUSTED_V4)
    # 32 hops is within bound: rightmost (only untrusted) hop is returned.
    assert client_identity(ok, s) == ("10.200.0.31", "http")
    # 33 hops exceeds the bound: the whole header is ignored, peer returned.
    assert client_identity(over, s) == ("192.0.2.1", "http")


def test_char_bound_exceeded_falls_back_within_keeps_valid_token() -> None:
    s = settings_for(TRUSTED_V4)
    # 31 junk tokens (~150 chars each) + 1 valid: ~4.7 KB header > 4096 limit
    # -> header ignored, direct peer returned.
    big = ", ".join(["a" * 150] * 31 + ["203.0.113.44"])
    assert client_identity(make_request(("192.0.2.1", 51000), xff=big), s) == (
        "192.0.2.1", "http"
    )
    # 25 junk tokens + 1 valid: under 4096 chars, under 32 hops -> valid token kept.
    small = ", ".join(["a" * 150] * 25 + ["203.0.113.44"])
    assert client_identity(make_request(("192.0.2.1", 51000), xff=small), s) == (
        "203.0.113.44", "http"
    )


# --- X-Forwarded-Proto: two-value decision -----------------------------------

def test_invalid_proto_falls_back_to_direct_scheme() -> None:
    for proto in ("HTTP/2", "wss", "HTTPS", "junk"):
        req = make_request(("192.0.2.1", 51000), xff="203.0.113.44", xfp=proto)
        assert client_identity(req, settings_for(TRUSTED_V4)) == (
            "203.0.113.44", "http"
        ), proto


def test_multi_value_proto_uses_first_comma_value() -> None:
    req = make_request(("192.0.2.1", 51000), xff="203.0.113.44", xfp="https, http")
    assert client_identity(req, settings_for(TRUSTED_V4)) == ("203.0.113.44", "https")


def test_whitespace_padded_proto_accepted() -> None:
    req = make_request(("192.0.2.1", 51000), xff="203.0.113.44", xfp=" https ")
    assert client_identity(req, settings_for(TRUSTED_V4)) == ("203.0.113.44", "https")


def test_other_forwarding_headers_are_ignored() -> None:
    scope = {
        "type": "http", "client": ("198.51.100.7", 51000), "scheme": "http",
        "headers": [
            (b"forwarded", b'for=1.2.3.4;proto=https'),
            (b"x-forwarded-host", b"example.com"),
            (b"x-forwarded-port", b"443"),
            (b"x-forwarded-proto", b"https"),
        ],
    }
    req = Request(scope)
    # Peer 198.51.100.x is outside the 192.0.2.0/24 trust list: forwarded
    # headers (including all the odd ones) must be ignored entirely.
    assert client_identity(req, settings_for(TRUSTED_V4)) == (
        "198.51.100.7", "http"
    )


# --- absent / non-IP direct peer ---------------------------------------------

def test_missing_client_returns_empty_identity() -> None:
    req = make_request(None, xff="1.2.3.4", xfp="https")
    assert client_identity(req, settings_for("0.0.0.0/0", cookie_secure=None)) == ("", "http")


def test_non_ip_client_not_trusted() -> None:
    req = make_request(("unix-socket-peer", 0), xff="1.2.3.4", xfp="https")
    assert client_identity(req, settings_for("0.0.0.0/0")) == ("unix-socket-peer", "http")


# --- no mutation -------------------------------------------------------------

def test_helper_leaves_scope_and_settings_unchanged() -> None:
    s = settings_for(f"{TRUSTED_V4}, {TRUSTED_V6}", cookie_secure=True)
    req = make_request(("192.0.2.1", 51000), xff="203.0.113.44", xfp="https")
    scope_before = {k: v for k, v in req.scope.items()}
    cidrs_before, cs_before = s.trusted_proxy_cidrs, s.cookie_secure
    nets_before = [str(n) for n in s._trusted_nets]

    client_identity(req, s)
    cookie_secure(req, s)

    assert dict(req.scope) == scope_before
    assert (s.trusted_proxy_cidrs, s.cookie_secure) == (cidrs_before, cs_before)
    assert [str(n) for n in s._trusted_nets] == nets_before


# --- cookie_secure -----------------------------------------------------------

def test_cookie_secure_auto_follows_direct_scheme() -> None:
    s = settings_for()
    https = make_request(("198.51.100.7", 51000), scheme="https")
    http = make_request(("198.51.100.7", 51000), scheme="http")
    assert cookie_secure(https, s) is True
    assert cookie_secure(http, s) is False


def test_cookie_secure_auto_follows_forwarded_scheme_when_trusted() -> None:
    s = settings_for(TRUSTED_V4)
    req = make_request(("192.0.2.1", 51000), xff="203.0.113.44", xfp="https")
    assert cookie_secure(req, s) is True
    plain = make_request(("192.0.2.1", 51000), xff="203.0.113.44")
    assert cookie_secure(plain, s) is False


def test_cookie_secure_explicit_overrides_auto() -> None:
    https = make_request(("198.51.100.7", 51000), scheme="https")
    http = make_request(("198.51.100.7", 51000), scheme="http")
    assert cookie_secure(http, settings_for(cookie_secure=True)) is True
    # Explicit false over https: a deliberate weakening the owner chose (C3).
    assert cookie_secure(https, settings_for(cookie_secure=False)) is False


def test_cookie_secure_override_wins_over_forged_proto() -> None:
    forged = make_request(("198.51.100.7", 51000), xff="1.1.1.1", xfp="https")
    assert cookie_secure(forged, settings_for(cookie_secure=False)) is False
    assert cookie_secure(forged, settings_for(cookie_secure=True)) is True
