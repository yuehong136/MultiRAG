"""Provider-neutral filtering and length policy for user-visible replies."""

from __future__ import annotations

from api.channel_execution.reasoning import StreamingReasoningFilter as StreamingReasoningFilter
from api.channel_execution.reasoning import strip_reasoning as strip_reasoning

SERVICE_UNAVAILABLE_TEXT = "服务暂时不可用，请稍后再试。"
ANSWER_TRUNCATED_SUFFIX = "\n\n（回答过长，演示版已截断）"


def truncate_answer(text: str, max_chars: int) -> str:
    """Apply the established Channel answer limit without changing its suffix."""

    if len(text) <= max_chars:
        return text
    if max_chars <= len(ANSWER_TRUNCATED_SUFFIX):
        return ANSWER_TRUNCATED_SUFFIX[:max_chars]
    prefix_length = max_chars - len(ANSWER_TRUNCATED_SUFFIX)
    return text[:prefix_length].rstrip() + ANSWER_TRUNCATED_SUFFIX
