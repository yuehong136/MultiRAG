"""OpenAI tool history shared by the synchronous and asynchronous chat backends."""

import json
from typing import Any

ToolResult = tuple[Any, str, Any, Any, Exception | None]


class ToolHistoryMixin:
    def _need_reasoning_content_back(self) -> bool:
        return False

    def _append_history(
        self,
        hist: list[dict[str, Any]],
        tool_call: Any,
        tool_res: Any,
        reasoning_content: str | None = None,
    ) -> list[dict[str, Any]]:
        return self._append_history_batch(hist, [(tool_call, tool_call.function.name, {}, tool_res, None)], reasoning_content)

    def _append_history_batch(
        self,
        hist: list[dict[str, Any]],
        results: list[ToolResult],
        reasoning_content: str | None = None,
    ) -> list[dict[str, Any]]:
        """Keep one assistant turn per batch, followed by each matching tool result."""
        calls = []
        for tc, _, _, _, _ in results:
            call = {
                "id": tc.id,
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                "type": "function",
            }
            # Only streaming SDK objects carry index; non-streaming objects do not.
            index = getattr(tc, "index", None)
            if index is not None:
                call["index"] = index
            calls.append(call)
        assistant: dict[str, Any] = {"role": "assistant", "tool_calls": calls}
        if reasoning_content is not None:
            assistant["reasoning_content"] = reasoning_content
        hist.append(assistant)
        for tc, _, _, result, error in results:
            content = str(error) if error is not None else json.dumps(result, ensure_ascii=False) if isinstance(result, dict) else str(result)
            hist.append({"role": "tool", "tool_call_id": tc.id, "content": content})
        return hist
