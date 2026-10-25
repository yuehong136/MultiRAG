"""Bind protocol probes to their exact owned loopback fixture origins."""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch
from urllib.parse import urlsplit


@contextmanager
def fixture_destinations(urls: list[str]) -> Iterator[None]:
    """Keep production HTTP pinning while admitting only registered fixtures."""
    from common import safe_mcp_http

    origins: set[tuple[str, str, int]] = set()
    for url in urls:
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or not parsed.port or parsed.username or parsed.password:
            raise ValueError("MCP protocol fixtures must use exact loopback URLs.")
        origins.add((parsed.scheme, parsed.hostname, parsed.port))
    original = safe_mcp_http.assert_url_is_safe

    def validate(url: str) -> tuple[str, str]:
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.hostname, parsed.port or 80) in origins and not parsed.username and not parsed.password:
            return "127.0.0.1", "127.0.0.1"
        return original(url)

    with patch.object(safe_mcp_http, "assert_url_is_safe", validate):
        yield
