"""``/v1/messages``: Anthropic's Messages API in front of OpenAI-protocol upstreams.

LiteLLM's adapter (``litellm/llms/anthropic/experimental_pass_through/adapters``)
turns the request into a chat completion and a complete answer back into a
message. This module feeds it, removes what an OpenAI-protocol upstream would
reject (the pinned adapter passes unknown Anthropic fields through), and
frames the result; streamed answers go through ``anthropic_stream``.

``litellm`` takes seconds to import, so it is loaded on first use (or by
``warm_up`` at start) and never at import time of the data plane.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from api.model_gateway.anthropic_stream import MessageStream, sse, stop_reason, usage_fields, with_pings
from api.model_gateway.errors import GatewayError

logger = logging.getLogger(__name__)

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    413: "request_too_large",
    429: "rate_limit_error",
    503: "overloaded_error",
    504: "timeout_error",
    529: "overloaded_error",
}
_FALLBACK_MESSAGE = "The model service rejected the request."
# Messages fields an OpenAI-protocol upstream does not know. LiteLLM 1.82 forwards them untouched.
_ANTHROPIC_ONLY = (
    "top_k",
    "output_config",
    "context_management",
    "service_tier",
    "thinking",
    "web_search_options",
    "container",
    "mcp_servers",
    "inference_geo",
    "speed",
    "betas",
    "anthropic_beta",
    "anthropic_version",
)


def error_type(status_code: int) -> str:
    """Anthropic's error type for an HTTP status."""
    return _ERROR_TYPES.get(status_code, "api_error" if status_code >= 500 else "invalid_request_error")


def error_body(status_code: int, message: str, request_id: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"type": "error", "error": {"type": error_type(status_code), "message": message}}
    if request_id:
        body["request_id"] = request_id
    return body


def gateway_error_body(error: GatewayError, request_id: str | None = None) -> dict[str, Any]:
    return error_body(error.status_code, error.message, request_id)


def upstream_error_body(status_code: int, raw: bytes, request_id: str | None = None) -> bytes:
    """An upstream's OpenAI-shaped refusal as Anthropic's error envelope."""
    message = _FALLBACK_MESSAGE
    try:
        error = json.loads(raw).get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str) and error["message"]:
            message = error["message"]
        elif isinstance(error, str) and error:
            message = error
    except (ValueError, UnicodeDecodeError, AttributeError):
        pass
    return json.dumps(error_body(status_code, message, request_id), ensure_ascii=False).encode("utf-8")


_litellm: Any = None
_adapter: Any = None


def _load() -> None:
    global _litellm, _adapter
    if _adapter is None:
        import litellm
        from litellm.llms.anthropic.experimental_pass_through.adapters.transformation import AnthropicAdapter

        _litellm, _adapter = litellm, AnthropicAdapter()


async def warm_up() -> None:
    """Import the adapter off the event loop so the first request does not wait for it."""
    try:
        await asyncio.to_thread(_load)
    except Exception:
        logger.exception("model gateway: LiteLLM's Anthropic adapter could not be loaded; /v1/messages will fail")


def _text_document(block: Any) -> Any:
    """A plain-text document block as the text it holds; the adapter drops it otherwise."""
    if isinstance(block, dict) and block.get("type") == "document":
        source = block.get("source")
        if isinstance(source, dict) and source.get("type") == "text" and isinstance(source.get("data"), str):
            return {"type": "text", "text": source["data"]}
    return block


def _prepare(payload: dict[str, Any]) -> dict[str, Any]:
    """The request as the adapter can take it: what it would drop silently is moved to where it reads."""
    request = dict(payload)
    declared = request.get("system")
    system: list[Any] = [{"type": "text", "text": declared}] if isinstance(declared, str) and declared else list(declared) if isinstance(declared, list) else []
    folded = False
    messages: list[Any] = []
    for message in request["messages"]:
        if isinstance(message, dict) and message.get("role") == "system":
            # Claude Code sends system turns inside ``messages`` for models it does not know.
            content = message.get("content")
            system.extend(content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}])
            folded = True
            continue
        if isinstance(message, dict) and isinstance(message.get("content"), list):
            message = {**message, "content": [_text_document(block) for block in message["content"]]}
        messages.append(message)
    request["messages"] = messages
    if folded:
        request["system"] = system
    if isinstance(request.get("tools"), list):
        # Server-side tools (bash, web search, ...) run at Anthropic; other upstreams cannot honour them.
        request["tools"] = [tool for tool in request["tools"] if isinstance(tool, dict) and "input_schema" in tool]
        if not request["tools"]:
            request.pop("tools")
            request.pop("tool_choice", None)
    return request


def _pdf_part(part: Any) -> Any:
    """A PDF the adapter filed as an image, as the chat-completions ``file`` part."""
    if isinstance(part, dict) and part.get("type") == "image_url":
        image = part.get("image_url")
        url = image.get("url") if isinstance(image, dict) else image
        if isinstance(url, str) and url.startswith("data:application/pdf"):
            return {"type": "file", "file": {"filename": "document.pdf", "file_data": url}}
    return part


def _finish(body: dict[str, Any]) -> dict[str, Any]:
    """The adapter's chat completion without the Anthropic-only parts it left in."""
    stops = body.pop("stop_sequences", None)
    if isinstance(stops, list) and stops and "stop" not in body:
        body["stop"] = [stop for stop in stops if isinstance(stop, str)][:4]
    for key in _ANTHROPIC_ONLY:
        body.pop(key, None)
    if isinstance(body.get("tools"), list):
        body["tools"] = [tool for tool in body["tools"] if isinstance(tool, dict) and tool.get("type") == "function"]
        if not body["tools"]:
            body.pop("tools")
            body.pop("tool_choice", None)
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        message.pop("thinking_blocks", None)
        if isinstance(message.get("content"), list):
            message["content"] = [_pdf_part(part) for part in message["content"]]
    return body


