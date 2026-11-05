"""Bounded HTTP fetching for untrusted recipe URLs."""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import zlib
from dataclasses import dataclass

import httpcore
import httpx


MAX_RESPONSE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
FETCH_TIMEOUT_SECONDS = 20.0
ASSET_TIMEOUT_SECONDS = 10.0
MAX_ASSET_BYTES = 10 * 1024 * 1024
_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_IMAGE_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
_USER_AGENT = (
    "Mozilla/5.0 (compatible; MaynardCookbook/1.0; +https://cookbook.maynardfolks.com)"
)


class FetchError(Exception):
    """A safe-to-display error while fetching a recipe source page."""


class UnsafeURLError(FetchError):
    """The URL is malformed or resolves to a non-public address."""


class FetchTooLargeError(FetchError):
    """The page body exceeds the configured decoded-byte limit."""


class FetchHTTPError(FetchError):
    """The remote server returned a non-success status or bad redirect."""


class FetchTimeoutError(FetchError):
    """The complete fetch exceeded its time budget."""


@dataclass(frozen=True, slots=True)
class ValidatedURL:
    url: httpx.URL
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FetchedPage:
    source_url: str
    final_url: str
    status_code: int
    content_type: str
    html: str
    title: str = ""
    site_name: str | None = None
    description: str | None = None
    canonical_url: str | None = None
    og_image: str | None = None
    visible_text: str = ""
    json_ld: tuple[str, ...] = ()


