"""Chat-completion chunks as Anthropic Messages stream events.

LiteLLM 1.82's ``AnthropicStreamWrapper`` shares one event queue between all
streams, drops the chunk that opens a block (and with it the first tool
arguments) and files ``reasoning_content`` under a text block. Its rewrite
upstream is not in the pinned release, so the event sequence is produced here
after the same rules as LiteLLM's current adapter and new-api's
``to_claude_messages_resp``: one open block at a time, a block opens on the
first delta of its kind, a tool block opens once per call and always carries
the arguments that came with it.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

logger = logging.getLogger(__name__)

# Anthropic sends ``ping`` during long silences; clients and proxies time out without it.
PING_INTERVAL_SECONDS = 10.0
_STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "function_call": "tool_use", "content_filter": "refusal"}


def stop_reason(finish_reason: object) -> str:
    return _STOP_REASONS.get(str(finish_reason), "end_turn")


def usage_fields(usage: dict[str, Any]) -> dict[str, int]:
    """OpenAI usage as Anthropic's, where cached input is counted apart from ``input_tokens``."""

    def number(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0

    details = usage.get("prompt_tokens_details")
    details = details if isinstance(details, dict) else {}
    read = number(details.get("cached_tokens")) or number(usage.get("cache_read_input_tokens"))
    written = number(details.get("cache_creation_tokens")) or number(usage.get("cache_creation_input_tokens"))
    return {
        "input_tokens": max(number(usage.get("prompt_tokens")) - read, 0),
        "output_tokens": number(usage.get("completion_tokens")),
        "cache_creation_input_tokens": written,
        "cache_read_input_tokens": read,
    }


def sse(event: dict[str, Any]) -> bytes:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n".encode()


class MessageStream:
    """The state of one translated stream: which block is open and what the final events must say."""

    def __init__(self, model: str, tool_names: dict[str, str]) -> None:
        self._model = model
        self._tool_names = tool_names
        self._started = False
        self._next_index = 0
        self._open: tuple[str, int] | None = None
        # Tool calls by the upstream's index: the block that carries each, or None once it is closed.
        self._tools: dict[int, int | None] = {}
        self._stop_reason = "end_turn"
        self._usage: dict[str, int] = usage_fields({})

    def start(self) -> Iterator[dict[str, Any]]:
        if self._started:
            return
        self._started = True
        message: dict[str, Any] = {
            "id": f"msg_{uuid.uuid4().hex}",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": self._model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": usage_fields({}),
        }
        yield {"type": "message_start", "message": message}

    def _close(self) -> Iterator[dict[str, Any]]:
        if self._open is None:
            return
        kind, index = self._open
        self._open = None
        if kind == "tool_use":
            for key, block in self._tools.items():
                if block == index:
                    self._tools[key] = None
        yield {"type": "content_block_stop", "index": index}

    def _block(self, kind: str, start: dict[str, Any]) -> Iterator[dict[str, Any]]:
        yield from self._close()
        self._open = (kind, self._next_index)
        self._next_index += 1
        yield {"type": "content_block_start", "index": self._open[1], "content_block": start}

    def _delta(self, delta: dict[str, Any]) -> dict[str, Any]:
        assert self._open is not None
        return {"type": "content_block_delta", "index": self._open[1], "delta": delta}

    def feed(self, chunk: dict[str, Any]) -> Iterator[dict[str, Any]]:
        """The events one chat-completion chunk stands for."""
        yield from self.start()
        if isinstance(chunk.get("usage"), dict):
            self._usage = usage_fields(chunk["usage"])
        choices = chunk.get("choices")
        choice: dict[str, Any] = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        raw_delta = choice.get("delta")
        delta: dict[str, Any] = raw_delta if isinstance(raw_delta, dict) else {}

        reasoning = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            if self._open is None or self._open[0] != "thinking":
                yield from self._block("thinking", {"type": "thinking", "thinking": "", "signature": ""})
            yield self._delta({"type": "thinking_delta", "thinking": reasoning})

        text = delta.get("content")
        if isinstance(text, str) and text:
            if self._open is None or self._open[0] != "text":
                yield from self._block("text", {"type": "text", "text": ""})
            yield self._delta({"type": "text_delta", "text": text})

        calls = delta.get("tool_calls")
        for call in calls if isinstance(calls, list) else []:
            if not isinstance(call, dict):
                continue
            raw_function, raw_index, raw_id = call.get("function"), call.get("index"), call.get("id")
            function: dict[str, Any] = raw_function if isinstance(raw_function, dict) else {}
            key: int = raw_index if isinstance(raw_index, int) else 0
            name, arguments = function.get("name"), function.get("arguments")
            if key not in self._tools:
                if not isinstance(name, str) or not name:
                    continue
                identifier = raw_id if isinstance(raw_id, str) and raw_id else f"toolu_{uuid.uuid4().hex[:24]}"
                yield from self._block("tool_use", {"type": "tool_use", "id": identifier, "name": self._tool_names.get(name, name), "input": {}})
                assert self._open is not None
                self._tools[key] = self._open[1]
            if isinstance(arguments, str) and arguments:
                if self._open is None or self._tools[key] != self._open[1]:
                    # Arguments for a call whose block has already been closed cannot be delivered in order.
                    logger.warning("model gateway: dropped interleaved arguments of tool call %s in a Messages stream", key)
                    continue
                yield self._delta({"type": "input_json_delta", "partial_json": arguments})

        if choice.get("finish_reason") is not None:
            self._stop_reason = stop_reason(choice["finish_reason"])

    def finish(self) -> Iterator[dict[str, Any]]:
        yield from self.start()
        yield from self._close()
        yield {"type": "message_delta", "delta": {"stop_reason": self._stop_reason, "stop_sequence": None}, "usage": self._usage}
        yield {"type": "message_stop"}


async def with_pings(source: AsyncIterator[bytes]) -> AsyncIterator[bytes | None]:
    """``source``, with ``None`` for every ``PING_INTERVAL_SECONDS`` it stays silent."""
    interval = PING_INTERVAL_SECONDS
    iterator = source.__aiter__()
    pending: asyncio.Future[bytes] | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(iterator.__anext__())
            done, _ = await asyncio.wait({pending}, timeout=interval)
            if not done:
                yield None
                continue
            task, pending = pending, None
            try:
                yield task.result()
            except StopAsyncIteration:
                return
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        close = getattr(iterator, "aclose", None)
        if close is not None:
            await close()
