"""Discord Gateway transport with verified startup and bounded shutdown."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

from api.channels.core.base import Channel, IncomingMessage, OutgoingMessage
from api.channels.managed import opaque_account
from api.channels.provider import ChannelWorkerError

LOGGER = logging.getLogger(__name__)


class _Client(discord.Client):
    async def on_error(self, event_method: str, *args: Any, **kwargs: Any) -> None:
        # SDK's default exception logger can include the raw event and content.
        LOGGER.error("channel_event=discord_callback_failed error_code=CHANNEL_PROVIDER_EVENT_FAILED")


class DiscordChannel(Channel):
    channel_id = "discord"

    def __init__(self, credential: dict[str, str]) -> None:
        super().__init__()
        self._credential = credential
        self.account_id = opaque_account(credential["bot_id"])
        intents = discord.Intents.default()
        intents.message_content = True
        self._client = _Client(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self._client.on_message = self.handle_message
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done() and not self._client.is_closed()

    async def start(self) -> None:
        try:
            await self._client.login(self._credential["bot_token"])
            if self._client.user is None or str(self._client.user.id) != self._credential["bot_id"]:
                raise ChannelWorkerError("CHANNEL_PROVIDER_ACCOUNT_MISMATCH")
            self._task = asyncio.create_task(self._connect(), name="discord-gateway")
            async with asyncio.timeout(30):
                await self._client.wait_until_ready()
            if not self.is_running:
                raise ChannelWorkerError("CHANNEL_TRANSPORT_STOPPED")
        except Exception:
            await self.stop()
            raise ChannelWorkerError("CHANNEL_PROVIDER_START_FAILED") from None

    async def _connect(self) -> None:
        try:
            await self._client.connect(reconnect=True)
        except Exception:
            LOGGER.error("channel_event=discord_gateway_failed error_code=CHANNEL_PROVIDER_CONNECTION_FAILED")

    async def stop(self) -> None:
        try:
            async with asyncio.timeout(5):
                await self._client.close()
        finally:
            if self._task is not None:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
                self._task = None

    async def handle_message(self, message: discord.Message) -> None:
        if message.author.bot or message.webhook_id or not message.content.strip():
            return
        # Discord group DMs and guild threads remain group traffic under the
        # existing admin policy. Unrecognised destinations fail closed.
        if isinstance(message.channel, discord.DMChannel):
            chat_type = "p2p"
        elif isinstance(message.channel, (discord.TextChannel, discord.Thread, discord.GroupChannel)):
            chat_type = "group"
        else:
            return
        await self._dispatch(
            IncomingMessage(
                channel=self.channel_id,
                account_id=self.account_id,
                chat_id=str(message.channel.id),
                chat_type=chat_type,
                message_id=str(message.id),
                sender_id=str(message.author.id),
                text=message.content,
            )
        )

    async def send(self, message: OutgoingMessage) -> None:
        try:
            target = self._client.get_channel(int(message.chat_id)) or await self._client.fetch_channel(int(message.chat_id))
            if not isinstance(target, discord.abc.Messageable):
                raise ValueError
            reference = None
            if message.reply_to_message_id:
                reference = discord.MessageReference(message_id=int(message.reply_to_message_id), channel_id=int(message.chat_id), fail_if_not_exists=False)
            # Discord counts UTF-16 code units. Keep surrogate pairs together.
            chunk = ""
            for character in message.text:
                if len((chunk + character).encode("utf-16-le")) > 4000:
                    await target.send(chunk, reference=reference, allowed_mentions=discord.AllowedMentions.none())
                    chunk = ""
                chunk += character
            if chunk:
                await target.send(chunk, reference=reference, allowed_mentions=discord.AllowedMentions.none())
        except Exception:
            raise ChannelWorkerError("CHANNEL_PROVIDER_SEND_REJECTED") from None
