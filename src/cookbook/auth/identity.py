"""Trusted client identity and cookie-security helpers (auth plan C4 + C3).

This is the *single* owner of forwarded-header interpretation. The
application decides whether the direct TCP peer is trusted (via
``TRUSTED_PROXY_CIDRS``) and how far to walk ``X-Forwarded-For``; uvicorn is
launched with ``--no-proxy-headers`` so it does not rewrite the peer or
interpret these headers first (see the Dockerfile and README).

Two public helpers:

* ``client_identity(request, settings) -> (ip, scheme)`` — the real client IP
  and effective scheme for the request. Consumers (lockout, cookie Secure)
  must not reparse forwarding headers themselves — call this.
* ``cookie_secure(request, settings) -> bool`` — the Secure-flag decision for
  cookie emission, honouring ``COOKIE_SECURE`` overrides when set.

Design notes:
* Trust is peer-based (direct TCP source), never header-based: an untrusted
  peer's forwarded headers are ignored entirely, even if they name trusted
  addresses.
* The right-to-left X-Forwarded-For walk selects the *first* address outside
  the trusted networks (the leftmost untrusted hop in the chain from the
  client's perspective). Malformed tokens are dropped; a fully malformed chain
  or an all-trusted chain falls back to the direct peer.
* Bounds (at most :data:`MAX_HOPS` and :data:`MAX_HEADER_CHARS` header
  characters) reject absurd header sizes cheaply without parsing every token.
* No exceptions ever leak from malformed request headers — malformed input
  falls back to the direct peer or empty string, never raises.
"""
from __future__ import annotations

import ipaddress

from starlette.requests import Request

from ..config import Settings

#: Hard caps that bound per-request processing (C4). Headers exceeding these
#: fall back to the direct peer rather than being parsed.
MAX_HOPS = 32
MAX_HEADER_CHARS = 4096

_XFF_HEADER = b"x-forwarded-for"
_XFP_HEADER = b"x-forwarded-proto"


def _canonical_ip(text: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse ``text`` to an IP address, promoting IPv4-mapped IPv6 to IPv4."""
    ip = ipaddress.ip_address(text)
    mapped = getattr(ip, "ipv4_mapped", None)
    return mapped if mapped is not None else ip


def _is_trusted(ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
                nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
                ) -> bool:
    """Whether ``ip`` falls inside any trusted network.

    Cross-family comparisons (IPv4 peer vs. IPv6 net, or the reverse) are
    always false — ``ipaddress`` does not support cross-version membership.
    An IPv4-mapped IPv6 peer (``::ffff:1.2.3.4``) is canonicalized to its
    IPv4 form first, so it matches a plain IPv4 trust network.
    """
    for net in nets:
        if net.version == ip.version and ip in net:
            return True
    return False


def _direct_peer(request: Request
                 ) -> tuple[str, ipaddress.IPv4Address | ipaddress.IPv6Address | None]:
    """The direct TCP peer: (original text, canonical IP or ``None``).

    A missing or non-IP peer returns the original text (or ``""``) and
    ``None``. Callers treat a ``None`` peer as "not trusted" and as the
    returned identity value (no forwarding is ever honored).
    """
    client = request.client
    if client is None:
        return ("", None)
    host = client.host
    try:
        return (host, _canonical_ip(host))
    except ValueError:
        return (host, None)


def _scheme_from_direct(request: Request) -> str:
    # Direct scheme from the ASGI scope (not request.url — url needs a path).
    return request.scope.get("scheme", "http")


def _scheme_from_proto(request: Request, direct_scheme: str) -> str:
    """Effective scheme when a trusted peer supplied a valid proto header.

    Accepts only the exact lowercase ``http``/``https`` in the *first*
    comma-separated value (whitespace-trimmed). Any other value — including
    ``HTTP/2``, junk, or a second valid value after a comma — falls back to
    the direct scheme. Never raises.
    """
    header = request.headers.get("x-forwarded-proto")
    if header is None:
        return direct_scheme
    first = header.split(",", 1)[0].strip()
    if first in ("http", "https"):
        return first
    return direct_scheme


def _client_ip_from_chain(request: Request,
                          direct_ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
                          direct_host: str,
                          nets: list[ipaddress.IPv4Network | ipaddress.IPv6Network]
                          ) -> str:
    """Walk ``X-Forwarded-For`` right-to-left and select the first untrusted IP.

    Returns the canonical text of that address, or falls back to the direct
    peer (canonical if parseable, original text otherwise) when:
      * the header is absent;
      * it exceeds :data:`MAX_HOPS` or :data:`MAX_HEADER_CHARS` (bound);
      * every parseable token is trusted (all-trusted chain);
      * no parseable token exists (entirely malformed chain).

    Individual malformed tokens are dropped; the walk continues over the
    remaining valid tokens.
    """
    header = request.headers.get("x-forwarded-for")
    if header is None:
        return str(direct_ip)

    # Bound processing early: a header too long or with too many commas is
    # almost certainly adversarial or broken — never parsed.
    if len(header) > MAX_HEADER_CHARS or header.count(",") >= MAX_HOPS:
        return str(direct_ip)

    selected: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None
    for token in reversed(header.split(",")):
        token = token.strip()
        if not token:
            continue
        try:
            ip = _canonical_ip(token)
        except ValueError:
            continue  # individual malformed token: drop, keep walking
        if not _is_trusted(ip, nets):
            selected = ip
            break
    if selected is not None:
        return str(selected)

    # All tokens trusted, or none parseable: fall back to the direct peer.
    return str(direct_ip)


def client_identity(request: Request, settings: Settings) -> tuple[str, str]:
    """Return ``(client_ip, scheme)`` for the request per C4.

    * Untrusted direct peer: forwarded headers are **fully ignored**; the
      identity is the peer (canonical IP text, or original text when the
      peer is absent/non-IP) plus the direct scheme.
    * Trusted direct peer: X-Forwarded-For is walked right-to-left for the
      first untrusted address; X-Forwarded-Proto (first value, only
      ``http``/``https``) overrides the scheme; both fall back to the direct
      peer/scheme when absent or entirely invalid.

    Never raises on malformed headers. Does not mutate ``request.scope`` or
    ``settings``.
    """
    direct_host, direct_ip = _direct_peer(request)
    direct_scheme = _scheme_from_direct(request)

    # Absent or non-IP peer: never trusted — no forwarding is honored at all,
    # and the returned identity is the original peer text (or "" when absent).
    # Downstream lockout consumers must explicitly handle the empty case.
    if direct_ip is None:
        return (direct_host, direct_scheme)

    nets = settings._trusted_nets
    if not _is_trusted(direct_ip, nets):
        return (str(direct_ip), direct_scheme)

    # Trusted peer: honor (bounded) forwarded headers.
    ip = _client_ip_from_chain(request, direct_ip, direct_host, nets)
    scheme = _scheme_from_proto(request, direct_scheme)
    return (ip, scheme)


def cookie_secure(request: Request, settings: Settings) -> bool:
    """Return the Secure-flag decision for emitting cookies on this request.

    An explicit ``COOKIE_SECURE`` (true or false) always overrides — the
    false-over-https case is a deliberate weakening the owner chose (C3).
    Otherwise the flag follows the effective scheme from
    :func:`client_identity`: ``https`` -> True, ``http`` -> False.
    """
    if settings.cookie_secure is not None:
        return settings.cookie_secure
    _ip, scheme = client_identity(request, settings)
    return scheme == "https"
