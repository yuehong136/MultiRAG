"""Disabled-by-default U14 runtime assembly and live reauthorization."""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from datetime import UTC, datetime
from functools import lru_cache

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import (
    EnterpriseSubjectState,
    ExternalIdentityState,
    IdentityProviderHealthState,
)
from api.db.db_models import (
    ChannelBinding,
    ChatChannel,
    EnterpriseSubjectLink,
    ExternalIdentity,
    IdentityProviderAccount,
    IdentityProviderChannelLink,
    McpInteractionPresentation,
    MCPServer,
    User,
    UserTenant,
    async_session_factory,
)
from api.identity.contracts import (
    ExternalIdentityRecord,
    IdentityResolutionResult,
    IdentityResolutionStatus,
    UserMembershipRecord,
)
from api.identity.mcp_delegation.runtime import (
    get_mcp_delegation_service,
    resolve_mcp_credential_provider,
)
from api.identity.mcp_interactions.contracts import (
    InteractionErrorCode,
    InteractionStateError,
)
from api.identity.mcp_interactions.crypto import InteractionPayloadCipher
from api.identity.mcp_interactions.service import (
    InteractionServiceLimits,
    PersistentInteractionService,
    run_interaction_worker,
)
from api.identity.principal import (
    AuthenticationContext,
    AuthenticationSource,
    EnterpriseSubject,
    IdentityAssurance,
    Principal,
    VerifiedEnterpriseSubjectEvidence,
    VerifiedProviderIdentity,
    build_principal_from_resolved_identity,
)
from api.identity.run_context import RunContext
from common.app_config import AppConfigError, get_app_config
from common.mcp_interactions import InteractionHandler, InteractionResume
from common.mcp_tool_call_conn import MCPToolCallSession

_active_service: PersistentInteractionService | None = None
_worker_task: asyncio.Task[None] | None = None
_stopping: asyncio.Event | None = None


@lru_cache(maxsize=1)
def get_mcp_interaction_service() -> PersistentInteractionService:
    config = get_app_config().identity.mcp_interactions.require_enabled()
    if not get_app_config().identity.mcp_delegation.enabled:
        raise AppConfigError("identity.mcp_interactions requires identity.mcp_delegation")
    if async_session_factory is None:
        raise AppConfigError("identity.mcp_interactions requires PostgreSQL AsyncSession")
    keys = [secret.get_secret_value() for secret in config.payload_encryption_keys]
    return PersistentInteractionService(
        session_factory=async_session_factory,
        cipher=InteractionPayloadCipher.from_base64_keyring(keys),
        limits=InteractionServiceLimits(
            lease_seconds=config.lease_seconds,
            batch_size=config.batch_size,
            max_rounds=config.max_rounds,
            max_payload_bytes=config.max_payload_bytes,
            ttl_seconds=config.ttl_seconds,
        ),
    )


def resolve_mcp_interaction_handler() -> InteractionHandler | None:
    return _active_service


