"""Generation-scoped, bounded callback admission to the independent worker."""

from __future__ import annotations

import hashlib
import json

from redis.asyncio import Redis

from api.channel_runtime.callbacks import CallbackBatch, CallbackMessage, CallbackReceipt

# One request is admitted atomically. A full queue rejects the callback so the
# provider can retry, rather than trimming messages it already acknowledged.
_ADMIT = """
if redis.call('LLEN', KEYS[1]) + #ARGV > 256 then return 0 end
for i=1,#ARGV do redis.call('RPUSH', KEYS[1], ARGV[i]) end
redis.call('EXPIRE', KEYS[1], 600)
return 1
"""
_ACK = """
local value = redis.call('LINDEX', KEYS[1], 0)
if not value then return 1 end
local entry = cjson.decode(value)
if entry.receipt ~= ARGV[1] then return 0 end
redis.call('LPOP', KEYS[1])
return 1
"""


def callback_inbox_key(tenant_id: str, binding_id: str, generation: int) -> str:
    scope = json.dumps([tenant_id, binding_id, generation], separators=(",", ":"))
    return "multirag:channel:callback:v1:" + hashlib.sha256(scope.encode()).hexdigest()


class CallbackInbox:
    def __init__(self, redis: Redis, tenant_id: str, binding_id: str, generation: int) -> None:
        self._redis = redis
        self.key = callback_inbox_key(tenant_id, binding_id, generation)

    async def admit(self, messages: list[CallbackMessage]) -> bool:
        if not messages:
            return True
        entries = []
        for message in messages:
            body = message.model_dump_json()
            receipt = hashlib.sha256(body.encode()).hexdigest()
            entries.append(CallbackReceipt(receipt=receipt, message=message).model_dump_json())
        return bool(await self._redis.eval(_ADMIT, 1, self.key, *entries))

    async def peek(self) -> CallbackBatch:
        value = await self._redis.lindex(self.key, 0)
        return CallbackBatch(items=[CallbackReceipt.model_validate_json(value)] if value else [])

    async def ack(self, receipt: str) -> bool:
        return bool(await self._redis.eval(_ACK, 1, self.key, receipt))
