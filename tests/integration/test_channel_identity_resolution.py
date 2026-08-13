"""Real PostgreSQL authority and JIT contracts for Channel identity promotion."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import TracebackType

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.channel_execution.models import (
    ChannelExecutionCommand,
    ExecutionTargetRef,
    TrustedChannelContext,
)
from api.db import IdentityProviderHealthState, UserAccountKind, UserTenantRole
from api.db.db_models import (
    ChannelBinding,
    ChatChannel,
    ExternalIdentity,
    ExternalIdentityAlias,
    IdentityBindingEvent,
    IdentityProviderAccount,
    IdentityProviderChannelLink,
    IdentityProviderTenant,
    IdentityTenantPolicy,
    Tenant,
    User,
    UserTenant,
)
from api.identity.contracts import ProvisioningMode
from api.identity.providers.contracts import (
    ExternalIdentityAssertion,
    ProviderContext,
    ProviderDirectoryStatus,
    ProviderIdentity,
    ProviderIdentityResult,
    ProviderIdentityStatus,
)
from api.identity.provisioning import HmacLinkCodeCodec, IdentityProvisioningService
from api.identity.provisioning_repository import (
    SqlAlchemyIdentityProvisioningRepository,
)
from api.identity_adapters.channel_runtime import (
    ChannelIdentityAuthorityStatus,
    ChannelIdentityResolver,
    IdentityProviderRegistry,
    SqlAlchemyChannelIdentityAuthorityResolver,
    SqlAlchemyChannelIdentityReader,
)


class _DirectoryProvider:
    def __init__(
        self,
        result: ProviderIdentityResult,
        *,
        before_resolve: Callable[[], None] = lambda: None,
    ) -> None:
        self.result = result
        self.calls = 0
        self._before_resolve = before_resolve

    async def resolve(
        self,
        context: ProviderContext,
        assertion: ExternalIdentityAssertion,
    ) -> ProviderIdentityResult:
        del context, assertion
        self._before_resolve()
        self.calls += 1
        return self.result

    async def refresh(
        self,
        context: ProviderContext,
        provider_user_id: str,
    ) -> ProviderIdentityResult:
        del context, provider_user_id
        raise AssertionError("Channel promotion must resolve its event assertion")


class _SessionTracker:
    def __init__(self) -> None:
        self.open_sessions = 0
        self.max_open_sessions = 0


class _TrackedSessionContext:
    def __init__(self, session: AsyncSession, tracker: _SessionTracker) -> None:
        self._session = session
        self._tracker = tracker

    async def __aenter__(self) -> AsyncSession:
        entered = await self._session.__aenter__()
        self._tracker.open_sessions += 1
        self._tracker.max_open_sessions = max(
            self._tracker.max_open_sessions,
            self._tracker.open_sessions,
        )
        return entered

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None:
        try:
            return await self._session.__aexit__(
                exc_type,
                exc_value,
                traceback,
            )
        finally:
            self._tracker.open_sessions -= 1


class _TrackingSessionFactory(async_sessionmaker[AsyncSession]):
    def __init__(
        self,
        engine: AsyncEngine,
        tracker: _SessionTracker,
    ) -> None:
        super().__init__(engine, expire_on_commit=False)
        self._tracker = tracker

    def __call__(self) -> _TrackedSessionContext:
        return _TrackedSessionContext(super().__call__(), self._tracker)


def _command(
    *,
    provider_tenant_key: str,
    open_id: str,
    provider_user_id: str,
) -> ChannelExecutionCommand:
    return ChannelExecutionCommand.model_validate(
        {
            "event_id": "event-1",
            "conversation_key": "feishu:chat:user",
            "message": {"type": "text", "content": "hello"},
            "actor": {
                "provider": "feishu",
                "subject": open_id,
                "conversation": "chat-secret",
                "identity": {
                    "provider": "feishu",
                    "provider_tenant_key": provider_tenant_key,
                    "identifiers": [
                        {"kind": "open_id", "value": open_id},
                        {"kind": "user_id", "value": provider_user_id},
                    ],
                },
            },
        }
    )


async def test_real_authority_link_jit_and_active_reverification(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    suffix = uuid.uuid4().hex
    tenant_id = uuid.uuid4().hex
    channel_id = uuid.uuid4().hex
    binding_id = uuid.uuid4().hex
    provider_tenant_id = uuid.uuid4().hex
    provider_account_id = uuid.uuid4().hex
    link_id = uuid.uuid4().hex
    provider_tenant_key = f"provider-tenant-{suffix}"
    provider_account_key = f"app-{suffix}"
    open_id = f"open-{suffix}"
    provider_user_id = f"user-{suffix}"
    proof_at = datetime.now(UTC) - timedelta(seconds=1)
    factory = async_sessionmaker(
        bootstrapped_async_engine,
        expire_on_commit=False,
    )
    tracker = _SessionTracker()
    tracking_factory = _TrackingSessionFactory(bootstrapped_async_engine, tracker)
    context = TrustedChannelContext(
        binding_id=binding_id,
        tenant_id=tenant_id,
        target=ExecutionTargetRef(
            target_type="multirag.canvas_agent",
            target_id=uuid.uuid4().hex,
            revision_id=uuid.uuid4().hex,
        ),
        enabled=True,
        binding_generation=1,
        provider="feishu",
    )

    async with factory.begin() as session:
        session.add(
            Tenant(
                id=tenant_id,
                name="Channel identity integration tenant",
                llm_id="test-llm",
                embd_id="test-embedding",
                asr_id="test-asr",
                img2txt_id="test-image",
                parser_ids="naive",
            )
        )
        session.add(
            ChatChannel(
                id=channel_id,
                tenant_id=tenant_id,
                name="Channel identity integration",
                channel="feishu",
                config={
                    "credential": {"app_id": provider_account_key},
                    "domain": "feishu",
                },
                status=1,
                generation=1,
            )
        )
        await session.flush()
        session.add(
            ChannelBinding(
                id=binding_id,
                channel_id=channel_id,
                target_type="multirag.canvas_agent",
                target_id=context.target.target_id,
                target_revision_id=context.target.revision_id,
                policy={},
                enabled=True,
                generation=1,
            )
        )

    authority = SqlAlchemyChannelIdentityAuthorityResolver(tracking_factory)
    no_link = await authority.resolve(context=context)
    assert no_link.status is ChannelIdentityAuthorityStatus.NO_LINK

    async with factory.begin() as session:
        session.add(
            IdentityProviderTenant(
                id=provider_tenant_id,
                tenant_id=tenant_id,
                provider="feishu",
                provider_tenant_key=provider_tenant_key,
                verified_at=proof_at,
            )
        )
        await session.flush()
        session.add(
            IdentityProviderAccount(
                id=provider_account_id,
                tenant_id=tenant_id,
                provider="feishu",
                provider_tenant_key=provider_tenant_key,
                provider_account_key=provider_account_key,
                identity_revision=1,
                identity_health_state=IdentityProviderHealthState.HEALTHY.value,
            )
        )
        session.add(
            IdentityTenantPolicy(
                id=tenant_id,
                tenant_id=tenant_id,
                mode=ProvisioningMode.JIT.value,
                revision=1,
                link_code_ttl_seconds=300,
                changed_at=proof_at,
            )
        )
        await session.flush()
        session.add(
            IdentityProviderChannelLink(
                id=link_id,
                tenant_id=tenant_id,
                provider="feishu",
                provider_account_id=provider_account_id,
                channel_id=channel_id,
                linked_at=proof_at,
            )
        )

    linked = await authority.resolve(context=context)
    assert linked.status is ChannelIdentityAuthorityStatus.LINKED
    generation_drift = await authority.resolve(
        context=replace(context, binding_generation=2),
    )
    assert generation_drift.status is ChannelIdentityAuthorityStatus.INVALID
    async with factory.begin() as session:
        account = await session.get(IdentityProviderAccount, provider_account_id)
        assert account is not None
        account.identity_health_state = IdentityProviderHealthState.DEGRADED.value
    unhealthy = await authority.resolve(context=context)
    assert unhealthy.status is ChannelIdentityAuthorityStatus.INVALID
    async with factory.begin() as session:
        account = await session.get(IdentityProviderAccount, provider_account_id)
        assert account is not None
        account.identity_health_state = IdentityProviderHealthState.HEALTHY.value

    def _assert_short_read_sessions_closed() -> None:
        assert tracker.open_sessions == 0
        assert tracker.max_open_sessions <= 1

    provider = _DirectoryProvider(
        ProviderIdentityResult(
            status=ProviderIdentityStatus.RESOLVED,
            identity=ProviderIdentity(
                provider="feishu",
                provider_tenant_key=provider_tenant_key,
                provider_account_id=provider_account_id,
                provider_user_id=provider_user_id,
                verified_at=proof_at,
                open_id=open_id,
                display_name="Directory user",
                provider_status=ProviderDirectoryStatus.ACTIVE,
            ),
        ),
        before_resolve=_assert_short_read_sessions_closed,
    )
    repository = SqlAlchemyIdentityProvisioningRepository(factory)
    codec = HmacLinkCodeCodec(
        keys={"test-key-v1": b"k" * 32},
        active_key_id="test-key-v1",
    )

    def _provisioning_service() -> IdentityProvisioningService:
        return IdentityProvisioningService(
            repository,
            repository,
            codec,
        )

    resolver = ChannelIdentityResolver(
        authority_resolver=authority,
        identity_reader=SqlAlchemyChannelIdentityReader(tracking_factory),
        provider_registry=IdentityProviderRegistry(
            {"feishu": lambda: provider},
        ),
        provisioning_service_factory=_provisioning_service,
    )
    user_ids: tuple[str, ...] = ()
    try:
        first = await resolver.resolve(
            context=context,
            command=_command(
                provider_tenant_key=provider_tenant_key,
                open_id=open_id,
                provider_user_id=provider_user_id,
            ),
        )
        assert first.principal is not None
        assert first.principal_id == first.principal.platform_user_id
        assert first.principal.tenant_id == tenant_id
        assert first.principal.authentication.assurance_verified_at == proof_at
        assert tracker.open_sessions == 0
        assert tracker.max_open_sessions == 1

        second = await resolver.resolve(
            context=context,
            command=_command(
                provider_tenant_key=provider_tenant_key,
                open_id=open_id,
                provider_user_id=provider_user_id,
            ),
        )
        assert second.principal_id == first.principal_id
        assert provider.calls == 2
        assert tracker.open_sessions == 0
        assert tracker.max_open_sessions == 1

        async with factory() as session:
            user_ids = tuple(
                await session.scalars(
                    select(UserTenant.user_id).where(
                        UserTenant.tenant_id == tenant_id,
                    )
                )
            )
            assert len(user_ids) == 1
            user = await session.get(User, user_ids[0])
            assert user is not None
            assert user.account_kind == UserAccountKind.EXTERNAL.value
            assert user.email is None and user.password is None
            membership = await session.scalar(select(UserTenant).where(UserTenant.tenant_id == tenant_id))
            assert membership is not None
            assert membership.role == UserTenantRole.NORMAL.value
            assert await session.scalar(select(func.count()).select_from(ExternalIdentity).where(ExternalIdentity.tenant_id == tenant_id)) == 1
            assert await session.scalar(select(func.count()).select_from(IdentityBindingEvent).where(IdentityBindingEvent.tenant_id == tenant_id)) == 1
    finally:
        async with factory.begin() as session:
            if not user_ids:
                user_ids = tuple(
                    await session.scalars(
                        select(UserTenant.user_id).where(
                            UserTenant.tenant_id == tenant_id,
                        )
                    )
                )
            for model in (
                IdentityBindingEvent,
                ExternalIdentityAlias,
                ExternalIdentity,
                UserTenant,
                IdentityProviderChannelLink,
                IdentityTenantPolicy,
                IdentityProviderAccount,
                IdentityProviderTenant,
            ):
                await session.execute(delete(model).where(model.tenant_id == tenant_id))
            if user_ids:
                await session.execute(delete(User).where(User.id.in_(user_ids)))
            await session.execute(delete(ChannelBinding).where(ChannelBinding.id == binding_id))
            await session.execute(delete(ChatChannel).where(ChatChannel.id == channel_id))
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
