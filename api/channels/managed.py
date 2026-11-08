"""Shared descriptor and safe HTTP lifecycle for text transports (CHN-P15)."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from typing import Any

import aiohttp
from pydantic import ValidationError

from api.channel_providers import provider_spec
from api.channel_runtime.schemas import RuntimeCredential
from api.channels.core.base import Channel
from api.channels.provider import ChannelWorkerError, ManagedChannelPlan, WorkerTuning
from common.app_config import AppConfig


class ManagedWorkerProvider:
    def __init__(self, name: str, factory: Callable[[dict[str, str]], Channel]) -> None:
        self.name = name
        self._factory = factory

    def tuning(self, app_config: AppConfig) -> WorkerTuning:
        del app_config
        # Conservative common defaults; these transports have no SDK-specific
        # tuning section. In particular they do not borrow Feishu configuration.
        return WorkerTuning(100, 3, 4, 86_400, 86_400, 30, 10, 8_000, 4_000, 120.0)

    def build_managed(self, *, credential: RuntimeCredential, public_config: Mapping[str, object]) -> ManagedChannelPlan:
        spec = provider_spec(self.name)
        fields = {path.rsplit(".", 1)[-1]: credential.value(path.rsplit(".", 1)[-1]) for path in spec.credential_paths}
        if any(not value for value in fields.values()):
            raise ChannelWorkerError("CHANNEL_RUNTIME_CONFIG_INVALID")
        # Revalidate the decrypted whole credential. A malformed legacy row
        # must not bypass the same constraints as a management create.
        try:
            spec.config_model.model_validate({"credential": fields})
        except ValidationError:
            raise ChannelWorkerError("CHANNEL_RUNTIME_CONFIG_INVALID") from None
        allowed = public_config.get("allowed_user_ids", [])
        if not isinstance(allowed, list) or any(not isinstance(item, str) or not item.strip() for item in allowed):
            raise ChannelWorkerError("CHANNEL_RUNTIME_CONFIG_INVALID")
        account = spec.account_identity({"credential": fields})
        if not account:
            raise ChannelWorkerError("CHANNEL_RUNTIME_CONFIG_INVALID")
        return ManagedChannelPlan(channel=self._factory(fields), account_id=account, allowed_sender_ids=frozenset(allowed))


def opaque_account(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def text_chunks(text: str, *, max_bytes: int, encoding: str) -> list[str]:
    chunks: list[str] = []
    chunk = ""
    size = 0
    for character in text:
        width = len(character.encode(encoding))
        if size + width > max_bytes:
            chunks.append(chunk)
            chunk, size = "", 0
        chunk += character
        size += width
    if chunk:
        chunks.append(chunk)
    return chunks


class HttpTextChannel(Channel):
    """One owned async session, bounded requests, and log-safe failures."""

    def __init__(self, credential: dict[str, str], identity: str) -> None:
        super().__init__()
        self.credential = credential
        self.account_id = opaque_account(identity)
        self._session: aiohttp.ClientSession | None = None
        self._task: asyncio.Task[None] | None = None

    @property
    def is_running(self) -> bool:
        return self._session is not None and not self._session.closed and self._task is not None and not self._task.done()

    async def open_http(self) -> None:
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35, connect=5), trust_env=False)

    async def request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        if self._session is None:
            raise ChannelWorkerError("CHANNEL_TRANSPORT_STOPPED")
        try:
            async with self._session.request(method, url, allow_redirects=False, **kwargs) as response:
                if response.status < 200 or response.status >= 300:
                    raise ChannelWorkerError("CHANNEL_PROVIDER_HTTP_REJECTED")
                data = await response.json()
                if not isinstance(data, dict):
                    raise ChannelWorkerError("CHANNEL_PROVIDER_RESPONSE_INVALID")
                return data
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise ChannelWorkerError("CHANNEL_PROVIDER_TRANSPORT_FAILED") from None

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self._session is not None:
            await self._session.close()
            self._session = None
