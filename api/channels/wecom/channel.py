"""WeCom app text replies with one token cache per isolated worker."""

from __future__ import annotations

import asyncio
import time

from api.channels.callback import CallbackChannel
from api.channels.core.base import OutgoingMessage
from api.channels.provider import ChannelWorkerError


class WeComChannel(CallbackChannel):
    channel_id = "wecom"

    def __init__(self, credential: dict[str, str]) -> None:
        super().__init__(credential, credential["corp_id"] + ":" + credential["agent_id"])
        self._token = ""
        self._expires = 0.0
        self._token_lock = asyncio.Lock()

    async def _access_token(self) -> str:
        async with self._token_lock:
            if self._token and time.monotonic() < self._expires:
                return self._token
            data = await self.request("GET", "https://qyapi.weixin.qq.com/cgi-bin/gettoken", params={"corpid": self.credential["corp_id"], "corpsecret": self.credential["corp_secret"]})
            if data.get("errcode") != 0 or not isinstance(data.get("access_token"), str):
                raise ChannelWorkerError("CHANNEL_PROVIDER_CREDENTIAL_REJECTED")
            self._token = data["access_token"]
            self._expires = time.monotonic() + max(0, int(data.get("expires_in", 7200)) - 60)
            return self._token

    async def send(self, message: OutgoingMessage) -> None:
        if "|" in message.chat_id or message.chat_id.casefold() == "@all" or not message.chat_id or message.chat_id != message.chat_id.strip():
            raise ChannelWorkerError("CHANNEL_PROVIDER_DESTINATION_INVALID")
        # WeCom limits text to 2048 UTF-8 bytes, rather than Python characters.
        chunks: list[str] = []
        chunk = ""
        for character in message.text:
            if len((chunk + character).encode()) > 2000:
                chunks.append(chunk)
                chunk = ""
            chunk += character
        if chunk:
            chunks.append(chunk)
        for text in chunks:
            token = await self._access_token()
            data = await self.request(
                "POST",
                "https://qyapi.weixin.qq.com/cgi-bin/message/send",
                params={"access_token": token},
                json={"touser": message.chat_id, "msgtype": "text", "agentid": int(self.credential["agent_id"]), "text": {"content": text}, "safe": 0},
            )
            if data.get("errcode") != 0 or data.get("invaliduser") or data.get("unlicenseduser"):
                if data.get("errcode") in {40014, 42001}:
                    self._token, self._expires = "", 0.0
                raise ChannelWorkerError("CHANNEL_PROVIDER_SEND_REJECTED")
