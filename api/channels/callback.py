"""A callback transport pulls from the shared listener without opening ports."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from api.channel_runtime.callbacks import CallbackBatch
from api.channels.core.base import IncomingMessage, MessageAdmission
from api.channels.managed import HttpTextChannel
from api.channels.provider import ChannelWorkerError


class CallbackChannel(HttpTextChannel):
    retryable_ingress = True

    def __init__(self, credential: dict[str, str], identity: str) -> None:
        super().__init__(credential, identity)
        self._peek: Callable[[], Awaitable[CallbackBatch]] | None = None
        self._ack: Callable[[str], Awaitable[None]] | None = None

    def bind_inbox(self, peek: Callable[[], Awaitable[CallbackBatch]], ack: Callable[[str], Awaitable[None]]) -> None:
        self._peek, self._ack = peek, ack

    async def start(self) -> None:
        if self._peek is None or self._ack is None:
            raise ChannelWorkerError("CHANNEL_CALLBACK_INBOX_NOT_CONFIGURED")
        # Probe the actual generation-scoped inbox before reporting connected.
        # A missing API route or stale generation is a startup failure.
        await self._peek()
        await self.open_http()
        self._task = asyncio.create_task(self._consume(), name=f"{self.channel_id}-callback-consumer")

    async def _consume(self) -> None:
        assert self._peek is not None and self._ack is not None
        failures = 0
        while True:
            try:
                batch = await self._peek()
                for item in batch.items:
                    message = item.message
                    # Admission is the same in-memory worker boundary used by
                    # Gateway/polling transports. Crash recovery after enqueue
                    # is deliberately outside CHN-P15 (see CHN-O14).
                    if self._handler is None:
                        raise ChannelWorkerError("CHANNEL_CALLBACK_INBOX_NOT_CONFIGURED")
                    admission = await self._dispatch(IncomingMessage(channel=self.channel_id, account_id=self.account_id, **message.model_dump()))
                    if admission is MessageAdmission.RETRY:
                        await asyncio.sleep(0.5)
                        continue
                    await self._ack(item.receipt)
                failures = 0
                if not batch.items:
                    await asyncio.sleep(0.5)
            except Exception:
                failures += 1
                if failures >= 5:
                    # The generic worker monitor observes task termination and
                    # reports a classified transport failure, never this body.
                    return
                await asyncio.sleep(min(2**failures, 15))
