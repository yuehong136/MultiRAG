"""Real PostgreSQL U14 CAS, encryption, leasing, and restart recovery."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from api.db import UserAccountKind
from api.db.db_models import McpInteraction, McpInteractionResumeJob, Tenant, User
from api.identity.mcp_interactions import (
    InteractionErrorCode,
    InteractionLease,
    InteractionServiceLimits,
    InteractionStateError,
    PersistentInteractionService,
    ResponseClaimStatus,
)
from api.identity.mcp_interactions.crypto import InteractionPayloadCipher
from api.identity.mcp_interactions.repository import InteractionRepository
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
    InteractionLeaseFence,
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
    lease_seconds: int = 30,
    batch_size: int = 10,
    ttl_seconds: int = 600,
) -> PersistentInteractionService:
    return PersistentInteractionService(
        session_factory=factory,
        cipher=InteractionPayloadCipher([key]),
        limits=InteractionServiceLimits(
            lease_seconds=lease_seconds,
            batch_size=batch_size,
            max_rounds=5,
            max_payload_bytes=65_536,
            ttl_seconds=ttl_seconds,
        ),
    )


def _repository(
    session: AsyncSession,
    *,
    key: bytes,
) -> InteractionRepository:
    return InteractionRepository(
        session,
        cipher=InteractionPayloadCipher([key]),
        max_rounds=5,
        max_payload_bytes=65_536,
    )


async def _seed_actor(
    factory: async_sessionmaker[AsyncSession],
    *,
    tenant_id: str,
    user_id: str,
    label: str,
) -> None:
    async with factory.begin() as session:
        session.add_all(
            [
                Tenant(
                    id=tenant_id,
                    name=f"U14 {label} tenant",
                    llm_id="test-llm",
                    embd_id="test-embedding",
                    asr_id="test-asr",
                    img2txt_id="test-image",
                    parser_ids="naive",
                ),
                User(
                    id=user_id,
                    nickname=f"U14 {label} user",
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


async def _clear_resume_jobs(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with factory.begin() as session:
        await session.execute(sa.delete(McpInteractionResumeJob))


def _with_fence(
    lease: InteractionLease,
    *,
    owner: str,
    attempt: int,
) -> InteractionLease:
    fence = InteractionLeaseFence(
        job_id=lease.job_id,
        owner=owner,
        attempt=attempt,
    )
    request = replace(lease.resume.request, lease_fence=fence)
    resume = replace(lease.resume, request=request)
    return InteractionLease(
        job_id=lease.job_id,
        owner=owner,
        attempt=attempt,
        resume=resume,
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


async def test_stale_owner_and_attempt_cannot_advance_or_finalize(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4().hex
    user_id = uuid.uuid4().hex
    key = b"f" * 32
    await _clear_resume_jobs(factory)
    await _seed_actor(
        factory,
        tenant_id=tenant_id,
        user_id=user_id,
        label="fence",
    )
    service = _service(factory, key=key)
    receipt = await service.pause(_request(tenant_id=tenant_id, user_id=user_id))
    await service.submit_response(
        principal=_principal(tenant_id=tenant_id, user_id=user_id),
        interaction_id=receipt.interaction_id,
        revision=receipt.revision,
        response_idempotency_key="fence-response",
        input_responses={
            "leave-form": {
                "action": "accept",
                "content": {"start": "2026-09-03"},
            }
        },
    )
    async with factory.begin() as session:
        first = await _repository(session, key=key).lease_ready(
            owner="worker-a",
            lease_seconds=30,
            limit=1,
        )
    assert len(first) == 1
    expired = first[0]
    async with factory.begin() as session:
        job = await session.get(McpInteractionResumeJob, expired.job_id)
        assert job is not None
        job.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    async with factory.begin() as session:
        second = await _repository(session, key=key).lease_ready(
            owner="worker-b",
            lease_seconds=30,
            limit=1,
        )
    assert len(second) == 1
    current = second[0]
    assert current.attempt == expired.attempt + 1
    stale_owner = _with_fence(
        current,
        owner=expired.owner,
        attempt=current.attempt,
    )
    stale_attempt = _with_fence(
        current,
        owner=current.owner,
        attempt=current.attempt - 1,
    )

    for stale in (stale_owner, stale_attempt):
        for operation in ("advance", "complete", "retry", "terminal"):
            async with factory.begin() as session:
                repository = _repository(session, key=key)
                with pytest.raises(InteractionStateError) as rejected:
                    if operation == "advance":
                        await repository.pause(
                            replace(
                                stale.resume.request,
                                request_state="next-round-state",
                            )
                        )
                    elif operation == "complete":
                        await repository.complete(
                            lease=stale,
                            result={"status": "prepared"},
                        )
                    elif operation == "retry":
                        await repository.retry(
                            lease=stale,
                            delay_seconds=1,
                            code=InteractionErrorCode.REMOTE_RESULT_UNKNOWN,
                        )
                    else:
                        await repository.terminal_fail(
                            lease=stale,
                            code=InteractionErrorCode.REMOTE_RESULT_UNKNOWN,
                        )
                assert rejected.value.code is InteractionErrorCode.STATE_CONFLICT

    async with factory.begin() as session:
        await _repository(session, key=key).complete(
            lease=current,
            result={"status": "prepared"},
        )
    async with factory() as session:
        row = await session.get(McpInteraction, receipt.interaction_id)
        assert row is not None
        assert row.state == "completed"


async def test_long_call_renewal_prevents_second_worker_from_releasing_job(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4().hex
    user_id = uuid.uuid4().hex
    key = b"r" * 32
    await _clear_resume_jobs(factory)
    await _seed_actor(
        factory,
        tenant_id=tenant_id,
        user_id=user_id,
        label="renew",
    )
    first_worker = _service(
        factory,
        key=key,
        lease_seconds=1,
        batch_size=1,
    )
    receipt = await first_worker.pause(
        _request(tenant_id=tenant_id, user_id=user_id),
    )
    await first_worker.submit_response(
        principal=_principal(tenant_id=tenant_id, user_id=user_id),
        interaction_id=receipt.interaction_id,
        revision=receipt.revision,
        response_idempotency_key="renew-response",
        input_responses={
            "leave-form": {
                "action": "accept",
                "content": {"start": "2026-09-04"},
            }
        },
    )
    started = asyncio.Event()
    release = asyncio.Event()

    class _SlowExecutor:
        async def execute(self, resume: InteractionResume) -> object:
            del resume
            started.set()
            await release.wait()
            return {"status": "prepared"}

    class _MustNotExecute:
        async def execute(self, resume: InteractionResume) -> object:
            del resume
            raise AssertionError("second worker executed a renewed lease")

    first_task = asyncio.create_task(first_worker.run_once(owner="worker-a", executor=_SlowExecutor()))
    await asyncio.wait_for(started.wait(), timeout=2)
    await asyncio.sleep(1.2)
    second_worker = _service(
        factory,
        key=key,
        lease_seconds=1,
        batch_size=1,
    )

    assert await second_worker.run_once(owner="worker-b", executor=_MustNotExecute()) == 0
    release.set()
    assert await asyncio.wait_for(first_task, timeout=2) == 1
    async with factory() as session:
        row = await session.get(McpInteraction, receipt.interaction_id)
        assert row is not None
        assert row.state == "completed"


async def test_post_lock_wall_clock_rejects_lease_expired_while_waiting(
    bootstrapped_async_engine: AsyncEngine,
) -> None:
    """A transaction started before expiry must not revive after lock wait."""

    factory = async_sessionmaker(bootstrapped_async_engine, expire_on_commit=False)
    tenant_id = uuid.uuid4().hex
    user_id = uuid.uuid4().hex
    key = b"w" * 32
    await _clear_resume_jobs(factory)
    await _seed_actor(
        factory,
        tenant_id=tenant_id,
        user_id=user_id,
        label="wall-clock",
    )
    service = _service(factory, key=key)
    receipt = await service.pause(
        _request(tenant_id=tenant_id, user_id=user_id),
    )
    await service.submit_response(
        principal=_principal(tenant_id=tenant_id, user_id=user_id),
        interaction_id=receipt.interaction_id,
        revision=receipt.revision,
        response_idempotency_key="wall-clock-response",
        input_responses={
            "leave-form": {
                "action": "accept",
                "content": {"start": "2026-09-05"},
            }
        },
    )
    async with factory.begin() as session:
        leases = await _repository(session, key=key).lease_ready(
            owner="worker-before-deadline",
            lease_seconds=1,
            limit=1,
        )
    assert len(leases) == 1
    lease = leases[0]

    lock_holder = factory()
    await lock_holder.begin()
    try:
        await lock_holder.scalar(sa.select(McpInteraction).where(McpInteraction.id == receipt.interaction_id).with_for_update())
        started = asyncio.Event()

        async def complete_after_lock_wait() -> None:
            async with factory.begin() as session:
                started.set()
                await _repository(session, key=key).complete(
                    lease=lease,
                    result={"status": "prepared"},
                )

        contender = asyncio.create_task(complete_after_lock_wait())
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0.1)
        assert not contender.done()
        await asyncio.sleep(1.1)
        await lock_holder.commit()

        with pytest.raises(InteractionStateError) as rejected:
            await asyncio.wait_for(contender, timeout=2)
        assert rejected.value.code is InteractionErrorCode.STATE_CONFLICT
    finally:
        if lock_holder.in_transaction():
            await lock_holder.rollback()
        await lock_holder.close()
