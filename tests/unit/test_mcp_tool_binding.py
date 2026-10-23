"""Model aliases must never become MCP wire or authorization authority."""

from __future__ import annotations

from copy import deepcopy
from functools import partial
from types import SimpleNamespace
from typing import Any

import pytest

from agent.component import agent_with_tools
from agent.tools.base import LLMToolPluginCallSession
from common.constants import MCPServerType
from common.mcp_tool_call_conn import MCP_TOOL_CALL_TIMEOUT, MCPToolBinding
from mcp.types import Tool
from tests.support.agent_tool_binding import CANONICAL_TOOLS, assert_registration, delegation_service, registered_agent


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


@pytest.mark.parametrize("sdk_metadata", [False, True])
async def test_agent_constructor_preserves_colliding_tools_and_canonical_authority(monkeypatch: pytest.MonkeyPatch, sdk_metadata: bool) -> None:
    servers = {key: SimpleNamespace(id=key, tenant_id="tenant-probe", url=f"https://{key}.example/mcp", server_type=MCPServerType.STREAMABLE_HTTP, variables={}, headers={}) for key in CANONICAL_TOOLS}
    metadata: dict[str, dict[str, Any]] = {}
    for key, names in CANONICAL_TOOLS.items():
        metadata[key] = {}
        for name in names:
            meta = {
                "name": name,
                "description": f"{key}/{name}",
                "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]},
                "annotations": {"readOnlyHint": True},
                "outputSchema": {"type": "object", "properties": {"result": {"type": "string"}}},
            }
            metadata[key][name] = Tool.model_validate(meta) if sdk_metadata else meta
    before = deepcopy(metadata)
    service, issuer = delegation_service(servers)
    calls: list[tuple[str, str, dict[str, Any]]] = []
    sessions = []

    class Session:
        _recent_logs: list[str] = []

        def __init__(self, server: SimpleNamespace, _variables: Any, _headers: Any, *, credential_provider: Any, call_context: Any) -> None:
            self._mcp_server = server
            self.provider = credential_provider
            self.call_context = call_context
            self.delegated_resource_name = credential_provider.resource_name
            self.closed = False
            sessions.append(self)

        def wait_ready(self, timeout: float) -> bool:
            return True

        def tool_call(self, name: str, arguments: dict[str, Any], _timeout: float) -> str:
            assert _timeout == MCP_TOOL_CALL_TIMEOUT
            credential = self.provider.credential_for(name)
            assert credential.canonical_tool_name == name
            calls.append((self._mcp_server.id, name, arguments))
            return self._mcp_server.id

        def close_sync(self, timeout: float) -> None:
            self.closed = True

    monkeypatch.setattr(agent_with_tools, "MCPToolCallSession", Session)
    with registered_agent(servers, metadata, service) as registered:
        aliases = assert_registration(registered, metadata)
        assert set(aliases) == {("a", "compat_echo"), ("a", "compat_echo_0"), ("a", "compat_echo_2"), ("b", "compat_echo")}
        assert aliases[("a", "compat_echo_2")] == "compat_echo_2"
        for exported in registered.agent.tool_meta[1:]:
            assert "READ-ONLY" in exported["function"]["description"]
            assert "result" in exported["function"]["description"]
            assert exported["function"]["parameters"] == {"type": "object", "properties": {"value": {"type": "string"}}, "required": ["value"]}
        assert metadata == before
        assert issuer.requests == []
        for (server_id, canonical), alias in aliases.items():
            arguments = {"value": f"{server_id}:{canonical}"}
            assert await registered.agent.toolcall_session.tool_call_async(alias, arguments) == server_id
            assert calls[-1] == (server_id, canonical, arguments)
            assert registered.callback.call_args.args == ("binding-probe", alias, arguments, server_id)
            assert registered.callback.call_args.kwargs["elapsed_time"] >= 0
        with pytest.raises(AssertionError, match="does not exist"):
            await registered.agent.toolcall_session.tool_call_async("compat_fail", {})
        assert len(calls) == len(issuer.requests) == 4
        assert await registered.agent.toolcall_session.tool_call_async("compat_echo_0", {"value": "native"}) == "native-result"
        registered.native.invoke.assert_called_once_with(value="native")

    with registered_agent(servers, metadata, service, user="bob") as registered:
        aliases = assert_registration(registered, metadata)
        assert set(aliases) == {("b", "compat_echo")}
        assert await registered.agent.toolcall_session.tool_call_async(aliases[("b", "compat_echo")], {"value": "bob"}) == "b"
        assert issuer.requests[-1].principal.platform_user_id == "bob"
        assert issuer.requests[-1].resource_name == "resource-b"
    assert metadata == before
    assert all(session.closed for session in sessions)
