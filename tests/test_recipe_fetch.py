from __future__ import annotations

import asyncio
import gzip

import pytest
import httpx

from cookbook.recipes import ingest
from cookbook.recipes.ingest import (
    FetchHTTPError,
    FetchTimeoutError,
    FetchTooLargeError,
    UnsafeURLError,
    fetch_page,
    validate_url,
)


class AsyncChunks(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/recipe",
        "http:///missing-host",
        "http://user:password@example.com/recipe",
        "http://127.0.0.1/recipe",
        "http://10.1.2.3/recipe",
        "http://169.254.169.254/latest/meta-data/",
        "http://192.0.2.1/recipe",
        "http://224.0.0.1/recipe",
        "http://[::1]/recipe",
        "http://[fc00::1]/recipe",
        "http://[2001:db8::1]/recipe",
    ],
)
async def test_validate_url_rejects_non_http_or_non_public_destination(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        await validate_url(url)


@pytest.mark.asyncio
async def test_validate_url_accepts_public_ipv4_literal() -> None:
    result = await validate_url("https://1.1.1.1/recipe")

    assert result.addresses == ("1.1.1.1",)
    assert result.url.scheme == "https"


@pytest.mark.asyncio
async def test_validate_url_rejects_hostname_with_any_non_public_dns_answer(monkeypatch) -> None:
    async def mixed_answers(host: str, port: int) -> list[str]:
        assert host == "recipes.example"
        assert port == 443
        return ["1.1.1.1", "10.0.0.9"]

    monkeypatch.setattr(
        "cookbook.recipes.ingest.resolve_public_addresses", mixed_answers
    )

    with pytest.raises(UnsafeURLError):
        await validate_url("https://recipes.example/recipe")


@pytest.mark.asyncio
async def test_validate_url_retains_all_public_dns_answers(monkeypatch) -> None:
    async def public_answers(host: str, port: int) -> list[str]:
        return ["1.1.1.1", "2606:4700:4700::1111"]

    monkeypatch.setattr(
        "cookbook.recipes.ingest.resolve_public_addresses", public_answers
    )

    result = await validate_url("https://recipes.example/recipe#instructions")

    assert result.addresses == ("1.1.1.1", "2606:4700:4700::1111")
    assert result.url.fragment == ""


@pytest.mark.asyncio
async def test_validate_url_rejects_hostname_without_dns_answers(monkeypatch) -> None:
    async def no_answers(host: str, port: int) -> list[str]:
        return []

    monkeypatch.setattr(
        "cookbook.recipes.ingest.resolve_public_addresses", no_answers
    )

    with pytest.raises(UnsafeURLError):
        await validate_url("https://missing.example/recipe")


def mock_transport(monkeypatch, handler):
    def make_client(validated):
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            timeout=5,
            headers={"User-Agent": "test"},
        )

    monkeypatch.setattr(ingest, "_make_client", make_client)


@pytest.mark.asyncio
async def test_fetch_page_returns_bounded_html_and_final_url(monkeypatch) -> None:
    async def public_ip(host: str, port: int) -> list[str]:
        return ["1.1.1.1"]

    monkeypatch.setattr(ingest, "resolve_public_addresses", public_ip)
    mock_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            stream=AsyncChunks([b"<html><title>Pie</title></html>"]),
            request=request,
        ),
    )

    page = await fetch_page("https://recipes.example/pie")

    assert page.source_url == "https://recipes.example/pie"
    assert page.final_url == "https://recipes.example/pie"
    assert page.status_code == 200
    assert page.html == "<html><title>Pie</title></html>"
    assert page.title == "Pie"


@pytest.mark.asyncio
async def test_fetch_page_revalidates_redirect_before_requesting_target(monkeypatch) -> None:
    requests = []

    async def resolve(host: str, port: int) -> list[str]:
        return [host] if host == "10.0.0.5" else ["1.1.1.1"]

    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://10.0.0.5/private"}, request=request)

    monkeypatch.setattr(ingest, "resolve_public_addresses", resolve)
    mock_transport(monkeypatch, handler)

    with pytest.raises(UnsafeURLError):
        await fetch_page("http://recipes.example/start")

    assert requests == ["http://recipes.example/start"]


@pytest.mark.asyncio
async def test_fetch_page_reports_malformed_redirect_as_fetch_error(monkeypatch) -> None:
    async def public_ip(host: str, port: int) -> list[str]:
        return ["1.1.1.1"]

    monkeypatch.setattr(ingest, "resolve_public_addresses", public_ip)
    mock_transport(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"location": "http://[::1"}, request=request
        ),
    )

    with pytest.raises(FetchHTTPError, match="invalid redirect"):
        await fetch_page("http://recipes.example/start")


