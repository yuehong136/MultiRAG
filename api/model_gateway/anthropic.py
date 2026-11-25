"""``/v1/messages``: Anthropic's Messages API in front of OpenAI-protocol upstreams.

The gateway does not translate protocols itself. LiteLLM's adapter
(``litellm/llms/anthropic/experimental_pass_through/adapters``) turns the
request into a chat completion and the upstream's answer, whole or streamed,
back into Anthropic's shape; this module only feeds it and frames the result.

``litellm`` takes seconds to import, so it is loaded on first use (or by
``warm_up`` at start) and never at import time of the data plane.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

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
    529: "overloaded_error",
}
_FALLBACK_MESSAGE = "The model service rejected the request."


def error_type(status_code: int) -> str:
    """Anthropic's error type for an HTTP status."""
    return _ERROR_TYPES.get(status_code, "api_error" if status_code >= 500 else "invalid_request_error")


def error_body(status_code: int, message: str) -> dict[str, Any]:
    return {"type": "error", "error": {"type": error_type(status_code), "message": message}}


def gateway_error_body(error: GatewayError) -> dict[str, Any]:
    return error_body(error.status_code, error.message)


def upstream_error_body(status_code: int, raw: bytes) -> bytes:
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
    return json.dumps(error_body(status_code, message), ensure_ascii=False).encode("utf-8")


def _sse(event: dict[str, Any]) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


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
            if isinstance(payload, dict) and not isinstance(payload.get("usage"), dict):
                # The adapter reads usage unconditionally; an upstream that reports none gets zeros.
                payload["usage"] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            completion = _litellm.ModelResponse(**payload)
            message = _adapter.translate_completion_output_params(completion, self._tool_names)
        except Exception as error:
            raise ValueError("the upstream response is not a chat completion") from error
        if message is None:
            raise ValueError("the upstream response is not a chat completion")
        return json.dumps(message if isinstance(message, dict) else message.model_dump(), ensure_ascii=False).encode("utf-8")

    async def stream(self, upstream: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
        """Chat completion chunks as Messages events; a relay error ends the stream with an ``error`` event."""
        failure: list[dict[str, Any]] = []
        try:
            async for event in _adapter.translate_completion_output_params_streaming(self._chunks(upstream, failure), self._model, self._tool_names):
                yield event
        except Exception:
            logger.exception("model gateway: translating a stream to the Messages format failed")
            failure.append(error_body(502, "The model service sent a response the gateway could not translate."))
        if failure:
            yield _sse(failure[0])

    async def _chunks(self, upstream: AsyncIterator[bytes], failure: list[dict[str, Any]]) -> AsyncIterator[Any]:
        pending = b""
        async for piece in upstream:
            pending += piece
            *lines, pending = pending.split(b"\n")
            for line in lines:
                data = line.strip()
                if not data.startswith(b"data:"):
                    continue
                data = data[5:].strip()
                if not data or data == b"[DONE]":
                    continue
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if "error" in chunk and "choices" not in chunk:
                    # The relay's own report of a broken upstream stream.
                    error = chunk["error"] if isinstance(chunk["error"], dict) else {}
                    failure.append(error_body(502, str(error.get("message") or _FALLBACK_MESSAGE)))
                    return
                if not chunk.get("choices"):
                    # A usage-only chunk; the adapter expects every chunk to carry a choice.
                    chunk["choices"] = [{"index": 0, "delta": {}}]
                yield _litellm.ModelResponseStream(**chunk)


async def translate_request(payload: dict[str, Any]) -> Translation:
    """A Messages request body as a chat completion. Raises ``ValueError`` for a body that is not one."""
    await asyncio.to_thread(_load)
    model = payload.get("model")
    if not payload.get("messages"):
        raise ValueError("You must provide a non-empty messages parameter.")
    try:
        body, tool_names = _adapter.translate_completion_input_params_with_tool_mapping(dict(payload))
    except Exception as error:
        raise ValueError(f"The request is not a valid Messages request: {type(error).__name__}") from error
    if body is None:
        raise ValueError("The request is not a valid Messages request.")
    translated = dict(body)
    if payload.get("stream") is True:
        translated["stream"] = True
    return Translation(translated, dict(tool_names or {}), str(model))
