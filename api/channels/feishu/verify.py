"""Feishu credential probe: authenticate once, build nothing (CHN-O6).

Runs in the **API** process, so it must not import lark-oapi -- the SDK
installs a process-global event loop, and this module exists precisely so an
admin can check a credential without starting a worker. One plain HTTPS POST
answers the only question being asked.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Final

import httpx

from api.channels.verification import ChannelCredentialRejected, ChannelVerificationUnavailable

LOGGER = logging.getLogger(__name__)

_TOKEN_PATH: Final = "/open-apis/auth/v3/tenant_access_token/internal"
_HOSTS: Final[dict[str, str]] = {
    "feishu": "https://open.feishu.cn",
    "lark": "https://open.larksuite.com",
}
# The endpoint answers in well under a second when reachable. The budget is a
# ceiling on how long an admin stares at a spinner, not a latency target.
_TIMEOUT_SECONDS: Final = 5.0


class _FeishuCredentialVerifier:
    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        # Only a test seam. ``None`` is httpx's own default transport, so the
        # shipped verifier is the plain one.
        self._transport = transport

    async def verify_credential(
        self,
        *,
        credential: Mapping[str, str],
        public_config: Mapping[str, object],
    ) -> None:
        app_id = credential.get("app_id", "")
        app_secret = credential.get("app_secret", "")
        if not app_id or not app_secret:
            raise ChannelCredentialRejected("CHANNEL_CREDENTIAL_INCOMPLETE")

        domain = public_config.get("domain")
        host = _HOSTS.get(domain if isinstance(domain, str) else "", _HOSTS["feishu"])

        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT_SECONDS, transport=self._transport) as client:
                response = await client.post(
                    f"{host}{_TOKEN_PATH}",
                    json={"app_id": app_id, "app_secret": app_secret},
                )
        except httpx.HTTPError as error:
            # Never a rejection: we did not hear back, so we know nothing about
            # the credential. Log the exception *type* only -- httpx puts the
            # request URL in str(error).
            LOGGER.warning(
                "channel_verify_event=probe_unreachable provider=feishu error_type=%s",
                type(error).__name__,
            )
            raise ChannelVerificationUnavailable from error

        _raise_for_feishu_answer(response)


def _raise_for_feishu_answer(response: httpx.Response) -> None:
    """Map one Feishu envelope onto verdict / no-verdict.

    Feishu answers a wrong App Secret with HTTP 400 and a non-zero ``code``,
    so status alone cannot tell "bad credential" from "service degraded".
    Anything that is not a parseable envelope is treated as no-verdict.
    """

    try:
        payload: Any = response.json()
    except ValueError:
        raise ChannelVerificationUnavailable from None
    if not isinstance(payload, dict) or not isinstance(payload.get("code"), int):
        raise ChannelVerificationUnavailable
    code = payload["code"]
    if code == 0:
        return
    LOGGER.info(
        "channel_verify_event=probe_rejected provider=feishu status=%s provider_code=%s",
        response.status_code,
        code,
    )
    if response.status_code >= 500:
        raise ChannelVerificationUnavailable
    raise ChannelCredentialRejected


CREDENTIAL_VERIFIER: Final = _FeishuCredentialVerifier()
