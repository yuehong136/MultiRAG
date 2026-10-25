"""Request-local public destination binding for MCP HTTP transports."""

import asyncio
from typing import Any

import httpx2

from common.ssrf_guard import assert_url_is_safe


async def validate_mcp_url(url: Any) -> None:
    if not isinstance(url, str) or not url or url != url.strip():
        raise ValueError("Invalid MCP URL")
    original = httpx2.URL(url)
    if original.username or original.password:
        raise ValueError("MCP URL credentials are not allowed")
    await asyncio.to_thread(assert_url_is_safe, url)


class PublicMCPTransport(httpx2.AsyncBaseTransport):
    """Validate every hop, connect to its IP and retain Host/TLS identity.

    The SDK handles redirects against the original request URL. Each redirected
    or SSE endpoint request passes this transport again; no global DNS state or
    ambient proxy can change the validated destination.
    """

    def __init__(self) -> None:
        self._transport = httpx2.AsyncHTTPTransport(trust_env=False, retries=0)

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        original = request.url
        if original.username or original.password:
            raise ValueError("MCP URL credentials are not allowed")
        hostname, ip = await asyncio.to_thread(assert_url_is_safe, str(original))
        headers = request.headers.copy()
        headers["Host"] = original.netloc.decode("ascii")
        pinned = httpx2.Request(request.method, original.copy_with(host=ip), headers=headers, stream=request.stream, extensions={**request.extensions, "sni_hostname": hostname})
        return await self._transport.handle_async_request(pinned)

    async def aclose(self) -> None:
        await self._transport.aclose()


def create_public_mcp_http_client(headers: dict[str, str] | None = None, timeout: httpx2.Timeout | None = None, auth: httpx2.Auth | None = None) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(headers=headers, timeout=timeout or httpx2.Timeout(30, read=300), auth=auth, follow_redirects=True, trust_env=False, transport=PublicMCPTransport())
