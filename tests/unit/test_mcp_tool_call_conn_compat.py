"""Characterize the legacy MCP client before the SDK v2 migration.

EIM-F2 intentionally keeps the production dependency graph unchanged.  These
tests pin the observable MCP SDK 1 behavior that the modern-client migration
must either preserve or deliberately replace.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from types import SimpleNamespace
from typing import Any

import pytest

from common.constants import MCPServerType
from common.mcp_tool_call_conn import MCPToolCallSession, MCPToolTimeoutError
from mcp.types import CallToolResult, TextContent


def _bare_session(server_type: MCPServerType = MCPServerType.STREAMABLE_HTTP) -> MCPToolCallSession:
    session = object.__new__(MCPToolCallSession)
    session._custom_header = {"X-Request-ID": "request-1"}
    session._mcp_server = SimpleNamespace(
        id="compat-server",
        url=" http://127.0.0.1:8765/mcp ",
        server_type=server_type,
        headers={
            "Authorization": "Bearer ${token}",
            "X-Tenant": "${tenant}",
        },
    )
    session._server_variables = {"token": "fixture-token", "tenant": "tenant-1"}
    session._queue = asyncio.Queue()
    session._close = False
    session._initialized = asyncio.Event()
    session._init_error = None
    session._server_instructions = None
    session._server_capabilities = None
    session._recent_logs = []
    session._last_tool_call_meta = None
    return session


@pytest.mark.parametrize(
    ("server_type", "expected_transport"),
    [
        (MCPServerType.SSE, "sse"),
        (MCPServerType.STREAMABLE_HTTP, "streamable-http"),
    ],
)
async def test_legacy_transports_build_headers_and_manually_initialize(
    monkeypatch: pytest.MonkeyPatch,
    server_type: MCPServerType,
    expected_transport: str,
) -> None:
    from common import mcp_tool_call_conn

    observed: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_sse(url: str, headers: dict[str, str]) -> AsyncIterator[tuple[str, str]]:
        observed.update(transport="sse", url=url, headers=headers)
        yield "sse-read", "sse-write"

    @asynccontextmanager
    async def fake_streamable_http(url: str, headers: dict[str, str]) -> AsyncIterator[tuple[str, str, None]]:
        observed.update(transport="streamable-http", url=url, headers=headers)
        yield "http-read", "http-write", None

    class FakeClientSession:
        def __init__(self, read_stream: str, write_stream: str, *, logging_callback: Any) -> None:
            observed["streams"] = (read_stream, write_stream)
            observed["logging_callback"] = logging_callback

        async def __aenter__(self) -> "FakeClientSession":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def initialize(self) -> SimpleNamespace:
            observed["initialize_calls"] = observed.get("initialize_calls", 0) + 1
            return SimpleNamespace(instructions="fixture instructions", capabilities={"tools": True})

    async def fake_process(
        self: MCPToolCallSession,
        client_session: FakeClientSession | None,
        error_message: str | None = None,
    ) -> None:
        observed["processed"] = (client_session, error_message)

    monkeypatch.setattr(mcp_tool_call_conn, "sse_client", fake_sse)
    monkeypatch.setattr(mcp_tool_call_conn, "streamablehttp_client", fake_streamable_http)
    monkeypatch.setattr(mcp_tool_call_conn, "ClientSession", FakeClientSession)
    monkeypatch.setattr(MCPToolCallSession, "_process_mcp_tasks", fake_process)

    session = _bare_session(server_type)
    await session._mcp_server_loop()

    assert observed["transport"] == expected_transport
    assert observed["url"] == "http://127.0.0.1:8765/mcp"
    assert observed["headers"] == {
        "Authorization": "Bearer fixture-token",
        "X-Tenant": "tenant-1",
        "X-Request-ID": "request-1",
    }
    assert observed["initialize_calls"] == 1
    assert observed["processed"][1] is None
    assert session._initialized.is_set()
    assert session.server_instructions == "fixture instructions"
    assert session.server_capabilities == {"tools": True}


@pytest.mark.parametrize("status", [401, 403])
async def test_http_auth_failures_are_only_preserved_in_connection_error_text(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    from common import mcp_tool_call_conn

    observed: dict[str, str | None] = {}

    @asynccontextmanager
    async def failing_transport(_url: str, _headers: dict[str, str]) -> AsyncIterator[tuple[str, str, None]]:
        raise RuntimeError(f"HTTP {status}")
        yield "unreachable", "unreachable", None

    async def fake_process(
        self: MCPToolCallSession,
        client_session: None,
        error_message: str | None = None,
    ) -> None:
        observed["error"] = error_message

    monkeypatch.setattr(mcp_tool_call_conn, "streamablehttp_client", failing_transport)
    monkeypatch.setattr(MCPToolCallSession, "_process_mcp_tasks", fake_process)

    session = _bare_session()
    await session._mcp_server_loop()

    assert session._initialized.is_set()
    assert session._init_error == f"Connection failed for server compat-server: HTTP {status}"
    assert observed["error"] == session._init_error
    assert not hasattr(session, "_auth_error_status")


async def test_tool_error_remains_text_with_side_channel_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CallToolResult(
        isError=True,
        content=[TextContent(type="text", text="fixture denied")],
        structuredContent={"code": "fixture_denied"},
    )

    async def fake_call_server(*_args: object, **_kwargs: object) -> CallToolResult:
        return result

    session = _bare_session()
    monkeypatch.setattr(session, "_call_mcp_server", fake_call_server)

    text = await session._call_mcp_tool("compat_fail", {"value": 1})

    assert text.startswith("MCP server error:")
    assert "fixture denied" in text
    assert session.get_last_tool_call_meta() == {
        "tool_name": "compat_fail",
        "arguments": {"value": 1},
        "text": f"MCP server error: {result.content}",
        "structured_content": {"code": "fixture_denied"},
        "meta": None,
        "server_logs": [],
        "is_error": True,
    }


async def test_timeout_stops_waiting_but_does_not_cancel_the_serial_worker() -> None:
    slow_started = asyncio.Event()
    release_slow = asyncio.Event()
    fast_started = asyncio.Event()
    fast_queued = asyncio.Event()

    class ObservedQueue(asyncio.Queue[Any]):
        async def put(self, item: Any) -> None:
            await super().put(item)
            if item[1].get("name") == "fast":
                fast_queued.set()

    class FakeClientSession:
        async def call_tool(self, *, name: str, **_kwargs: object) -> str:
            if name == "slow":
                slow_started.set()
                await release_slow.wait()
                return "slow-completed"
            fast_started.set()
            return "fast-completed"

    session = _bare_session()
    session._queue = ObservedQueue()
    processor = asyncio.create_task(session._process_mcp_tasks(FakeClientSession()))  # type: ignore[arg-type]
    slow_call = asyncio.create_task(session._call_mcp_server("tool_call", request_timeout=0.05, name="slow", arguments={}))

    await asyncio.wait_for(slow_started.wait(), timeout=0.5)
    with pytest.raises(MCPToolTimeoutError, match="timed out"):
        await slow_call

    fast_call = asyncio.create_task(session._call_mcp_server("tool_call", request_timeout=0.5, name="fast", arguments={}))

    await asyncio.wait_for(fast_queued.wait(), timeout=0.5)
    assert session._queue.qsize() == 1
    assert not fast_started.is_set()

    release_slow.set()
    assert await asyncio.wait_for(fast_call, timeout=0.5) == "fast-completed"
    assert fast_started.is_set()

    processor.cancel()
    with suppress(asyncio.CancelledError):
        await asyncio.wait_for(processor, timeout=0.5)


async def test_cancelling_task_processor_exits_within_a_bounded_time() -> None:
    session = _bare_session()
    processor = asyncio.create_task(session._process_mcp_tasks(None, "fixture shutdown"))
    await asyncio.sleep(0)

    processor.cancel()
    with suppress(asyncio.CancelledError):
        await asyncio.wait_for(processor, timeout=0.5)

    assert processor.done()