async def resolve_public_addresses(host: str, port: int) -> list[str]:
    """Resolve a hostname once; callers reject the whole answer set if unsafe."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        loop = asyncio.get_running_loop()
        try:
            records = await loop.getaddrinfo(
                host, port, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM
            )
        except (OSError, socket.gaierror) as exc:
            raise UnsafeURLError("The recipe source hostname could not be resolved.") from exc
        addresses = [record[4][0] for record in records]
    else:
        addresses = [str(literal)]

    normalized: list[str] = []
    for address in addresses:
        try:
            value = str(ipaddress.ip_address(address))
        except ValueError as exc:
            raise UnsafeURLError("The recipe source resolved to an invalid address.") from exc
        if value not in normalized:
            normalized.append(value)
    return normalized


def _is_public_unicast(address: str) -> bool:
    value = ipaddress.ip_address(address)
    return value.is_global and not value.is_multicast and not value.is_reserved


async def validate_url(url: str) -> ValidatedURL:
    """Validate an HTTP(S) URL and resolve every destination before connecting."""
    try:
        parsed = httpx.URL(url)
    except (httpx.InvalidURL, TypeError, ValueError) as exc:
        raise UnsafeURLError("Enter a valid HTTP or HTTPS recipe URL.") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.host:
        raise UnsafeURLError("Only HTTP and HTTPS recipe URLs are supported.")
    if parsed.username or parsed.password:
        raise UnsafeURLError("Recipe URLs cannot contain a username or password.")

    host = parsed.host.rstrip(".").lower()
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    addresses = await resolve_public_addresses(host, port)
    if not addresses:
        raise UnsafeURLError("The recipe source hostname has no public addresses.")
    if any(not _is_public_unicast(address) for address in addresses):
        raise UnsafeURLError("The recipe source must resolve only to public addresses.")
    return ValidatedURL(parsed.copy_with(fragment=None), tuple(addresses))


class _PinnedNetworkBackend(httpcore.AsyncNetworkBackend):
    """Connect to validated IPs while httpcore retains the URL host for TLS."""

    def __init__(self, host: str, addresses: tuple[str, ...]) -> None:
        self._host = host.rstrip(".").lower()
        self._addresses = addresses
        self._backend = httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ) -> httpcore.AsyncNetworkStream:
        if host.rstrip(".").lower() != self._host:
            raise httpcore.ConnectError("Unvalidated host requested by HTTP transport.")
        last_error: Exception | None = None
        for address in self._addresses:
            try:
                return await self._backend.connect_tcp(
                    address,
                    port,
                    timeout=timeout,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except Exception as exc:
                last_error = exc
        assert last_error is not None
        raise last_error

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("Unix sockets are not available for URL fetching.")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, validated: ValidatedURL) -> None:
        super().__init__(trust_env=False)
        self._pool = httpcore.AsyncConnectionPool(
            network_backend=_PinnedNetworkBackend(validated.url.host, validated.addresses)
        )


def _make_client(validated: ValidatedURL) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=_PinnedHTTPTransport(validated),
        follow_redirects=False,
        timeout=httpx.Timeout(FETCH_TIMEOUT_SECONDS),
        trust_env=False,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Encoding": "gzip, deflate, identity",
        },
    )


async def fetch_page(
    url: str,
    *,
    timeout: float = FETCH_TIMEOUT_SECONDS,
    max_bytes: int = MAX_RESPONSE_BYTES,
    max_redirects: int = MAX_REDIRECTS,
) -> FetchedPage:
    """Fetch a public HTML page, manually validating every redirect destination."""
    if timeout <= 0 or max_bytes <= 0 or max_redirects < 0:
        raise ValueError("Fetch limits must be positive (redirect limit may be zero).")
    if (
        timeout > FETCH_TIMEOUT_SECONDS
        or max_bytes > MAX_RESPONSE_BYTES
        or max_redirects > MAX_REDIRECTS
    ):
        raise ValueError("Fetch limits cannot exceed the product caps.")
    source_url = url
    current_url = url
    redirects = 0
    try:
        async with asyncio.timeout(timeout):
            while True:
                validated = await validate_url(current_url)
                async with _make_client(validated) as client:
                    async with client.stream("GET", validated.url) as response:
                        if response.status_code in _REDIRECT_STATUSES:
                            location = response.headers.get("location")
                            if not location:
                                raise FetchHTTPError("The recipe source returned an invalid redirect.")
                            if redirects >= max_redirects:
                                raise FetchHTTPError("The recipe source exceeded the redirect limit.")
                            current_url = str(response.url.join(location))
                            redirects += 1
                            continue
                        if not 200 <= response.status_code < 300:
                            raise FetchHTTPError(
                                f"The recipe source returned HTTP {response.status_code}."
                            )
                        content_type = response.headers.get("content-type", "")
                        media_type = content_type.split(";", 1)[0].strip().casefold()
                        if media_type and media_type not in {
                            "text/html",
                            "application/xhtml+xml",
                        }:
                            raise FetchHTTPError("The recipe source did not return an HTML page.")
                        content_length = response.headers.get("content-length")
                        if content_length is not None:
                            try:
                                if int(content_length) > max_bytes:
                                    raise FetchTooLargeError("The recipe source page is too large.")
                            except ValueError:
                                pass
                        content_encoding = response.headers.get("content-encoding", "")
                        encoding = content_encoding.strip().casefold()
                        if encoding in {"", "identity"}:
                            decoder = None
                        elif encoding == "gzip":
                            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                        elif encoding == "deflate":
                            decoder = zlib.decompressobj(zlib.MAX_WBITS)
                        else:
                            raise FetchHTTPError(
                                "The recipe source used an unsupported content encoding."
                            )

                        body = bytearray()
                        received_bytes = 0
                        try:
                            async for chunk in response.aiter_raw():
                                received_bytes += len(chunk)
                                if received_bytes > max_bytes:
                                    raise FetchTooLargeError(
                                        "The recipe source page is too large."
                                    )
                                if decoder is None:
                                    decoded = chunk
                                else:
                                    pending = chunk
                                    decoded = bytearray()
                                    while pending:
                                        remaining = max_bytes - len(body) - len(decoded)
                                        piece = decoder.decompress(pending, remaining + 1)
                                        decoded.extend(piece)
                                        pending = decoder.unconsumed_tail
                                        if len(body) + len(decoded) > max_bytes:
                                            raise FetchTooLargeError(
                                                "The recipe source page is too large."
                                            )
                                if len(body) + len(decoded) > max_bytes:
                                    raise FetchTooLargeError(
                                        "The recipe source page is too large."
                                    )
                                body.extend(decoded)
                            if decoder is not None and not decoder.eof:
                                raise FetchHTTPError(
                                    "The recipe source returned an incomplete compressed page."
                                )
                        except zlib.error as exc:
                            raise FetchHTTPError(
                                "The recipe source returned an invalid compressed page."
                            ) from exc
                        try:
                            encoding = response.encoding or "utf-8"
                        except LookupError:
                            encoding = "utf-8"
                        html = bytes(body).decode(encoding, errors="replace")
                        from .page_extraction import extract_page

                        return extract_page(
                            html,
                            source_url=source_url,
                            final_url=str(response.url),
                            status_code=response.status_code,
                            content_type=content_type,
                        )
    except TimeoutError as exc:
        raise FetchTimeoutError("Fetching the recipe source exceeded the time limit.") from exc
    except httpx.TimeoutException as exc:
        raise FetchTimeoutError("Fetching the recipe source exceeded the time limit.") from exc
    except httpx.InvalidURL as exc:
        raise FetchHTTPError("The recipe source returned an invalid redirect.") from exc
    except httpx.RemoteProtocolError as exc:
        if str(exc).startswith("Invalid URL in location header"):
            raise FetchHTTPError("The recipe source returned an invalid redirect.") from exc
        raise FetchError("The recipe source returned an invalid HTTP response.") from exc
    except httpx.RequestError as exc:
        raise FetchError("The recipe source could not be fetched.") from exc


async def fetch_public_bytes(
    url: str,
    *,
    max_bytes: int,
    timeout: float = ASSET_TIMEOUT_SECONDS,
    max_redirects: int = MAX_REDIRECTS,
) -> tuple[bytes, str]:
    """Fetch a bounded public image, validating DNS and every redirect hop."""
    if (
        max_bytes <= 0
        or max_bytes > MAX_ASSET_BYTES
        or timeout <= 0
        or timeout > ASSET_TIMEOUT_SECONDS
        or max_redirects < 0
        or max_redirects > MAX_REDIRECTS
    ):
        raise ValueError("Asset fetch limits exceed the product caps.")

    current_url = url
    redirects = 0
    try:
        async with asyncio.timeout(timeout):
            while True:
                validated = await validate_url(current_url)
                async with _make_client(validated) as client:
                    client.headers.update(
                        {
                            "Accept": "image/jpeg,image/png,image/webp",
                            "Accept-Encoding": "identity",
                        }
                    )
                    async with client.stream("GET", validated.url) as response:
                        if response.status_code in _REDIRECT_STATUSES:
                            location = response.headers.get("location")
                            if not location:
                                raise FetchHTTPError("The image source returned an invalid redirect.")
                            if redirects >= max_redirects:
                                raise FetchHTTPError("The image source exceeded the redirect limit.")
                            current_url = str(response.url.join(location))
                            redirects += 1
                            continue
                        if not 200 <= response.status_code < 300:
                            raise FetchHTTPError(
                                f"The image source returned HTTP {response.status_code}."
                            )
                        content_type = (
                            response.headers.get("content-type", "")
                            .split(";", 1)[0]
                            .strip()
                            .casefold()
                        )
                        if content_type not in _IMAGE_CONTENT_TYPES:
                            raise FetchHTTPError("The source did not return a supported image.")
                        content_encoding = response.headers.get("content-encoding", "")
                        if content_encoding.strip().casefold() not in {"", "identity"}:
                            raise FetchHTTPError("The image source used an unsupported encoding.")
                        content_length = response.headers.get("content-length")
                        if content_length is not None:
                            try:
                                if int(content_length) > max_bytes:
                                    raise FetchTooLargeError("The source image is too large.")
                            except ValueError:
                                pass
                        body = bytearray()
                        async for chunk in response.aiter_raw():
                            if len(body) + len(chunk) > max_bytes:
                                raise FetchTooLargeError("The source image is too large.")
                            body.extend(chunk)
                        return bytes(body), content_type
    except TimeoutError as exc:
        raise FetchTimeoutError("Fetching the source image exceeded the time limit.") from exc
    except httpx.TimeoutException as exc:
        raise FetchTimeoutError("Fetching the source image exceeded the time limit.") from exc
    except httpx.InvalidURL as exc:
        raise FetchHTTPError("The image source returned an invalid redirect.") from exc
    except httpx.RemoteProtocolError as exc:
        if str(exc).startswith("Invalid URL in location header"):
            raise FetchHTTPError("The image source returned an invalid redirect.") from exc
        raise FetchError("The image source returned an invalid HTTP response.") from exc
    except httpx.RequestError as exc:
        raise FetchError("The source image could not be fetched.") from exc
