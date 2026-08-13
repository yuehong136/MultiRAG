"""Unit contracts for MultiRAG's MCP SDK 2 client wrapper."""

import asyncio
import inspect
import json
from contextvars import ContextVar
from types import SimpleNamespace
from typing import Any

import pytest

from common.constants import MCPServerType
from common.mcp_tool_call_conn import (
    MCPConnectionError,
    MCPToolCallSession,
    MCPToolTimeoutError,
    _extract_http_status,
    mcp_tool_metadata_to_openai_tool,
)
from mcp.types import (
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    InputRequiredResult,
    TextContent,
    Tool,
    ToolAnnotations,
)


def _bare_session(server_type: MCPServerType = MCPServerType.STREAMABLE_HTTP) -> MCPToolCallSession:
    """Build a wrapper without starting its background thread or network."""
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
    session._close = False
    session._initialized = asyncio.Event()
    session._shutdown_event = asyncio.Event()
    session._client_closed = asyncio.Event()
    session._init_error = None
    session._init_error_status = None
    session._http_auth_status = ContextVar(
        f"test_mcp_http_auth_status_{id(session)}",
        default=None,
    )
    session._client = None
    session._inflight_tasks = set()
    session._server_instructions = None
    session._server_capabilities = None
    session._protocol_version = None
    session._recent_logs = []
    session._last_tool_call_meta = None
    return session


def test_mcp_session_exposes_an_opaque_instance_local_call_context_seam() -> None:
    assert "call_context" in inspect.signature(MCPToolCallSession).parameters
    session_a = _bare_session()
    session_b = _bare_session()
    context_a = object()
    context_b = object()
    session_a._call_context = context_a
    session_b._call_context = context_b

    assert session_a.call_context is context_a
    assert session_b.call_context is context_b
    assert session_a.call_context is not session_b.call_context


@pytest.mark.parametrize(
    ("server_type", "expected_transport", "expected_mode"),
    [
        (MCPServerType.SSE, "sse", "legacy"),
        (MCPServerType.STREAMABLE_HTTP, "streamable-http", "auto"),
    ],
)
async def test_sdk2_transport_mode_and_header_wiring(
    monkeypatch: pytest.MonkeyPatch,
    server_type: MCPServerType,
    expected_transport: str,
    expected_mode: str,
) -> None:
    from common import mcp_tool_call_conn

    observed: dict[str, Any] = {}

    class FakeHTTPClient:
        def __init__(self, **kwargs: Any) -> None:
            observed["http_client_kwargs"] = kwargs
            self.event_hooks: dict[str, list[Any]] = {"request": [], "response": []}

        async def __aenter__(self) -> "FakeHTTPClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    def fake_sse(url: str, *, headers: dict[str, str]) -> object:
        observed.update(transport="sse", url=url, transport_headers=headers)
        return object()

    def fake_streamable_http(url: str, *, http_client: FakeHTTPClient) -> object:
        observed.update(transport="streamable-http", url=url, http_client=http_client)
        return object()

    class FakeClient:
        instructions = "fixture instructions"
        server_capabilities = {"tools": True}
        protocol_version = "2025-11-25" if expected_mode == "legacy" else "2026-07-28"

        def __init__(self, transport: object, **kwargs: Any) -> None:
            observed["client_transport"] = transport
            observed["client_kwargs"] = kwargs

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            observed["client_exited"] = True

    monkeypatch.setattr(mcp_tool_call_conn.httpx2, "AsyncClient", FakeHTTPClient)
    monkeypatch.setattr(mcp_tool_call_conn, "sse_client", fake_sse)
    monkeypatch.setattr(mcp_tool_call_conn, "streamable_http_client", fake_streamable_http)
    monkeypatch.setattr(mcp_tool_call_conn, "Client", FakeClient)

    session = _bare_session(server_type)
    session._shutdown_event.set()
    await session._mcp_server_loop()

    expected_headers = {
        "Authorization": "Bearer fixture-token",
        "X-Tenant": "tenant-1",
        "X-Request-ID": "request-1",
    }
    assert observed["transport"] == expected_transport
    assert observed["url"] == "http://127.0.0.1:8765/mcp"
    assert observed["client_kwargs"]["mode"] == expected_mode
    assert "initialize" not in observed
    if server_type == MCPServerType.SSE:
        assert observed["transport_headers"] == expected_headers
        assert "http_client_kwargs" not in observed
    else:
        assert observed["http_client_kwargs"]["headers"] == expected_headers
        assert observed["http_client_kwargs"]["follow_redirects"] is True
        transport_timeout = observed["http_client_kwargs"]["timeout"]
        assert transport_timeout.connect == 30
        assert transport_timeout.write == 30
        assert transport_timeout.pool == 30
        assert transport_timeout.read == 300
        assert observed["http_client"].event_hooks["response"] == [session._on_http_response]
    assert session.server_instructions == "fixture instructions"
    assert session.server_capabilities == {"tools": True}
    assert session.protocol_version == FakeClient.protocol_version
    assert session._client_closed.is_set()
    assert observed["client_exited"] is True

    session._server_variables["token"] = ""
    assert "Authorization" not in session._build_headers()