def _chunk(line: bytes) -> dict[str, Any] | None:
    data = line.strip()
    if not data.startswith(b"data:"):
        return None
    data = data[5:].strip()
    if not data or data == b"[DONE]":
        return None
    try:
        chunk = json.loads(data)
    except ValueError:
        return None
    return chunk if isinstance(chunk, dict) else None


class Translation:
    """One Messages request: its chat-completion form and the way back."""

    def __init__(self, body: dict[str, Any], tool_names: dict[str, str], model: str) -> None:
        self.body = body
        self._tool_names = tool_names
        self._model = model

    def response(self, raw: bytes) -> bytes:
        """A complete chat completion as a Messages response. Raises ``ValueError`` when it is not one."""
        try:
            payload = json.loads(raw)
            usage = payload.get("usage")
            if not isinstance(usage, dict):
                # The adapter reads usage unconditionally; an upstream that reports none gets zeros.
                usage = {}
                payload["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            finish = payload["choices"][0].get("finish_reason")
            completion = _litellm.ModelResponse(**payload)
            message = _adapter.translate_completion_output_params(completion, self._tool_names)
        except Exception as error:
            raise ValueError("the upstream response is not a chat completion") from error
        if message is None:
            raise ValueError("the upstream response is not a chat completion")
        result: dict[str, Any] = dict(message if isinstance(message, dict) else message.model_dump())
        # The caller asked for its own model name, and Anthropic's ids, block shapes and usage fields.
        result["id"] = f"msg_{uuid.uuid4().hex}"
        result["model"] = self._model
        result["stop_reason"] = stop_reason(finish)
        result["usage"] = usage_fields(usage)
        content = []
        for block in result.get("content") or []:
            if not isinstance(block, dict) or (block.get("type") == "text" and not block.get("text")):
                continue
            block = {key: value for key, value in block.items() if key != "provider_specific_fields"}
            if block.get("type") == "thinking" and block.get("signature") is None:
                block["signature"] = ""
            content.append(block)
        result["content"] = content
        return json.dumps(result, ensure_ascii=False).encode("utf-8")

    async def stream(self, upstream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Chat completion chunks as Messages events; a relay error ends the stream with an ``error`` event."""
        state = MessageStream(self._model, self._tool_names)
        pending = b""
        try:
            async for piece in with_pings(upstream):
                if piece is None:
                    for event in state.start():
                        yield sse(event)
                    yield sse({"type": "ping"})
                    continue
                pending += piece
                *lines, pending = pending.split(b"\n")
                for line in lines:
                    chunk = _chunk(line)
                    if chunk is None:
                        continue
                    if "error" in chunk and "choices" not in chunk:
                        # The relay's own report of a broken upstream stream.
                        error = chunk["error"] if isinstance(chunk["error"], dict) else {}
                        yield sse(error_body(502, str(error.get("message") or _FALLBACK_MESSAGE)))
                        return
                    for event in state.feed(chunk):
                        yield sse(event)
        except Exception:
            logger.exception("model gateway: translating a stream to the Messages format failed")
            yield sse(error_body(502, "The model service sent a response the gateway could not translate."))
            return
        for event in state.finish():
            yield sse(event)


async def translate_request(payload: dict[str, Any]) -> Translation:
    """A Messages request body as a chat completion. Raises ``ValueError`` for a body that is not one."""
    await asyncio.to_thread(_load)
    model = payload.get("model")
    if not isinstance(payload.get("messages"), list) or not payload["messages"]:
        raise ValueError("You must provide a non-empty messages parameter.")
    try:
        body, tool_names = _adapter.translate_completion_input_params_with_tool_mapping(_prepare(payload))
    except Exception as error:
        raise ValueError(f"The request is not a valid Messages request: {type(error).__name__}") from error
    if body is None:
        raise ValueError("The request is not a valid Messages request.")
    translated = _finish(dict(body))
    if payload.get("stream") is True:
        translated["stream"] = True
        # The final ``message_delta`` reports usage, so the upstream has to send it.
        translated["stream_options"] = {"include_usage": True}
    return Translation(translated, dict(tool_names or {}), str(model))


async def count_tokens(payload: dict[str, Any]) -> int:
    """Input tokens of a Messages request, by LiteLLM's local tokenizer for the named model.

    An estimate: the upstream's own tokenizer is not consulted, as with the
    providers LiteLLM itself has no counting endpoint for.
    """
    translation = await translate_request({**payload, "stream": False})
    body = translation.body

    def count() -> int:
        return int(_litellm.token_counter(model=str(payload.get("model") or ""), messages=body.get("messages") or [], tools=body.get("tools"), tool_choice=body.get("tool_choice")))

    try:
        return await asyncio.to_thread(count)
    except Exception as error:
        raise ValueError(f"The request could not be counted: {type(error).__name__}") from error


def models_page(models: list[tuple[str, int]]) -> dict[str, Any]:
    """Model names and creation times as Anthropic's ``GET /v1/models`` page."""
    data = [{"type": "model", "id": name, "display_name": name, "created_at": datetime.fromtimestamp(created, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")} for name, created in models]
    return {"data": data, "has_more": False, "first_id": data[0]["id"] if data else None, "last_id": data[-1]["id"] if data else None}
