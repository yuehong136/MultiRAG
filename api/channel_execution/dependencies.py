"""FastAPI dependencies for the private Channel execution API."""

from __future__ import annotations

import secrets
from collections.abc import AsyncIterator
from threading import Lock
from typing import cast

from fastapi import Depends, HTTPException, Request, status
from pydantic import SecretStr
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.channel_control.secret_store import get_channel_secret_store
from api.channel_execution.adapters import (
    AsyncExecutionRedis,
    RedisChannelExecutionStateStore,
    SessionFactoryBindingResolver,
)
from api.channel_execution.errors import ChannelStateUnavailableError
from api.channel_execution.executors import (
    MultiRAGCanvasAgentExecutor,
    MultiRAGDialogExecutor,
    SqlAlchemyCanvasTargetDriver,
    SqlAlchemyDialogTargetDriver,
)
from api.channel_execution.models import ChannelExecutionCommand, TrustedChannelContext, WorkloadIdentity
from api.channel_execution.protocols import (
    BindingCapabilityResolver,
    BindingResolver,
    ChannelConversationStore,
    ChannelPrincipalResolver,
    ExecutionClaimStore,
    WorkloadAuthenticator,
)
from api.channel_execution.registry import TargetExecutorRegistry
from api.channel_execution.service import ChannelExecutionService, PublishedTargetExecutionService
from api.channel_runtime.tokens import derive_binding_workload_token
from api.db.db_models import get_async_db
from api.identity.providers.feishu import FeishuEnterpriseIdentityProvider
from api.identity.provisioning import HmacLinkCodeCodec, IdentityProvisioningService
from api.identity.provisioning_repository import SqlAlchemyIdentityProvisioningRepository
from api.identity_adapters.channel_credentials import SessionFactoryChannelProviderCredentialResolver
from api.identity_adapters.channel_runtime import (
    ChannelIdentityResolver,
    IdentityProviderRegistry,
    SqlAlchemyChannelIdentityAuthorityResolver,
    SqlAlchemyChannelIdentityReader,
)
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
    """Constant-time authenticator for control and binding-scoped runtimes.

    The configured token authenticates only supervisor control requests. Child
    workers receive an HMAC-derived token scoped to one binding generation.
    The class remains injectable so mTLS or workload-OIDC can replace it.
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
        master_token = self._token.get_secret_value()
        raw_binding_id = request.path_params.get("binding_id")
        binding_id = str(raw_binding_id) if raw_binding_id is not None else None
        binding_generation: int | None = None
        expected_token = master_token
        if binding_id is not None:
            raw_generation = request.headers.get("X-Channel-Binding-Generation", "")
            try:
                binding_generation = int(raw_generation)
            except ValueError:
                binding_generation = None
            try:
                expected_token = derive_binding_workload_token(
                    master_token,
                    binding_id=binding_id,
                    generation=binding_generation if binding_generation is not None else 0,
                )
            except ValueError:
                expected_token = ""
        invalid_credential = separator != " " or scheme.lower() != "bearer" or not supplied_token or not expected_token or not secrets.compare_digest(supplied_token, expected_token)
        if invalid_credential:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized channel runtime.",
            )
        return WorkloadIdentity(
            subject=self._subject,
            binding_id=binding_id,
            binding_generation=binding_generation,
        )


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
        tenant_id: str,
        principal_id: str | None,
    ) -> str | None:
        del binding_id, binding_generation, conversation_key, tenant_id, principal_id
        raise ChannelStateUnavailableError()

    async def put_session(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        conversation_key: str,
        session_id: str,
        tenant_id: str,
        principal_id: str | None,
    ) -> None:
        del binding_id, binding_generation, conversation_key, session_id, tenant_id, principal_id
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


def _require_async_session_factory() -> async_sessionmaker[AsyncSession]:
    from api.db import db_models

    session_factory = db_models.async_session_factory
    if session_factory is None:
        raise ChannelStateUnavailableError()
    return session_factory


def get_binding_resolver() -> BindingResolver:
    """Resolve trusted bindings from the MultiRAG control-plane tables."""

    return SessionFactoryBindingResolver(_require_async_session_factory())


def get_binding_capability_resolver() -> BindingCapabilityResolver:
    """Resolve one binding for a generation-scoped capability preflight."""

    return SessionFactoryBindingResolver(_require_async_session_factory())


_identity_provider_registry_lock = Lock()
_identity_provider_registry: IdentityProviderRegistry | None = None
_identity_provider_registry_session_factory: async_sessionmaker[AsyncSession] | None = None


def _build_identity_provider_registry(
    session_factory: async_sessionmaker[AsyncSession],
) -> IdentityProviderRegistry:
    credential_resolver = SessionFactoryChannelProviderCredentialResolver(
        session_factory,
        get_channel_secret_store(),
    )
    return IdentityProviderRegistry(
        {
            "feishu": lambda: FeishuEnterpriseIdentityProvider(
                credential_resolver,
            ),
        },
    )


def get_identity_provider_registry() -> IdentityProviderRegistry:
    """Retain exactly one provider registry for the active DB lifecycle.

    FastAPI may run this synchronous dependency concurrently in its thread
    pool.  ``functools.lru_cache`` permits duplicate cold-miss executions, so
    it is not sufficient for provider cache and single-flight ownership.
    """

    global _identity_provider_registry
    global _identity_provider_registry_session_factory

    session_factory = _require_async_session_factory()
    registry = _identity_provider_registry
    if registry is not None and _identity_provider_registry_session_factory is session_factory:
        return registry
    with _identity_provider_registry_lock:
        registry = _identity_provider_registry
        if registry is None or _identity_provider_registry_session_factory is not session_factory:
            registry = _build_identity_provider_registry(session_factory)
            _identity_provider_registry = registry
            _identity_provider_registry_session_factory = session_factory
        return registry


def _reset_identity_provider_registry_for_testing() -> None:
    """Drop retained process state at an explicit application/test boundary."""

    global _identity_provider_registry
    global _identity_provider_registry_session_factory

    with _identity_provider_registry_lock:
        _identity_provider_registry = None
        _identity_provider_registry_session_factory = None


def _build_identity_provisioning_service() -> IdentityProvisioningService:
    session_factory = _require_async_session_factory()
    repository = SqlAlchemyIdentityProvisioningRepository(session_factory)
    provisioning = get_app_config().identity.provisioning
    codec = HmacLinkCodeCodec(
        keys=provisioning.require_hmac_keyring(),
        active_key_id=provisioning.active_key_id,
    )
    return IdentityProvisioningService(repository, repository, codec)


def get_channel_principal_resolver(
    provider_registry: IdentityProviderRegistry = Depends(get_identity_provider_registry),
) -> ChannelPrincipalResolver:
    """Compose link authority, I3/I4/I6 and P1 over short sessions."""

    session_factory = _require_async_session_factory()
    return ChannelIdentityResolver(
        authority_resolver=SqlAlchemyChannelIdentityAuthorityResolver(
            session_factory,
        ),
        identity_reader=SqlAlchemyChannelIdentityReader(session_factory),
        provider_registry=provider_registry,
        provisioning_service_factory=_build_identity_provisioning_service,
    )


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
        cast(AsyncExecutionRedis, redis),
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


def get_published_target_execution_service(
    db: AsyncSession = Depends(get_async_db),
) -> PublishedTargetExecutionService:
    """Build the registered MultiRAG target execution graph."""

    registry = TargetExecutorRegistry(
        [
            MultiRAGCanvasAgentExecutor(SqlAlchemyCanvasTargetDriver(db)),
            MultiRAGDialogExecutor(SqlAlchemyDialogTargetDriver(db)),
        ]
    )
    return PublishedTargetExecutionService(registry)


def get_channel_execution_service(
    binding_resolver: BindingResolver = Depends(get_binding_resolver),
    principal_resolver: ChannelPrincipalResolver = Depends(get_channel_principal_resolver),
    conversation_store: ChannelConversationStore = Depends(get_channel_conversation_store),
    claim_store: ExecutionClaimStore = Depends(get_execution_claim_store),
    target_service: PublishedTargetExecutionService = Depends(get_published_target_execution_service),
) -> ChannelExecutionService:
    """Build the request-scoped execution graph over one AsyncSession."""

    return ChannelExecutionService(
        binding_resolver=binding_resolver,
        principal_resolver=principal_resolver,
        conversation_store=conversation_store,
        claim_store=claim_store,
        target_service=target_service,
    )
