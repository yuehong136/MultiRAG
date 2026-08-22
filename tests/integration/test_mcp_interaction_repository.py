"""Real PostgreSQL U14 CAS, encryption, leasing, and restart recovery."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.db import UserAccountKind
from api.db.db_models import McpInteraction, Tenant, User
from api.identity.mcp_interactions import (
    InteractionErrorCode,
    InteractionServiceLimits,
    InteractionStateError,
    PersistentInteractionService,
    ResponseClaimStatus,
)
from api.identity.mcp_interactions.crypto import InteractionPayloadCipher
from api.identity.principal import (
    AuthenticatedActor,
    AuthenticationContext,
    AuthenticationSource,
    IdentityAssurance,
    Principal,
    TenantMembershipEvidence,
    build_principal_from_authenticated_actor,
)
from common.constants import StatusEnum
from common.mcp_interactions import (
    InteractionEffect,
    InteractionRequest,
    InteractionResume,
)


def _principal(*, tenant_id: str, user_id: str) -> Principal:
    now = datetime.now(UTC)
    return build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(platform_user_id=user_id, display_name="U14 tester"),
        membership=TenantMembershipEvidence(
            platform_user_id=user_id,
            tenant_id=tenant_id,
        ),
        authentication=AuthenticationContext(
            source=AuthenticationSource.WEB_SESSION,
            assurance=IdentityAssurance.AUTHENTICATED,
            validated_at=now,
        ),
    )


def _request(*, tenant_id: str, user_id: str, **overrides: object) -> InteractionRequest:
    values: dict[str, object] = {
        "tenant_id": tenant_id,
        "platform_user_id": user_id,
        "agent_id": "agent-a",
        "agent_revision_id": "release-a",
        "mcp_server_id": "server-a",
        "resource_name": "leave-service",
        "resource_uri": "https://mcp.example/leave",
        "tool_name": "prepare_leave",
        "original_arguments": {"reason": "private-reason"},
        "input_requests": {
            "leave-form": {
                "method": "elicitation/create",
                "params": {
                    "mode": "form",
                    "message": "Need a date",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"start": {"type": "string"}},
                        "required": ["start"],
                        "additionalProperties": False,
                    },
                },
            }
        },
        "output_schema": {
            "type": "object",
            "properties": {"status": {"const": "prepared"}},
            "required": ["status"],
            "additionalProperties": False,
        },
        "request_state": "opaque-request-state",
        "effect": InteractionEffect.PREPARE,
        "replay_mode": "reusable",
        "policy_revision": "policy-a",
        "credential_generation": 3,
        "expires_at": datetime.now(UTC) + timedelta(minutes=10),
    }
    values.update(overrides)
    return InteractionRequest(**values)  # type: ignore[arg-type]


def _service(
    factory: async_sessionmaker[AsyncSession],
    *,
    key: bytes,
) -> PersistentInteractionService:
    return PersistentInteractionService(
        session_factory=factory,
        cipher=InteractionPayloadCipher([key]),
        limits=InteractionServiceLimits(
            lease_seconds=30,
            batch_size=10,
            max_rounds=5,
            max_payload_bytes=65_536,
        ),
    )


class _Completes:
    async def execute(self, resume: InteractionResume) -> object:
        assert resume.request.original_arguments == {"reason": "private-reason"}
        assert resume.request.request_state == "opaque-request-state"
        return {"status": "prepared"}


class _InvalidResult:
    async def execute(self, resume: InteractionResume) -> object:
        del resume
        return {"status": "executed"}


async def test_restart_recovers_encrypted_round_and_completes_once(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4().hex
    user_id = uuid.uuid4().hex
    key = b"u14-integration-key-material-32b!"[:32]
    async with factory.begin() as session:
        session.add_all(
            [
                Tenant(
                    id=tenant_id,
                    name="U14 tenant",
                    llm_id="test-llm",
                    embd_id="test-embedding",
                    asr_id="test-asr",
                    img2txt_id="test-image",
                    parser_ids="naive",
                ),
                User(
                    id=user_id,
                    nickname="U14 user",
                    email=None,
                    password=None,
                    account_kind=UserAccountKind.EXTERNAL.value,
                    login_channel="feishu",
                    is_authenticated=True,
                    is_active=True,
                    is_anonymous=False,
                    status=StatusEnum.VALID.value,
                ),
            ]
        )
    first_process = _service(factory, key=key)
    receipt = await first_process.pause(_request(tenant_id=tenant_id, user_id=user_id))
    projection = await first_process.projection(
        tenant_id=tenant_id,
        platform_user_id=user_id,
        interaction_id=receipt.interaction_id,
    )
    assert projection.state == "awaiting_input"
    assert set(projection.input_requests) == {"leave-form"}
    assert "private-reason" not in repr(projection)
    assert "opaque-request-state" not in repr(projection)
    claim = await first_process.submit_response(
        principal=_principal(tenant_id=tenant_id, user_id=user_id),
        interaction_id=receipt.interaction_id,
        revision=receipt.revision,
        response_idempotency_key="provider-event-a",
        input_responses={
            "leave-form": {
                "action": "accept",
                "content": {"start": "2026-09-01"},
            }
        },
    )
    duplicate = await first_process.submit_response(
        principal=_principal(tenant_id=tenant_id, user_id=user_id),
        interaction_id=receipt.interaction_id,
        revision=receipt.revision,
        response_idempotency_key="provider-event-a",
        input_responses={
            "leave-form": {
                "action": "accept",
                "content": {"start": "2026-09-01"},
            }
        },
    )
    assert claim.status is ResponseClaimStatus.ACCEPTED
    assert duplicate.status is ResponseClaimStatus.DUPLICATE

    # Simulate a new process: no in-memory future/session survives, only the
    # key ring and database are reused.
    second_process = _service(factory, key=key)
    assert await second_process.run_once(owner="worker-after-restart", executor=_Completes()) == 1
    assert await second_process.run_once(owner="worker-after-restart", executor=_Completes()) == 0

    async with factory() as session:
        row = await session.get(McpInteraction, receipt.interaction_id)
        assert row is not None
        assert row.state == "completed"
        serialized = " ".join(
            (
                row.original_arguments_ciphertext,
                row.input_requests_ciphertext,
                row.request_state_ciphertext,
                row.output_schema_ciphertext or "",
                row.result_ciphertext or "",
            )
        )
        assert "private-reason" not in serialized
        assert "opaque-request-state" not in serialized
        assert "2026-09-01" not in serialized

    invalid_receipt = await second_process.pause(
        _request(tenant_id=tenant_id, user_id=user_id),
    )
    await second_process.submit_response(
        principal=_principal(tenant_id=tenant_id, user_id=user_id),
        interaction_id=invalid_receipt.interaction_id,
        revision=invalid_receipt.revision,
        response_idempotency_key="provider-event-invalid-result",
        input_responses={
            "leave-form": {
                "action": "accept",
                "content": {"start": "2026-09-02"},
            }
        },
    )
    assert (
        await second_process.run_once(
            owner="worker-invalid-result",
            executor=_InvalidResult(),
        )
        == 1
    )
    async with factory() as session:
        row = await session.get(McpInteraction, invalid_receipt.interaction_id)
        assert row is not None
        assert row.state == "failed"
        assert row.result_ciphertext is None


async def test_response_actor_and_competing_idempotency_are_fail_closed(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4().hex
    user_id = uuid.uuid4().hex
    other_user_id = uuid.uuid4().hex
    key = b"0" * 32
    async with factory.begin() as session:
        session.add_all(
            [
                Tenant(
                    id=tenant_id,
                    name="U14 CAS tenant",
                    llm_id="test-llm",
                    embd_id="test-embedding",
                    asr_id="test-asr",
                    img2txt_id="test-image",
                    parser_ids="naive",
                ),
                *[
                    User(
                        id=item,
                        nickname="U14 actor",
                        email=None,
                        password=None,
                        account_kind=UserAccountKind.EXTERNAL.value,
                        login_channel="feishu",
                        is_authenticated=True,
                        is_active=True,
                        is_anonymous=False,
                        status=StatusEnum.VALID.value,
                    )
                    for item in (user_id, other_user_id)
                ],
            ]
        )
    service = _service(factory, key=key)
    receipt = await service.pause(_request(tenant_id=tenant_id, user_id=user_id))
    response = {
        "leave-form": {
            "action": "accept",
            "content": {"start": "2026-09-01"},
        }
    }
    with pytest.raises(InteractionStateError) as wrong_actor:
        await service.submit_response(
            principal=_principal(tenant_id=tenant_id, user_id=other_user_id),
            interaction_id=receipt.interaction_id,
            revision=receipt.revision,
            response_idempotency_key="wrong-actor",
            input_responses=response,
        )
    assert wrong_actor.value.code is InteractionErrorCode.ACTOR_MISMATCH

    await service.submit_response(
        principal=_principal(tenant_id=tenant_id, user_id=user_id),
        interaction_id=receipt.interaction_id,
        revision=receipt.revision,
        response_idempotency_key="first-response",
        input_responses=response,
    )
    with pytest.raises(InteractionStateError) as competing:
        await service.submit_response(
            principal=_principal(tenant_id=tenant_id, user_id=user_id),
            interaction_id=receipt.interaction_id,
            revision=receipt.revision,
            response_idempotency_key="different-response",
            input_responses=response,
        )
    assert competing.value.code is InteractionErrorCode.STATE_CONFLICT

    expiring = await service.pause(_request(tenant_id=tenant_id, user_id=user_id))
    async with factory.begin() as session:
        row = await session.get(McpInteraction, expiring.interaction_id)
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(InteractionStateError) as expired:
        await service.submit_response(
            principal=_principal(tenant_id=tenant_id, user_id=user_id),
            interaction_id=expiring.interaction_id,
            revision=expiring.revision,
            response_idempotency_key="expired-response",
            input_responses=response,
        )
    assert expired.value.code is InteractionErrorCode.EXPIRED
    async with factory() as session:
        row = await session.get(McpInteraction, expiring.interaction_id)
        assert row is not None
        assert row.state == "expired"

    cancelled = await service.pause(_request(tenant_id=tenant_id, user_id=user_id))
    await service.submit_response(
        principal=_principal(tenant_id=tenant_id, user_id=user_id),
        interaction_id=cancelled.interaction_id,
        revision=cancelled.revision,
        response_idempotency_key="cancel-response",
        input_responses={"leave-form": {"action": "cancel"}},
    )
    async with factory() as session:
        row = await session.get(McpInteraction, cancelled.interaction_id)
        assert row is not None
        assert row.state == "cancelled"