class ReauthorizingInteractionExecutor:
    """Re-read live identity/membership/policy and mint a new bearer per resume."""

    def __init__(self, *, handler: InteractionHandler) -> None:
        self._handler = handler

    async def execute(self, resume: InteractionResume) -> object:
        principal, server = await self._load_live_authority(resume)
        request = resume.request
        run_context = RunContext(
            tenant_id=request.tenant_id,
            principal=principal,
            agent_id=request.agent_id,
            agent_revision_id=request.agent_revision_id,
        )
        credential_provider = resolve_mcp_credential_provider(
            mcp_server=server,
            run_context=run_context,
        )
        if credential_provider is None:
            raise InteractionStateError(InteractionErrorCode.REAUTHORIZATION_DENIED)
        if not credential_provider.is_authorized(request.tool_name):
            raise InteractionStateError(InteractionErrorCode.REAUTHORIZATION_DENIED)
        current = credential_provider.interaction_authorization(request.tool_name)
        if current.effect not in {"read", "prepare"} or current.effect != request.effect.value or current.replay_mode != request.replay_mode:
            raise InteractionStateError(InteractionErrorCode.REAUTHORIZATION_DENIED)
        session = MCPToolCallSession(
            server,
            server.variables,
            call_context=run_context,
            credential_provider=credential_provider,
            interaction_handler=self._handler,
            legacy_interaction_tools=(frozenset({request.tool_name}) if request.request_state.startswith("legacy.") else frozenset()),
            tool_output_schemas=({request.tool_name: dict(request.output_schema)} if request.output_schema is not None else None),
        )
        try:
            return await session.resume_tool_call(resume)
        finally:
            await session.close()

    async def _load_live_authority(
        self,
        resume: InteractionResume,
    ) -> tuple[Principal, MCPServer]:
        if async_session_factory is None:
            raise InteractionStateError(InteractionErrorCode.REAUTHORIZATION_DENIED)
        request = resume.request
        if request.external_identity_id is None:
            # A background worker cannot recreate a Web/API authentication
            # ceremony from a stored user id. Future Web renderers must hand in
            # a separate server-verified resume grant; never deserialize one.
            raise InteractionStateError(InteractionErrorCode.REAUTHORIZATION_DENIED)
        async with async_session_factory() as session:
            identity = await session.scalar(
                sa.select(ExternalIdentity).where(
                    ExternalIdentity.id == request.external_identity_id,
                    ExternalIdentity.tenant_id == request.tenant_id,
                    ExternalIdentity.user_id == request.platform_user_id,
                    ExternalIdentity.state == ExternalIdentityState.ACTIVE.value,
                )
            )
            membership = await session.scalar(
                sa.select(UserTenant).where(
                    UserTenant.tenant_id == request.tenant_id,
                    UserTenant.user_id == request.platform_user_id,
                    UserTenant.status == "1",
                )
            )
            user = await session.get(User, request.platform_user_id)
            server = await session.get(MCPServer, request.mcp_server_id)
            if (
                identity is None
                or identity.verified_at is None
                or membership is None
                or user is None
                or not user.is_active
                or user.status != "1"
                or server is None
                or server.tenant_id != request.tenant_id
                or server.url != request.resource_uri
            ):
                raise InteractionStateError(InteractionErrorCode.REAUTHORIZATION_DENIED)
            policy = get_mcp_delegation_service().tool_policy.tools.get(request.tool_name)
            requirement = None if policy is None else policy.enterprise_subject
            provider_requirement = None if policy is None else policy.provider_identity
            subject = None
            if requirement is not None:
                subject = await session.scalar(
                    sa.select(EnterpriseSubjectLink).where(
                        EnterpriseSubjectLink.tenant_id == request.tenant_id,
                        EnterpriseSubjectLink.user_id == request.platform_user_id,
                        EnterpriseSubjectLink.subject_type == requirement.subject_type,
                        EnterpriseSubjectLink.issuer == requirement.issuer,
                        EnterpriseSubjectLink.issuer_tenant == requirement.issuer_tenant,
                        EnterpriseSubjectLink.state == EnterpriseSubjectState.ACTIVE.value,
                    )
                )
                if subject is None or subject.verified_at is None:
                    raise InteractionStateError(InteractionErrorCode.REAUTHORIZATION_DENIED)
            provider_identity = None
            if provider_requirement is not None:
                provider_identity = await self._load_live_provider_identity(
                    session=session,
                    resume=resume,
                    identity=identity,
                )
            now = datetime.now(UTC)
            assurance = IdentityAssurance.ENTERPRISE_VERIFIED if subject is not None else IdentityAssurance.DIRECTORY_VERIFIED
            assurance_verified_at = subject.verified_at if subject is not None else identity.verified_at
            authentication = AuthenticationContext(
                source=AuthenticationSource.ENTERPRISE_IDENTITY,
                assurance=assurance,
                validated_at=now,
                assurance_verified_at=assurance_verified_at,
                provider=identity.provider,
                external_identity_id=identity.id,
            )
            result = IdentityResolutionResult(
                status=IdentityResolutionStatus.RESOLVED,
                identity=ExternalIdentityRecord(
                    id=identity.id,
                    tenant_id=identity.tenant_id,
                    user_id=identity.user_id,
                    provider=identity.provider,
                    provider_tenant_key=identity.provider_tenant_key,
                    subject_type=identity.subject_type,
                    subject_value=identity.subject_value,
                    state=identity.state,
                    verified_at=identity.verified_at,
                    last_seen_at=identity.last_seen_at,
                    identity_revision=identity.identity_revision,
                    attributes=tuple(sorted(identity.attributes.items())),
                ),
                membership=UserMembershipRecord(
                    user_id=membership.user_id,
                    tenant_id=membership.tenant_id,
                    role=membership.role,
                ),
            )
            subject_evidence = None
            if subject is not None:
                subject_verified_at = subject.verified_at
                assert subject_verified_at is not None
                subject_evidence = VerifiedEnterpriseSubjectEvidence(
                    platform_user_id=request.platform_user_id,
                    tenant_id=request.tenant_id,
                    enterprise_subject=EnterpriseSubject(
                        subject_type=subject.subject_type,
                        subject=subject.subject_value,
                        issuer=subject.issuer,
                        issuer_tenant=subject.issuer_tenant,
                        verified_at=subject_verified_at,
                    ),
                )
            principal = build_principal_from_resolved_identity(
                result=result,
                authentication=authentication,
                provider_identity=provider_identity,
                enterprise_subject_evidence=subject_evidence,
            )
            return principal, server

    @staticmethod
    async def _load_live_provider_identity(
        *,
        session: AsyncSession,
        resume: InteractionResume,
        identity: ExternalIdentity,
    ) -> VerifiedProviderIdentity:
        """Rebuild Channel provider evidence from the current authority graph."""

        request = resume.request
        rows = (
            await session.execute(
                sa.select(
                    McpInteractionPresentation,
                    IdentityProviderAccount,
                    ChannelBinding,
                    ChatChannel,
                    IdentityProviderChannelLink,
                )
                .join(
                    IdentityProviderAccount,
                    sa.and_(
                        IdentityProviderAccount.id == McpInteractionPresentation.provider_account_id,
                        IdentityProviderAccount.tenant_id == McpInteractionPresentation.tenant_id,
                        IdentityProviderAccount.provider == McpInteractionPresentation.provider,
                    ),
                )
                .join(
                    ChannelBinding,
                    ChannelBinding.id == McpInteractionPresentation.binding_id,
                )
                .join(ChatChannel, ChatChannel.id == ChannelBinding.channel_id)
                .join(
                    IdentityProviderChannelLink,
                    sa.and_(
                        IdentityProviderChannelLink.channel_id == ChatChannel.id,
                        IdentityProviderChannelLink.provider_account_id == IdentityProviderAccount.id,
                    ),
                )
                .where(
                    McpInteractionPresentation.interaction_id == request.interaction_id,
                    McpInteractionPresentation.tenant_id == request.tenant_id,
                    McpInteractionPresentation.revision == resume.revision,
                    McpInteractionPresentation.provider == identity.provider,
                    IdentityProviderAccount.provider_tenant_key == identity.provider_tenant_key,
                    IdentityProviderAccount.identity_health_state == IdentityProviderHealthState.HEALTHY.value,
                    ChannelBinding.enabled.is_(True),
                    ChannelBinding.generation == McpInteractionPresentation.binding_generation,
                    ChatChannel.tenant_id == request.tenant_id,
                    ChatChannel.channel == identity.provider,
                    ChatChannel.status == 1,
                    IdentityProviderChannelLink.tenant_id == request.tenant_id,
                    IdentityProviderChannelLink.provider == identity.provider,
                )
                .limit(2)
            )
        ).all()
        if len(rows) != 1 or identity.verified_at is None:
            raise InteractionStateError(InteractionErrorCode.REAUTHORIZATION_DENIED)
        presentation, account, binding, channel, link = rows[0]
        if (
            presentation.provider_account_id != account.id
            or presentation.binding_id != binding.id
            or presentation.binding_generation != binding.generation
            or binding.channel_id != channel.id
            or link.channel_id != channel.id
            or link.provider_account_id != account.id
            or account.tenant_id != request.tenant_id
            or account.provider != identity.provider
            or account.provider_tenant_key != identity.provider_tenant_key
            or account.identity_health_state != IdentityProviderHealthState.HEALTHY.value
        ):
            raise InteractionStateError(InteractionErrorCode.REAUTHORIZATION_DENIED)
        return VerifiedProviderIdentity(
            platform_user_id=request.platform_user_id,
            tenant_id=request.tenant_id,
            provider=identity.provider,
            provider_tenant=identity.provider_tenant_key,
            provider_account_id=account.id,
            subject_type=identity.subject_type,
            subject=identity.subject_value,
            verified_at=identity.verified_at,
        )


