"""Real PostgreSQL contracts for durable Channel interaction presentations."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.channel_execution.interaction_presentations import (
    InteractionCallbackPayload,
    InteractionPresentationError,
    InteractionPresentationErrorCode,
    InteractionPresentationService,
)
from api.channel_execution.models import (
    ChannelActor,
    ExecutionTargetRef,
    TrustedChannelContext,
)
from api.db import IdentityProviderHealthState, UserAccountKind
from api.db.db_models import (
    ChannelBinding,
    ChatChannel,
    IdentityProviderAccount,
    IdentityProviderChannelLink,
    IdentityProviderTenant,
    McpInteraction,
    McpInteractionCallbackReceipt,
    McpInteractionPresentation,
    Tenant,
    User,
)
from api.identity.contracts import (
    ExternalIdentityRecord,
    IdentityResolutionResult,
    IdentityResolutionStatus,
    UserMembershipRecord,
)
from api.identity.mcp_interactions import (
    InteractionServiceLimits,
    PersistentInteractionService,
)
from api.identity.mcp_interactions.crypto import InteractionPayloadCipher
from api.identity.mcp_interactions.validation import interaction_schema_digest
from api.identity.principal import (
    AuthenticationContext,
    AuthenticationSource,
    IdentityAssurance,
    Principal,
    build_principal_from_resolved_identity,
)
from common.constants import StatusEnum
from common.mcp_interactions import InteractionEffect, InteractionRequest

_SCHEMA = "usr_ai"
_U15_REVISION = "d8f0a2b4c6e8"


def _enterprise_principal(
    *,
    tenant_id: str,
    user_id: str,
    external_identity_id: str,
    verified_at: datetime,
) -> Principal:
    identity = ExternalIdentityRecord(
        id=external_identity_id,
        tenant_id=tenant_id,
        user_id=user_id,
        provider="feishu",
        provider_tenant_key="tenant-key",
        subject_type="employee_no",
        subject_value="employee-1",
        state="active",
        verified_at=verified_at,
        last_seen_at=verified_at,
        identity_revision=1,
        attributes=(("display_name", "U15 tester"),),
    )
    return build_principal_from_resolved_identity(
        result=IdentityResolutionResult(
            status=IdentityResolutionStatus.RESOLVED,
            identity=identity,
            membership=UserMembershipRecord(
                user_id=user_id,
                tenant_id=tenant_id,
                role="normal",
            ),
        ),
        authentication=AuthenticationContext(
            source=AuthenticationSource.ENTERPRISE_IDENTITY,
            assurance=IdentityAssurance.DIRECTORY_VERIFIED,
            validated_at=verified_at + timedelta(seconds=1),
            assurance_verified_at=verified_at,
            provider="feishu",
            external_identity_id=external_identity_id,
        ),
    )


def _interaction_request(
    *,
    tenant_id: str,
    user_id: str,
    external_identity_id: str,
    expires_at: datetime,
    input_requests: dict[str, Any] | None = None,
) -> InteractionRequest:
    return InteractionRequest(
        tenant_id=tenant_id,
        platform_user_id=user_id,
        external_identity_id=external_identity_id,
        identity_revision=1,
        agent_id="agent-u15",
        agent_revision_id="release-u15",
        mcp_server_id="server-u15",
        resource_name="leave-service",
        resource_uri="https://mcp.example/leave",
        tool_name="prepare_leave",
        original_arguments={"private": "must-remain-encrypted"},
        input_requests=input_requests
        or {
            "leave-form": {
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "message": "请选择请假日期",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "start": {
                                "type": "string",
                                "format": "date",
                                "title": "开始日期",
                            }
                        },
                        "required": ["start"],
                        "additionalProperties": False,
                    },
                },
            }
        },
        output_schema=None,
        request_state="opaque-request-state",
        effect=InteractionEffect.PREPARE,
        replay_mode="reusable",
        policy_revision="policy-u15",
        credential_generation=1,
        expires_at=expires_at,
    )


async def _seed_scope(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    user_id: str,
    channel_id: str,
    binding_id: str,
    provider_tenant_id: str,
    provider_account_id: str,
    link_id: str,
    provider_tenant_key: str,
) -> None:
    now = datetime.now(UTC)
    async with factory.begin() as session:
        session.add_all(
            [
                Tenant(
                    id=tenant_id,
                    name="U15 integration tenant",
                    llm_id="test-llm",
                    embd_id="test-embedding",
                    asr_id="test-asr",
                    img2txt_id="test-image",
                    parser_ids="naive",
                ),
                User(
                    id=user_id,
                    nickname="U15 integration user",
                    email=None,
                    password=None,
                    account_kind=UserAccountKind.EXTERNAL.value,
                    login_channel="feishu",
                    is_authenticated=True,
                    is_active=True,
                    is_anonymous=False,
                    status=StatusEnum.VALID.value,
                ),
                ChatChannel(
                    id=channel_id,
                    tenant_id=tenant_id,
                    name="U15 Feishu",
                    channel="feishu",
                    config={},
                    status=1,
                    generation=1,
                ),
            ]
        )
        await session.flush()
        session.add(
            IdentityProviderTenant(
                id=provider_tenant_id,
                tenant_id=tenant_id,
                provider="feishu",
                provider_tenant_key=provider_tenant_key,
                verified_at=now,
            )
        )
        await session.flush()
        session.add(
            IdentityProviderAccount(
                id=provider_account_id,
                tenant_id=tenant_id,
                provider="feishu",
                provider_tenant_key=provider_tenant_key,
                provider_account_key=f"app-{provider_account_id}",
                identity_revision=1,
                identity_health_state=IdentityProviderHealthState.HEALTHY.value,
            )
        )
        session.add(
            ChannelBinding(
                id=binding_id,
                channel_id=channel_id,
                target_type="multirag.canvas_agent",
                target_id=uuid.uuid4().hex,
                target_revision_id=uuid.uuid4().hex,
                policy={},
                enabled=True,
                generation=1,
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
                linked_at=now,
            )
        )


async def _cleanup_scope(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    user_id: str,
    channel_id: str,
    binding_id: str,
) -> None:
    async with factory.begin() as session:
        await session.execute(sa.delete(McpInteractionPresentation).where(McpInteractionPresentation.binding_id == binding_id))
        await session.execute(sa.delete(McpInteraction).where(McpInteraction.tenant_id == tenant_id))
        await session.execute(sa.delete(IdentityProviderChannelLink).where(IdentityProviderChannelLink.tenant_id == tenant_id))
        await session.execute(sa.delete(ChannelBinding).where(ChannelBinding.id == binding_id))
        await session.execute(sa.delete(ChatChannel).where(ChatChannel.id == channel_id))
        await session.execute(sa.delete(IdentityProviderAccount).where(IdentityProviderAccount.tenant_id == tenant_id))
        await session.execute(sa.delete(IdentityProviderTenant).where(IdentityProviderTenant.tenant_id == tenant_id))
        await session.execute(sa.delete(User).where(User.id == user_id))
        await session.execute(sa.delete(Tenant).where(Tenant.id == tenant_id))


def test_u15_migration_matches_orm_and_scope_constraints(
    pg_scratch_engine: sa.Engine,
    alembic_cfg: Any,
) -> None:
    revision = ScriptDirectory.from_config(alembic_cfg).get_revision(_U15_REVISION)
    assert revision is not None
    migration = revision.module
    schema = "u15"
    original_schema = migration.SCHEMA
    original_op = migration.op

    try:
        with pg_scratch_engine.begin() as connection:
            connection.execute(sa.text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.execute(sa.text(f'CREATE SCHEMA "{schema}"'))
            connection.execute(sa.text(f'CREATE TABLE "{schema}".t_ai_mcp_interactions (id VARCHAR(32) NOT NULL, tenant_id VARCHAR(32) NOT NULL, PRIMARY KEY (id), UNIQUE (id, tenant_id))'))
            connection.execute(sa.text(f'CREATE TABLE "{schema}".t_ai_channel_bindings (id VARCHAR(32) PRIMARY KEY)'))
            connection.execute(
                sa.text(
                    f'CREATE TABLE "{schema}".t_ai_identity_provider_accounts '
                    "(id VARCHAR(32) NOT NULL, tenant_id VARCHAR(32) NOT NULL, "
                    "provider VARCHAR(64) NOT NULL, PRIMARY KEY (id), "
                    "UNIQUE (id, tenant_id, provider))"
                )
            )
            migration.SCHEMA = schema
            migration.op = Operations(MigrationContext.configure(connection))
            migration.upgrade()

            inspector = sa.inspect(connection)
            for model in (
                McpInteractionPresentation,
                McpInteractionCallbackReceipt,
            ):
                columns = {item["name"]: item for item in inspector.get_columns(model.__tablename__, schema=schema)}
                assert set(columns) == {column.name for column in model.__table__.columns}
                for column in model.__table__.columns:
                    assert columns[column.name]["nullable"] is column.nullable
                    expected_length = getattr(column.type, "length", None)
                    if expected_length is not None:
                        assert columns[column.name]["type"].length == expected_length

            presentation_fks = {
                item["name"]: (
                    tuple(item["constrained_columns"]),
                    item["referred_table"],
                    tuple(item["referred_columns"]),
                    item["options"].get("ondelete"),
                )
                for item in inspector.get_foreign_keys(McpInteractionPresentation.__tablename__, schema=schema)
            }
            assert presentation_fks["fk_mcp_interaction_presentations_provider_account_scope"] == (
                ("provider_account_id", "tenant_id", "provider"),
                "t_ai_identity_provider_accounts",
                ("id", "tenant_id", "provider"),
                "RESTRICT",
            )
            receipt_fks = {
                item["name"]: (
                    tuple(item["constrained_columns"]),
                    item["referred_table"],
                    tuple(item["referred_columns"]),
                    item["options"].get("ondelete"),
                )
                for item in inspector.get_foreign_keys(McpInteractionCallbackReceipt.__tablename__, schema=schema)
            }
            assert receipt_fks["fk_mcp_interaction_callback_receipts_presentation_scope"] == (
                ("presentation_id", "binding_id"),
                McpInteractionPresentation.__tablename__,
                ("id", "binding_id"),
                "CASCADE",
            )

            presentation_indexes = {
                item["name"]: tuple(item["column_names"]) for item in inspector.get_indexes(McpInteractionPresentation.__tablename__, schema=schema) if not item.get("duplicates_constraint")
            }
            assert presentation_indexes["ix_mcp_interaction_presentations_delivery_ready"] == (
                "binding_id",
                "binding_generation",
                "delivery_state",
                "delivery_next_attempt_at",
                "delivery_lease_until",
                "created_at",
            )
            assert presentation_indexes["ix_mcp_interaction_presentations_reconcile"] == ("response_state", "updated_at")
            receipt_indexes = {
                item["name"]: tuple(item["column_names"]) for item in inspector.get_indexes(McpInteractionCallbackReceipt.__tablename__, schema=schema) if not item.get("duplicates_constraint")
            }
            assert receipt_indexes["ix_mcp_interaction_callback_receipts_ready"] == ("state", "next_attempt_at", "lease_until", "created_at")

            presentation_checks = {item["name"] for item in inspector.get_check_constraints(McpInteractionPresentation.__tablename__, schema=schema)}
            receipt_checks = {item["name"] for item in inspector.get_check_constraints(McpInteractionCallbackReceipt.__tablename__, schema=schema)}
            assert {
                "ck_mcp_interaction_presentations_counters",
                "ck_mcp_interaction_presentations_delivery_lease",
                "ck_mcp_interaction_presentations_nonempty",
                "ck_mcp_interaction_presentations_source_digest",
            }.issubset(presentation_checks)
            assert {
                "ck_mcp_interaction_callback_receipts_attempt_digest",
                "ck_mcp_interaction_callback_receipts_lease",
                "ck_mcp_interaction_callback_receipts_state",
            }.issubset(receipt_checks)

            migration.downgrade()
            assert not inspector.has_table(McpInteractionPresentation.__tablename__, schema=schema)
            assert not inspector.has_table(McpInteractionCallbackReceipt.__tablename__, schema=schema)
            connection.execute(sa.text(f'DROP SCHEMA "{schema}" CASCADE'))
    finally:
        migration.SCHEMA = original_schema
        migration.op = original_op


async def test_presentation_callback_leases_and_restart_recovery(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    suffix = uuid.uuid4().hex
    tenant_id = uuid.uuid4().hex
    user_id = uuid.uuid4().hex
    external_identity_id = uuid.uuid4().hex
    channel_id = uuid.uuid4().hex
    binding_id = uuid.uuid4().hex
    provider_tenant_id = uuid.uuid4().hex
    provider_account_id = uuid.uuid4().hex
    link_id = uuid.uuid4().hex
    provider_tenant_key = f"tenant-{suffix}"
    key = b"u15-integration-key-material-32b"[:32]
    cipher = InteractionPayloadCipher([key])
    principal = _enterprise_principal(
        tenant_id=tenant_id,
        user_id=user_id,
        external_identity_id=external_identity_id,
        verified_at=datetime.now(UTC) - timedelta(seconds=2),
    )
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
        principal_id=user_id,
        principal=principal,
    )

    await _seed_scope(
        factory,
        tenant_id=tenant_id,
        user_id=user_id,
        channel_id=channel_id,
        binding_id=binding_id,
        provider_tenant_id=provider_tenant_id,
        provider_account_id=provider_account_id,
        link_id=link_id,
        provider_tenant_key=provider_tenant_key,
    )
    try:
        interaction_service = PersistentInteractionService(
            session_factory=factory,
            cipher=cipher,
            limits=InteractionServiceLimits(
                lease_seconds=30,
                batch_size=1,
                max_rounds=5,
                max_payload_bytes=65_536,
                ttl_seconds=600,
            ),
        )
        receipt = await interaction_service.pause(
            _interaction_request(
                tenant_id=tenant_id,
                user_id=user_id,
                external_identity_id=external_identity_id,
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        first_process = InteractionPresentationService(
            session_factory=factory,
            cipher=cipher,
        )
        notice = await first_process.register(
            context=context,
            interaction_id=receipt.interaction_id,
            revision=receipt.revision,
            source_event_id=f"event-{suffix}",
            conversation_ref=f"chat-{suffix}",
            presentation_ref=f"message-{suffix}",
        )
        duplicate_notice = await first_process.register(
            context=context,
            interaction_id=receipt.interaction_id,
            revision=receipt.revision,
            source_event_id=f"event-{suffix}",
            conversation_ref=f"chat-{suffix}",
            presentation_ref=f"message-{suffix}",
        )
        assert duplicate_notice == notice
        with pytest.raises(InteractionPresentationError) as exc_info:
            await first_process.register(
                context=context,
                interaction_id=receipt.interaction_id,
                revision=receipt.revision,
                source_event_id=f"event-{suffix}",
                conversation_ref=f"chat-{suffix}",
                presentation_ref="different-message",
            )
        assert exc_info.value.code is InteractionPresentationErrorCode.STATE_CONFLICT

        async with factory() as session:
            interaction = await session.get(McpInteraction, receipt.interaction_id)
            assert interaction is not None
            assert interaction.provider == "feishu"
            assert interaction.presentation_ref == f"message-{suffix}"
            presentations = tuple(await session.scalars(sa.select(McpInteractionPresentation).where(McpInteractionPresentation.interaction_id == receipt.interaction_id)))
            assert len(presentations) == 1
            assert f"event-{suffix}" not in presentations[0].source_event_digest
            assert "start" not in str(presentations[0].delivery_projection)
            assert "start" not in presentations[0].form_mapping_ciphertext

        # A fresh service instance recovers the pending delivery.  Expiring its
        # DB lease demonstrates that both owner and rotated one-time token fence
        # ACKs across worker restarts.
        second_process = InteractionPresentationService(
            session_factory=factory,
            cipher=cipher,
        )
        stale_delivery = await second_process.claim_delivery(
            binding_id=binding_id,
            binding_generation=1,
            owner="channel-worker-a",
            lease_seconds=30,
        )
        assert stale_delivery is not None
        assert stale_delivery.kind == "form"
        assert stale_delivery.action_nonce is not None
        async with factory.begin() as session:
            row = await session.get(
                McpInteractionPresentation,
                stale_delivery.delivery_id,
            )
            assert row is not None
            db_now = cast(datetime, await session.scalar(sa.select(sa.func.now())))
            row.delivery_lease_until = db_now - timedelta(seconds=1)
            assert stale_delivery.delivery_token not in (row.delivery_token_digest or "")
            assert stale_delivery.action_nonce not in (row.nonce_digest or "")

        with pytest.raises(InteractionPresentationError) as exc_info:
            await second_process.acknowledge_delivery(
                binding_id=binding_id,
                binding_generation=1,
                delivery_id=stale_delivery.delivery_id,
                owner="channel-worker-a",
                delivery_token=stale_delivery.delivery_token,
                success=True,
                safe_error_code=None,
            )
        assert exc_info.value.code is InteractionPresentationErrorCode.DELIVERY_CONFLICT

        current_delivery = await InteractionPresentationService(
            session_factory=factory,
            cipher=cipher,
        ).claim_delivery(
            binding_id=binding_id,
            binding_generation=1,
            owner="channel-worker-b",
            lease_seconds=30,
        )
        assert current_delivery is not None
        assert current_delivery.delivery_token != stale_delivery.delivery_token
        assert current_delivery.action_nonce != stale_delivery.action_nonce

        # Begin ACK before expiry, hold its presentation row lock past the
        # deadline, then release it. PostgreSQL now() would preserve the old
        # transaction timestamp and incorrectly accept this stale owner.
        async with factory.begin() as session:
            delivery_row = await session.get(
                McpInteractionPresentation,
                current_delivery.delivery_id,
            )
            assert delivery_row is not None
            db_now = cast(
                datetime,
                await session.scalar(sa.select(sa.func.clock_timestamp())),
            )
            delivery_row.delivery_lease_until = db_now + timedelta(seconds=1)

        lock_holder = factory()
        await lock_holder.begin()
        try:
            await lock_holder.scalar(sa.select(McpInteractionPresentation).where(McpInteractionPresentation.id == current_delivery.delivery_id).with_for_update())
            started = asyncio.Event()

            async def acknowledge_after_lock_wait() -> None:
                started.set()
                await second_process.acknowledge_delivery(
                    binding_id=binding_id,
                    binding_generation=1,
                    delivery_id=current_delivery.delivery_id,
                    owner="channel-worker-b",
                    delivery_token=current_delivery.delivery_token,
                    success=True,
                    safe_error_code=None,
                )

            contender = asyncio.create_task(acknowledge_after_lock_wait())
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.sleep(0.1)
            assert not contender.done()
            await asyncio.sleep(1.1)
            await lock_holder.commit()
            with pytest.raises(InteractionPresentationError) as exc_info:
                await asyncio.wait_for(contender, timeout=2)
            assert exc_info.value.code is InteractionPresentationErrorCode.DELIVERY_CONFLICT
        finally:
            if lock_holder.in_transaction():
                await lock_holder.rollback()
            await lock_holder.close()

        expired_wait_delivery = current_delivery
        current_delivery = await second_process.claim_delivery(
            binding_id=binding_id,
            binding_generation=1,
            owner="channel-worker-c",
            lease_seconds=30,
        )
        assert current_delivery is not None
        assert current_delivery.delivery_token != expired_wait_delivery.delivery_token
        assert current_delivery.action_nonce != expired_wait_delivery.action_nonce
        with pytest.raises(InteractionPresentationError):
            await second_process.acknowledge_delivery(
                binding_id=binding_id,
                binding_generation=1,
                delivery_id=current_delivery.delivery_id,
                owner="channel-worker-a",
                delivery_token=stale_delivery.delivery_token,
                success=True,
                safe_error_code=None,
            )
        await second_process.acknowledge_delivery(
            binding_id=binding_id,
            binding_generation=1,
            delivery_id=current_delivery.delivery_id,
            owner="channel-worker-c",
            delivery_token=current_delivery.delivery_token,
            success=True,
            safe_error_code=None,
        )

        field_id = cast(str, current_delivery.projection["fields"][0]["name"])
        callback = InteractionCallbackPayload(
            event_id=f"callback-{suffix}",
            nonce=cast(str, current_delivery.action_nonce),
            message_id=f"message-{suffix}",
            actor=ChannelActor(
                provider="feishu",
                subject=f"open-{suffix}",
                conversation=f"chat-{suffix}",
            ),
            form_value={field_id: "2026-09-01"},
        )
        callback_result = await second_process.receive_callback(
            binding_id=binding_id,
            binding_generation=1,
            action_id=receipt.interaction_id,
            revision=receipt.revision,
            payload=callback,
        )
        duplicate_result = await InteractionPresentationService(
            session_factory=factory,
            cipher=cipher,
        ).receive_callback(
            binding_id=binding_id,
            binding_generation=1,
            action_id=receipt.interaction_id,
            revision=receipt.revision,
            payload=callback,
        )
        assert callback_result.status == "accepted"
        assert duplicate_result.status == "duplicate"
        conflicting_callback = callback.model_copy(update={"form_value": {field_id: "2026-09-02"}})
        with pytest.raises(InteractionPresentationError) as exc_info:
            await second_process.receive_callback(
                binding_id=binding_id,
                binding_generation=1,
                action_id=receipt.interaction_id,
                revision=receipt.revision,
                payload=conflicting_callback,
            )
        assert exc_info.value.code is InteractionPresentationErrorCode.STATE_CONFLICT

        async with factory() as session:
            callback_row = await session.scalar(sa.select(McpInteractionCallbackReceipt).where(McpInteractionCallbackReceipt.binding_id == binding_id))
            assert callback_row is not None
            serialized = " ".join(
                (
                    callback_row.event_digest,
                    callback_row.payload_digest,
                    callback_row.payload_ciphertext,
                )
            )
            assert f"callback-{suffix}" not in serialized
            assert "2026-09-01" not in serialized

        stale_callback_lease = await InteractionPresentationService(
            session_factory=factory,
            cipher=cipher,
        ).lease_callback(owner="callback-worker-a", lease_seconds=30)
        assert stale_callback_lease is not None
        assert stale_callback_lease.input_responses == {
            "leave-form": {
                "action": "accept",
                "content": {"start": "2026-09-01"},
            }
        }
        async with factory.begin() as session:
            callback_row = await session.get(
                McpInteractionCallbackReceipt,
                stale_callback_lease.receipt_id,
            )
            assert callback_row is not None
            db_now = cast(datetime, await session.scalar(sa.select(sa.func.now())))
            callback_row.lease_until = db_now - timedelta(seconds=1)

        with pytest.raises(InteractionPresentationError):
            await second_process.mark_callback_claimed(stale_callback_lease)
        current_callback_lease = await InteractionPresentationService(
            session_factory=factory,
            cipher=cipher,
        ).lease_callback(owner="callback-worker-b", lease_seconds=30)
        assert current_callback_lease is not None
        assert current_callback_lease.attempt == stale_callback_lease.attempt + 1
        with pytest.raises(InteractionPresentationError):
            await second_process.mark_callback_claimed(stale_callback_lease)
        await second_process.mark_callback_claimed(current_callback_lease)

        # Simulate the durable U14 worker advancing to another input round.
        # The presentation worker must recover that round and later replace it
        # with a terminal card without any in-memory state from prior workers.
        second_round_inputs = {
            "leave-reason": {
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "message": "请填写原因",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {
                            "reason": {
                                "type": "string",
                                "title": "原因",
                                "minLength": 1,
                                "maxLength": 120,
                            }
                        },
                        "required": ["reason"],
                        "additionalProperties": False,
                    },
                },
            }
        }
        encrypted_inputs = cipher.encrypt(
            tenant_id=tenant_id,
            interaction_id=receipt.interaction_id,
            revision=2,
            purpose="input_requests",
            value=second_round_inputs,
        )
        async with factory.begin() as session:
            interaction = await session.get(McpInteraction, receipt.interaction_id)
            assert interaction is not None
            interaction.revision = 2
            interaction.round_count = 2
            interaction.state = "awaiting_input"
            interaction.input_requests_ciphertext = encrypted_inputs.ciphertext
            interaction.input_requests_key_id = encrypted_inputs.key_id
            interaction.schema_digest = interaction_schema_digest(second_round_inputs)
            interaction.expires_at = datetime.now(UTC) + timedelta(minutes=10)
            interaction.updated_at = datetime.now(UTC)

        recovery_process = InteractionPresentationService(
            session_factory=factory,
            cipher=cipher,
        )
        assert await recovery_process.reconcile() >= 1
        round_two = await recovery_process.claim_delivery(
            binding_id=binding_id,
            binding_generation=1,
            owner="channel-worker-c",
            lease_seconds=30,
            action_id=receipt.interaction_id,
            revision=2,
        )
        assert round_two is not None
        assert round_two.kind == "form"
        assert round_two.revision == 2

        async with factory.begin() as session:
            interaction = await session.get(McpInteraction, receipt.interaction_id)
            assert interaction is not None
            interaction.state = "completed"
            interaction.updated_at = datetime.now(UTC)
        assert await recovery_process.reconcile() >= 1
        terminal = await InteractionPresentationService(
            session_factory=factory,
            cipher=cipher,
        ).claim_delivery(
            binding_id=binding_id,
            binding_generation=1,
            owner="channel-worker-after-restart",
            lease_seconds=30,
            action_id=receipt.interaction_id,
            revision=2,
        )
        assert terminal is not None
        assert terminal.kind == "terminal"
        assert terminal.action_nonce is None
        assert terminal.projection == {
            "state": "completed",
            "message": "处理已完成。",
        }
        await recovery_process.acknowledge_delivery(
            binding_id=binding_id,
            binding_generation=1,
            delivery_id=terminal.delivery_id,
            owner="channel-worker-after-restart",
            delivery_token=terminal.delivery_token,
            success=True,
            safe_error_code=None,
        )
        async with factory() as session:
            rows = tuple(
                await session.scalars(sa.select(McpInteractionPresentation).where(McpInteractionPresentation.interaction_id == receipt.interaction_id).order_by(McpInteractionPresentation.revision))
            )
            assert [row.revision for row in rows] == [1, 2]
            assert rows[0].response_state == "terminal"
            assert rows[1].response_state == "terminal"
            assert rows[1].delivery_state == "delivered"
    finally:
        await _cleanup_scope(
            factory,
            tenant_id=tenant_id,
            user_id=user_id,
            channel_id=channel_id,
            binding_id=binding_id,
        )