@pytest.mark.asyncio
async def test_fetch_page_follows_five_redirects_and_rejects_a_sixth(monkeypatch) -> None:
    calls = 0
    redirects_to_send = 5

    async def public_ip(host: str, port: int) -> list[str]:
        return ["1.1.1.1"]

    def handler(request):
        nonlocal calls
        calls += 1
        if calls <= redirects_to_send:
            return httpx.Response(
                302,
                headers={"location": f"/hop-{calls}"},
                request=request,
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            stream=AsyncChunks([b"done"]),
            request=request,
        )

    monkeypatch.setattr(ingest, "resolve_public_addresses", public_ip)
    mock_transport(monkeypatch, handler)

    page = await fetch_page("http://recipes.example/start")
    assert page.final_url == "http://recipes.example/hop-5"
    assert calls == 6

    calls = 0
    redirects_to_send = 6
    with pytest.raises(FetchHTTPError, match="redirect limit"):
        await fetch_page("http://recipes.example/start", max_redirects=5)
    assert calls == 6


@pytest.mark.asyncio
async def test_fetch_page_enforces_declared_and_streamed_decoded_size(monkeypatch) -> None:
    async def public_ip(host: str, port: int) -> list[str]:
        return ["1.1.1.1"]

    monkeypatch.setattr(ingest, "resolve_public_addresses", public_ip)
    mock_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"content-length": "11", "content-type": "text/html"},
            stream=AsyncChunks([b"small"]),
            request=request,
        ),
    )
    with pytest.raises(FetchTooLargeError):
        await fetch_page("http://recipes.example/", max_bytes=10)

    compressed = gzip.compress(b"x" * 1000)
    assert len(compressed) < 30
    mock_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            stream=AsyncChunks([compressed]),
            headers={
                "content-encoding": "gzip",
                "content-type": "text/html",
                "content-length": str(len(compressed)),
            },
            request=request,
        ),
    )
    with pytest.raises(FetchTooLargeError):
        await fetch_page("http://recipes.example/", max_bytes=30)


@pytest.mark.asyncio
async def test_fetch_page_accepts_bounded_gzip_html(monkeypatch) -> None:
    async def public_ip(host: str, port: int) -> list[str]:
        return ["1.1.1.1"]

    monkeypatch.setattr(ingest, "resolve_public_addresses", public_ip)
    compressed = gzip.compress(b"<html><title>Compressed</title></html>")
    mock_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            stream=AsyncChunks([compressed]),
            headers={
                "content-encoding": "gzip",
                "content-type": "text/html",
                "content-length": str(len(compressed)),
            },
            request=request,
        ),
    )

    page = await fetch_page("http://recipes.example/")

    assert page.title == "Compressed"


@pytest.mark.asyncio
async def test_fetch_page_reports_http_status_and_overall_timeout(monkeypatch) -> None:
    async def public_ip(host: str, port: int) -> list[str]:
        return ["1.1.1.1"]

    monkeypatch.setattr(ingest, "resolve_public_addresses", public_ip)
    mock_transport(
        monkeypatch,
        lambda request: httpx.Response(503, request=request),
    )
    with pytest.raises(FetchHTTPError, match="503"):
        await fetch_page("http://recipes.example/")

    async def slow_handler(request):
        await asyncio.Event().wait()

    mock_transport(monkeypatch, slow_handler)
    with pytest.raises(FetchTimeoutError):
        await fetch_page("http://recipes.example/", timeout=0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeout": ingest.FETCH_TIMEOUT_SECONDS + 1},
        {"max_bytes": ingest.MAX_RESPONSE_BYTES + 1},
        {"max_redirects": ingest.MAX_REDIRECTS + 1},
    ],
)
async def test_fetch_page_does_not_allow_limits_above_product_caps(kwargs) -> None:
    with pytest.raises(ValueError, match="cannot exceed"):
        await fetch_page("http://recipes.example/", **kwargs)


@pytest.mark.asyncio
async def test_fetch_page_rejects_non_html_content_type(monkeypatch) -> None:
    async def public_ip(host: str, port: int) -> list[str]:
        return ["1.1.1.1"]

    monkeypatch.setattr(ingest, "resolve_public_addresses", public_ip)
    mock_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"content-type": "image/png"}, content=b"PNG", request=request
        ),
    )
    with pytest.raises(FetchHTTPError, match="HTML"):
        await fetch_page("http://recipes.example/")


@pytest.mark.asyncio
async def test_pinned_network_backend_connects_only_to_validated_addresses() -> None:
    backend = ingest._PinnedNetworkBackend(
        "recipes.example", ("1.1.1.1", "2606:4700:4700::1111")
    )
    connected_to = []

    class FakeBackend:
        async def connect_tcp(self, host, port, **kwargs):
            connected_to.append((host, port))
            return object()

    backend._backend = FakeBackend()
    await backend.connect_tcp("recipes.example", 443)

    assert connected_to == [("1.1.1.1", 443)]
    with pytest.raises(ingest.httpcore.ConnectError, match="Unvalidated host"):
        await backend.connect_tcp("attacker.example", 443)
