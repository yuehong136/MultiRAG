from dataclasses import dataclass

import pytest

from api.channels.state_store import (
    FAILED_TTL_SECONDS,
    PROCESSING_TTL_SECONDS,
    RedisChannelStateStore,
    conversation_key,
)


@dataclass
class _Entry:
    value: bytes
    expires_at: float | None


class _FakeAsyncRedis:
    """Small redis.asyncio-shaped fake with deterministic expiry handling."""

    def __init__(self) -> None:
        self.now = 0.0
        self.entries: dict[str, _Entry] = {}
        self.eval_scripts: list[str] = []

    async def set(
        self,
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool | None:
        self._purge(name)
        if nx and name in self.entries:
            return None
        expires_at = None if ex is None else self.now + ex
        self.entries[name] = _Entry(value=value.encode("utf-8"), expires_at=expires_at)
        return True

    async def get(self, name: str) -> bytes | None:
        self._purge(name)
        entry = self.entries.get(name)
        return None if entry is None else entry.value

    async def delete(self, *names: str) -> int:
        deleted = 0
        for name in names:
            self._purge(name)
            if name in self.entries:
                del self.entries[name]
                deleted += 1
        return deleted

    async def eval(self, script: str, numkeys: int, *keys_and_args: str | int) -> int:
        self.eval_scripts.append(script)
        assert numkeys == 1
        key = str(keys_and_args[0])
        owner_token = str(keys_and_args[1]).encode("utf-8")
        current = await self.get(key)
        if current != owner_token:
            return 0
        if 'redis.call("EXPIRE"' in script:
            ttl_seconds = int(keys_and_args[2])
            self.entries[key].expires_at = self.now + ttl_seconds
            return 1
        if 'redis.call("DEL"' in script:
            return await self.delete(key)
        raise AssertionError("unexpected Lua script")

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def ttl(self, name: str) -> int | None:
        self._purge(name)
        entry = self.entries.get(name)
        if entry is None or entry.expires_at is None:
            return None
        return int(entry.expires_at - self.now)

    def only_key(self) -> str:
        assert len(self.entries) == 1
        return next(iter(self.entries))

    def _purge(self, name: str) -> None:
        entry = self.entries.get(name)
        if entry is not None and entry.expires_at is not None and entry.expires_at <= self.now:
            del self.entries[name]


def test_conversation_key_is_stable_opaque_and_boundary_safe() -> None:
    identifiers = ("cli-app-id", "agent-sensitive-id", "release-v17", "chat-sensitive-id", "open-sensitive-id")

    key = conversation_key(*identifiers)

    assert key == conversation_key(*identifiers)
    assert len(key) == 64
    assert all(character in "0123456789abcdef" for character in key)
    assert all(identifier not in key for identifier in identifiers)
    assert key != conversation_key("cli-app-id", "agent-sensitive-id", "release-v17", "chat-sensitive-id", "another-user")
    assert conversation_key("ab", "c", "d", "e", "f") != conversation_key("a", "bc", "d", "e", "f")


async def test_message_dedupe_statuses_and_ttls() -> None:
    redis = _FakeAsyncRedis()
    store = RedisChannelStateStore(redis, scope=("binding", "binding-1"), dedupe_ttl_seconds=7_200)

    assert await store.claim_message("om-sensitive-message") is True
    message_key = redis.only_key()
    assert "cli-app-id" not in message_key
    assert "om-sensitive-message" not in message_key
    assert redis.entries[message_key].value == b"processing"
    assert redis.ttl(message_key) == PROCESSING_TTL_SECONDS
    assert await store.claim_message("om-sensitive-message") is False

    await store.mark_replied("om-sensitive-message")
    assert redis.entries[message_key].value == b"replied"
    assert redis.ttl(message_key) == 7_200
    assert await store.claim_message("om-sensitive-message") is False

    await store.mark_executed("om-sensitive-message")
    assert redis.entries[message_key].value == b"executed"
    assert redis.ttl(message_key) == 7_200
    assert await store.claim_message("om-sensitive-message") is False

    redis.advance(7_201)
    assert await store.claim_message("om-sensitive-message") is True
    await store.mark_failed("om-sensitive-message")
    assert redis.entries[message_key].value == b"failed"
    assert redis.ttl(message_key) == FAILED_TTL_SECONDS

    redis.advance(FAILED_TTL_SECONDS + 1)
    assert await store.claim_message("om-sensitive-message") is True


async def test_session_round_trip_expiry_override_and_reset() -> None:
    redis = _FakeAsyncRedis()
    store = RedisChannelStateStore(redis, scope=("binding", "binding-1"), session_ttl_seconds=90)
    conversation = conversation_key("cli-app-id", "agent-id", "release-1", "chat-id", "open-id")

    assert await store.get_session(conversation) is None
    await store.put_session(conversation, "agent-session-1")

    session_key = redis.only_key()
    assert conversation not in session_key
    assert all(raw_identifier not in session_key for raw_identifier in ("cli-app-id", "agent-id", "chat-id", "open-id"))
    assert await store.get_session(conversation) == "agent-session-1"
    assert redis.ttl(session_key) == 90

    await store.put_session(conversation, "agent-session-2", ttl_seconds=45)
    assert await store.get_session(conversation) == "agent-session-2"
    assert redis.ttl(session_key) == 45

    await store.reset_session(conversation)
    assert await store.get_session(conversation) is None


async def test_leader_lease_is_owner_safe_and_configurable() -> None:
    redis = _FakeAsyncRedis()
    owner_tokens = iter(("owner-token-1", "owner-token-2", "owner-token-3"))
    store = RedisChannelStateStore(
        redis,
        scope=("binding", "binding-1"),
        leader_ttl_seconds=20,
        leader_renew_interval_seconds=7,
        owner_token_factory=lambda: next(owner_tokens),
    )

    assert store.leader_ttl_seconds == 20
    assert store.leader_renew_interval_seconds == 7
    assert await store.acquire_leader(lease_name="feishu-events") == "owner-token-1"
    leader_key = redis.only_key()
    assert "cli-app-id" not in leader_key
    assert "feishu-events" not in leader_key
    assert redis.ttl(leader_key) == 20
    assert await store.acquire_leader(lease_name="feishu-events") is None

    redis.advance(5)
    assert await store.renew_leader("wrong-owner", lease_name="feishu-events") is False
    assert redis.ttl(leader_key) == 15
    assert await store.renew_leader("owner-token-1", lease_name="feishu-events") is True
    assert redis.ttl(leader_key) == 20

    assert await store.release_leader("wrong-owner", lease_name="feishu-events") is False
    assert await redis.get(leader_key) == b"owner-token-1"
    assert await store.release_leader("owner-token-1", lease_name="feishu-events") is True
    assert await redis.get(leader_key) is None

    assert len(redis.eval_scripts) == 4
    assert all('redis.call("GET"' in script for script in redis.eval_scripts)
    assert any('redis.call("EXPIRE"' in script for script in redis.eval_scripts)
    assert any('redis.call("DEL"' in script for script in redis.eval_scripts)


async def test_redis_errors_propagate_without_in_memory_fallback() -> None:
    class _FailingRedis(_FakeAsyncRedis):
        async def set(
            self,
            name: str,
            value: str,
            *,
            ex: int | None = None,
            nx: bool = False,
        ) -> bool | None:
            raise ConnectionError("redis unavailable")

    store = RedisChannelStateStore(_FailingRedis(), scope=("binding", "binding-1"))

    with pytest.raises(ConnectionError, match="redis unavailable"):
        await store.claim_message("om-sensitive-message")


@pytest.mark.parametrize(
    ("field", "kwargs"),
    [
        ("dedupe_ttl_seconds", {"dedupe_ttl_seconds": 0}),
        ("session_ttl_seconds", {"session_ttl_seconds": 0}),
        ("leader_ttl_seconds", {"leader_ttl_seconds": 0}),
        ("leader_renew_interval_seconds", {"leader_renew_interval_seconds": 0}),
    ],
)
def test_store_rejects_non_positive_ttls(field: str, kwargs: dict[str, int]) -> None:
    with pytest.raises(ValueError, match=field):
        RedisChannelStateStore(_FakeAsyncRedis(), scope=("binding", "binding-1"), **kwargs)


def test_store_rejects_renew_interval_not_shorter_than_lease() -> None:
    with pytest.raises(ValueError, match="must be less"):
        RedisChannelStateStore(
            _FakeAsyncRedis(),
            scope=("binding", "binding-1"),
            leader_ttl_seconds=20,
            leader_renew_interval_seconds=20,
        )


async def test_two_bindings_on_one_provider_account_do_not_share_a_namespace() -> None:
    """Closes the cross-tenant lease-squatting hole.

    The lease is taken *before* the credential is verified, so while the
    namespace came from the provider account id alone, any tenant that knew
    another tenant's (non-secret) app id could enable a channel with a junk
    secret, win the lease, and keep the rightful tenant's worker from ever
    restarting. Same Redis, same lease name, different binding: both must win.
    """

    redis = _FakeAsyncRedis()
    victim = RedisChannelStateStore(redis, scope=("binding", "binding-tenant-a"))
    squatter = RedisChannelStateStore(redis, scope=("binding", "binding-tenant-b"))

    assert await victim.acquire_leader(lease_name="feishu") is not None
    assert await squatter.acquire_leader(lease_name="feishu") is not None
    assert len(redis.entries) == 2


async def test_one_binding_still_holds_its_own_lease_exclusively() -> None:
    """Per-binding scoping must not weaken the single-runner guarantee."""

    redis = _FakeAsyncRedis()
    first = RedisChannelStateStore(redis, scope=("binding", "binding-1"))
    second = RedisChannelStateStore(redis, scope=("binding", "binding-1"))

    assert await first.acquire_leader(lease_name="feishu") is not None
    assert await second.acquire_leader(lease_name="feishu") is None


async def test_demo_scope_cannot_collide_with_a_managed_binding() -> None:
    redis = _FakeAsyncRedis()
    demo = RedisChannelStateStore(redis, scope=("demo", "feishu", "cli-app-id"))
    managed = RedisChannelStateStore(redis, scope=("binding", "cli-app-id"))

    assert await demo.acquire_leader(lease_name="feishu") is not None
    assert await managed.acquire_leader(lease_name="feishu") is not None
    assert len(redis.entries) == 2


@pytest.mark.parametrize("scope", [(), ("",), ("binding", "")])
def test_store_rejects_an_empty_scope(scope: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="scope"):
        RedisChannelStateStore(_FakeAsyncRedis(), scope=scope)
