"""Browser HTTP relay: validate and pin each hop before any outbound request."""

import asyncio
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

from common.ssrf_guard import assert_url_is_safe, pin_dns


class SafeCrawlNetwork:
    """Fulfill browser requests through guarded Python HTTP, never route.continue."""

    def __init__(self, pins: dict[str, str]) -> None:
        self.pins = dict(pins)
        self.blocked = False

    def fetch(self, url: str, method: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
        for hop in range(11):
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError("Disallowed browser request URL.")
            hostname = parsed.hostname
            if hostname not in self.pins:
                _, self.pins[hostname] = assert_url_is_safe(url)
            with requests.Session() as session:
                # Ambient proxies would resolve the hostname independently of
                # the checked/pinned address and defeat the transport boundary.
                session.trust_env = False
                with pin_dns(hostname, self.pins[hostname]):
                    response = session.request(method, url, headers=headers, timeout=10, allow_redirects=False)
                with response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("Location")
                        if not location or hop == 10:
                            raise ValueError("Invalid or excessive browser redirect.")
                        url = urljoin(url, location)
                        continue
                    # requests decompresses the body; don't forward stale wire
                    # lengths/encoding to Playwright's fulfilled response.
                    result_headers = {key: value for key, value in response.headers.items() if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}}
                    return response.status_code, result_headers, response.content
        raise ValueError("Excessive browser redirect.")

    async def intercept(self, route: Any) -> None:
        request = route.request
        if request.method not in {"GET", "HEAD"}:
            await route.abort()
            return
        headers = {key: value for key, value in request.headers.items() if key.lower() in {"accept", "accept-language", "user-agent"}}
        try:
            status, response_headers, binary = await asyncio.to_thread(self.fetch, request.url, request.method, headers)
        except (ValueError, requests.RequestException):
            self.blocked = True
            await route.abort()
            return
        await route.fulfill(status=status, headers=response_headers, body=binary)

    async def install(self, page: Any, *, context: Any, **_kwargs: Any) -> Any:
        await context.route("**/*", self.intercept)

        async def close_socket(websocket: Any) -> None:
            # No connect_to_server call: the peer is never contacted.
            await websocket.close()

        await context.route_web_socket("**/*", close_socket)
        return page
