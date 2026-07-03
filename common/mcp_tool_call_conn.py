import asyncio
import json
import logging
import os
import threading
import weakref
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from string import Template
from typing import Any, Literal, Protocol

# MCP 服务器初始化超时时间（秒），可通过环境变量配置
MCP_INIT_TIMEOUT = int(os.environ.get("MCP_INIT_TIMEOUT", 15))
# MCP 工具调用超时时间（秒），可通过环境变量配置
MCP_TOOL_CALL_TIMEOUT = int(os.environ.get("MCP_TOOL_CALL_TIMEOUT", 60))
# 是否将服务端日志拼接到工具结果字符串末尾。
# 默认关闭，避免污染 LLM 的 tool observation；保留环境变量开关兼容旧行为。
MCP_APPEND_SERVER_LOGS_TO_RESULT = os.environ.get("MCP_APPEND_SERVER_LOGS_TO_RESULT", "false").lower() in {"1", "true", "yes", "on"}

from typing import override

from common.constants import MCPServerType
from mcp.client.session import ClientSession
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamablehttp_client
from mcp.types import CallToolResult, ImageContent, ListToolsResult, TextContent, Tool

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

MCPTaskType = Literal["list_tools", "tool_call"]
MCPTask = tuple[MCPTaskType, dict[str, Any], asyncio.Queue[Any]]


class MCPToolTimeoutError(Exception):
    """MCP 工具调用内部超时异常。

    与 asyncio.TimeoutError / concurrent.futures.TimeoutError 区分开，
    避免 Python 3.12+ 中两者是同一个类 (builtins.TimeoutError) 导致
    except 分支错误捕获的问题。
    """


class ToolCallSession(Protocol):
    def tool_call(self, name: str, arguments: dict[str, Any]) -> str: ...


