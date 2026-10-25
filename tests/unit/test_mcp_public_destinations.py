"""MCP probes reject unsafe destinations before writes; transport binds each hop."""

import asyncio
import sys
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import httpx2
import pytest

from api.db.services.mcp_server_service import MCPServerService
from api.db.services.user_service import TenantService
from common import safe_mcp_http
from common.constants import RetCode


@pytest.mark.parametrize("url", [None, 1, True, [], {}, "", " https://example.com", "file:///etc/passwd", "http://127.0.0.1/mcp", "http://[::1]/mcp", "https://u:p@example.com"])
async def test_url_validation_rejects_invalid_or_private(url: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        await safe_mcp_http.validate_mcp_url(url)


async def test_transport_binds_dns_per_request_and_retains_host_and_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safe_mcp_http, "assert_url_is_safe", lambda url: ("public.example", "93.184.216.34"))
    transport = safe_mcp_http.PublicMCPTransport()
    observed: list[httpx2.Request] = []

    async def send(request: httpx2.Request) -> httpx2.Response:
        observed.append(request)
        return httpx2.Response(200, content=b"ok")

    monkeypatch.setattr(transport._transport, "handle_async_request", send)
    try:
        async with httpx2.AsyncClient(transport=transport) as client:
            response = await client.get("https://public.example:8443/mcp?q=1")
        assert response.request.url.host == "public.example"
        assert observed[0].url.host == "93.184.216.34"
        assert observed[0].headers["host"] == "public.example:8443"
        assert observed[0].extensions["sni_hostname"] == "public.example"
    finally:
        await transport.aclose()


async def test_redirect_is_revalidated_before_any_private_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def validate(url: str) -> tuple[str, str]:
        if "127.0.0.1" in url:
            raise ValueError("private address")
        return "public.example", "93.184.216.34"

    monkeypatch.setattr(safe_mcp_http, "assert_url_is_safe", validate)
    transport = safe_mcp_http.PublicMCPTransport()

    async def send(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        return httpx2.Response(302, headers={"location": "http://127.0.0.1/private"})

    monkeypatch.setattr(transport._transport, "handle_async_request", send)
    async with httpx2.AsyncClient(transport=transport, follow_redirects=True) as client:
        with pytest.raises(ValueError, match="private address"):
            await client.get("https://public.example/mcp")
    assert seen == ["https://93.184.216.34/mcp"]


async def test_parallel_requests_have_no_shared_dns_pins(monkeypatch: pytest.MonkeyPatch) -> None:
    def validate(url: str) -> tuple[str, str]:
        host = httpx2.URL(url).host
        return host, "93.184.216.34" if host == "one.example" else "8.8.8.8"

    monkeypatch.setattr(safe_mcp_http, "assert_url_is_safe", validate)
    transport = safe_mcp_http.PublicMCPTransport()
    observed: dict[str, str] = {}

    async def send(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(0)
        observed[request.headers["host"]] = request.url.host
        return httpx2.Response(200, content=b"ok")

    monkeypatch.setattr(transport._transport, "handle_async_request", send)
    async with httpx2.AsyncClient(transport=transport) as client:
        await asyncio.gather(client.get("https://one.example/mcp"), client.get("https://two.example/mcp"))
    assert observed == {"one.example": "93.184.216.34", "two.example": "8.8.8.8"}


def test_mcp_update_authorizes_before_dns_or_probe(client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    route = sys.modules["api.apps.restful_apis.mcp"]
    monkeypatch.setattr(MCPServerService, "get_by_id", classmethod(lambda *_a: SimpleNamespace(tenant_id="other")))
    dns = AsyncMock()
    probe = Mock()
    monkeypatch.setattr(route, "validate_mcp_url", dns)
    monkeypatch.setattr(route, "get_mcp_tools", probe)
    result = client.put("/api/v1/mcp/servers/private", json={"url": "http://127.0.0.1"})
    assert result.json()["retcode"] == int(RetCode.DATA_ERROR)
    dns.assert_not_called()
    probe.assert_not_called()


@pytest.mark.parametrize("operation", ["create", "update", "import", "test"])
def test_mcp_unsafe_probe_has_no_writes_or_external_calls(client: Any, monkeypatch: pytest.MonkeyPatch, operation: str) -> None:
    route = sys.modules["api.apps.restful_apis.mcp"]
    monkeypatch.setattr(TenantService, "get_by_id", classmethod(lambda *_a: SimpleNamespace(id="user-unit")))
    monkeypatch.setattr(MCPServerService, "get_by_name_and_tenant", classmethod(lambda *_a, **_kw: (False, None)))
    server = SimpleNamespace(tenant_id="user-unit", to_dict=lambda: {"name": "owned", "url": "https://public.example", "server_type": "sse"})
    monkeypatch.setattr(MCPServerService, "get_by_id", classmethod(lambda *_a: server))
    write = Mock()
    probe = Mock()
    monkeypatch.setattr(MCPServerService, "insert", write)
    monkeypatch.setattr(MCPServerService, "filter_update", write)
    monkeypatch.setattr(route, "get_mcp_tools", probe)
    monkeypatch.setattr(route, "MCPToolCallSession", probe)
    payload = {"name": "blocked", "url": "http://127.0.0.1", "server_type": "sse"}
    if operation == "create":
        result = client.post("/api/v1/mcp/servers", json=payload)
    elif operation == "update":
        result = client.put("/api/v1/mcp/servers/owned", json=payload)
    elif operation == "import":
        result = client.post("/api/v1/mcp/servers/import", json={"mcpServers": {"blocked": {"type": "sse", "url": payload["url"]}}})
        assert result.json()["data"]["results"][0]["success"] is False
    else:
        result = client.post("/api/v1/mcp/servers/owned/test", json={"url": payload["url"], "server_type": "sse"})
    if operation != "import":
        assert result.json()["retcode"] == int(RetCode.DATA_ERROR)
    write.assert_not_called()
    probe.assert_not_called()


async def test_canceled_http_request_closes_its_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(safe_mcp_http, "assert_url_is_safe", lambda _url: ("public.example", "93.184.216.34"))
    transport = safe_mcp_http.PublicMCPTransport()
    entered = asyncio.Event()

    async def send(_request: httpx2.Request) -> httpx2.Response:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("canceled transport resumed")

    close = AsyncMock(wraps=transport._transport.aclose)
    monkeypatch.setattr(transport._transport, "handle_async_request", send)
    monkeypatch.setattr(transport._transport, "aclose", close)
    async with httpx2.AsyncClient(transport=transport) as client:
        request = asyncio.create_task(client.get("https://public.example/mcp"))
        await asyncio.wait_for(entered.wait(), timeout=2)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
    close.assert_awaited_once()


@pytest.mark.parametrize("failure", [httpx2.ConnectError, httpx2.ReadTimeout])
async def test_http_failure_keeps_its_typed_outcome(monkeypatch: pytest.MonkeyPatch, failure: type[httpx2.TransportError]) -> None:
    monkeypatch.setattr(safe_mcp_http, "assert_url_is_safe", lambda _url: ("public.example", "93.184.216.34"))
    transport = safe_mcp_http.PublicMCPTransport()

    async def send(_request: httpx2.Request) -> httpx2.Response:
        raise failure("controlled transport failure")

    monkeypatch.setattr(transport._transport, "handle_async_request", send)
    async with httpx2.AsyncClient(transport=transport) as client:
        with pytest.raises(failure, match="controlled transport failure"):
            await client.get("https://public.example/mcp")
