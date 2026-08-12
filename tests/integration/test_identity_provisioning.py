"""Real PostgreSQL transaction tests for EIM-I6 provisioning."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

import api.identity.provisioning_repository as provisioning_repository_module
from api.db import (
    ExternalIdentityState,
    IdentityProviderHealthState,
    UserAccountKind,
    UserTenantRole,
)
from api.db.db_models import (
    ExternalIdentity,
    ExternalIdentityAlias,
    IdentityBindingEvent,
    IdentityLinkCode,
    IdentityProviderAccount,
    IdentityProviderTenant,
    IdentityTenantPolicy,
    Tenant,
    User,
    UserTenant,
)
from api.identity.contracts import (
    AliasKey,
    IdentityErrorCode,
    ProviderAliasType,
    ProviderContext,
    ProvisioningAction,
    ProvisioningMode,
)
from api.identity.provisioning_contracts import (
    LinkCodeIssueCommand,
    ProvisioningOutcome,
    ProvisioningPolicyCreate,
    ProvisioningPolicyUpdate,
    ProvisioningPolicyWriteOutcome,
    ProvisioningRepositoryError,
    VerifiedProvisioningAlias,
    VerifiedProvisioningCommand,
)
from api.identity.provisioning_repository import (
    SqlAlchemyIdentityProvisioningRepository,
)
from common.constants import StatusEnum


@dataclass(frozen=True, slots=True)
class _Graph:
    factory: async_sessionmaker[AsyncSession]
    tenant_id: str
    account_id: str
    provider_tenant_id: str
    provider_tenant_key: str
    account_key: str
    now: datetime

    @property
    def context(self) -> ProviderContext:
        return ProviderContext(
            tenant_id=self.tenant_id,
            provider="feishu",
            provider_tenant_key=self.provider_tenant_key,
            provider_account_id=self.account_id,
            provider_account_key=self.account_key,
            provider_account_revision=1,
        )

    @property
    def repository(self) -> SqlAlchemyIdentityProvisioningRepository:
        return SqlAlchemyIdentityProvisioningRepository(self.factory)


@pytest.fixture
async def graph(
    bootstrapped_async_engine: AsyncEngine,
) -> AsyncIterator[_Graph]:
    suffix = uuid.uuid4().hex
    factory = async_sessionmaker(
        bootstrapped_async_engine,
        expire_on_commit=False,
    )
    value = _Graph(
        factory=factory,
        tenant_id=uuid.uuid4().hex,
        account_id=uuid.uuid4().hex,
        provider_tenant_id=uuid.uuid4().hex,
        provider_tenant_key=f"tenant-{suffix}",
        account_key=f"app-{suffix}",
        now=datetime.now(UTC) - timedelta(seconds=2),
    )
    async with factory.begin() as session:
        session.add(
            Tenant(
                id=value.tenant_id,
                name="I6 transaction tenant",
                llm_id="test-llm",
                embd_id="test-embedding",
                asr_id="test-asr",
                img2txt_id="test-image",
                parser_ids="naive",
            )
        )
        await session.flush()
        session.add(
            IdentityProviderTenant(
                id=value.provider_tenant_id,
                tenant_id=value.tenant_id,
                provider="feishu",
                provider_tenant_key=value.provider_tenant_key,
                verified_at=value.now,
            )
        )
        await session.flush()
        session.add(
            IdentityProviderAccount(
                id=value.account_id,
                tenant_id=value.tenant_id,
                provider="feishu",
                provider_tenant_key=value.provider_tenant_key,
                provider_account_key=value.account_key,
                identity_revision=1,
                identity_health_state=IdentityProviderHealthState.HEALTHY.value,
            )
        )
    try:
        yield value
    finally:
        async with factory.begin() as session:
            user_ids = tuple(
                await session.scalars(
                    select(UserTenant.user_id).where(
                        UserTenant.tenant_id == value.tenant_id,
                    )
                )
            )
            for model in (
                IdentityBindingEvent,
                IdentityLinkCode,
                ExternalIdentityAlias,
                ExternalIdentity,
                IdentityTenantPolicy,
                IdentityProviderAccount,
                IdentityProviderTenant,
                UserTenant,
            ):
                await session.execute(delete(model).where(model.tenant_id == value.tenant_id))
            if user_ids:
                await session.execute(delete(User).where(User.id.in_(user_ids)))
            await session.execute(delete(Tenant).where(Tenant.id == value.tenant_id))


async def _create_policy(
    graph: _Graph,
    mode: ProvisioningMode,
    *,
    ttl: int = 600,
) -> None:
    result = await graph.repository.create_policy(
        ProvisioningPolicyCreate(
            tenant_id=graph.tenant_id,
            mode=mode,
            link_code_ttl_seconds=ttl,
            changed_at=graph.now,
        )
    )
    assert result.outcome is ProvisioningPolicyWriteOutcome.CREATED


async def _add_local_member(graph: _Graph) -> str:
    user_id = uuid.uuid4().hex
    async with graph.factory.begin() as session:
        session.add_all(
            [
                User(
                    id=user_id,
                    nickname="Local member",
                    email=f"{user_id}@example.test",
                    password="not-a-real-hash",
                    account_kind=UserAccountKind.LOCAL.value,
                    login_channel="password",
                    is_authenticated=True,
                    is_active=True,
                    is_anonymous=False,
                    status=StatusEnum.VALID.value,
                    is_superuser=False,
                ),
                UserTenant(
                    id=uuid.uuid4().hex,
                    user_id=user_id,
                    tenant_id=graph.tenant_id,
                    role=UserTenantRole.NORMAL.value,
                    invited_by=user_id,
                    status=StatusEnum.VALID.value,
                ),
            ]
        )
    return user_id


def _command(
    graph: _Graph,
    *,
    action: ProvisioningAction | None,
    subject: str,
    alias: str,
    digest: str,
    policy_revision: int | None = 1,
    link_key: str | None = None,
    link_digest: str | None = None,
) -> VerifiedProvisioningCommand:
    alias_key = AliasKey(ProviderAliasType.OPEN_ID, alias)
    return VerifiedProvisioningCommand(
        context=graph.context,
        asserted_alias=alias_key,
        action=action,
        policy_revision=policy_revision,
        subject_value=subject,
        aliases=(
            VerifiedProvisioningAlias(
                ProviderAliasType.OPEN_ID,
                alias,
            ),
        ),
        verified_at=graph.now + timedelta(seconds=1),
        jit_nickname="Directory User" * 20,
        link_code_key_id=link_key,
        link_code_digest=link_digest,
        request_digest_key_id="audit-key-v1",
        request_digest=digest,
    )


async def test_policy_is_explicit_and_cas_is_revisioned(graph: _Graph) -> None:
    assert await graph.repository.get_policy(graph.tenant_id) is None
    await _create_policy(graph, ProvisioningMode.LINK_ONLY, ttl=300)
    snapshot = await graph.repository.get_policy(graph.tenant_id)
    assert snapshot is not None
    assert snapshot.mode is ProvisioningMode.LINK_ONLY
    assert snapshot.link_code_ttl_seconds == 300

    applied = await graph.repository.cas_policy(
        ProvisioningPolicyUpdate(
            tenant_id=graph.tenant_id,
            expected_revision=1,
            mode=ProvisioningMode.JIT,
            link_code_ttl_seconds=600,
            changed_at=graph.now + timedelta(seconds=1),
        )
    )
    assert applied.outcome is ProvisioningPolicyWriteOutcome.APPLIED
    assert applied.snapshot is not None and applied.snapshot.revision == 2
    stale = await graph.repository.cas_policy(
        ProvisioningPolicyUpdate(
            tenant_id=graph.tenant_id,
            expected_revision=1,
            mode=ProvisioningMode.PREPROVISIONED,
            link_code_ttl_seconds=900,
            changed_at=graph.now + timedelta(seconds=2),
        )
    )
    assert stale.outcome is ProvisioningPolicyWriteOutcome.REVISION_CONFLICT
    assert stale.snapshot is not None and stale.snapshot.mode is ProvisioningMode.JIT


async def test_concurrent_jit_creates_one_external_user_and_binding(
    graph: _Graph,
) -> None:
    await _create_policy(graph, ProvisioningMode.JIT)
    command = _command(
        graph,
        action=ProvisioningAction.CREATE_NORMAL_MEMBER,
        subject="directory-user-1",
        alias="open-user-1",
        digest="1" * 64,
    )
    first, second = await asyncio.gather(
        graph.repository.provision_verified_identity(command),
        graph.repository.provision_verified_identity(command),
    )
    assert {first.outcome, second.outcome} == {
        ProvisioningOutcome.JIT_CREATED,
        ProvisioningOutcome.ALREADY_BOUND,
    }
    assert first.identity is not None and second.identity is not None
    assert first.identity.id == second.identity.id
    async with graph.factory() as session:
        user = await session.get(User, first.identity.user_id)
        assert user is not None
        assert user.email is None and user.password is None
        assert user.account_kind == UserAccountKind.EXTERNAL.value
        assert len(user.nickname) == 100
        membership = await session.scalar(select(UserTenant).where(UserTenant.tenant_id == graph.tenant_id))
        assert membership is not None
        assert membership.role == UserTenantRole.NORMAL.value
        assert membership.invited_by == user.id
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdentityBindingEvent)
                .where(
                    IdentityBindingEvent.tenant_id == graph.tenant_id,
                )
            )
            == 1
        )


async def test_link_code_is_single_active_and_binds_only_its_user(
    graph: _Graph,
) -> None:
    await _create_policy(graph, ProvisioningMode.LINK_ONLY)
    user_id = await _add_local_member(graph)
    issued_at = graph.now
    first_grant = await graph.repository.issue_link_code(
        LinkCodeIssueCommand(
            context=graph.context,
            target_user_id=user_id,
            digest_key_id="link-key-v1",
            code_digest="a" * 64,
            policy_revision=1,
            provider_account_revision=1,
            provider_account_last_scope_change_at=None,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(minutes=10),
        )
    )
    second_grant = await graph.repository.issue_link_code(
        LinkCodeIssueCommand(
            context=graph.context,
            target_user_id=user_id,
            digest_key_id="link-key-v1",
            code_digest="b" * 64,
            policy_revision=1,
            provider_account_revision=1,
            provider_account_last_scope_change_at=None,
            issued_at=issued_at + timedelta(seconds=1),
            expires_at=issued_at + timedelta(minutes=10, seconds=1),
        )
    )
    async with graph.factory() as session:
        old = await session.get(IdentityLinkCode, first_grant.id)
        current = await session.get(IdentityLinkCode, second_grant.id)
        assert old is not None and old.state == "revoked"
        assert current is not None and current.state == "pending"

    result = await graph.repository.provision_verified_identity(
        _command(
            graph,
            action=ProvisioningAction.REQUIRE_LINK,
            subject="directory-linked-1",
            alias="open-linked-1",
            digest="2" * 64,
            link_key="link-key-v1",
            link_digest="b" * 64,
        )
    )
    assert result.outcome is ProvisioningOutcome.LINK_CODE_BOUND
    assert result.identity is not None and result.identity.user_id == user_id
    async with graph.factory() as session:
        user = await session.get(User, user_id)
        grant = await session.get(IdentityLinkCode, second_grant.id)
        assert user is not None and user.account_kind == UserAccountKind.HYBRID.value
        assert grant is not None and grant.state == "consumed"
        assert grant.consumed_external_identity_id == result.identity.id


async def test_alias_conflict_rolls_back_jit_without_orphan_user(
    graph: _Graph,
) -> None:
    await _create_policy(graph, ProvisioningMode.JIT)
    existing = await graph.repository.provision_verified_identity(
        _command(
            graph,
            action=ProvisioningAction.CREATE_NORMAL_MEMBER,
            subject="directory-existing",
            alias="shared-open-id",
            digest="3" * 64,
        )
    )
    assert existing.identity is not None
    async with graph.factory() as session:
        before = await session.scalar(
            select(func.count())
            .select_from(UserTenant)
            .where(
                UserTenant.tenant_id == graph.tenant_id,
            )
        )

    with pytest.raises(ProvisioningRepositoryError) as raised:
        await graph.repository.provision_verified_identity(
            _command(
                graph,
                action=ProvisioningAction.CREATE_NORMAL_MEMBER,
                subject="directory-conflicting",
                alias="shared-open-id",
                digest="4" * 64,
            )
        )
    assert raised.value.code is IdentityErrorCode.LINK_CONFLICT
    assert "shared-open-id" not in str(raised.value)
    async with graph.factory() as session:
        after = await session.scalar(
            select(func.count())
            .select_from(UserTenant)
            .where(
                UserTenant.tenant_id == graph.tenant_id,
            )
        )
        assert after == before
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ExternalIdentity)
                .where(
                    ExternalIdentity.tenant_id == graph.tenant_id,
                )
            )
            == 1
        )


async def test_inactive_canonical_never_reactivates(graph: _Graph) -> None:
    await _create_policy(graph, ProvisioningMode.PREPROVISIONED)
    user_id = await _add_local_member(graph)
    identity_id = uuid.uuid4().hex
    async with graph.factory.begin() as session:
        session.add(
            ExternalIdentity(
                id=identity_id,
                tenant_id=graph.tenant_id,
                user_id=user_id,
                provider="feishu",
                provider_tenant_key=graph.provider_tenant_key,
                subject_type="user_id",
                subject_value="directory-inactive",
                state=ExternalIdentityState.INACTIVE.value,
                verified_at=graph.now,
                identity_revision=2,
                attributes={},
            )
        )
    result = await graph.repository.provision_verified_identity(
        _command(
            graph,
            action=ProvisioningAction.BIND_PREPROVISIONED,
            subject="directory-inactive",
            alias="inactive-open-id",
            digest="5" * 64,
        )
    )
    assert result.error_code is IdentityErrorCode.INACTIVE
    async with graph.factory() as session:
        identity = await session.get(ExternalIdentity, identity_id)
        assert identity is not None and identity.state == ExternalIdentityState.INACTIVE.value
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ExternalIdentityAlias)
                .where(
                    ExternalIdentityAlias.tenant_id == graph.tenant_id,
                )
            )
            == 0
        )


async def test_preprovisioned_binds_existing_member_without_creating_user(
    graph: _Graph,
) -> None:
    await _create_policy(graph, ProvisioningMode.PREPROVISIONED)
    user_id = await _add_local_member(graph)
    identity_id = uuid.uuid4().hex
    async with graph.factory.begin() as session:
        session.add(
            ExternalIdentity(
                id=identity_id,
                tenant_id=graph.tenant_id,
                user_id=user_id,
                provider="feishu",
                provider_tenant_key=graph.provider_tenant_key,
                subject_type="user_id",
                subject_value="directory-preprovisioned",
                state=ExternalIdentityState.PENDING_LINK.value,
                identity_revision=1,
                attributes={},
            )
        )
    async with graph.factory() as session:
        before = await session.scalar(select(func.count()).select_from(User))
    result = await graph.repository.provision_verified_identity(
        _command(
            graph,
            action=ProvisioningAction.BIND_PREPROVISIONED,
            subject="directory-preprovisioned",
            alias="open-preprovisioned",
            digest="6" * 64,
        )
    )
    assert result.outcome is ProvisioningOutcome.PREPROVISIONED_BOUND
    assert result.identity is not None and result.identity.user_id == user_id
    async with graph.factory() as session:
        after = await session.scalar(select(func.count()).select_from(User))
        user = await session.get(User, user_id)
        identity = await session.get(ExternalIdentity, identity_id)
        assert after == before
        assert user is not None and user.account_kind == UserAccountKind.HYBRID.value
        assert identity is not None and identity.state == ExternalIdentityState.ACTIVE.value


@pytest.mark.parametrize(
    ("policy_revision", "account_revision", "expected"),
    [
        (2, 1, IdentityErrorCode.POLICY_UNAVAILABLE),
        (1, 2, IdentityErrorCode.REVISION_CONFLICT),
    ],
)
async def test_stale_link_code_issue_fails_without_grant(
    graph: _Graph,
    policy_revision: int,
    account_revision: int,
    expected: IdentityErrorCode,
) -> None:
    await _create_policy(graph, ProvisioningMode.LINK_ONLY)
    user_id = await _add_local_member(graph)
    with pytest.raises(ProvisioningRepositoryError) as raised:
        await graph.repository.issue_link_code(
            LinkCodeIssueCommand(
                context=graph.context,
                target_user_id=user_id,
                digest_key_id="link-key-v1",
                code_digest="c" * 64,
                policy_revision=policy_revision,
                provider_account_revision=account_revision,
                provider_account_last_scope_change_at=None,
                issued_at=graph.now,
                expires_at=graph.now + timedelta(minutes=10),
            )
        )
    assert raised.value.code is expected
    async with graph.factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdentityLinkCode)
                .where(
                    IdentityLinkCode.tenant_id == graph.tenant_id,
                )
            )
            == 0
        )


async def test_link_alias_conflict_rolls_back_code_consumption(
    graph: _Graph,
) -> None:
    await _create_policy(graph, ProvisioningMode.LINK_ONLY)
    target_user_id = await _add_local_member(graph)
    other_user_id = await _add_local_member(graph)
    other_identity_id = uuid.uuid4().hex
    async with graph.factory.begin() as session:
        session.add(
            ExternalIdentity(
                id=other_identity_id,
                tenant_id=graph.tenant_id,
                user_id=other_user_id,
                provider="feishu",
                provider_tenant_key=graph.provider_tenant_key,
                subject_type="user_id",
                subject_value="directory-other",
                state=ExternalIdentityState.ACTIVE.value,
                verified_at=graph.now,
                identity_revision=1,
                attributes={},
            )
        )
        await session.flush()
        session.add(
            ExternalIdentityAlias(
                id=uuid.uuid4().hex,
                tenant_id=graph.tenant_id,
                external_identity_id=other_identity_id,
                provider="feishu",
                provider_tenant_key=graph.provider_tenant_key,
                provider_account_key=graph.account_key,
                alias_type=ProviderAliasType.OPEN_ID.value,
                alias_value="occupied-open-id",
                verified_at=graph.now,
            )
        )
    grant = await graph.repository.issue_link_code(
        LinkCodeIssueCommand(
            context=graph.context,
            target_user_id=target_user_id,
            digest_key_id="link-key-v1",
            code_digest="d" * 64,
            policy_revision=1,
            provider_account_revision=1,
            provider_account_last_scope_change_at=None,
            issued_at=graph.now,
            expires_at=graph.now + timedelta(minutes=10),
        )
    )
    with pytest.raises(ProvisioningRepositoryError) as raised:
        await graph.repository.provision_verified_identity(
            _command(
                graph,
                action=ProvisioningAction.REQUIRE_LINK,
                subject="directory-new-link",
                alias="occupied-open-id",
                digest="7" * 64,
                link_key="link-key-v1",
                link_digest="d" * 64,
            )
        )
    assert raised.value.code is IdentityErrorCode.LINK_CONFLICT
    async with graph.factory() as session:
        persisted = await session.get(IdentityLinkCode, grant.id)
        assert persisted is not None and persisted.state == "pending"
        assert persisted.consumed_at is None
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ExternalIdentity)
                .where(
                    ExternalIdentity.tenant_id == graph.tenant_id,
                    ExternalIdentity.subject_value == "directory-new-link",
                )
            )
            == 0
        )


async def test_consumed_code_replay_requires_exact_request_fingerprint(
    graph: _Graph,
) -> None:
    await _create_policy(graph, ProvisioningMode.LINK_ONLY)
    user_id = await _add_local_member(graph)
    grant = await graph.repository.issue_link_code(
        LinkCodeIssueCommand(
            context=graph.context,
            target_user_id=user_id,
            digest_key_id="link-key-v1",
            code_digest="e" * 64,
            policy_revision=1,
            provider_account_revision=1,
            provider_account_last_scope_change_at=None,
            issued_at=graph.now,
            expires_at=graph.now + timedelta(minutes=10),
        )
    )
    command = _command(
        graph,
        action=ProvisioningAction.REQUIRE_LINK,
        subject="directory-replay",
        alias="open-replay",
        digest="8" * 64,
        link_key="link-key-v1",
        link_digest="e" * 64,
    )
    created = await graph.repository.provision_verified_identity(command)
    assert created.outcome is ProvisioningOutcome.LINK_CODE_BOUND
    replay = await graph.repository.provision_verified_identity(command)
    assert replay.outcome is ProvisioningOutcome.ALREADY_BOUND
    different = _command(
        graph,
        action=ProvisioningAction.REQUIRE_LINK,
        subject="directory-replay",
        alias="open-replay",
        digest="9" * 64,
        link_key="link-key-v1",
        link_digest="e" * 64,
    )
    with pytest.raises(ProvisioningRepositoryError) as raised:
        await graph.repository.provision_verified_identity(different)
    assert raised.value.code is IdentityErrorCode.LINK_REQUIRED
    async with graph.factory() as session:
        persisted = await session.get(IdentityLinkCode, grant.id)
        assert persisted is not None and persisted.state == "consumed"
        assert persisted.consumed_at is not None
        assert persisted.consumed_at >= persisted.issued_at
        assert persisted.consumed_at < persisted.expires_at


async def test_active_alias_reverify_is_policy_independent_and_monotonic(
    graph: _Graph,
) -> None:
    await _create_policy(graph, ProvisioningMode.JIT)
    initial = await graph.repository.provision_verified_identity(
        _command(
            graph,
            action=ProvisioningAction.CREATE_NORMAL_MEMBER,
            subject="directory-active",
            alias="open-active-a",
            digest="a" * 64,
        )
    )
    assert initial.identity is not None
    async with graph.factory.begin() as session:
        await session.execute(delete(IdentityTenantPolicy).where(IdentityTenantPolicy.tenant_id == graph.tenant_id))
    command = _command(
        graph,
        action=None,
        policy_revision=None,
        subject="directory-active",
        alias="open-active-b",
        digest="b" * 64,
    )
    refreshed = await graph.repository.provision_verified_identity(command)
    assert refreshed.outcome is ProvisioningOutcome.ALREADY_BOUND
    assert refreshed.identity is not None
    assert refreshed.identity.last_seen_at == command.verified_at
    async with graph.factory() as session:
        aliases = (
            await session.scalars(
                select(ExternalIdentityAlias).where(
                    ExternalIdentityAlias.external_identity_id == initial.identity.id,
                )
            )
        ).all()
        assert {alias.alias_value for alias in aliases} == {
            "open-active-a",
            "open-active-b",
        }


async def test_concurrent_two_subjects_cannot_bind_same_target_user(
    graph: _Graph,
) -> None:
    await _create_policy(graph, ProvisioningMode.LINK_ONLY)
    user_id = await _add_local_member(graph)

    async def issue_and_bind(suffix: str, digit: str):
        await graph.repository.issue_link_code(
            LinkCodeIssueCommand(
                context=graph.context,
                target_user_id=user_id,
                digest_key_id="link-key-v1",
                code_digest=digit * 64,
                policy_revision=1,
                provider_account_revision=1,
                provider_account_last_scope_change_at=None,
                issued_at=graph.now,
                expires_at=graph.now + timedelta(minutes=10),
            )
        )
        return await graph.repository.provision_verified_identity(
            _command(
                graph,
                action=ProvisioningAction.REQUIRE_LINK,
                subject=f"directory-race-{suffix}",
                alias=f"open-race-{suffix}",
                digest=("1" if digit == "c" else "2") * 64,
                link_key="link-key-v1",
                link_digest=digit * 64,
            )
        )

    results = await asyncio.gather(
        issue_and_bind("one", "c"),
        issue_and_bind("two", "f"),
        return_exceptions=True,
    )
    resolved = [result for result in results if not isinstance(result, BaseException) and result.outcome is ProvisioningOutcome.LINK_CODE_BOUND]
    assert len(resolved) == 1
    async with graph.factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ExternalIdentity)
                .where(
                    ExternalIdentity.tenant_id == graph.tenant_id,
                    ExternalIdentity.user_id == user_id,
                )
            )
            == 1
        )


async def test_concurrent_policy_create_converges_to_one_authoritative_row(
    graph: _Graph,
) -> None:
    first, second = await asyncio.gather(
        graph.repository.create_policy(
            ProvisioningPolicyCreate(
                tenant_id=graph.tenant_id,
                mode=ProvisioningMode.LINK_ONLY,
                link_code_ttl_seconds=600,
                changed_at=graph.now,
            )
        ),
        graph.repository.create_policy(
            ProvisioningPolicyCreate(
                tenant_id=graph.tenant_id,
                mode=ProvisioningMode.LINK_ONLY,
                link_code_ttl_seconds=600,
                changed_at=graph.now,
            )
        ),
    )
    assert {first.outcome, second.outcome} == {
        ProvisioningPolicyWriteOutcome.CREATED,
        ProvisioningPolicyWriteOutcome.EXISTS,
    }
    async with graph.factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(IdentityTenantPolicy)
                .where(
                    IdentityTenantPolicy.tenant_id == graph.tenant_id,
                )
            )
            == 1
        )


@pytest.mark.parametrize(
    "state",
    [ExternalIdentityState.CONFLICT.value, ExternalIdentityState.REVOKED.value],
)
async def test_terminal_canonical_state_never_reactivates(
    graph: _Graph,
    state: str,
) -> None:
    await _create_policy(graph, ProvisioningMode.PREPROVISIONED)
    user_id = await _add_local_member(graph)
    identity_id = uuid.uuid4().hex
    async with graph.factory.begin() as session:
        session.add(
            ExternalIdentity(
                id=identity_id,
                tenant_id=graph.tenant_id,
                user_id=user_id,
                provider="feishu",
                provider_tenant_key=graph.provider_tenant_key,
                subject_type="user_id",
                subject_value=f"directory-{state}",
                state=state,
                verified_at=graph.now,
                identity_revision=3,
                attributes={},
            )
        )
    result = await graph.repository.provision_verified_identity(
        _command(
            graph,
            action=ProvisioningAction.BIND_PREPROVISIONED,
            subject=f"directory-{state}",
            alias=f"open-{state}",
            digest=("c" if state == "conflict" else "d") * 64,
        )
    )
    assert result.error_code is IdentityErrorCode.LINK_CONFLICT
    async with graph.factory() as session:
        identity = await session.get(ExternalIdentity, identity_id)
        assert identity is not None and identity.state == state


async def test_active_reverify_never_moves_last_seen_or_alias_proof_backward(
    graph: _Graph,
) -> None:
    await _create_policy(graph, ProvisioningMode.JIT)
    command = _command(
        graph,
        action=ProvisioningAction.CREATE_NORMAL_MEMBER,
        subject="directory-monotonic",
        alias="open-monotonic",
        digest="e" * 64,
    )
    initial = await graph.repository.provision_verified_identity(command)
    assert initial.identity is not None
    newer = replace(
        command,
        action=None,
        policy_revision=None,
        verified_at=command.verified_at + timedelta(milliseconds=100),
        request_digest="f" * 64,
    )
    await graph.repository.provision_verified_identity(newer)
    older = replace(
        newer,
        verified_at=command.verified_at,
        request_digest="0" * 64,
    )
    result = await graph.repository.provision_verified_identity(older)
    assert result.identity is not None
    assert result.identity.last_seen_at == newer.verified_at
    async with graph.factory() as session:
        alias = await session.scalar(
            select(ExternalIdentityAlias).where(
                ExternalIdentityAlias.external_identity_id == initial.identity.id,
            )
        )
        assert alias is not None and alias.verified_at == newer.verified_at


@pytest.mark.parametrize("grant_state", ["revoked", "consumed", "expired", "wrong"])
async def test_invalid_link_grant_is_fail_closed_without_identity_write(
    graph: _Graph,
    grant_state: str,
) -> None:
    await _create_policy(graph, ProvisioningMode.LINK_ONLY)
    user_id = await _add_local_member(graph)
    issued_at = graph.now
    grant = await graph.repository.issue_link_code(
        LinkCodeIssueCommand(
            context=graph.context,
            target_user_id=user_id,
            digest_key_id="link-key-v1",
            code_digest="1" * 64,
            policy_revision=1,
            provider_account_revision=1,
            provider_account_last_scope_change_at=None,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(minutes=10),
        )
    )
    async with graph.factory.begin() as session:
        model = await session.get(IdentityLinkCode, grant.id)
        assert model is not None
        if grant_state == "revoked":
            model.state = "revoked"
            model.revoked_at = issued_at + timedelta(seconds=1)
        elif grant_state == "consumed":
            # A structurally valid consumed row needs an identity target.
            identity = ExternalIdentity(
                id=uuid.uuid4().hex,
                tenant_id=graph.tenant_id,
                user_id=user_id,
                provider="feishu",
                provider_tenant_key=graph.provider_tenant_key,
                subject_type="user_id",
                subject_value="already-consumed-subject",
                state=ExternalIdentityState.ACTIVE.value,
                verified_at=issued_at,
                identity_revision=1,
                attributes={},
            )
            session.add(identity)
            await session.flush()
            model.state = "consumed"
            model.consumed_at = issued_at + timedelta(seconds=1)
            model.consumed_external_identity_id = identity.id
        elif grant_state == "expired":
            model.expires_at = datetime.now(UTC) - timedelta(milliseconds=1)

    digest = "2" * 64 if grant_state == "wrong" else "1" * 64
    with pytest.raises(ProvisioningRepositoryError) as raised:
        await graph.repository.provision_verified_identity(
            _command(
                graph,
                action=ProvisioningAction.REQUIRE_LINK,
                subject="directory-invalid-grant",
                alias="open-invalid-grant",
                digest="3" * 64,
                link_key="link-key-v1",
                link_digest=digest,
            )
        )
    assert raised.value.code is IdentityErrorCode.LINK_REQUIRED
    async with graph.factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ExternalIdentity)
                .where(
                    ExternalIdentity.tenant_id == graph.tenant_id,
                    ExternalIdentity.subject_value == "directory-invalid-grant",
                )
            )
            == 0
        )


@pytest.mark.parametrize(
    "mutation",
    ["policy", "account_revision", "account_scope", "account_health"],
)
async def test_provisioning_rechecks_policy_and_account_generation(
    graph: _Graph,
    mutation: str,
) -> None:
    await _create_policy(graph, ProvisioningMode.JIT)
    context = graph.context
    async with graph.factory.begin() as session:
        if mutation == "policy":
            policy = await session.get(IdentityTenantPolicy, graph.tenant_id)
            assert policy is not None
            policy.mode = ProvisioningMode.LINK_ONLY.value
            policy.revision = 2
        elif mutation == "account_revision":
            account = await session.get(IdentityProviderAccount, graph.account_id)
            assert account is not None
            account.identity_revision = 2
        elif mutation == "account_scope":
            marker = graph.now + timedelta(seconds=1)
            account = await session.get(IdentityProviderAccount, graph.account_id)
            assert account is not None
            account.last_scope_change_at = marker
        else:
            account = await session.get(IdentityProviderAccount, graph.account_id)
            assert account is not None
            account.identity_health_state = IdentityProviderHealthState.DEGRADED.value
    command = _command(
        graph,
        action=ProvisioningAction.CREATE_NORMAL_MEMBER,
        subject=f"directory-stale-{mutation}",
        alias=f"open-stale-{mutation}",
        digest="4" * 64,
    )
    if mutation == "account_scope":
        context = replace(context, provider_account_last_scope_change_at=None)
        command = replace(command, context=context)
    with pytest.raises(ProvisioningRepositoryError) as raised:
        await graph.repository.provision_verified_identity(command)
    expected = {
        "policy": IdentityErrorCode.POLICY_UNAVAILABLE,
        "account_revision": IdentityErrorCode.REVISION_CONFLICT,
        "account_scope": IdentityErrorCode.REVISION_CONFLICT,
        "account_health": IdentityErrorCode.INACTIVE,
    }[mutation]
    assert raised.value.code is expected
    async with graph.factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ExternalIdentity)
                .where(
                    ExternalIdentity.tenant_id == graph.tenant_id,
                )
            )
            == 0
        )


async def test_link_code_cannot_cross_provider_account(
    graph: _Graph,
) -> None:
    await _create_policy(graph, ProvisioningMode.LINK_ONLY)
    user_id = await _add_local_member(graph)
    await graph.repository.issue_link_code(
        LinkCodeIssueCommand(
            context=graph.context,
            target_user_id=user_id,
            digest_key_id="link-key-v1",
            code_digest="5" * 64,
            policy_revision=1,
            provider_account_revision=1,
            provider_account_last_scope_change_at=None,
            issued_at=graph.now,
            expires_at=graph.now + timedelta(minutes=10),
        )
    )
    other_account_id = uuid.uuid4().hex
    other_account_key = f"other-{graph.account_key}"
    async with graph.factory.begin() as session:
        session.add(
            IdentityProviderAccount(
                id=other_account_id,
                tenant_id=graph.tenant_id,
                provider="feishu",
                provider_tenant_key=graph.provider_tenant_key,
                provider_account_key=other_account_key,
                identity_revision=1,
                identity_health_state=IdentityProviderHealthState.HEALTHY.value,
            )
        )
    wrong_context = replace(
        graph.context,
        provider_account_id=other_account_id,
        provider_account_key=other_account_key,
    )
    command = replace(
        _command(
            graph,
            action=ProvisioningAction.REQUIRE_LINK,
            subject="directory-cross-account",
            alias="open-cross-account",
            digest="6" * 64,
            link_key="link-key-v1",
            link_digest="5" * 64,
        ),
        context=wrong_context,
    )
    with pytest.raises(ProvisioningRepositoryError) as raised:
        await graph.repository.provision_verified_identity(command)
    assert raised.value.code is IdentityErrorCode.LINK_REQUIRED


async def test_reverse_identity_slot_blocks_second_subject_and_preserves_code(
    graph: _Graph,
) -> None:
    await _create_policy(graph, ProvisioningMode.LINK_ONLY)
    user_id = await _add_local_member(graph)
    async with graph.factory.begin() as session:
        session.add(
            ExternalIdentity(
                id=uuid.uuid4().hex,
                tenant_id=graph.tenant_id,
                user_id=user_id,
                provider="feishu",
                provider_tenant_key=graph.provider_tenant_key,
                subject_type="user_id",
                subject_value="directory-first-slot",
                state=ExternalIdentityState.ACTIVE.value,
                verified_at=graph.now,
                identity_revision=1,
                attributes={},
            )
        )
    grant = await graph.repository.issue_link_code(
        LinkCodeIssueCommand(
            context=graph.context,
            target_user_id=user_id,
            digest_key_id="link-key-v1",
            code_digest="7" * 64,
            policy_revision=1,
            provider_account_revision=1,
            provider_account_last_scope_change_at=None,
            issued_at=graph.now,
            expires_at=graph.now + timedelta(minutes=10),
        )
    )
    result = await graph.repository.provision_verified_identity(
        _command(
            graph,
            action=ProvisioningAction.REQUIRE_LINK,
            subject="directory-second-slot",
            alias="open-second-slot",
            digest="8" * 64,
            link_key="link-key-v1",
            link_digest="7" * 64,
        )
    )
    assert result.error_code is IdentityErrorCode.LINK_CONFLICT
    async with graph.factory() as session:
        persisted = await session.get(IdentityLinkCode, grant.id)
        assert persisted is not None and persisted.state == "pending"
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ExternalIdentity)
                .where(
                    ExternalIdentity.tenant_id == graph.tenant_id,
                )
            )
            == 1
        )


async def test_link_expiry_is_rechecked_after_blocking_locks(
    graph: _Graph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _create_policy(graph, ProvisioningMode.LINK_ONLY, ttl=60)
    user_id = await _add_local_member(graph)
    grant = await graph.repository.issue_link_code(
        LinkCodeIssueCommand(
            context=graph.context,
            target_user_id=user_id,
            digest_key_id="link-key-v1",
            code_digest="8" * 64,
            policy_revision=1,
            provider_account_revision=1,
            provider_account_last_scope_change_at=None,
            issued_at=graph.now,
            expires_at=graph.now + timedelta(seconds=60),
        )
    )
    observed_times = iter(
        (
            graph.now + timedelta(seconds=1),
            graph.now + timedelta(seconds=60),
        )
    )

    async def _advancing_database_now(_session: AsyncSession) -> datetime:
        return next(observed_times)

    monkeypatch.setattr(
        provisioning_repository_module,
        "_database_now",
        _advancing_database_now,
    )
    with pytest.raises(ProvisioningRepositoryError) as raised:
        await graph.repository.provision_verified_identity(
            _command(
                graph,
                action=ProvisioningAction.REQUIRE_LINK,
                subject="directory-expired-while-waiting",
                alias="open-expired-while-waiting",
                digest="9" * 64,
                link_key="link-key-v1",
                link_digest="8" * 64,
            )
        )
    assert raised.value.code is IdentityErrorCode.LINK_REQUIRED
    async with graph.factory() as session:
        persisted = await session.get(IdentityLinkCode, grant.id)
        assert persisted is not None and persisted.state == "pending"


async def test_provider_proof_age_is_rechecked_after_blocking_locks(
    graph: _Graph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _create_policy(graph, ProvisioningMode.JIT)
    observed_times = iter(
        (
            graph.now + timedelta(seconds=1),
            graph.now + timedelta(minutes=5, seconds=2),
        )
    )

    async def _advancing_database_now(_session: AsyncSession) -> datetime:
        return next(observed_times)

    monkeypatch.setattr(
        provisioning_repository_module,
        "_database_now",
        _advancing_database_now,
    )
    with pytest.raises(ProvisioningRepositoryError) as raised:
        await graph.repository.provision_verified_identity(
            _command(
                graph,
                action=ProvisioningAction.CREATE_NORMAL_MEMBER,
                subject="directory-stale-while-waiting",
                alias="open-stale-while-waiting",
                digest="a" * 64,
            )
        )
    assert raised.value.code is IdentityErrorCode.ASSERTION_INVALID
    async with graph.factory() as session:
        assert await session.scalar(select(func.count()).select_from(ExternalIdentity).where(ExternalIdentity.tenant_id == graph.tenant_id)) == 0


async def test_link_issue_expiry_is_rechecked_after_membership_lock(
    graph: _Graph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _create_policy(graph, ProvisioningMode.LINK_ONLY, ttl=60)
    user_id = await _add_local_member(graph)
    observed_times = iter(
        (
            graph.now + timedelta(seconds=1),
            graph.now + timedelta(seconds=60),
        )
    )

    async def _advancing_database_now(_session: AsyncSession) -> datetime:
        return next(observed_times)

    monkeypatch.setattr(
        provisioning_repository_module,
        "_database_now",
        _advancing_database_now,
    )
    with pytest.raises(ProvisioningRepositoryError) as raised:
        await graph.repository.issue_link_code(
            LinkCodeIssueCommand(
                context=graph.context,
                target_user_id=user_id,
                digest_key_id="link-key-v1",
                code_digest="b" * 64,
                policy_revision=1,
                provider_account_revision=1,
                provider_account_last_scope_change_at=None,
                issued_at=graph.now,
                expires_at=graph.now + timedelta(seconds=60),
            )
        )
    assert raised.value.code is IdentityErrorCode.ASSERTION_INVALID
    async with graph.factory() as session:
        assert await session.scalar(select(func.count()).select_from(IdentityLinkCode).where(IdentityLinkCode.tenant_id == graph.tenant_id)) == 0
