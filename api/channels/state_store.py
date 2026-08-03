"""Redis-backed state used by inbound channel adapters.

The store deliberately keeps transport identifiers out of Redis keys.  Every
identifier is SHA-256 hashed before it becomes a key component; opaque session
IDs and lease owner tokens are stored only as values.
"""

import hashlib
import secrets
from collections.abc import Callable
from typing import Protocol, runtime_checkable

PROCESSING_TTL_SECONDS = 600
FAILED_TTL_SECONDS = 3_600
DEFAULT_DEDUPE_TTL_SECONDS = 86_400
DEFAULT_SESSION_TTL_SECONDS = 86_400
DEFAULT_LEADER_TTL_SECONDS = 30
DEFAULT_LEADER_RENEW_INTERVAL_SECONDS = 10

_KEY_PREFIX = "multirag:channel:v1"
_DEFAULT_LEASE_NAME = "event-consumer"

_RENEW_LEADER_LUA = """
local current = redis.call("GET", KEYS[1])
if current == ARGV[1] then
    return redis.call("EXPIRE", KEYS[1], tonumber(ARGV[2]))
end
return 0
""".strip()

_RELEASE_LEADER_LUA = """
local current = redis.call("GET", KEYS[1])
if current == ARGV[1] then
    return redis.call("DEL", KEYS[1])
end
return 0
""".strip()


@runtime_checkable
class AsyncRedisClient(Protocol):
    """Subset of ``redis.asyncio.Redis`` required by the state store."""

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None: ...

    async def get(self, name: str) -> bytes | str | None: ...

    async def delete(self, *names: str) -> int: ...

    async def eval(self, script: str, numkeys: int, *keys_and_args: str | int) -> int | bool: ...


@runtime_checkable
class ChannelStateStore(Protocol):
    """State operations consumed by a channel event service."""

    async def claim_message(self, message_id: str) -> bool: ...

    async def mark_replied(self, message_id: str) -> None: ...

    async def mark_executed(self, message_id: str) -> None: ...

    async def mark_failed(self, message_id: str) -> None: ...

    async def get_session(self, conversation: str) -> str | None: ...

    async def put_session(self, conversation: str, session_id: str, *, ttl_seconds: int | None = None) -> None: ...

    async def reset_session(self, conversation: str) -> None: ...


def _hash_identifiers(*identifiers: str) -> str:
    """Hash an unambiguous, length-prefixed sequence of identifiers."""

    digest = hashlib.sha256()
    for identifier in identifiers:
        encoded = identifier.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, byteorder="big"))
        digest.update(encoded)
    return digest.hexdigest()


def conversation_key(app_id: str, agent_id: str, release_marker: str, chat_id: str, sender_open_id: str) -> str:
    """Return a stable opaque key for one channel conversation."""

    return _hash_identifiers(app_id, agent_id, release_marker, chat_id, sender_open_id)


def binding_conversation_key(binding_id: str, provider: str, chat_id: str, sender_id: str) -> str:
    """Return the server-side conversation key for a managed binding."""

    return _hash_identifiers("binding-v1", binding_id, provider, chat_id, sender_id)