async def test_init_timeout_does_not_limit_connected_session_lifetime(monkeypatch: pytest.MonkeyPatch) -> None:
    from common import mcp_tool_call_conn

    class FakeHTTPClient:
        def __init__(self) -> None:
            self.event_hooks: dict[str, list[Any]] = {"request": [], "response": []}

        async def __aenter__(self) -> "FakeHTTPClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class FakeClient:
        instructions = None
        server_capabilities = {}
        protocol_version = "2026-07-28"

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> "FakeClient":
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(mcp_tool_call_conn, "MCP_INIT_TIMEOUT", 0.01)
    monkeypatch.setattr(mcp_tool_call_conn.httpx2, "AsyncClient", lambda **_kwargs: FakeHTTPClient())
    monkeypatch.setattr(mcp_tool_call_conn, "streamable_http_client", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(mcp_tool_call_conn, "Client", FakeClient)

    session = _bare_session()
    runner = asyncio.create_task(session._mcp_server_loop())
    await asyncio.wait_for(session._initialized.wait(), timeout=0.2)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(asyncio.shield(runner), timeout=0.03)
    assert session.is_ready()

    session._shutdown_event.set()
    await asyncio.wait_for(runner, timeout=0.2)


@pytest.mark.parametrize("status", [401, 403])
async def test_http_auth_failures_are_typed_after_sdk_normalizes_error(status: int) -> None:
    session = _bare_session()

    class FakeWireSession:
        async def call_tool(self, *_args: object, **_kwargs: object) -> None:
            await session._on_http_response(SimpleNamespace(status_code=status))  # type: ignore[arg-type]
            raise RuntimeError("SDK normalized transport error")

    session._client = SimpleNamespace(session=FakeWireSession())
    session._initialized.set()

    nested = ExceptionGroup("transport", [RuntimeError(f"HTTP status {status}")])
    assert _extract_http_status(nested) == status
    with pytest.raises(MCPConnectionError) as raised:
        await session._call_mcp_server("tool_call", name="denied", arguments={})
    assert raised.value.status_code == status


async def test_http_auth_status_is_isolated_between_concurrent_calls() -> None:
    session = _bare_session()
    both_started = asyncio.Event()
    started: set[str] = set()

    class FakeWireSession:
        async def call_tool(self, name: str, *_args: object, **_kwargs: object) -> None:
            status = 401 if name == "unauthenticated" else 403
            await session._on_http_response(SimpleNamespace(status_code=status))  # type: ignore[arg-type]
            started.add(name)
            if len(started) == 2:
                both_started.set()
            await both_started.wait()
            raise RuntimeError("SDK normalized transport error")

    session._client = SimpleNamespace(session=FakeWireSession())
    session._initialized.set()

    results = await asyncio.gather(
        session._call_mcp_server("tool_call", name="unauthenticated", arguments={}),
        session._call_mcp_server("tool_call", name="forbidden", arguments={}),
        return_exceptions=True,
    )

    assert [result.status_code for result in results if isinstance(result, MCPConnectionError)] == [401, 403]


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "Authentication required"),
        (403, "Permission denied"),
    ],
)
def test_sync_tool_call_preserves_auth_category(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    message: str,
) -> None:
    from common import mcp_tool_call_conn

    class FailedFuture:
        def result(self, *, timeout: float | int) -> str:
            del timeout
            raise MCPConnectionError("normalized error", status_code=status)

    def fake_schedule(coroutine: Any, _loop: object) -> FailedFuture:
        coroutine.close()
        return FailedFuture()

    session = _bare_session()
    session._event_loop = object()  # type: ignore[assignment]
    monkeypatch.setattr(mcp_tool_call_conn.asyncio, "run_coroutine_threadsafe", fake_schedule)

    assert message in session.tool_call("denied", {})
    assert session.get_last_tool_call_meta()["connection_status"] == status  # type: ignore[index]


