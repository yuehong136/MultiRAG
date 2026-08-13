import asyncio
import json
import logging
import os
import re
import threading
import time
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from contextlib import AsyncExitStack
from contextvars import ContextVar
from dataclasses import dataclass
from string import Template
from typing import Any, Protocol, cast, override

import httpx2

from mcp.client import Client, Transport
from mcp.client.sse import sse_client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from mcp.types import CallToolResult, ImageContent, InputRequiredResult, ListToolsResult, TextContent, Tool

# MCP 服务器初始化超时时间（秒），可通过环境变量配置
MCP_INIT_TIMEOUT = int(os.environ.get("MCP_INIT_TIMEOUT", 15))
# MCP 工具调用超时时间（秒），可通过环境变量配置
MCP_TOOL_CALL_TIMEOUT = int(os.environ.get("MCP_TOOL_CALL_TIMEOUT", 60))
# Owner loop startup is a local thread handoff, not a network operation. Keep a
# fixed bound so constructor/immediate-close cannot race a not-yet-running loop.
MCP_OWNER_LOOP_START_TIMEOUT = 5.0
# 是否将服务端日志拼接到工具结果字符串末尾。
# 默认关闭，避免污染 LLM 的 tool observation；保留环境变量开关兼容旧行为。
MCP_APPEND_SERVER_LOGS_TO_RESULT = os.environ.get("MCP_APPEND_SERVER_LOGS_TO_RESULT", "false").lower() in {"1", "true", "yes", "on"}

from common.constants import MCPServerType

# MCP 日志级别 → Python logging 级别映射（Phase 4）
_MCP_LEVEL_MAP: dict[str, int] = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "notice": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
    "alert": logging.CRITICAL,
    "emergency": logging.CRITICAL,
}


class MCPToolTimeoutError(Exception):
    """MCP 工具调用内部超时异常。

    与 asyncio.TimeoutError / concurrent.futures.TimeoutError 区分开，
    避免 Python 3.12+ 中两者是同一个类 (builtins.TimeoutError) 导致
    except 分支错误捕获的问题。
    """


