"""Model aliases must never become MCP wire or authorization authority."""

from __future__ import annotations

from functools import partial
from typing import Any

from agent.tools.base import LLMToolPluginCallSession
from common.mcp_tool_call_conn import MCPToolBinding


async def test_duplicate_model_aliases_dispatch_each_server_canonical_tool() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    class Session:
        _recent_logs: list[str] = []

        def __init__(self, server_id: str) -> None:
            self.server_id = server_id

        def tool_call(self, name: str, arguments: dict[str, Any], _timeout: float) -> str:
            calls.append((self.server_id, name, arguments))
            return self.server_id

    callbacks: list[tuple[object, ...]] = []

    def record_callback(*args: object, **_kwargs: object) -> None:
        callbacks.append(args)

    first = Session("server-a")
    second = Session("server-b")
    router = LLMToolPluginCallSession(
        {
            "search_0": MCPToolBinding(
                session=first,  # type: ignore[arg-type]
                original_name="search",
                mcp_server_id="server-a",
                resource_name="resource-a",
            ),
            "search_1": MCPToolBinding(
                session=second,  # type: ignore[arg-type]
                original_name="search",
                mcp_server_id="server-b",
                resource_name="resource-b",
            ),
        },
        partial(record_callback),
    )

    assert await router.tool_call_async("search_0", {"query": "one"}) == "server-a"
    assert await router.tool_call_async("search_1", {"query": "two"}) == "server-b"
    assert calls == [
        ("server-a", "search", {"query": "one"}),
        ("server-b", "search", {"query": "two"}),
    ]
    assert [callback[0] for callback in callbacks] == ["search_0", "search_1"]
