"""LINE signed ingress through the API; token-authenticated push replies."""

from __future__ import annotations

from api.channels.callback import CallbackChannel
from api.channels.core.base import OutgoingMessage
from api.channels.managed import text_chunks


class LineChannel(CallbackChannel):
    channel_id = "line"

    def __init__(self, credential: dict[str, str]) -> None:
        super().__init__(credential, credential["channel_id"])

    async def send(self, message: OutgoingMessage) -> None:
        # Push does not depend on short-lived, single-use reply tokens while
        # a target waits in the worker queue or performs a long generation.
        for text in text_chunks(message.text, max_bytes=8000, encoding="utf-16-le"):
            await self.request(
                "POST",
                "https://api.line.me/v2/bot/message/push",
                headers={"Authorization": f"Bearer {self.credential['access_token']}"},
                json={"to": message.chat_id, "messages": [{"type": "text", "text": text}]},
            )