async def test_call_tool_result_uses_sdk2_snake_case(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CallToolResult(
        content=[TextContent(text="fallback")],
        structuredContent={"answer": 42},
        isError=False,
        _meta={"cached": True},
    )

    async def fake_call_server(*_args: object, **_kwargs: object) -> CallToolResult:
        return result

    session = _bare_session()
    monkeypatch.setattr(session, "_call_mcp_server", fake_call_server)

    text = await session._call_mcp_tool("structured", {"value": 1})

    assert json.loads(text.split("\n(meta:", maxsplit=1)[0]) == {"answer": 42}
    meta = session.get_last_tool_call_meta()
    assert meta is not None
    assert meta["structured_content"] == {"answer": 42}
    assert meta["meta"] == {"cached": True}
    assert meta["is_error"] is False


async def test_tool_error_remains_text_with_side_channel_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    result = CallToolResult(
        isError=True,
        content=[TextContent(text="fixture denied")],
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


async def test_input_required_is_surfaced_once_and_json_serializable(monkeypatch: pytest.MonkeyPatch) -> None:
    request = ElicitRequest(
        params=ElicitRequestFormParams(
            message="Need leave dates",
            requestedSchema={
                "type": "object",
                "properties": {"start": {"type": "string"}},
            },
        )
    )
    result = InputRequiredResult(
        inputRequests={"leave-form": request},
        requestState="opaque-state",
        _meta={"trace": "trace-1"},
    )
    call_count = 0

    async def fake_call_server(*_args: object, **_kwargs: object) -> InputRequiredResult:
        nonlocal call_count
        call_count += 1
        return result

    session = _bare_session()
    monkeypatch.setattr(session, "_call_mcp_server", fake_call_server)

    text = await session._call_mcp_tool("prepare_leave", {"days": 1})
    payload = json.loads(text)
    meta = session.get_last_tool_call_meta()

    assert call_count == 1
    assert payload["interaction_required"] is True
    assert payload["input_required"]["resultType"] == "input_required"
    assert payload["input_required"]["requestState"] == "opaque-state"
    assert meta is not None and meta["interaction_required"] is True
    assert meta["request_state"] == "opaque-state"
    assert meta["input_requests"]["leave-form"]["method"] == "elicitation/create"
    json.dumps(meta)


async def test_concurrent_calls_do_not_head_of_line_block_and_timeout_cancels() -> None:
    slow_started = asyncio.Event()
    slow_cancelled = asyncio.Event()
    never_release = asyncio.Event()
    observed: list[tuple[str, dict[str, Any]]] = []

    class FakeWireSession:
        async def call_tool(self, name: str, _arguments: dict[str, Any], **kwargs: Any) -> str:
            observed.append((name, kwargs))
            if name == "slow":
                slow_started.set()
                try:
                    await never_release.wait()
                except asyncio.CancelledError:
                    slow_cancelled.set()
                    raise
            return "fast-completed"

    session = _bare_session()
    session._client = SimpleNamespace(session=FakeWireSession())
    session._initialized.set()

    slow_call = asyncio.create_task(session._call_mcp_server("tool_call", request_timeout=0.2, name="slow", arguments={}))
    await asyncio.wait_for(slow_started.wait(), timeout=0.2)
    fast_result = await asyncio.wait_for(
        session._call_mcp_server("tool_call", request_timeout=0.1, name="fast", arguments={}),
        timeout=0.2,
    )

    assert fast_result == "fast-completed"
    assert not slow_call.done()
    with pytest.raises(MCPToolTimeoutError, match="timed out"):
        await slow_call
    await asyncio.wait_for(slow_cancelled.wait(), timeout=0.2)
    assert session._inflight_tasks == set()
    assert all(kwargs["allow_input_required"] is True for _, kwargs in observed)


async def test_close_cancels_inflight_and_waits_for_transport_exit() -> None:
    call_started = asyncio.Event()
    call_cancelled = asyncio.Event()
    never_release = asyncio.Event()

    class FakeWireSession:
        async def call_tool(self, *_args: object, **_kwargs: object) -> None:
            call_started.set()
            try:
                await never_release.wait()
            except asyncio.CancelledError:
                call_cancelled.set()
                raise

    session = _bare_session()
    session._client = SimpleNamespace(session=FakeWireSession())
    session._initialized.set()

    async def transport_exit() -> None:
        await session._shutdown_event.wait()
        session._client_closed.set()

    exit_task = asyncio.create_task(transport_exit())
    call_task = asyncio.create_task(session._call_mcp_server("tool_call", name="slow", arguments={}))
    await asyncio.wait_for(call_started.wait(), timeout=0.2)

    await asyncio.wait_for(session._close_on_event_loop(timeout=0.2), timeout=0.3)
    await exit_task

    assert call_task.cancelled()
    assert call_cancelled.is_set()
    assert session._shutdown_event.is_set()
    assert session._client_closed.is_set()
    assert session._inflight_tasks == set()


async def test_close_is_bounded_when_inflight_call_ignores_cancellation() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    never_release = asyncio.Event()

    async def stubborn_call() -> None:
        started.set()
        try:
            await never_release.wait()
        except asyncio.CancelledError:
            await release.wait()

    session = _bare_session()
    session._initialized.set()
    session._client_closed.set()
    task = asyncio.create_task(stubborn_call())
    session._inflight_tasks.add(task)
    task.add_done_callback(session._inflight_tasks.discard)
    await started.wait()

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    await session._close_on_event_loop(timeout=0.01)

    assert loop.time() - started_at < 0.1
    assert not task.done()
    release.set()
    await asyncio.wait_for(task, timeout=0.1)


async def test_async_close_is_bounded_when_owner_loop_does_not_respond(monkeypatch: pytest.MonkeyPatch) -> None:
    from concurrent.futures import Future

    from common import mcp_tool_call_conn

    session = _bare_session()
    session._event_loop = SimpleNamespace(is_running=lambda: True)  # type: ignore[assignment]
    pending_close: Future[None] = Future()
    finalized_with: list[float] = []

    def fake_schedule(coroutine: Any, _loop: object) -> Future[None]:
        coroutine.close()
        return pending_close

    monkeypatch.setattr(mcp_tool_call_conn.asyncio, "run_coroutine_threadsafe", fake_schedule)
    monkeypatch.setattr(session, "_finalize_owner_thread", finalized_with.append)

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    await session.close(timeout=0.01)

    assert loop.time() - started_at < 0.1
    assert session._close is True
    assert pending_close.cancelled()
    assert len(finalized_with) == 1
    assert 0 <= finalized_with[0] <= 0.01


def test_finalize_stops_owner_loop_before_run_forever_starts() -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    session = _bare_session()
    session._event_loop = asyncio.new_event_loop()
    session._thread_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mcp-delayed-loop")
    start_gate = threading.Event()

    def delayed_run_forever() -> None:
        start_gate.wait()
        session._event_loop.run_forever()

    session._loop_thread_future = session._thread_pool.submit(delayed_run_forever)
    gate_timer = threading.Timer(0.02, start_gate.set)
    gate_timer.start()
    try:
        session._finalize_owner_thread(timeout=0.5)

        assert session._loop_thread_future.done()
        assert session._event_loop.is_closed()
    finally:
        gate_timer.cancel()
        start_gate.set()
        if not session._event_loop.is_closed():
            session._event_loop.call_soon_threadsafe(session._event_loop.stop)
        try:
            session._loop_thread_future.result(timeout=0.5)
        finally:
            if not session._event_loop.is_closed():
                session._event_loop.close()
            session._thread_pool.shutdown(wait=True, cancel_futures=True)


def test_constructor_waits_for_owner_loop_before_immediate_close(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from common import mcp_tool_call_conn

    start_gate = threading.Event()
    real_executor = mcp_tool_call_conn.ThreadPoolExecutor

    class GatedThreadPoolExecutor(real_executor):  # type: ignore[misc,valid-type]
        def submit(self, function: Any, /, *args: Any, **kwargs: Any) -> Any:
            def delayed_call() -> Any:
                start_gate.wait()
                return function(*args, **kwargs)

            return super().submit(delayed_call)

    async def fake_server_loop(self: MCPToolCallSession) -> None:
        self._initialized.set()
        try:
            await self._shutdown_event.wait()
        finally:
            self._client_closed.set()

    monkeypatch.setattr(mcp_tool_call_conn, "ThreadPoolExecutor", GatedThreadPoolExecutor)
    monkeypatch.setattr(MCPToolCallSession, "_mcp_server_loop", fake_server_loop)
    gate_timer = threading.Timer(0.02, start_gate.set)
    gate_timer.start()
    session: MCPToolCallSession | None = None
    try:
        session = MCPToolCallSession(
            SimpleNamespace(
                id="immediate-close-server",
                url="http://127.0.0.1:8765/mcp",
                server_type=MCPServerType.STREAMABLE_HTTP,
                headers={},
            )
        )

        assert session._owner_loop_started.is_set()
        assert session._event_loop.is_running()
        session.close_sync(timeout=1)
        assert session._runner_future.done()
        assert session._loop_thread_future.done()
        assert session._event_loop.is_closed()
    finally:
        gate_timer.cancel()
        start_gate.set()
        if session is not None and not session._event_loop.is_closed():
            session.close_sync(timeout=1)


def test_close_sync_stops_owner_loop_and_executor(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_server_loop(self: MCPToolCallSession) -> None:
        self._client = SimpleNamespace()
        self._protocol_version = "2026-07-28"
        self._initialized.set()
        try:
            await self._shutdown_event.wait()
        finally:
            self._client = None
            self._client_closed.set()

    monkeypatch.setattr(MCPToolCallSession, "_mcp_server_loop", fake_server_loop)
    session = MCPToolCallSession(
        SimpleNamespace(
            id="lifecycle-server",
            url="http://127.0.0.1:8765/mcp",
            server_type=MCPServerType.STREAMABLE_HTTP,
            headers={},
        )
    )

    assert session.wait_ready(timeout=1)
    session.close_sync(timeout=1)

    assert session._runner_future.done()
    assert session._loop_thread_future.done()
    assert not session._event_loop.is_running()
    assert session._event_loop.is_closed()
    assert session not in MCPToolCallSession._ALL_INSTANCES


def test_tool_metadata_uses_sdk2_snake_case_attributes() -> None:
    tool = Tool(
        name="preview_leave",
        description="Preview a leave request",
        inputSchema={"type": "object", "properties": {}},
        outputSchema={"type": "object", "properties": {"days": {"type": "integer"}}},
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )

    converted = mcp_tool_metadata_to_openai_tool(tool)

    assert converted["function"]["parameters"] == tool.input_schema
    assert "READ-ONLY" in converted["function"]["description"]
    assert "idempotent" in converted["function"]["description"]
    assert "days(integer)" in converted["function"]["description"]