class MCPConnectionError(Exception):
    """MCP transport or negotiation failure with an optional HTTP status."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(slots=True)
class _HTTPAuthStatus:
    """Mutable per-call status shared with transport child tasks."""

    status_code: int | None = None


def _extract_http_status(error: BaseException) -> int | None:
    """Find an HTTP status in nested SDK/httpx exception groups."""
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status

    direct_status = getattr(error, "status_code", None)
    if isinstance(direct_status, int):
        return direct_status

    if isinstance(error, BaseExceptionGroup):
        for nested in error.exceptions:
            if nested_status := _extract_http_status(nested):
                return nested_status

    match = re.search(r"(?:HTTP(?: status)?[ =:]*)\b(401|403)\b", str(error), flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


class ToolCallSession(Protocol):
    def tool_call(self, name: str, arguments: dict[str, Any]) -> str: ...


class MCPToolCallSession(ToolCallSession):
    _ALL_INSTANCES: weakref.WeakSet["MCPToolCallSession"] = weakref.WeakSet()

    def __init__(
        self,
        mcp_server: Any,
        server_variables: dict[str, Any] | None = None,
        custom_header: dict[str, str] | None = None,
        *,
        call_context: object | None = None,
    ) -> None:
        self.__class__._ALL_INSTANCES.add(self)

        self._call_context = call_context
        self._custom_header = custom_header
        self._mcp_server = mcp_server
        self._server_variables = server_variables or {}
        self._close = False
        self._initialized = asyncio.Event()
        self._shutdown_event = asyncio.Event()
        self._client_closed = asyncio.Event()
        self._init_error: str | None = None
        self._init_error_status: int | None = None
        self._http_auth_status: ContextVar[_HTTPAuthStatus | None] = ContextVar(
            f"mcp_http_auth_status_{id(self)}",
            default=None,
        )
        self._client: Client | None = None
        self._inflight_tasks: set[asyncio.Task[Any]] = set()

        # SDK 2 negotiation metadata: discover for modern, initialize for legacy.
        self._server_instructions: str | None = None
        self._server_capabilities: Any = None
        self._protocol_version: str | None = None

        # Phase 4: Accumulator for MCP logging/progress notifications during tool calls
        self._recent_logs: list[str] = []
        # 最近一次工具调用的旁路元数据，供 API / SSE / 调试界面按需读取
        self._last_tool_call_meta: dict[str, Any] | None = None

        self._event_loop = asyncio.new_event_loop()
        self._thread_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="multirag-mcp")
        self._owner_loop_started = threading.Event()
        self._loop_thread_future = self._thread_pool.submit(self._event_loop.run_forever)
        self._event_loop.call_soon_threadsafe(self._owner_loop_started.set)
        if not self._owner_loop_started.wait(timeout=MCP_OWNER_LOOP_START_TIMEOUT):
            self._finalize_owner_thread(timeout=1)
            raise RuntimeError(f"MCP owner event loop did not start for server {self._mcp_server.id}")

        self._runner_future = asyncio.run_coroutine_threadsafe(self._mcp_server_loop(), self._event_loop)

    def _build_headers(self) -> dict[str, str]:
        raw_headers: dict[str, str] = self._mcp_server.headers or {}
        custom_headers = self._custom_header or {}
        headers: dict[str, str] = {}

        for name, value in raw_headers.items():
            rendered_name = Template(name).safe_substitute(self._server_variables)
            rendered_value = Template(value).safe_substitute(self._server_variables)
            normalized_value = rendered_value.strip()
            if rendered_name.strip() and normalized_value and normalized_value.casefold() != "bearer":
                headers[rendered_name] = rendered_value

        for name, value in custom_headers.items():
            rendered_name = Template(name).safe_substitute(custom_headers)
            rendered_value = Template(value).safe_substitute(custom_headers)
            if rendered_name.strip() and rendered_value.strip():
                headers[rendered_name] = rendered_value
        return headers

    async def _on_http_response(self, response: httpx2.Response) -> None:
        """Capture authorization status before the SDK normalizes its error."""
        if response.status_code in {401, 403}:
            status = self._http_auth_status.get()
            if status is not None:
                status.status_code = response.status_code

    async def _mcp_server_loop(self) -> None:
        url = self._mcp_server.url.strip()
        headers = self._build_headers()
        auth_status = _HTTPAuthStatus()
        auth_status_token = self._http_auth_status.set(auth_status)

        try:
            async with AsyncExitStack() as stack:
                if self._mcp_server.server_type == MCPServerType.SSE:
                    transport = cast(Transport, sse_client(url, headers=headers))
                    mode = "legacy"
                elif self._mcp_server.server_type == MCPServerType.STREAMABLE_HTTP:
                    owned_http_client = create_mcp_http_client(headers=headers)
                    owned_http_client.event_hooks["response"].append(self._on_http_response)
                    http_client = await stack.enter_async_context(owned_http_client)
                    transport = cast(Transport, streamable_http_client(url, http_client=http_client))
                    mode = "auto"
                else:
                    raise ValueError(f"Unsupported MCP server type: {self._mcp_server.server_type}, id: {self._mcp_server.id}")

                async with asyncio.timeout(MCP_INIT_TIMEOUT):
                    client = await stack.enter_async_context(
                        Client(
                            transport,
                            mode=mode,
                            logging_callback=self._on_logging,
                            read_timeout_seconds=MCP_TOOL_CALL_TIMEOUT,
                        )
                    )
                self._client = client
                self._save_client_state(client)
                self._initialized.set()
                logging.info(
                    "MCP client connected to server %s with protocol %s",
                    self._mcp_server.id,
                    self._protocol_version,
                )
                await self._shutdown_event.wait()
        except TimeoutError:
            message = f"Timeout connecting to MCP server {self._mcp_server.id} (timeout={MCP_INIT_TIMEOUT}s)"
            logging.error(message)
            self._init_error = message
        except asyncio.CancelledError:
            logging.info("MCP client loop cancelled for server %s", self._mcp_server.id)
            raise
        except Exception as error:
            self._init_error_status = _extract_http_status(error) or auth_status.status_code
            message = f"Connection failed for server {self._mcp_server.id}: {error}"
            logging.exception(message)
            self._init_error = message
        finally:
            self._http_auth_status.reset(auth_status_token)
            self._client = None
            self._initialized.set()
            self._client_closed.set()

    def _save_client_state(self, client: Client) -> None:
        self._server_instructions = client.instructions
        self._server_capabilities = client.server_capabilities
        self._protocol_version = client.protocol_version
        if self._server_instructions:
            logging.info(
                "MCP server %s provides instructions (%s chars)",
                self._mcp_server.id,
                len(self._server_instructions),
            )

    async def _on_logging(self, params: Any) -> None:
        """Phase 4: MCP logging notification callback.

        当服务端通过 ctx.info() / ctx.log() 发送日志通知时触发。
        日志同时写入 Python logging 和 _recent_logs 列表，
        后者在 _call_mcp_tool 中附加到工具返回结果。

        如果服务端不发送日志通知，此回调永远不会被调用（零开销）。
        """
        level = getattr(params, "level", "info")
        level_str = getattr(level, "value", str(level))
        data = getattr(params, "data", "")
        py_level = _MCP_LEVEL_MAP.get(level_str, logging.INFO)
        logging.log(py_level, f"[MCP:{self._mcp_server.id}] {data}")
        self._recent_logs.append(f"[{level_str}] {data}")
        del self._recent_logs[:-100]

    async def _call_mcp_server(
        self,
        task_type: str,
        request_timeout: float | int = MCP_TOOL_CALL_TIMEOUT,
        **kwargs: Any,
    ) -> Any:
        if self._close:
            raise ValueError("Session is closed")

        if not await self._wait_initialized(timeout=MCP_INIT_TIMEOUT + 5):
            raise MCPConnectionError(
                self._init_error or f"MCP server {self._mcp_server.id} did not become ready",
                status_code=self._init_error_status,
            )

        client = self._client
        if client is None:
            raise MCPConnectionError(
                self._init_error or f"MCP server {self._mcp_server.id} connection is closed",
                status_code=self._init_error_status,
            )

        current_task = asyncio.current_task()
        if current_task is not None:
            self._inflight_tasks.add(current_task)

        auth_status = _HTTPAuthStatus()
        auth_status_token = self._http_auth_status.set(auth_status)
        try:
            async with asyncio.timeout(request_timeout):
                if task_type == "list_tools":
                    return await client.list_tools(cache_mode="refresh")
                if task_type == "tool_call":
                    return await client.session.call_tool(
                        kwargs["name"],
                        kwargs.get("arguments"),
                        read_timeout_seconds=request_timeout,
                        progress_callback=kwargs.get("progress_callback"),
                        input_responses=kwargs.get("input_responses"),
                        request_state=kwargs.get("request_state"),
                        allow_input_required=True,
                    )
                raise ValueError(f"Unknown MCP task {task_type}")
        except TimeoutError:
            raise MCPToolTimeoutError(f"MCP request '{task_type}' timed out after {request_timeout}s") from None
        except Exception as error:
            status_code = _extract_http_status(error) or auth_status.status_code
            if status_code in {401, 403}:
                raise MCPConnectionError(str(error), status_code=status_code) from error
            raise
        finally:
            self._http_auth_status.reset(auth_status_token)
            if current_task is not None:
                self._inflight_tasks.discard(current_task)

    async def _call_mcp_tool(self, name: str, arguments: dict[str, Any], request_timeout: float | int = MCP_TOOL_CALL_TIMEOUT) -> str:
        self._last_tool_call_meta = None
        # Progress notifications are request-scoped. Generic server logging has
        # no request correlation and stays only in the bounded connection log.
        call_logs: list[str] = []

        # Phase 4: 定义进度回调，将服务端 report_progress 收集到本次调用
        # 如果服务端不调用 report_progress，此回调不会被触发（零开销）
        async def _on_progress(progress: float, total: float | None, message: str | None) -> None:
            total_str = f"/{total}" if total is not None else ""
            msg = f" {message}" if message else ""
            call_logs.append(f"[progress {progress}{total_str}]{msg}")

        result: CallToolResult | InputRequiredResult = await self._call_mcp_server(
            "tool_call",
            name=name,
            arguments=arguments,
            progress_callback=_on_progress,
            request_timeout=request_timeout,
        )

        if isinstance(result, InputRequiredResult):
            payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            structured_content = {
                "interaction_required": True,
                "input_required": payload,
            }
            text_result = json.dumps(structured_content, ensure_ascii=False, indent=2)
            self._last_tool_call_meta = {
                "tool_name": name,
                "arguments": arguments,
                "text": text_result,
                "structured_content": structured_content,
                "meta": result.meta,
                "server_logs": list(call_logs),
                "is_error": False,
                "interaction_required": True,
                "input_requests": payload.get("inputRequests"),
                "request_state": payload.get("requestState"),
            }
            return text_result

        if result.is_error:
            self._last_tool_call_meta = {
                "tool_name": name,
                "arguments": arguments,
                "text": f"MCP server error: {result.content}",
                "structured_content": result.structured_content,
                "meta": result.meta,
                "server_logs": list(call_logs),
                "is_error": True,
            }
            return f"MCP server error: {result.content}"

        parts: list[str] = []

        # Phase 3: 优先使用 structured_content（服务端返回结构化 JSON 时）
        # 兼容性：不提供 structured_content 的服务器此字段为 None，自动走 content 分支
        if result.structured_content:
            parts.append(json.dumps(result.structured_content, ensure_ascii=False, indent=2))
        elif result.content:
            # 遍历所有 content 项（不仅仅是 [0]），兼容多内容和非文本内容
            for item in result.content:
                if isinstance(item, TextContent):
                    parts.append(item.text)
                elif isinstance(item, ImageContent):
                    parts.append(f"[Image: {item.mime_type}]")
                else:
                    parts.append(f"[{type(item).__name__}]")
        else:
            # 兼容性：content 为空列表时不再 IndexError（修复现有隐患）
            parts.append("(empty response)")

        # Phase 3: 附加 meta 信息（如缓存标识 cached=True/False）
        # 兼容性：不提供 meta 的服务器此字段为 None，自动跳过
        if result.meta:
            meta_str = ", ".join(f"{k}={v}" for k, v in result.meta.items())
            parts.append(f"(meta: {meta_str})")

        text_result = "\n".join(parts)
        self._last_tool_call_meta = {
            "tool_name": name,
            "arguments": arguments,
            "text": text_result,
            "structured_content": result.structured_content,
            "meta": result.meta,
            "server_logs": list(call_logs),
            "is_error": False,
        }

        # Phase 4: 兼容旧行为，按需附加本次调用期间收集的服务端日志/进度
        if call_logs and MCP_APPEND_SERVER_LOGS_TO_RESULT:
            parts.append("\n--- Server Logs ---")
            parts.extend(call_logs[-10:])  # 最多附带 10 条，防止过长

        return "\n".join(parts)

    async def _get_tools_from_mcp_server(self, request_timeout: float | int = 15) -> list[Tool]:
        try:
            result: ListToolsResult = await self._call_mcp_server("list_tools", request_timeout=request_timeout)
            return result.tools
        except Exception:
            raise

    async def _wait_initialized(self, timeout: float | int = MCP_INIT_TIMEOUT + 5) -> bool:
        """等待 MCP 会话初始化完成"""
        try:
            await asyncio.wait_for(self._initialized.wait(), timeout=timeout)
            return self._init_error is None
        except TimeoutError:
            return False

    def wait_ready(self, timeout: float | int = MCP_INIT_TIMEOUT + 5) -> bool:
        """
        同步等待 MCP 会话初始化完成。

        Returns:
            True 如果初始化成功，False 如果失败或超时
        """
        if self._close:
            return False

        future = asyncio.run_coroutine_threadsafe(self._wait_initialized(timeout), self._event_loop)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            future.cancel()
            logging.error(f"Timeout waiting for MCP server {self._mcp_server.id} to initialize")
            return False
        except Exception as e:
            logging.exception(f"Error waiting for MCP server {self._mcp_server.id}: {e}")
            return False

    def is_ready(self) -> bool:
        """检查 MCP 会话是否已初始化完成且无错误"""
        return self._initialized.is_set() and self._init_error is None

    def get_last_tool_call_meta(self) -> dict[str, Any] | None:
        """获取最近一次工具调用的旁路元数据。

        返回的结构保持宽松，避免扩大与 ragflow upstream 的签名差异。
        当前包含：
        - tool_name
        - arguments
        - text
        - structured_content
        - meta
        - server_logs
        - is_error
        """
        if self._last_tool_call_meta is None:
            return None
        return dict(self._last_tool_call_meta)

    @property
    def call_context(self) -> object | None:
        """Return opaque, instance-local context for future call authorization."""
        return self._call_context

    @property
    def server_instructions(self) -> str | None:
        """Phase 1: 获取 MCP 服务端在 initialize() 中返回的 instructions。

        如果服务端未提供 instructions 或尚未初始化完成，返回 None。
        """
        return self._server_instructions

    @property
    def server_capabilities(self) -> Any:
        """Phase 1: 获取 MCP 服务端在 initialize() 中返回的 capabilities。

        可用于检查服务端是否支持 prompts、resources、logging 等特性。
        如果服务端尚未初始化完成，返回 None。
        """
        return self._server_capabilities

    @property
    def protocol_version(self) -> str | None:
        """Return the protocol version negotiated by SDK 2."""
        return self._protocol_version

    def get_tools(self, timeout: float | int = 10) -> list[Tool]:
        if self._close:
            raise ValueError("Session is closed")

        future = asyncio.run_coroutine_threadsafe(self._get_tools_from_mcp_server(request_timeout=timeout), self._event_loop)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
            future.cancel()
            msg = f"Timeout when fetching tools from MCP server: {self._mcp_server.id} (timeout={timeout})"
            logging.error(msg)
            raise RuntimeError(msg)
        except Exception:
            logging.exception(f"Error fetching tools from MCP server: {self._mcp_server.id}")
            raise

    @override
    def tool_call(self, name: str, arguments: dict[str, Any], timeout: float | int = MCP_TOOL_CALL_TIMEOUT) -> str:
        if self._close:
            return "Error: Session is closed"

        # 将 timeout 传递给内层 _call_mcp_tool，确保 MCP 实际调用也使用相同的超时
        future = asyncio.run_coroutine_threadsafe(self._call_mcp_tool(name, arguments, request_timeout=timeout), self._event_loop)
        try:
            # 外层 future 超时略大于内层，给调度留缓冲
            return future.result(timeout=timeout + 10)
        except MCPToolTimeoutError as e:
            # 内层 MCP 调用超时（从协程内部抛出），说明 MCP 服务端在 timeout 秒内未响应
            logging.error(f"MCP tool '{name}' timed out on server {self._mcp_server.id}: {e}")
            return f"Timeout calling tool '{name}' (timeout={timeout}s). The MCP server did not respond in time."
        except FuturesTimeoutError:
            # 外层 future 超时（调度层面），一般不会触发（因为外层 timeout 更大）
            future.cancel()
            logging.error(f"Future timeout calling tool '{name}' on MCP server: {self._mcp_server.id} (timeout={timeout})")
            return f"Timeout calling tool '{name}' (timeout={timeout})."
        except MCPConnectionError as error:
            if error.status_code == 401:
                text = f"Authentication required for MCP server {self._mcp_server.id} (HTTP 401)."
            elif error.status_code == 403:
                text = f"Permission denied by MCP server {self._mcp_server.id} (HTTP 403)."
            else:
                text = f"MCP connection failed for server {self._mcp_server.id}: {error}."
            self._last_tool_call_meta = {
                "tool_name": name,
                "arguments": arguments,
                "text": text,
                "structured_content": None,
                "meta": None,
                "server_logs": [],
                "is_error": True,
                "connection_status": error.status_code,
            }
            logging.error(text)
            return text
        except Exception as e:
            logging.exception(f"Error calling tool '{name}' on MCP server: {self._mcp_server.id}")
            return f"Error calling tool '{name}': {e}."

    async def _close_on_event_loop(self, timeout: float = 5) -> None:
        """Close the SDK client on the loop that owns its async resources."""
        self._close = True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, 0)
        current_task = asyncio.current_task()
        inflight = [task for task in self._inflight_tasks if task is not current_task and not task.done()]
        for task in inflight:
            task.cancel()
        if inflight:
            _, pending = await asyncio.wait(inflight, timeout=max(0, deadline - loop.time()))
            if pending:
                logging.error(
                    "%s MCP calls did not stop before closing server %s (timeout=%ss)",
                    len(pending),
                    self._mcp_server.id,
                    timeout,
                )

        self._shutdown_event.set()
        if not self._initialized.is_set() and not self._runner_future.done():
            self._runner_future.cancel()

        if not self._client_closed.is_set():
            remaining = max(0, deadline - loop.time())
            try:
                if remaining == 0:
                    raise TimeoutError
                await asyncio.wait_for(self._client_closed.wait(), timeout=remaining)
            except TimeoutError:
                self._runner_future.cancel()
                logging.error(
                    "Timeout while closing MCP client for server %s (timeout=%ss)",
                    self._mcp_server.id,
                    timeout,
                )

    def _finalize_owner_thread(self, timeout: float) -> None:
        """Stop the owner loop and join its executor from a foreign thread."""
        stopped = True
        try:
            if not self._event_loop.is_closed():
                # Scheduling stop before run_forever() starts is intentional. The
                # callback will run as soon as the owner thread enters the loop,
                # closing the owner-startup failure race.
                self._event_loop.call_soon_threadsafe(self._event_loop.stop)
        except RuntimeError:
            if not self._event_loop.is_closed():
                logging.exception("Failed to stop MCP event loop for server %s", self._mcp_server.id)
        try:
            self._loop_thread_future.result(timeout=timeout)
        except FuturesTimeoutError:
            stopped = False
            logging.error(
                "Timeout while stopping MCP event loop for server %s (timeout=%ss)",
                self._mcp_server.id,
                timeout,
            )
        except Exception:
            logging.exception("MCP event loop failed while closing server %s", self._mcp_server.id)
        if stopped and not self._event_loop.is_closed():
            self._event_loop.close()
        self._thread_pool.shutdown(wait=stopped, cancel_futures=not stopped)
        self.__class__._ALL_INSTANCES.discard(self)

    def _start_owner_reaper(self, timeout: float = 5) -> None:
        """Finalize a session whose close was initiated on its owner loop."""
        threading.Thread(
            target=self._finalize_owner_thread,
            args=(timeout,),
            name=f"multirag-mcp-reaper-{self._mcp_server.id}",
            daemon=True,
        ).start()

    async def close(self, timeout: float = 5) -> None:
        self._close = True
        timeout = max(timeout, 0)
        if not self._event_loop.is_running():
            await asyncio.to_thread(self._finalize_owner_thread, timeout)
            return

        current_loop = asyncio.get_running_loop()
        if current_loop is self._event_loop:
            await self._close_on_event_loop(timeout)
            self._event_loop.call_soon(self._start_owner_reaper, timeout)
            return

        deadline = current_loop.time() + timeout
        future = asyncio.run_coroutine_threadsafe(self._close_on_event_loop(timeout), self._event_loop)
        wrapped = asyncio.wrap_future(future)
        try:
            done, _ = await asyncio.wait({wrapped}, timeout=max(0, deadline - current_loop.time()))
            if not done:
                future.cancel()
                logging.error(
                    "Timeout while scheduling MCP close for server %s (timeout=%ss)",
                    self._mcp_server.id,
                    timeout,
                )
            else:
                await wrapped
        finally:
            await asyncio.to_thread(self._finalize_owner_thread, max(0, deadline - current_loop.time()))

    def close_sync(self, timeout: float | int = 5) -> None:
        self._close = True
        timeout_value = max(float(timeout), 0)
        deadline = time.monotonic() + timeout_value
        if not self._event_loop.is_running():
            logging.warning(f"Event loop already stopped for {self._mcp_server.id}")
            self._finalize_owner_thread(timeout_value)
            return

        try:
            if asyncio.get_running_loop() is self._event_loop:
                asyncio.create_task(self.close(timeout=timeout_value))
                return
        except RuntimeError:
            pass

        try:
            future = asyncio.run_coroutine_threadsafe(self._close_on_event_loop(timeout_value), self._event_loop)
            try:
                future.result(timeout=max(0, deadline - time.monotonic()))
            except FuturesTimeoutError:
                future.cancel()
                logging.error(f"Timeout while closing session for server {self._mcp_server.id} (timeout={timeout})")
            except Exception:
                logging.exception(f"Unexpected error during close_sync for {self._mcp_server.id}")
        except Exception:
            logging.exception(f"Exception while scheduling close for server {self._mcp_server.id}")
        finally:
            self._finalize_owner_thread(max(0, deadline - time.monotonic()))


def close_multiple_mcp_toolcall_sessions(sessions: list[MCPToolCallSession]) -> None:
    logging.info(f"Want to clean up {len(sessions)} MCP sessions")
    cleanup_timeout = 6.0

    async def _gather_and_stop() -> None:
        try:
            await asyncio.gather(*[s.close() for s in sessions if s is not None], return_exceptions=True)
        except Exception:
            logging.exception("Exception during MCP session cleanup")
        finally:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass

    try:
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()

        cleanup_future = asyncio.run_coroutine_threadsafe(_gather_and_stop(), loop)
        try:
            cleanup_future.result(timeout=cleanup_timeout)
        except FuturesTimeoutError:
            cleanup_future.cancel()
            logging.error("Timeout while closing %s MCP sessions", len(sessions))
            loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=1)
        if thread.is_alive():
            logging.error("MCP cleanup event loop did not stop within 1s")
        else:
            loop.close()
    except Exception:
        logging.exception("Exception during MCP session cleanup thread management")

    logging.info(f"{len(sessions)} MCP sessions has been cleaned up. {len(list(MCPToolCallSession._ALL_INSTANCES))} in global context.")


def shutdown_all_mcp_sessions() -> None:
    """Gracefully shutdown all active MCPToolCallSession instances."""
    sessions = list(MCPToolCallSession._ALL_INSTANCES)
    if not sessions:
        logging.info("No MCPToolCallSession instances to close.")
        return

    logging.info(f"Shutting down {len(sessions)} MCPToolCallSession instances...")
    close_multiple_mcp_toolcall_sessions(sessions)
    logging.info("All MCPToolCallSession instances have been closed.")


def _summarize_output_schema(output_schema: dict[str, Any]) -> str:
    """从 output_schema JSON Schema 中提取简短的返回字段摘要。

    将嵌套的 JSON Schema 压缩为一行可读文本，避免把完整 Schema 塞入 prompt。
    例：{type: object, properties: {sql: ..., size: ..., time: ...}}
      → "Returns fields: sql(string), size(int|string), time(string)"
    """
    props = output_schema.get("properties")
    if not props:
        return ""

    fields: list[str] = []
    for field_name, field_def in props.items():
        # 提取类型信息
        if "type" in field_def:
            ftype = field_def["type"]
        elif "anyOf" in field_def:
            ftype = "|".join(t.get("type", "?") for t in field_def["anyOf"] if isinstance(t, dict) and t.get("type") != "null")
        else:
            ftype = "any"
        fields.append(f"{field_name}({ftype})")

    return "Returns fields: " + ", ".join(fields)


def mcp_tool_metadata_to_openai_tool(mcp_tool: Tool | dict[str, Any]) -> dict[str, Any]:
    """将 MCP Tool 元数据转换为 OpenAI function calling 格式。

    Phase 2 增强：
    - 将 annotations (readOnlyHint / destructiveHint / openWorldHint) 追加到 description，
      让 LLM 能区分只读安全工具和有副作用的工具。
    - 将 output_schema 压缩为简短的返回字段摘要追加到 description，
      避免把完整 JSON Schema 塞入 prompt 导致 token 膨胀。

    兼容性：
    - 如果 MCP 服务端不提供 annotations / outputSchema / description，
      对应字段为 None，自动跳过，行为与改动前完全一致。
    """
    if isinstance(mcp_tool, dict):
        name = mcp_tool["name"]
        desc = mcp_tool.get("description") or ""
        schema = mcp_tool.get("inputSchema", mcp_tool.get("input_schema"))
        if not isinstance(schema, dict):
            raise ValueError("MCP tool input schema must be an object")
        annotations = mcp_tool.get("annotations")
        output_schema = mcp_tool.get("outputSchema", mcp_tool.get("output_schema"))
    else:
        name = mcp_tool.name
        desc = mcp_tool.description or ""
        schema = mcp_tool.input_schema
        annotations = mcp_tool.annotations
        output_schema = mcp_tool.output_schema

    # Phase 2: 将 annotations 安全标注追加到 description 末尾
    hints: list[str] = []
    if annotations:
        ann = annotations if isinstance(annotations, dict) else annotations.model_dump(exclude_none=True, by_alias=True)
        if ann.get("readOnlyHint", ann.get("read_only_hint")):
            hints.append("READ-ONLY (safe, no side effects)")
        if ann.get("destructiveHint", ann.get("destructive_hint")):
            hints.append("DESTRUCTIVE (may modify data)")
        if ann.get("idempotentHint", ann.get("idempotent_hint")):
            hints.append("idempotent (safe to retry)")
        if ann.get("openWorldHint", ann.get("open_world_hint")):
            hints.append("calls external service")
    if hints:
        desc += f"\n[Hints: {', '.join(hints)}]"

    # Phase 2: 将 output_schema 压缩为简短摘要追加到 description
    # 不把完整 JSON Schema 放进 tool dict，避免 prompt 过长导致小模型返回空
    if output_schema and isinstance(output_schema, dict):
        summary = _summarize_output_schema(output_schema)
        if summary:
            desc += f"\n[{summary}]"

    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": schema,
        },
    }