async def start_mcp_interactions() -> None:
    global _active_service, _stopping, _worker_task
    if not get_app_config().identity.mcp_interactions.enabled:
        return
    service = get_mcp_interaction_service()
    _active_service = service
    _stopping = asyncio.Event()
    config = get_app_config().identity.mcp_interactions
    owner = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    _worker_task = asyncio.create_task(
        run_interaction_worker(
            service=service,
            executor=ReauthorizingInteractionExecutor(handler=service),
            owner=owner,
            poll_seconds=config.poll_seconds,
            stopping=_stopping,
        ),
        name="mcp-interaction-recovery",
    )


async def stop_mcp_interactions() -> None:
    global _active_service, _stopping, _worker_task
    task = _worker_task
    if _stopping is not None:
        _stopping.set()
    if task is not None:
        await task
    _worker_task = None
    _stopping = None
    _active_service = None


def reset_mcp_interactions() -> None:
    global _active_service, _stopping, _worker_task
    if _worker_task is not None and not _worker_task.done():
        raise RuntimeError("MCP interaction worker must be stopped before reset")
    _worker_task = None
    _stopping = None
    _active_service = None
    get_mcp_interaction_service.cache_clear()


__all__ = [
    "ReauthorizingInteractionExecutor",
    "get_mcp_interaction_service",
    "reset_mcp_interactions",
    "resolve_mcp_interaction_handler",
    "start_mcp_interactions",
    "stop_mcp_interactions",
]
