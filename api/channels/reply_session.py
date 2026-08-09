"""Default buffered implementation of the provider-neutral reply lifecycle."""

from __future__ import annotations

from api.channels.core.base import (
    Channel,
    IncomingMessage,
    OutgoingMessage,
    ReplyContext,
    ReplySessionState,
    ReplySessionStateError,
    ReplyStatus,
)
from api.channels.core.reply import SERVICE_UNAVAILABLE_TEXT, strip_reasoning, truncate_answer

CANCELLED_TEXT = "已停止生成。若此前已发起外部操作，其结果不视为已撤销。"


class BufferedReplySession:
    """Buffer deltas and deliver one ordinary text reply at successful completion."""

    def __init__(
        self,
        *,
        channel: Channel,
        source: IncomingMessage,
        max_content_chars: int,
        context: ReplyContext = ReplyContext(),
    ) -> None:
        if max_content_chars < 1:
            raise ValueError("reply content limit must be positive")
        self._channel = channel
        self._source = source
        self._max_content_chars = max_content_chars
        self._context = context
        self._parts: list[str] = []
        self._state = ReplySessionState.OPEN

    @property
    def state(self) -> ReplySessionState:
        return self._state

    @property
    def reply_message_id(self) -> str:
        """Ordinary replies do not expose a mutable provider message."""

        return ""

    async def append(self, content: str) -> None:
        self._require_open("append")
        if not isinstance(content, str):
            raise TypeError("reply delta must be a string")
        self._parts.append(content)

    async def set_status(
        self,
        status: ReplyStatus,
        *,
        queue_position: int = 0,
    ) -> None:
        """Buffered providers have no mutable surface; retain the lifecycle API."""

        del queue_position
        self._require_open("set status")
        if status not in {ReplyStatus.QUEUED, ReplyStatus.RUNNING}:
            raise ValueError("only non-terminal reply status can be set directly")

    async def complete(self) -> None:
        self._require_open("complete")
        self._state = ReplySessionState.COMPLETED
        content = strip_reasoning("".join(self._parts))
        if not content:
            raise ReplySessionStateError("cannot complete an empty reply")
        await self._channel.send(
            OutgoingMessage(
                chat_id=self._source.chat_id,
                content=truncate_answer(content, self._max_content_chars),
                reply_to_message_id=self._source.message_id,
            )
        )

    async def fail(self, error_code: str) -> None:
        self._require_open("fail")
        if not error_code:
            raise ValueError("reply failure code must not be empty")
        self._state = ReplySessionState.FAILED
        self._parts.clear()
        await self._channel.send(
            OutgoingMessage(
                chat_id=self._source.chat_id,
                content=SERVICE_UNAVAILABLE_TEXT,
                reply_to_message_id=self._source.message_id,
            )
        )

    async def cancel(self) -> None:
        self._require_open("cancel")
        self._state = ReplySessionState.CANCELLED
        self._parts.clear()
        await self._channel.send(
            OutgoingMessage(
                chat_id=self._source.chat_id,
                content=CANCELLED_TEXT,
                reply_to_message_id=self._source.message_id,
            )
        )

    async def acknowledge_feedback(self, *, helpful: bool) -> None:
        """Providers without mutable cards acknowledge through their callback toast."""

        del helpful

    def _require_open(self, operation: str) -> None:
        if self._state is not ReplySessionState.OPEN:
            raise ReplySessionStateError(f"cannot {operation} a {self._state.value} reply")
