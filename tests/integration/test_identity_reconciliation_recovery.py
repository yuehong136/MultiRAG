"""Cross-layer recovery test for a degraded Feishu provider account."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.channel_control.secret_store import EncryptedSecret
from api.db import (
    ExternalIdentityState,
    IdentityProviderHealthState,
    UserAccountKind,
)
from api.db.db_models import (
    ChannelSecret,
    ChatChannel,
    ExternalIdentity,
    ExternalIdentityAlias,
    IdentityProviderAccount,
    IdentityProviderChannelLink,
    IdentityProviderTenant,
    IdentityReconciliationCheckpoint,
    IdentityReconciliationTarget,
    Tenant,
    User,
    UserTenant,
)
from api.identity.contracts import ProviderContext
from api.identity.providers.contracts import (
    FeishuDirectoryUser,
    FeishuGetUserResponse,
    FeishuProviderCredential,
    FeishuTenantResponse,
    FeishuTenantTokenResponse,
    ProviderErrorCode,
    ProviderIdentityStatus,
)
from api.identity.providers.feishu import FeishuEnterpriseIdentityProvider
from api.identity.reconciliation.repository import (
    SqlAlchemyIdentityReconciliationRepository,
)
from api.identity.reconciliation.service import (
    IdentityReconciliationLimits,
    IdentityReconciliationService,
    ReconciliationProviderStatus,
    ReconciliationRunStatus,
)
from api.identity_adapters.channel_credentials import (
    SessionFactoryChannelProviderCredentialResolver,
    SessionFactoryReconciliationProviderCredentialResolver,
)
from api.identity_adapters.channel_runtime import IdentityProviderRegistry
from common.constants import StatusEnum


@dataclass(frozen=True, slots=True)
class _RecoveryGraph:
    factory: async_sessionmaker[AsyncSession]
    tenant_id: str
    account_id: str
    provider_tenant_id: str
    provider_tenant_key: str
    account_key: str
    user_id: str
    identity_id: str
    subject_value: str
    channel_id: str
    secret_id: str
    link_id: str

    @property
    def repository(self) -> SqlAlchemyIdentityReconciliationRepository:
        return SqlAlchemyIdentityReconciliationRepository(self.factory)


@pytest.fixture
async def recovery_graph(
    bootstrapped_async_engine: AsyncEngine,
) -> AsyncIterator[_RecoveryGraph]:
    suffix = uuid.uuid4().hex
    now = datetime.now(UTC) - timedelta(seconds=10)
    graph = _RecoveryGraph(
        factory=async_sessionmaker(
            bootstrapped_async_engine,
            expire_on_commit=False,
        ),
        tenant_id=uuid.uuid4().hex,
        account_id=uuid.uuid4().hex,
        provider_tenant_id=uuid.uuid4().hex,
        provider_tenant_key=f"tenant-{suffix}",
        account_key=f"cli_{suffix}",
        user_id=uuid.uuid4().hex,
        identity_id=uuid.uuid4().hex,
        subject_value=f"user-{suffix}",
        channel_id=uuid.uuid4().hex,
        secret_id=uuid.uuid4().hex,
        link_id=uuid.uuid4().hex,
    )
    async with graph.factory.begin() as session:
        session.add_all(
            [
                Tenant(
                    id=graph.tenant_id,
                    name="I8 recovery tenant",
                    llm_id="test-llm",
                    embd_id="test-embedding",
                    asr_id="test-asr",
                    img2txt_id="test-image",
                    parser_ids="naive",
                ),
                User(
                    id=graph.user_id,
                    nickname="I8 recovery member",
                    email=f"{graph.user_id}@example.test",
                    password=None,
                    account_kind=UserAccountKind.EXTERNAL.value,
                    login_channel="feishu",
                    is_authenticated=True,
                    is_active=True,
                    is_anonymous=False,
                    status=StatusEnum.VALID.value,
                    is_superuser=False,
                ),
            ],
        )
        await session.flush()
        session.add_all(
            [
                UserTenant(
                    id=uuid.uuid4().hex,
                    user_id=graph.user_id,
                    tenant_id=graph.tenant_id,
                    role="normal",
                    invited_by=graph.user_id,
                    status=StatusEnum.VALID.value,
                ),
                IdentityProviderTenant(
                    id=graph.provider_tenant_id,
                    tenant_id=graph.tenant_id,
                    provider="feishu",
                    provider_tenant_key=graph.provider_tenant_key,
                    verified_at=now,
                ),
                ChatChannel(
                    id=graph.channel_id,
                    tenant_id=graph.tenant_id,
                    name="I8 recovery channel",
                    channel="feishu",
                    config={
                        "credential": {"app_id": graph.account_key},
                        "domain": "feishu",
                    },
                    status=StatusEnum.VALID.value,
                    generation=1,
                ),
            ],
        )
        await session.flush()
        session.add_all(
            [
                IdentityProviderAccount(
                    id=graph.account_id,
                    tenant_id=graph.tenant_id,
                    provider="feishu",
                    provider_tenant_key=graph.provider_tenant_key,
                    provider_account_key=graph.account_key,
                    identity_revision=1,
                    identity_health_state=(IdentityProviderHealthState.HEALTHY.value),
                ),
                ExternalIdentity(
                    id=graph.identity_id,
                    tenant_id=graph.tenant_id,
                    user_id=graph.user_id,
                    provider="feishu",
                    provider_tenant_key=graph.provider_tenant_key,
                    subject_type="user_id",
                    subject_value=graph.subject_value,
                    state=ExternalIdentityState.ACTIVE.value,
                    verified_at=now - timedelta(minutes=1),
                    last_seen_at=now,
                    identity_revision=1,
                    attributes={},
                ),
                ChannelSecret(
                    id=graph.secret_id,
                    channel_id=graph.channel_id,
                    ciphertext="test-ciphertext",
                    key_id="test-key",
                    version=1,
                ),
            ],
        )
        await session.flush()
        session.add_all(
            [
                ExternalIdentityAlias(
                    id=uuid.uuid4().hex,
                    tenant_id=graph.tenant_id,
                    external_identity_id=graph.identity_id,
                    provider="feishu",
                    provider_tenant_key=graph.provider_tenant_key,
                    provider_account_key=graph.account_key,
                    alias_type="open_id",
                    alias_value=f"open-{suffix}",
                    verified_at=now,
                ),
                IdentityProviderChannelLink(
                    id=graph.link_id,
                    tenant_id=graph.tenant_id,
                    provider="feishu",
                    provider_account_id=graph.account_id,
                    channel_id=graph.channel_id,
                    linked_at=now,
                ),
            ],
        )
    try:
        yield graph
    finally:
        async with graph.factory.begin() as session:
            for model in (
                IdentityReconciliationTarget,
                IdentityReconciliationCheckpoint,
                ExternalIdentityAlias,
                IdentityProviderChannelLink,
                ChannelSecret,
                ExternalIdentity,
                UserTenant,
                IdentityProviderAccount,
                IdentityProviderTenant,
                ChatChannel,
            ):
                await session.execute(
                    delete(model).where(model.tenant_id == graph.tenant_id) if hasattr(model, "tenant_id") else delete(model).where(model.channel_id == graph.channel_id),
                )
            await session.execute(delete(User).where(User.id == graph.user_id))
            await session.execute(
                delete(Tenant).where(Tenant.id == graph.tenant_id),
            )


class _SecretStore:
    async def encrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        plaintext: Mapping[str, str],
        version: int,
    ) -> EncryptedSecret:
        del tenant_id, channel_id, plaintext, version
        raise AssertionError("recovery only reads an existing secret")

    async def decrypt(
        self,
        *,
        tenant_id: str,
        channel_id: str,
        encrypted: EncryptedSecret,
    ) -> Mapping[str, str]:
        del tenant_id, channel_id, encrypted
        return {"app_secret": "integration-secret"}


class _RecoveringFeishuClient:
    def __init__(self, *, tenant_key: str, user_id: str) -> None:
        self._tenant_key = tenant_key
        self._user_id = user_id
        self.get_user_calls = 0

    async def fetch_tenant_token(
        self,
        credential: FeishuProviderCredential,
    ) -> FeishuTenantTokenResponse:
        del credential
        return FeishuTenantTokenResponse(
            http_status=200,
            code=0,
            tenant_access_token="integration-token",
            expires_in_seconds=3_600,
        )

    async def get_tenant(
        self,
        credential: FeishuProviderCredential,
        *,
        tenant_access_token: str,
    ) -> FeishuTenantResponse:
        del credential, tenant_access_token
        return FeishuTenantResponse(
            http_status=200,
            code=0,
            tenant_key=self._tenant_key,
        )

    async def get_user(
        self,
        credential: FeishuProviderCredential,
        *,
        tenant_access_token: str,
        identifier_type: str,
        identifier_value: str,
    ) -> FeishuGetUserResponse:
        del credential, tenant_access_token
        assert identifier_type == "user_id"
        assert identifier_value == self._user_id
        self.get_user_calls += 1
        if self.get_user_calls <= 3:
            return FeishuGetUserResponse(http_status=503, code=0)
        return FeishuGetUserResponse(
            http_status=200,
            code=0,
            user=FeishuDirectoryUser(
                user_id=self._user_id,
                is_activated=True,
            ),
        )


async def _no_sleep(_delay: float) -> None:
    return None


async def _make_due(graph: _RecoveryGraph) -> None:
    due_at = datetime.now(UTC) - timedelta(seconds=1)
    async with graph.factory.begin() as session:
        await session.execute(
            update(IdentityReconciliationCheckpoint)
            .where(
                IdentityReconciliationCheckpoint.provider_account_id == graph.account_id,
            )
            .values(next_run_at=due_at),
        )
        await session.execute(
            update(IdentityReconciliationTarget)
            .where(
                IdentityReconciliationTarget.provider_account_id == graph.account_id,
            )
            .values(next_attempt_at=due_at),
        )


def _provider_context(account: IdentityProviderAccount) -> ProviderContext:
    return ProviderContext(
        tenant_id=account.tenant_id,
        provider=account.provider,
        provider_tenant_key=account.provider_tenant_key,
        provider_account_id=account.id,
        provider_account_key=account.provider_account_key,
        provider_account_revision=account.identity_revision,
        provider_account_last_scope_change_at=account.last_scope_change_at,
    )


async def test_degraded_account_recovers_only_through_reconciliation(
    recovery_graph: _RecoveryGraph,
) -> None:
    secret_store = _SecretStore()
    client = _RecoveringFeishuClient(
        tenant_key=recovery_graph.provider_tenant_key,
        user_id=recovery_graph.subject_value,
    )
    provider = FeishuEnterpriseIdentityProvider(
        SessionFactoryChannelProviderCredentialResolver(
            recovery_graph.factory,
            secret_store,
        ),
        reconciliation_credential_resolver=(
            SessionFactoryReconciliationProviderCredentialResolver(
                recovery_graph.factory,
                secret_store,
            )
        ),
        directory_client=client,
        contact_calls_per_second=15.0,
        reconciliation_calls_per_second=15.0,
        sleep=_no_sleep,
    )
    service = IdentityReconciliationService(
        recovery_graph.repository,
        IdentityProviderRegistry({"feishu": lambda: provider}),
        IdentityReconciliationLimits(
            lease_seconds=30,
            probe_safety_margin_seconds=5.0,
            probe_interval_seconds=0.1,
            cycle_interval_seconds=300,
            active_window_seconds=3_600,
            backoff_initial_seconds=1,
            backoff_max_seconds=8,
            not_found_confirmation_seconds=300,
            degrade_after_failures=3,
            max_tighten_per_cycle=10,
        ),
    )
    seeded = await service.seed_checkpoints()
    assert seeded.checkpoint_count == 1

    for expected_calls in range(1, 4):
        failed = await service.reconcile_one(owner="recovery-worker")
        assert failed.status is ReconciliationRunStatus.COMPLETED
        assert failed.provider_status is ReconciliationProviderStatus.UNAVAILABLE
        assert client.get_user_calls == expected_calls
        await _make_due(recovery_graph)

    async with recovery_graph.factory() as session:
        degraded_account = await session.get(
            IdentityProviderAccount,
            recovery_graph.account_id,
        )
        assert degraded_account is not None
        assert degraded_account.identity_health_state == IdentityProviderHealthState.DEGRADED.value
        degraded_context = _provider_context(degraded_account)

    foreground_before = await provider.refresh(
        degraded_context,
        recovery_graph.subject_value,
    )
    assert foreground_before.status is ProviderIdentityStatus.UNAVAILABLE
    assert foreground_before.error_code is ProviderErrorCode.CREDENTIAL_UNAVAILABLE
    assert client.get_user_calls == 3

    recovered_probe = await service.reconcile_one(owner="recovery-worker")
    assert recovered_probe.status is ReconciliationRunStatus.COMPLETED
    assert recovered_probe.provider_status is ReconciliationProviderStatus.RESOLVED
    assert client.get_user_calls == 4

    await _make_due(recovery_graph)
    dirty_cycle_finished = await service.reconcile_one(owner="recovery-worker")
    assert dirty_cycle_finished.status is ReconciliationRunStatus.IDLE
    await _make_due(recovery_graph)

    clean_probe = await service.reconcile_one(owner="recovery-worker")
    assert clean_probe.status is ReconciliationRunStatus.COMPLETED
    assert clean_probe.provider_status is ReconciliationProviderStatus.RESOLVED
    assert client.get_user_calls == 5
    await _make_due(recovery_graph)
    clean_cycle_finished = await service.reconcile_one(owner="recovery-worker")
    assert clean_cycle_finished.status is ReconciliationRunStatus.IDLE

    async with recovery_graph.factory() as session:
        healthy_account = await session.get(
            IdentityProviderAccount,
            recovery_graph.account_id,
        )
        assert healthy_account is not None
        assert healthy_account.identity_health_state == IdentityProviderHealthState.HEALTHY.value
        assert healthy_account.identity_revision == degraded_context.provider_account_revision + 1
        healthy_context = _provider_context(healthy_account)

    foreground_old_fence = await provider.refresh(
        degraded_context,
        recovery_graph.subject_value,
    )
    assert foreground_old_fence.status is ProviderIdentityStatus.UNAVAILABLE
    assert foreground_old_fence.error_code is ProviderErrorCode.CREDENTIAL_UNAVAILABLE
    assert client.get_user_calls == 5

    foreground_after = await provider.refresh(
        healthy_context,
        recovery_graph.subject_value,
    )
    assert foreground_after.status is ProviderIdentityStatus.RESOLVED
    assert client.get_user_calls == 6
