"""Protocol fixtures cannot authorize arbitrary private MCP destinations."""

import pytest

from common import safe_mcp_http
from tests.compat.mcp.loopback_transport import fixture_destinations


def test_fixture_scope_rejects_cross_origin_and_restores_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    denied: list[str] = []

    def original(url: str) -> tuple[str, str]:
        denied.append(url)
        raise ValueError("production destination denied")

    monkeypatch.setattr(safe_mcp_http, "assert_url_is_safe", original)
    with fixture_destinations(["http://127.0.0.1:23456/mcp"]):
        assert safe_mcp_http.assert_url_is_safe("http://127.0.0.1:23456/messages") == ("127.0.0.1", "127.0.0.1")
        for url in ["http://127.0.0.1:23457/mcp", "http://localhost:23456/mcp", "http://10.0.0.1/mcp", "http://user:password@127.0.0.1:23456/mcp"]:
            with pytest.raises(ValueError, match="production destination denied"):
                safe_mcp_http.assert_url_is_safe(url)
    assert len(denied) == 4
    assert safe_mcp_http.assert_url_is_safe is original


@pytest.mark.parametrize("url", ["http://localhost:23456/mcp", "http://10.0.0.1:23456/mcp", "https://127.0.0.1:23456/mcp", "http://127.0.0.1:0/mcp"])
def test_fixture_scope_requires_owned_literal_loopback(url: str) -> None:
    with pytest.raises(ValueError, match="exact loopback"):
        with fixture_destinations([url]):
            pytest.fail("invalid fixture destination was admitted")
