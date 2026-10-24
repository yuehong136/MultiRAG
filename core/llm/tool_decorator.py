#
#  Copyright 2026 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
"""Lightweight ``@tool`` decorator and matching ``ToolCallSession`` adapter.

Lets callers register plain Python functions as LLM tools without having to
hand-write the OpenAI function schema or build an MCP-style session::

    from core.llm.tool_decorator import tool

    @tool
    def get_weather(city: str) -> str:
        \"\"\"Get current weather for a city.

        :param city: City name to look up.
        \"\"\"
        return f"{city}: 21 C, partly cloudy"

    chat_mdl.bind_tools(tools=[get_weather])

The decorator introspects the function signature, type hints, and docstring,
attaches an OpenAI-format schema as ``fn.openai_schema``, and marks the
function with ``fn._is_tool = True`` so :meth:`Base.bind_tools` can detect
the new style. Only register trusted Python code. Schemas are best-effort
annotation descriptions; dispatch checks required and unexpected keywords but
does not coerce or validate Python value types. Unsupported variadic and
positional-only signatures fail at registration. Duplicate names are rejected.

Omitted bind arguments preserve the binding; an explicit empty list clears it.
Async tools run on the caller's loop, sync tools in the shared worker pool.
Timeout or cancellation of a sync waiter cannot stop worker side effects.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from types import UnionType
from typing import Any, Protocol, Union, cast, get_args, get_origin, get_type_hints, runtime_checkable

from common.misc_utils import thread_pool_exec

_PY_TO_JSON: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
    type(None): "null",
}


def _py_type_to_json(py_type: Any) -> dict[str, Any]:
    """Best-effort mapping from a Python annotation to a JSON-schema fragment.

    Handles ``Optional[T]`` / ``T | None`` with nullable JSON schemas.
    Defaults, rather than nullable annotations, determine required arguments.
    Unknown types fall back to ``{"type": "string"}`` so the schema stays
    valid even when annotations are missing.
    """
    if py_type is inspect.Parameter.empty or py_type is Any:
        return {"type": "string"}

    origin = get_origin(py_type)
    if origin in (Union, UnionType):
        return {"anyOf": [_py_type_to_json(arg) for arg in get_args(py_type)]}

    if origin in (list, tuple, set, frozenset):
        item_args = get_args(py_type)
        item_schema = _py_type_to_json(item_args[0]) if item_args else {"type": "string"}
        return {"type": "array", "items": item_schema}

    if origin is dict:
        return {"type": "object"}

    if isinstance(py_type, type):
        return {"type": _PY_TO_JSON.get(py_type, "string")}

    return {"type": "string"}


_PARAM_RE = re.compile(r"^\s*:param\s+(?P<name>\w+)\s*:\s*(?P<desc>.*?)\s*$")
_GOOGLE_ARGS_HDR_RE = re.compile(r"^(Args|Arguments|Parameters)\s*:\s*$")
_GOOGLE_SECTION_HDR_RE = re.compile(r"^(Returns?|Yields?|Raises|Notes?|Examples?|Attributes?|Todo|See Also|Warning|Warnings|Tip)\s*:\s*$")
# Google-style parameter line: leading indent, identifier, optional ``(type)``,
# then ``: description``. The description can be empty (continuation lines fill it).
_GOOGLE_PARAM_RE = re.compile(r"^(?P<indent>\s+)(?P<name>\w+)\s*(?:\([^)]*\))?\s*:\s*(?P<desc>.*)$")


def _parse_param_docs(docstring: str | None) -> tuple[str, dict[str, str]]:
    """Pull a function description and per-parameter descriptions out of a docstring.

    Recognises two conventions and handles multi-line descriptions in both:

    * **reST / Sphinx**: ``:param name: description`` followed by deeper-indented
      continuation lines.
    * **Google**: an ``Args:`` (or ``Arguments:`` / ``Parameters:``) section
      whose body is ``    name: description`` lines, with deeper-indented
      continuation lines folded onto the same entry. Other Google sections
      (``Returns:``, ``Raises:``, ...) terminate the description but are
      otherwise dropped — they aren't sent to the LLM.

    Both styles can co-exist in one docstring. Anything before the first
    parameter entry / section header becomes the function description.
    """
    if not docstring:
        return "", {}

    lines = inspect.cleandoc(docstring).splitlines()
    desc_lines: list[str] = []
    param_docs: dict[str, str] = {}
    state = "desc"  # "desc" | "rst_param" | "google_args" | "other_section"
    current_param: str | None = None
    current_indent = 0
    after_first_param = False

    def _append_continuation(name: str, text: str) -> None:
        param_docs[name] = (param_docs[name] + " " + text).strip() if param_docs.get(name) else text

    for line in lines:
        stripped = line.strip()
        line_indent = len(line) - len(line.lstrip())

        # reST :param: line — works in any state, resets it.
        m = _PARAM_RE.match(line)
        if m:
            current_param = m.group("name")
            current_indent = line_indent
            param_docs[current_param] = m.group("desc").strip()
            state = "rst_param"
            after_first_param = True
            continue

        # Google section headers.
        if _GOOGLE_ARGS_HDR_RE.match(stripped):
            state = "google_args"
            current_param = None
            after_first_param = True
            continue
        if _GOOGLE_SECTION_HDR_RE.match(stripped):
            state = "other_section"
            current_param = None
            after_first_param = True
            continue

        # Google ``    name: desc`` entry inside an Args block.
        if state == "google_args":
            gm = _GOOGLE_PARAM_RE.match(line)
            if gm:
                current_param = gm.group("name")
                current_indent = line_indent
                param_docs[current_param] = gm.group("desc").strip()
                continue

        # Continuation line for the most recent reST or Google param.
        if state in ("rst_param", "google_args") and current_param and stripped:
            if line_indent > current_indent:
                _append_continuation(current_param, stripped)
                continue

        # Blank line ends the current param's continuation but stays in-state.
        if not stripped:
            current_param = None
            continue

        # Lines outside any param block, before the first param/section,
        # accumulate as the function description.
        if not after_first_param:
            desc_lines.append(line)

    return "\n".join(desc_lines).strip(), param_docs


def _build_openai_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    sig = inspect.signature(fn)
    try:
        hints = get_type_hints(fn)
    except Exception:
        hints = {}

    description, param_docs = _parse_param_docs(fn.__doc__)

    properties: dict[str, dict[str, Any]] = {}
    required: list[str] = []
    for name, param in sig.parameters.items():
        if param.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise TypeError(f"Tool parameter {name!r} must accept a named argument")
        schema = _py_type_to_json(hints.get(name, param.annotation))
        if name in param_docs:
            schema["description"] = param_docs[name]
        properties[name] = schema
        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "type": "function",
        "function": {
            "name": fn.__name__,
            "description": description or fn.__name__,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


# Sentinel separating "caller did not pass a timeout" from "caller passed None
# (= run forever)". Plain ``None`` is a legal value for the kwarg.
_TIMEOUT_UNSET: Any = object()


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    timeout: float | int | None = _TIMEOUT_UNSET,
) -> Callable[..., Any]:
    """Mark ``fn`` as an LLM tool and attach an OpenAI-format schema to it.

    Usable in two styles:

    * Bare:        ``@tool``               — no per-tool timeout; the session
                                              falls back to its caller-supplied
                                              ``request_timeout`` (default 10s).
    * Parameterised: ``@tool(timeout=60)``  — 60s timeout, overrides the
                                              session's default for this tool.
                                              Pass ``timeout=None`` to disable
                                              the timeout entirely (the tool
                                              runs until it completes).

    The wrapped callable is the same callable — we only set attributes on it:

    * ``fn._is_tool = True`` — sentinel so :meth:`Base.bind_tools` can tell a
      ``@tool`` callable apart from a raw schema dict.
    * ``fn.openai_schema`` — the schema dict passed verbatim to the LLM
      provider in the ``tools=[...]`` request field.
    * ``fn._tool_timeout`` (only when ``timeout=`` was passed) — read by
      :class:`FunctionToolSession` to override its default timeout for this
      tool. May be ``None`` to mean "no timeout".
    """

    if timeout is not _TIMEOUT_UNSET:
        _validate_timeout(timeout)

    def decorate(f: Callable[..., Any]) -> Callable[..., Any]:
        f.openai_schema = _build_openai_schema(f)  # type: ignore[attr-defined]
        f._is_tool = True  # type: ignore[attr-defined]
        if timeout is not _TIMEOUT_UNSET:
            f._tool_timeout = timeout  # type: ignore[attr-defined]
        return f

    # ``@tool`` (no parens) — ``fn`` is the function being decorated.
    if fn is not None:
        if not callable(fn):
            raise TypeError(f"@tool used incorrectly. Use `@tool` or `@tool(timeout=N)`; got first positional argument of type {type(fn).__name__}.")
        return decorate(fn)

    # ``@tool(timeout=N)`` — return the decorator that will receive the function.
    return decorate


def is_tool(obj: Any) -> bool:
    return callable(obj) and getattr(obj, "_is_tool", False) is True


def _validate_timeout(timeout: Any) -> None:
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout < 0):
        raise ValueError("Tool timeout must be a finite non-negative number or None")


@runtime_checkable
class _ToolCallable(Protocol):
    openai_schema: dict[str, Any]

    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


class FunctionToolSession:
    """Adapter that lets a list of ``@tool``-decorated callables satisfy the
    :class:`common.mcp_tool_call_conn.ToolCallSession` protocol used by the
    chat model tool loop (duck-typed, no explicit inheritance to avoid
    pulling the MCP client SDK into this module's import graph).

    The chat model only ever calls ``tool_call`` / ``tool_call_async`` with
    ``(name, arguments)`` — this class looks the name up in ``tools_map`` and
    invokes the callable, awaiting it if it is a coroutine and otherwise
    pushing it through ``thread_pool_exec`` so the event loop is not blocked.
    """

    def __init__(self, tools: list[Callable[..., Any]]) -> None:
        self.tools_map: dict[str, _ToolCallable] = {}
        for fn in tools:
            if not is_tool(fn):
                raise TypeError(f"{getattr(fn, '__name__', fn)!r} is not a @tool-decorated callable")
            registered = cast(_ToolCallable, fn)
            name = registered.openai_schema["function"]["name"]
            if name in self.tools_map:
                raise ValueError(f"Duplicate tool name {name!r}")
            self.tools_map[name] = registered

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [deepcopy(fn.openai_schema) for fn in self.tools_map.values()]

    def tool_call(self, name: str, arguments: dict[str, Any], timeout: float | int | None = 10) -> Any:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.tool_call_async(name, arguments, request_timeout=timeout))
        raise RuntimeError("Use tool_call_async inside a running event loop")

    async def tool_call_async(self, name: str, arguments: dict[str, Any], request_timeout: float | int | None = 10) -> Any:
        if name not in self.tools_map:
            raise KeyError(f"Tool {name!r} is not registered")
        if not isinstance(arguments, Mapping):
            raise TypeError(f"Tool arguments for {name} must be an object, got {type(arguments).__name__}")
        fn = self.tools_map[name]
        inspect.signature(fn).bind(**arguments)
        _validate_timeout(request_timeout)
        if asyncio.iscoroutinefunction(fn):
            coro = fn(**arguments)
        else:
            # Sync callables run in the thread pool. asyncio.wait_for cancels
            # the awaiting task on timeout, but Python cannot interrupt the
            # underlying worker thread — the function keeps running in the
            # background until it returns. Callers should treat sync tools
            # that block on I/O accordingly.
            coro = thread_pool_exec(fn, **arguments)
        # Per-tool timeout set via ``@tool(timeout=N)`` overrides the
        # session-default. ``None`` is a legal explicit choice meaning
        # "wait forever" — ``asyncio.wait_for(..., timeout=None)`` handles it.
        configured = getattr(fn, "_tool_timeout", _TIMEOUT_UNSET)
        effective_timeout = request_timeout if configured is _TIMEOUT_UNSET else configured
        return await asyncio.wait_for(coro, timeout=effective_timeout)


def resolve_tool_binding(toolcall_session: Any = None, tools: list[Any] | None = None) -> tuple[Any, list[dict[str, Any]]] | None:
    """Validate a replacement atomically; an explicit empty list clears it.

    Explicit sessions remain responsible for authorization and dispatch. Omitted
    arguments leave a binding unchanged; schema-only and mixed lists are errors.
    """
    if tools is None and isinstance(toolcall_session, list):
        tools, toolcall_session = toolcall_session, None
    if tools is None:
        if toolcall_session is None:
            return None
        raise TypeError("An explicit tool session requires a schema list")
    if not isinstance(tools, list):
        raise TypeError("Tools must be a list")
    if not tools:
        return None, []
    if toolcall_session is None:
        if not all(is_tool(fn) for fn in tools):
            raise TypeError("Tools must all be @tool-decorated callables")
        session = FunctionToolSession(tools)
        return session, session.schemas
    names: set[str] = set()
    for schema in tools:
        if not isinstance(schema, dict) or schema.get("type") != "function" or not isinstance(schema.get("function"), dict):
            raise TypeError("Explicit sessions require OpenAI function schemas")
        name = schema["function"].get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("Tool schema requires a function name")
        if name in names:
            raise ValueError(f"Duplicate tool name {name!r}")
        names.add(name)
    return toolcall_session, deepcopy(tools)