class MCPToolCallSession(ToolCallSession):
    _ALL_INSTANCES: weakref.WeakSet["MCPToolCallSession"] = weakref.WeakSet()

    def __init__(self, mcp_server: Any, server_variables: dict[str, Any] | None = None, custom_header=None) -> None:
        self.__class__._ALL_INSTANCES.add(self)

        self._custom_header = custom_header
        self._mcp_server = mcp_server
        self._server_variables = server_variables or {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._close = False
        self._initialized = asyncio.Event()
        self._init_error: str | None = None

        # Phase 1: MCP server instructions & capabilities from initialize()
        self._server_instructions: str | None = None
        self._server_capabilities: Any = None

        # Phase 4: Accumulator for MCP logging/progress notifications during tool calls
        self._recent_logs: list[str] = []
        # 最近一次工具调用的旁路元数据，供 API / SSE / 调试界面按需读取
        self._last_tool_call_meta: dict[str, Any] | None = None

        self._event_loop = asyncio.new_event_loop()
        self._thread_pool = ThreadPoolExecutor(max_workers=1)
        self._thread_pool.submit(self._event_loop.run_forever)

        asyncio.run_coroutine_threadsafe(self._mcp_server_loop(), self._event_loop)

    async def _mcp_server_loop(self) -> None:
        url = self._mcp_server.url.strip()
        raw_headers: dict[str, str] = self._mcp_server.headers or {}
        custom_header: dict[str, str] = self._custom_header or {}
        headers: dict[str, str] = {}

        for h, v in raw_headers.items():
            nh = Template(h).safe_substitute(self._server_variables)
            nv = Template(v).safe_substitute(self._server_variables)
            if nh.strip() and nv.strip().strip("Bearer"):
                headers[nh] = nv

        for h, v in custom_header.items():
            nh = Template(h).safe_substitute(custom_header)
            nv = Template(v).safe_substitute(custom_header)
            headers[nh] = nv

        if self._mcp_server.server_type == MCPServerType.SSE:
            # SSE transport
            try:
                async with sse_client(url, headers) as stream:
                    async with ClientSession(*stream, logging_callback=self._on_logging) as client_session:  # type: ignore[misc]
                        try:
                            init_result = await asyncio.wait_for(client_session.initialize(), timeout=MCP_INIT_TIMEOUT)
                            self._save_init_result(init_result)
                            logging.info(f"client_session initialized successfully for server {self._mcp_server.id}")
                            self._initialized.set()
                            await self._process_mcp_tasks(client_session)
                        except TimeoutError:
                            msg = f"Timeout initializing client_session for server {self._mcp_server.id} (timeout={MCP_INIT_TIMEOUT}s)"
                            logging.error(msg)
                            self._init_error = msg
                            self._initialized.set()
                            await self._process_mcp_tasks(None, msg)
                        except asyncio.CancelledError:
                            logging.warning(f"SSE transport MCP session cancelled for server {self._mcp_server.id}")
                            self._initialized.set()
                            return
            except Exception as e:
                msg = f"Connection failed for server {self._mcp_server.id}: {e}"
                logging.exception(msg)
                self._init_error = msg
                self._initialized.set()
                await self._process_mcp_tasks(None, msg)

        elif self._mcp_server.server_type == MCPServerType.STREAMABLE_HTTP:
            # Streamable HTTP transport
            try:
                async with streamablehttp_client(url, headers) as (read_stream, write_stream, _):
                    async with ClientSession(read_stream, write_stream, logging_callback=self._on_logging) as client_session:
                        try:
                            init_result = await asyncio.wait_for(client_session.initialize(), timeout=MCP_INIT_TIMEOUT)
                            self._save_init_result(init_result)
                            logging.info(f"client_session initialized successfully for server {self._mcp_server.id}")
                            self._initialized.set()
                            await self._process_mcp_tasks(client_session)
                        except TimeoutError:
                            msg = f"Timeout initializing client_session for server {self._mcp_server.id} (timeout={MCP_INIT_TIMEOUT}s)"
                            logging.error(msg)
                            self._init_error = msg
                            self._initialized.set()
                            await self._process_mcp_tasks(None, msg)
                        except asyncio.CancelledError:
                            logging.warning(f"STREAMABLE_HTTP MCP session cancelled for server {self._mcp_server.id}")
                            self._initialized.set()
                            return
            except Exception as e:
                msg = f"Connection failed for server {self._mcp_server.id}: {e}"
                logging.exception(msg)
                self._init_error = msg
                self._initialized.set()
                await self._process_mcp_tasks(None, msg)

        else:
            msg = f"Unsupported MCP server type: {self._mcp_server.server_type}, id: {self._mcp_server.id}"
            self._init_error = msg
            self._initialized.set()
            await self._process_mcp_tasks(None, msg)

    def _save_init_result(self, init_result: Any) -> None:
        """Phase 1: 保存 MCP 服务器 initialize() 返回的 instructions 和 capabilities。

        使用 getattr 兼容不同版本的 MCP SDK 以及不提供这些字段的服务器。
        """
        self._server_instructions = getattr(init_result, "instructions", None)
        self._server_capabilities = getattr(init_result, "capabilities", None)
        if self._server_instructions:
            logging.info(f"MCP server {self._mcp_server.id} provides instructions ({len(self._server_instructions)} chars)")

    async def _on_logging(self, params: Any) -> None:
        """Phase 4: MCP logging notification callback.

        当服务端通过 ctx.info() / ctx.log() 发送日志通知时触发。
        日志同时写入 Python logging 和 _recent_logs 列表，
        后者在 _call_mcp_tool 中附加到工具返回结果。

        如果服务端不发送日志通知，此回调永远不会被调用（零开销）。
        """
        level_str = getattr(params, "level", "info")
        data = getattr(params, "data", "")
        py_level = _MCP_LEVEL_MAP.get(level_str, logging.INFO)
        logging.log(py_level, f"[MCP:{self._mcp_server.id}] {data}")
        self._recent_logs.append(f"[{level_str}] {data}")

    async def _process_mcp_tasks(self, client_session: ClientSession | None, error_message: str | None = None) -> None:
        while not self._close:
            try:
                mcp_task, arguments, result_queue = await asyncio.wait_for(self._queue.get(), timeout=1)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            logging.debug(f"Got MCP task {mcp_task} arguments {arguments}")

            r: Any = None

            if not client_session or error_message:
                r = ValueError(error_message)
                try:
                    await result_queue.put(r)
                except asyncio.CancelledError:
                    break
                continue

            try:
                if mcp_task == "list_tools":
                    r = await client_session.list_tools()
                elif mcp_task == "tool_call":
                    r = await client_session.call_tool(**arguments)
                else:
                    r = ValueError(f"Unknown MCP task {mcp_task}")
            except Exception as e:
                r = e
            except asyncio.CancelledError:
                break

            try:
                await result_queue.put(r)
            except asyncio.CancelledError:
                break

    async def _call_mcp_server(self, task_type: MCPTaskType, request_timeout: float | int = MCP_TOOL_CALL_TIMEOUT, **kwargs) -> Any:
        if self._close:
            raise ValueError("Session is closed")

        results: asyncio.Queue = asyncio.Queue()
        await self._queue.put((task_type, kwargs, results))

        try:
            result: CallToolResult | Exception = await asyncio.wait_for(results.get(), timeout=request_timeout)
            if isinstance(result, Exception):
                raise result
            return result
        except TimeoutError:
            # 用自定义异常包装，避免 Python 3.12+ 中 asyncio.TimeoutError
            # 与 concurrent.futures.TimeoutError 是同一类导致外层 except 分支误捕获
            raise MCPToolTimeoutError(f"MCP tool call '{task_type}' timed out after {request_timeout}s") from None
        except Exception:
            raise

    async def _call_mcp_tool(self, name: str, arguments: dict[str, Any], request_timeout: float | int = MCP_TOOL_CALL_TIMEOUT) -> str:
        # Phase 4: 清空日志累积器，本次调用期间的服务端日志/进度会写入 _recent_logs
        self._recent_logs = []
        self._last_tool_call_meta = None

        # Phase 4: 定义进度回调，将服务端 report_progress 收集到 _recent_logs
        # 如果服务端不调用 report_progress，此回调不会被触发（零开销）
        async def _on_progress(progress: float, total: float | None, message: str | None) -> None:
            total_str = f"/{total}" if total is not None else ""
            msg = f" {message}" if message else ""
            self._recent_logs.append(f"[progress {progress}{total_str}]{msg}")

        result: CallToolResult = await self._call_mcp_server(
            "tool_call",
            name=name,
            arguments=arguments,
            progress_callback=_on_progress,
            request_timeout=request_timeout,
        )

        if result.isError:
            self._last_tool_call_meta = {
                "tool_name": name,
                "arguments": arguments,
                "text": f"MCP server error: {result.content}",
                "structured_content": getattr(result, "structuredContent", None),
                "meta": getattr(result, "meta", None),
                "server_logs": list(self._recent_logs),
                "is_error": True,
            }
            return f"MCP server error: {result.content}"

        parts: list[str] = []

        # Phase 3: 优先使用 structuredContent（服务端返回结构化 JSON 时）
        # 兼容性：不提供 structuredContent 的服务器此字段为 None，自动走 content 分支
        if result.structuredContent:
            parts.append(json.dumps(result.structuredContent, ensure_ascii=False, indent=2))
        elif result.content:
            # 遍历所有 content 项（不仅仅是 [0]），兼容多内容和非文本内容
            for item in result.content:
                if isinstance(item, TextContent):
                    parts.append(item.text)
                elif isinstance(item, ImageContent):
                    parts.append(f"[Image: {getattr(item, 'mimeType', 'unknown')}]")
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
            "structured_content": getattr(result, "structuredContent", None),
            "meta": getattr(result, "meta", None),
            "server_logs": list(self._recent_logs),
            "is_error": False,
        }

        # Phase 4: 兼容旧行为，按需附加本次调用期间收集的服务端日志/进度
        if self._recent_logs and MCP_APPEND_SERVER_LOGS_TO_RESULT:
            parts.append("\n--- Server Logs ---")
            parts.extend(self._recent_logs[-10:])  # 最多附带 10 条，防止过长

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

    def get_tools(self, timeout: float | int = 10) -> list[Tool]:
        if self._close:
            raise ValueError("Session is closed")

        future = asyncio.run_coroutine_threadsafe(self._get_tools_from_mcp_server(request_timeout=timeout), self._event_loop)
        try:
            return future.result(timeout=timeout)
        except FuturesTimeoutError:
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
            logging.error(f"Future timeout calling tool '{name}' on MCP server: {self._mcp_server.id} (timeout={timeout})")
            return f"Timeout calling tool '{name}' (timeout={timeout})."
        except Exception as e:
            logging.exception(f"Error calling tool '{name}' on MCP server: {self._mcp_server.id}")
            return f"Error calling tool '{name}': {e}."

    async def close(self) -> None:
        if self._close:
            return

        self._close = True

        while not self._queue.empty():
            try:
                _, _, result_queue = self._queue.get_nowait()
                try:
                    await result_queue.put(asyncio.CancelledError("Session is closing"))
                except Exception:
                    pass
            except asyncio.QueueEmpty:
                break
            except Exception:
                break

        try:
            self._event_loop.call_soon_threadsafe(self._event_loop.stop)
        except Exception:
            pass

        try:
            self._thread_pool.shutdown(wait=True)
        except Exception:
            pass

        self.__class__._ALL_INSTANCES.discard(self)

    def close_sync(self, timeout: float | int = 5) -> None:
        if not self._event_loop.is_running():
            logging.warning(f"Event loop already stopped for {self._mcp_server.id}")
            return

        try:
            future = asyncio.run_coroutine_threadsafe(self.close(), self._event_loop)
            try:
                future.result(timeout=timeout)
            except FuturesTimeoutError:
                logging.error(f"Timeout while closing session for server {self._mcp_server.id} (timeout={timeout})")
            except Exception:
                logging.exception(f"Unexpected error during close_sync for {self._mcp_server.id}")
        except Exception:
            logging.exception(f"Exception while scheduling close for server {self._mcp_server.id}")


