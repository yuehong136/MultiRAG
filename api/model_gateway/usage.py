"""Token usage read from upstream responses without re-serialising them."""

import json
import re
from dataclasses import dataclass
from typing import Any

# Only events whose usage is an object are parsed; ``"usage": null`` chunks are skipped.
_USAGE_OBJECT = re.compile(rb'"usage"\s*:\s*\{')
_EVENT_SEPARATORS = (b"\r\n\r\n", b"\n\n", b"\r\r")
# An upstream that never ends an event must not grow the buffer without bound.
_MAX_EVENT_BYTES = 1 << 20


@dataclass(frozen=True, slots=True)
class Usage:
    """Token counts as reported upstream. ``None`` means unknown, never zero."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    details: dict[str, Any] | None = None
    source: str = "unavailable"


UNAVAILABLE = Usage()


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def usage_from_payload(payload: Any) -> Usage:
    """Read the ``usage`` object of an OpenAI-shaped response body or stream chunk."""
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return UNAVAILABLE
    prompt, completion, total = _count(usage.get("prompt_tokens")), _count(usage.get("completion_tokens")), _count(usage.get("total_tokens"))
    if prompt is None and completion is None and total is None:
        return UNAVAILABLE
    details = {name: value for name, value in usage.items() if name.endswith("_details") and isinstance(value, dict)}
    return Usage(prompt_tokens=prompt, completion_tokens=completion, total_tokens=total, details=details or None, source="reported")


def _event_payload(event: bytes) -> Any:
    data = [line[5:].lstrip(b" ") for line in event.splitlines() if line.startswith(b"data:")]
    if not data:
        return None
    try:
        return json.loads(b"\n".join(data))
    except (ValueError, UnicodeDecodeError):
        return None


class SSEUsageTap:
    """Pass an SSE byte stream through event by event while reading usage.

    Events are forwarded byte for byte. When the gateway asked the upstream
    for usage on the caller's behalf (``hide_usage_only_chunk``), the trailing
    chunk that carries usage and no choices is consumed instead of forwarded,
    so the caller sees the stream it asked for.
    """

    def __init__(self, *, hide_usage_only_chunk: bool) -> None:
        self._buffer = bytearray()
        self._hide = hide_usage_only_chunk
        self.usage = UNAVAILABLE

    def feed(self, chunk: bytes) -> bytes:
        self._buffer += chunk
        forwarded = bytearray()
        while True:
            end = self._next_event_end()
            if end < 0:
                break
            event = bytes(self._buffer[:end])
            del self._buffer[:end]
            if self._keep(event):
                forwarded += event
        if len(self._buffer) > _MAX_EVENT_BYTES:
            forwarded += self._buffer
            self._buffer.clear()
        return bytes(forwarded)

    def flush(self) -> bytes:
        rest = bytes(self._buffer)
        self._buffer.clear()
        return rest if rest and self._keep(rest) else b""

    def _next_event_end(self) -> int:
        start, end = -1, -1
        for separator in _EVENT_SEPARATORS:
            index = self._buffer.find(separator)
            if index >= 0 and (start < 0 or index < start):
                start, end = index, index + len(separator)
        return end

    def _keep(self, event: bytes) -> bool:
        if not _USAGE_OBJECT.search(event):
            return True
        payload = _event_payload(event)
        if not isinstance(payload, dict):
            return True
        usage = usage_from_payload(payload)
        if usage.source == "reported":
            self.usage = usage
        return not (self._hide and payload.get("choices") == [])
