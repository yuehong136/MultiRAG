"""Exact, shared message identifiers for revision and ingestion operations."""

import asyncio
from typing import Any

from common.asyncio_utils import finish_owned_work
from core.utils.redis_conn import REDIS_CONN
from memory.revisions.domain import RevisionValidationError, positive_int

MEMORY_COUNTER_KEY = "id_generator:memory"
_PHYSICAL_IDENTIFIERS = {"message_id", "source_id", "source_message_id", "raw_source_id", "current_message_id", "legacy_message_id"}
_PHYSICAL_IDENTIFIER_LISTS = {"message_ids", "source_ids", "evicted_source_ids"}
_ALLOCATE_ID = """
local current = redis.call('GET', KEYS[1]) or '1'
if not string.match(current, '^%d+$') then
    return redis.error_reply('Invalid memory counter')
end
current = string.gsub(current, '^0+', '')
if current == '' then current = '0' end
local minimum = ARGV[1]
if string.len(current) < string.len(minimum)
   or (string.len(current) == string.len(minimum) and current < minimum) then
    redis.call('SET', KEYS[1], minimum)
elseif redis.call('EXISTS', KEYS[1]) == 0 then
    redis.call('SET', KEYS[1], current)
end
redis.call('INCRBY', KEYS[1], 1)
return redis.call('GET', KEYS[1])
"""


async def allocate_message_id(*, minimum_message_id: int, counter_key: str = MEMORY_COUNTER_KEY) -> int:
    positive_int(minimum_message_id, "invalid_message_id")
    if minimum_message_id == 2**63 - 1:
        raise RevisionValidationError("message_id_exhausted")
    value = await finish_owned_work(asyncio.to_thread(REDIS_CONN.REDIS.eval, _ALLOCATE_ID, 1, counter_key, str(minimum_message_id)))
    if isinstance(value, bytes):
        value = value.decode("ascii")
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal():
        raise RuntimeError("Memory ID allocation was not confirmed")
    return positive_int(int(value), "invalid_message_id")


def wire_message_identifiers(value: Any, key: str = "") -> Any:
    """Keep signed-int64 message IDs exact for JavaScript consumers."""
    if type(value) is int and key in _PHYSICAL_IDENTIFIERS:
        return str(value)
    if isinstance(value, dict):
        return {name: wire_message_identifiers(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [str(item) if type(item) is int and key in _PHYSICAL_IDENTIFIER_LISTS else wire_message_identifiers(item) for item in value]
    return value