def close_multiple_mcp_toolcall_sessions(sessions: list[MCPToolCallSession]) -> None:
    logging.info(f"Want to clean up {len(sessions)} MCP sessions")

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

        asyncio.run_coroutine_threadsafe(_gather_and_stop(), loop).result()
        thread.join()
    except Exception:
        logging.exception("Exception during MCP session cleanup thread management")

    logging.info(f"{len(sessions)} MCP sessions has been cleaned up. {len(list(MCPToolCallSession._ALL_INSTANCES))} in global context.")


def shutdown_all_mcp_sessions():
    """Gracefully shutdown all active MCPToolCallSession instances."""
    sessions = list(MCPToolCallSession._ALL_INSTANCES)
    if not sessions:
        logging.info("No MCPToolCallSession instances to close.")
        return

    logging.info(f"Shutting down {len(sessions)} MCPToolCallSession instances...")
    close_multiple_mcp_toolcall_sessions(sessions)
    logging.info("All MCPToolCallSession instances have been closed.")


def _summarize_output_schema(output_schema: dict) -> str:
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


def mcp_tool_metadata_to_openai_tool(mcp_tool: Tool | dict) -> dict[str, Any]:
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
        schema = mcp_tool["inputSchema"]
        annotations = mcp_tool.get("annotations")
        output_schema = mcp_tool.get("outputSchema")
    else:
        name = mcp_tool.name
        desc = mcp_tool.description or ""
        schema = mcp_tool.inputSchema
        annotations = mcp_tool.annotations
        output_schema = getattr(mcp_tool, "outputSchema", None)

    # Phase 2: 将 annotations 安全标注追加到 description 末尾
    hints: list[str] = []
    if annotations:
        ann = annotations if isinstance(annotations, dict) else annotations.model_dump(exclude_none=True)
        if ann.get("readOnlyHint"):
            hints.append("READ-ONLY (safe, no side effects)")
        if ann.get("destructiveHint"):
            hints.append("DESTRUCTIVE (may modify data)")
        if ann.get("idempotentHint"):
            hints.append("idempotent (safe to retry)")
        if ann.get("openWorldHint"):
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
