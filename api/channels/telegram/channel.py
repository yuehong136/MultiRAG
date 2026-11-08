"""Telegram Bot API polling with chat-scoped message IDs and no backlog drop."""

from __future__ import annotations

import asyncio
from typing import Any

from api.channels.core.base import IncomingMessage, MessageAdmission, OutgoingMessage
from api.channels.managed import HttpTextChannel, text_chunks
from api.channels.provider import ChannelWorkerError


class TelegramChannel(HttpTextChannel):
    channel_id = "telegram"
    retryable_ingress = True

    def __init__(self, credential: dict[str, str]) -> None:
        super().__init__(credential, credential["bot_id"])
        self._offset = 0

    async def _api(self, method: str, payload: dict[str, Any]) -> Any:
        data = await self.request("POST", f"https://api.telegram.org/bot{self.credential['bot_token']}/{method}", json=payload)
        if data.get("ok") is not True or "result" not in data:
            raise ChannelWorkerError("CHANNEL_PROVIDER_REQUEST_REJECTED")
        return data.get("result")

    async def start(self) -> None:
        await self.open_http()
        try:
            me = await self._api("getMe", {})
            if not isinstance(me, dict) or str(me.get("id")) != self.credential["bot_id"]:
                raise ChannelWorkerError("CHANNEL_PROVIDER_ACCOUNT_MISMATCH")
            webhook = await self._api("getWebhookInfo", {})
            if not isinstance(webhook, dict) or webhook.get("url"):
                raise ChannelWorkerError("CHANNEL_TELEGRAM_WEBHOOK_ACTIVE")
            self._task = asyncio.create_task(self._poll(), name="telegram-poller")
        except Exception:
            await self.stop()
            raise

    async def _poll(self) -> None:
        failures = 0
        while True:
            try:
                updates = await self._api("getUpdates", {"offset": self._offset, "timeout": 25, "limit": 50, "allowed_updates": ["message"]})
                if not isinstance(updates, list):
                    raise ChannelWorkerError("CHANNEL_PROVIDER_RESPONSE_INVALID")
                for update in updates:
                    if not isinstance(update, dict) or not isinstance(update.get("update_id"), int):
                        raise ChannelWorkerError("CHANNEL_PROVIDER_RESPONSE_INVALID")
                    admission = await self.handle_update(update)
                    if admission is MessageAdmission.RETRY:
                        await asyncio.sleep(0.5)
                        break
                    self._offset = max(self._offset, update["update_id"] + 1)
                failures = 0
            except Exception:
                failures += 1
                if failures >= 5:
                    return
                await asyncio.sleep(min(2**failures, 15))

    async def handle_update(self, update: dict[str, Any]) -> MessageAdmission:
        message = update.get("message")
        if not isinstance(message, dict):
            return MessageAdmission.DENIED
        sender, chat = message.get("from"), message.get("chat")
        if not isinstance(sender, dict) or sender.get("is_bot") is not False or not isinstance(chat, dict):
            return MessageAdmission.DENIED
        text = message.get("text")
        if not isinstance(text, str) or not text.strip() or chat.get("type") not in {"private", "group", "supergroup"}:
            return MessageAdmission.DENIED
        if not all(isinstance(value, int) for value in (chat.get("id"), sender.get("id"), message.get("message_id"))):
            return MessageAdmission.DENIED
        # Telegram IDs repeat in every chat; dedupe is per binding in the
        # worker. Preserve the reply's original ID as the suffix.
        incoming = IncomingMessage(
            channel=self.channel_id,
            account_id=self.account_id,
            chat_id=str(chat["id"]),
            chat_type="p2p" if chat["type"] == "private" else "group",
            message_id=f"{chat['id']}:{message['message_id']}",
            sender_id=str(sender["id"]),
            text=text,
        )
        return await self._dispatch(incoming)

    async def send(self, message: OutgoingMessage) -> None:
        if not message.chat_id.lstrip("-").isdecimal():
            raise ChannelWorkerError("CHANNEL_PROVIDER_DESTINATION_INVALID")
        for text in text_chunks(message.text, max_bytes=8000, encoding="utf-16-le"):
            payload: dict[str, Any] = {"chat_id": int(message.chat_id), "text": text}
            if message.reply_to_message_id:
                chat_id, separator, original_id = message.reply_to_message_id.rpartition(":")
                if not separator or chat_id != message.chat_id or not original_id.isdecimal():
                    raise ChannelWorkerError("CHANNEL_PROVIDER_REPLY_INVALID")
                payload["reply_parameters"] = {"message_id": int(original_id), "allow_sending_without_reply": True}
            result = await self._api("sendMessage", payload)
            if not isinstance(result, dict) or not isinstance(result.get("message_id"), int):
                raise ChannelWorkerError("CHANNEL_PROVIDER_RESPONSE_INVALID")
