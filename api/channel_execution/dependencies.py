"""FastAPI dependencies for the private Channel execution API."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator

from fastapi import Depends, HTTPException, Request, status
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from api.channel_control.repository import SqlAlchemyChannelRepository
from api.channel_execution.adapters import RedisChannelExecutionStateStore, SqlAlchemyBindingResolver
from api.channel_execution.errors import ChannelStateUnavailableError
from api.channel_execution.executors import (
    MultiRAGCanvasAgentExecutor,
    MultiRAGDialogExecutor,
    SqlAlchemyCanvasCompletionAdapter,
    SqlAlchemyDialogCompletionAdapter,
)
from api.channel_execution.models import ChannelExecutionCommand, TrustedChannelContext, WorkloadIdentity
from api.channel_execution.protocols import (
    BindingResolver,
    ChannelConversationStore,
    ExecutionClaimStore,
    WorkloadAuthenticator,
)
from api.channel_execution.registry import TargetExecutorRegistry
from api.channel_execution.service import ChannelExecutionService, PublishedTargetExecutionService
from api.db.db_models import get_async_db
from common.app_config import get_app_config


class DenyAllWorkloadAuthenticator:
    """Fail-closed default until workload identity infrastructure is configured."""

    async def authenticate(self, request: Request) -> WorkloadIdentity:
        del request
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized channel runtime.",
        )


class StaticBearerWorkloadAuthenticator:
    """Constant-time authenticator for the first private Runner deployment.

    The class is intentionally injectable so mTLS or workload-OIDC can replace
    it without changing the execution route or domain services.
    """

    def __init__(
        self,
        token: SecretStr,
        *,
        subject: str = "multirag-channel-runtime",
    ) -> None:
        self._token = token
        self._subject = subject

    async def authenticate(self, request: Request) -> WorkloadIdentity:
        authorization = request.headers.get("Authorization", "")
        scheme, separator, supplied_token = authorization.partition(" ")
        expected_token = self._token.get_secret_value()
        invalid_credential = separator != " " or scheme.lower() != "bearer" or not supplied_token or not expected_token or not secrets.compare_digest(supplied_token, expected_token)
        if invalid_credential:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized channel runtime.",
            )
        return WorkloadIdentity(subject=self._subject)


class MissingBindingResolver:
    """Fail-closed placeholder until the Channel binding store is installed."""

    async def resolve(
        self,
        *,
        binding_id: str,
        workload: WorkloadIdentity,
        command: ChannelExecutionCommand,
    ) -> TrustedChannelContext | None:
        del binding_id, workload, command
        return None


class MissingChannelConversationStore:
    """Fail closed instead of silently degrading session state to process memory."""

    async def get_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
    ) -> str | None:
        del binding_id, binding_generation, conversation_key
        raise ChannelStateUnavailableError()

    async def put_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
        session_id: str,
    ) -> None:
        del binding_id, binding_generation, conversation_key, session_id
        raise ChannelStateUnavailableError()

    async def reset_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
    ) -> None:
        del binding_id, binding_generation, conversation_key
        raise ChannelStateUnavailableError()


class MissingExecutionClaimStore:
    """Fail closed when distributed event ownership has not been configured."""

    async def claim(self, *, binding_id: str, event_id: str) -> bool:
        del binding_id, event_id
        raise ChannelStateUnavailableError()

    async def complete(self, *, binding_id: str, event_id: str) -> None:
        del binding_id, event_id
        raise ChannelStateUnavailableError()

    async def fail(self, *, binding_id: str, event_id: str) -> None:
        del binding_id, event_id
        raise ChannelStateUnavailableError()


def get_workload_authenticator() -> WorkloadAuthenticator:
    """Build the configured private-API authenticator, otherwise fail closed."""

    channels = getattr(get_app_config(), "channels", None)
    control = getattr(channels, "control", None)
    configured_token = getattr(control, "internal_api_token", None)
    if isinstance(configured_token, SecretStr) and configured_token.get_secret_value():
        return StaticBearerWorkloadAuthenticator(configured_token)
    return DenyAllWorkloadAuthenticator()


async def require_channel_workload(
    request: Request,
    authenticator: WorkloadAuthenticator = Depends(get_workload_authenticator),
) -> WorkloadIdentity:
    """Authenticate the runtime process before any binding can be resolved."""

    return await authenticator.authenticate(request)


def get_binding_resolver(db: AsyncSession = Depends(get_async_db)) -> BindingResolver:
    """Resolve trusted bindings from the MultiRAG control-plane tables."""

    return SqlAlchemyBindingResolver(SqlAlchemyChannelRepository(db))


def _redis_host_port(raw_host: str) -> tuple[str, int]:
    host, separator, raw_port = raw_host.rpartition(":")
    if not separator:
        return raw_host, 6379
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ChannelStateUnavailableError() from exc
    normalized_host = host.removeprefix("[").removesuffix("]")
    if not normalized_host or not 0 < port < 65_536:
        raise ChannelStateUnavailableError()
    return normalized_host, port


async def get_channel_execution_redis() -> AsyncIterator[Redis]:
    """Yield a bounded async Redis client for one private execution request."""

    config = get_app_config().redis
    host, port = _redis_host_port(config.host)
    redis = Redis(
        host=host,
        port=port,
        db=config.db,
        username=config.username or None,
        password=config.password or None,
        decode_responses=True,
        socket_connect_timeout=5,
        socket_timeout=5,
    )
    try:
        yield redis
    finally:
        await redis.aclose()


def get_channel_execution_state_store(
    redis: Redis = Depends(get_channel_execution_redis),
) -> RedisChannelExecutionStateStore:
    """Create the shared conversation/idempotency adapter for this request."""

    control = get_app_config().channels.control
    return RedisChannelExecutionStateStore(
        redis,
        session_ttl_seconds=control.session_ttl_seconds,
        dedupe_ttl_seconds=control.dedupe_ttl_seconds,
    )


def get_channel_conversation_store(
    store: RedisChannelExecutionStateStore = Depends(get_channel_execution_state_store),
) -> ChannelConversationStore:
    """Return the distributed conversation store; never fall back to memory."""

    return store


def get_execution_claim_store(
    store: RedisChannelExecutionStateStore = Depends(get_channel_execution_state_store),
) -> ExecutionClaimStore:
    """Return the same distributed store for atomic event ownership."""

    return store


def get_channel_execution_service(
    db: AsyncSession = Depends(get_async_db),
    binding_resolver: BindingResolver = Depends(get_binding_resolver),
    conversation_store: ChannelConversationStore = Depends(get_channel_conversation_store),
    claim_store: ExecutionClaimStore = Depends(get_execution_claim_store),
) -> ChannelExecutionService:
    """Build the request-scoped execution graph over one AsyncSession."""

    registry = TargetExecutorRegistry(
        [
            MultiRAGCanvasAgentExecutor(SqlAlchemyCanvasCompletionAdapter(db)),
            MultiRAGDialogExecutor(SqlAlchemyDialogCompletionAdapter(db)),
        ]
    )
    return ChannelExecutionService(
        binding_resolver=binding_resolver,
        conversation_store=conversation_store,
        claim_store=claim_store,
        target_service=PublishedTargetExecutionService(registry),
    )
