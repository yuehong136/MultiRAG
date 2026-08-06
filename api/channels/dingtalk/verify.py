"""DingTalk credential probe: open a gateway connection and walk away (CHN-O6).

DingTalk has no "just authenticate" endpoint, so the check is the first step of
the real handshake -- ``connections/open`` returns an endpoint plus a ticket,
and we drop both without dialling the WebSocket. That is also why this probe is
honest: it exercises the same call the worker will make on startup.

Runs in the API process, so it talks HTTP directly rather than reusing
``dingtalk/channel.py`` -- that module owns a long-lived aiohttp session and a
reconnect loop, neither of which belongs in a request handler.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final

import httpx

from api.channels.verification import ChannelCredentialRejected, ChannelVerificationUnavailable

LOGGER = logging.getLogger(__name__)

_OPEN_CONNECTION_URL: Final = "https://api.dingtalk.com/v1.0/gateway/connections/open"
_BOT_MESSAGE_TOPIC: Final = "/v1.0/im/bot/messages/get"
_TIMEOUT_SECONDS: Final = 5.0


class _DingTalkCredentialVerifier:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # Only a test seam; ``None`` is httpx's own default transport.
        self._transport = transport

    async def verify_credential(
        self,
        *,
        credential: Mapping[str, str],
        public_config: Mapping[str, object],
    ) -> None:
        del public_config  # DingTalk has one host; nothing in config selects it.
        client_id = credential.get("client_id", "")
        client_secret = credential.get("client_secret", "")
        if not client_id or not client_secret:
            raise ChannelCredentialRejected("CHANNEL_CREDENTIAL_INCOMPLETE")

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=self._transport) as client:
                response = await client.post(
                    _OPEN_CONNECTION_URL,
                    json={
                        "clientId": client_id,
                        "clientSecret": client_secret,
                        "subscriptions": [{"type": "CALLBACK", "topic": _BOT_MESSAGE_TOPIC}],
                    },
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as error:
            LOGGER.warning(
                "channel_verify_event=probe_unreachable provider=dingtalk error_type=%s",
                type(error).__name__,
            )
            raise ChannelVerificationUnavailable from error

        if response.status_code >= 500:
            raise ChannelVerificationUnavailable
        if response.status_code >= 400:
            LOGGER.info(
                "channel_verify_event=probe_rejected provider=dingtalk status=%s",
                response.status_code,
            )
            raise ChannelCredentialRejected
        # A 2xx without a ticket is not a pass. It means the contract moved
        # under us, and calling that "credential is good" would be a guess.
        try:
            payload = response.json()
        except ValueError:
            raise ChannelVerificationUnavailable from None
        if not isinstance(payload, dict) or not payload.get("ticket") or not payload.get("endpoint"):
            raise ChannelVerificationUnavailable


CREDENTIAL_VERIFIER: Final = _DingTalkCredentialVerifier()