class RedisChannelStateStore:
    """Redis implementation for event deduplication, sessions, and leadership."""

    def __init__(
        self,
        redis: AsyncRedisClient,
        *,
        app_id: str,
        dedupe_ttl_seconds: int = DEFAULT_DEDUPE_TTL_SECONDS,
        session_ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
        leader_ttl_seconds: int = DEFAULT_LEADER_TTL_SECONDS,
        leader_renew_interval_seconds: int = DEFAULT_LEADER_RENEW_INTERVAL_SECONDS,
        owner_token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._redis = redis
        self._namespace = f"{_KEY_PREFIX}:{_hash_identifiers(app_id)}"
        self._dedupe_ttl_seconds = self._positive_ttl("dedupe_ttl_seconds", dedupe_ttl_seconds)
        self._session_ttl_seconds = self._positive_ttl("session_ttl_seconds", session_ttl_seconds)
        self._leader_ttl_seconds = self._positive_ttl("leader_ttl_seconds", leader_ttl_seconds)
        self._leader_renew_interval_seconds = self._positive_ttl("leader_renew_interval_seconds", leader_renew_interval_seconds)
        if self._leader_renew_interval_seconds >= self._leader_ttl_seconds:
            raise ValueError("leader_renew_interval_seconds must be less than leader_ttl_seconds")
        self._owner_token_factory = owner_token_factory or self._new_owner_token

    @property
    def leader_renew_interval_seconds(self) -> int:
        """Recommended interval between leader lease renewals."""

        return self._leader_renew_interval_seconds

    @property
    def leader_ttl_seconds(self) -> int:
        """Redis TTL used for the leader lease."""

        return self._leader_ttl_seconds

    async def claim_message(self, message_id: str) -> bool:
        """Atomically claim a message for processing for ten minutes."""

        claimed = await self._redis.set(
            self._message_key(message_id),
            "processing",
            nx=True,
            ex=PROCESSING_TTL_SECONDS,
        )
        return bool(claimed)

    async def mark_replied(self, message_id: str) -> None:
        """Mark a successfully replied message for the configured dedupe TTL."""

        await self._redis.set(
            self._message_key(message_id),
            "replied",
            ex=self._dedupe_ttl_seconds,
        )

    async def mark_executed(self, message_id: str) -> None:
        """Tombstone a message whose Agent/tool execution may have occurred.

        Ambiguous downstream failures retain the full dedupe window so a
        delayed duplicate cannot execute side-effecting tools a second time.
        """

        await self._redis.set(
            self._message_key(message_id),
            "executed",
            ex=self._dedupe_ttl_seconds,
        )

    async def mark_failed(self, message_id: str) -> None:
        """Keep a failed-message tombstone for one hour."""

        await self._redis.set(
            self._message_key(message_id),
            "failed",
            ex=FAILED_TTL_SECONDS,
        )

    async def get_session(self, conversation: str) -> str | None:
        """Return the opaque Agent session ID for a conversation."""

        value = await self._redis.get(self._session_key(conversation))
        if value is None:
            return None
        if isinstance(value, bytes):
            return value.decode("utf-8")
        if isinstance(value, str):
            return value
        raise TypeError(f"Redis returned unsupported session value type: {type(value).__name__}")

    async def put_session(self, conversation: str, session_id: str, *, ttl_seconds: int | None = None) -> None:
        """Store an opaque Agent session ID with a bounded lifetime."""

        ttl = self._session_ttl_seconds if ttl_seconds is None else self._positive_ttl("ttl_seconds", ttl_seconds)
        await self._redis.set(self._session_key(conversation), session_id, ex=ttl)

    async def reset_session(self, conversation: str) -> None:
        """Delete the Agent session associated with a conversation."""

        await self._redis.delete(self._session_key(conversation))

    async def acquire_leader(self, *, lease_name: str = _DEFAULT_LEASE_NAME) -> str | None:
        """Acquire a named leader lease and return its unguessable owner token."""

        owner_token = self._owner_token_factory()
        acquired = await self._redis.set(
            self._leader_key(lease_name),
            owner_token,
            nx=True,
            ex=self._leader_ttl_seconds,
        )
        return owner_token if acquired else None

    async def renew_leader(self, owner_token: str, *, lease_name: str = _DEFAULT_LEASE_NAME) -> bool:
        """Renew the lease only when ``owner_token`` still owns it."""

        renewed = await self._redis.eval(
            _RENEW_LEADER_LUA,
            1,
            self._leader_key(lease_name),
            owner_token,
            self._leader_ttl_seconds,
        )
        return bool(renewed)

    async def release_leader(self, owner_token: str, *, lease_name: str = _DEFAULT_LEASE_NAME) -> bool:
        """Release the lease only when ``owner_token`` still owns it."""

        released = await self._redis.eval(
            _RELEASE_LEADER_LUA,
            1,
            self._leader_key(lease_name),
            owner_token,
        )
        return bool(released)

    def _message_key(self, message_id: str) -> str:
        return f"{self._namespace}:message:{_hash_identifiers(message_id)}"

    def _session_key(self, conversation: str) -> str:
        return f"{self._namespace}:session:{_hash_identifiers(conversation)}"

    def _leader_key(self, lease_name: str) -> str:
        return f"{self._namespace}:leader:{_hash_identifiers(lease_name)}"

    @staticmethod
    def _positive_ttl(name: str, value: int) -> int:
        if value <= 0:
            raise ValueError(f"{name} must be greater than zero")
        return value

    @staticmethod
    def _new_owner_token() -> str:
        return secrets.token_urlsafe(32)
