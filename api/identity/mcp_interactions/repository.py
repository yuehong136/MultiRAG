"""Async PostgreSQL repository for durable MCP interaction rounds."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import McpInteraction, McpInteractionResumeJob
from api.identity.mcp_interactions.contracts import (
    InteractionErrorCode,
    InteractionLease,
    InteractionProjection,
    InteractionStateError,
    ResponseClaim,
    ResponseClaimStatus,
)
from api.identity.mcp_interactions.crypto import (
    EncryptedInteractionPayload,
    InteractionPayloadCipher,
)
from api.identity.mcp_interactions.validation import (
    interaction_schema_digest,
    normalize_input_requests,
    normalize_input_responses,
    normalize_interaction_result,
    normalize_output_schema,
    output_schema_digest,
)
from api.identity.principal import Principal
from common.mcp_interactions import (
    InteractionEffect,
    InteractionReceipt,
    InteractionRequest,
    InteractionResume,
)


def _idempotency_digest(
    *,
    tenant_id: str,
    interaction_id: str,
    revision: int,
    key: str,
) -> str:
    if not key.strip() or len(key) > 255:
        raise InteractionStateError(InteractionErrorCode.RESPONSE_INVALID)
    material = f"{tenant_id}\x00{interaction_id}\x00{revision}\x00{key}"
    return hashlib.sha256(("multirag.mcp-interaction.response.v1\x00" + material).encode()).hexdigest()


class InteractionRepository:
    """One transaction-scoped repository; callers own commit/rollback."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        cipher: InteractionPayloadCipher,
        max_rounds: int,
        max_payload_bytes: int,
    ) -> None:
        self._session = session
        self._cipher = cipher
        self._max_rounds = max_rounds
        self._max_payload_bytes = max_payload_bytes

    async def pause(self, request: InteractionRequest) -> InteractionReceipt:
        input_requests = normalize_input_requests(
            request.input_requests,
            max_payload_bytes=self._max_payload_bytes,
        )
        output_schema = normalize_output_schema(
            request.output_schema,
            max_payload_bytes=self._max_payload_bytes,
        )
        if request.interaction_id is None:
            return await self._create(
                request,
                input_requests=input_requests,
                output_schema=output_schema,
            )
        return await self._advance(
            request,
            input_requests=input_requests,
            output_schema=output_schema,
        )

    async def _create(
        self,
        request: InteractionRequest,
        *,
        input_requests: Mapping[str, Any],
        output_schema: Mapping[str, Any] | None,
    ) -> InteractionReceipt:
        interaction_id = uuid.uuid4().hex
        revision = 1
        original = self._encrypt(
            request,
            interaction_id=interaction_id,
            revision=revision,
            purpose="original_arguments",
            value=dict(request.original_arguments),
        )
        inputs = self._encrypt(
            request,
            interaction_id=interaction_id,
            revision=revision,
            purpose="input_requests",
            value=dict(input_requests),
        )
        state = self._encrypt(
            request,
            interaction_id=interaction_id,
            revision=revision,
            purpose="request_state",
            value=request.request_state,
        )
        encrypted_output_schema = self._encrypt_output_schema(
            request,
            interaction_id=interaction_id,
            revision=revision,
            output_schema=output_schema,
        )
        now = datetime.now(UTC)
        self._session.add(
            McpInteraction(
                id=interaction_id,
                tenant_id=request.tenant_id,
                platform_user_id=request.platform_user_id,
                external_identity_id=request.external_identity_id,
                identity_revision=request.identity_revision,
                agent_id=request.agent_id,
                agent_revision_id=request.agent_revision_id,
                mcp_server_id=request.mcp_server_id,
                resource_name=request.resource_name,
                resource_uri=request.resource_uri,
                tool_name=request.tool_name,
                call_digest=request.call_digest,
                schema_digest=interaction_schema_digest(input_requests),
                output_schema_digest=output_schema_digest(output_schema),
                output_schema_ciphertext=(encrypted_output_schema.ciphertext if encrypted_output_schema is not None else None),
                output_schema_key_id=(encrypted_output_schema.key_id if encrypted_output_schema is not None else None),
                original_arguments_ciphertext=original.ciphertext,
                original_arguments_key_id=original.key_id,
                input_requests_ciphertext=inputs.ciphertext,
                input_requests_key_id=inputs.key_id,
                request_state_ciphertext=state.ciphertext,
                request_state_key_id=state.key_id,
                policy_revision=request.policy_revision,
                credential_generation=request.credential_generation,
                effect=request.effect.value,
                replay_mode=request.replay_mode,
                state="awaiting_input",
                revision=revision,
                round_count=1,
                expires_at=request.expires_at,
                created_at=now,
                updated_at=now,
            )
        )
        await self._session.flush()
        return InteractionReceipt(interaction_id=interaction_id, revision=revision)

    async def _advance(
        self,
        request: InteractionRequest,
        *,
        input_requests: Mapping[str, Any],
        output_schema: Mapping[str, Any] | None,
    ) -> InteractionReceipt:
        assert request.interaction_id is not None
        assert request.previous_revision is not None
        assert request.expires_at is not None
        row = await self._locked_interaction(
            interaction_id=request.interaction_id,
            tenant_id=request.tenant_id,
        )
        if row is None:
            raise InteractionStateError(InteractionErrorCode.NOT_FOUND)
        if row.state != "resuming" or row.revision != request.previous_revision:
            raise InteractionStateError(InteractionErrorCode.STATE_CONFLICT)
        if row.round_count >= self._max_rounds:
            raise InteractionStateError(InteractionErrorCode.ROUND_LIMIT)
        if not self._same_binding(
            row,
            request,
            output_schema=output_schema,
        ):
            raise InteractionStateError(InteractionErrorCode.STATE_CONFLICT)
        job = await self._session.scalar(
            sa.select(McpInteractionResumeJob)
            .where(
                McpInteractionResumeJob.interaction_id == row.id,
                McpInteractionResumeJob.revision == row.revision,
                McpInteractionResumeJob.state == "leased",
            )
            .with_for_update()
        )
        if job is None:
            raise InteractionStateError(InteractionErrorCode.STATE_CONFLICT)
        revision = row.revision + 1
        original = self._encrypt(
            request,
            interaction_id=row.id,
            revision=revision,
            purpose="original_arguments",
            value=dict(request.original_arguments),
        )
        inputs = self._encrypt(
            request,
            interaction_id=row.id,
            revision=revision,
            purpose="input_requests",
            value=dict(input_requests),
        )
        state = self._encrypt(
            request,
            interaction_id=row.id,
            revision=revision,
            purpose="request_state",
            value=request.request_state,
        )
        encrypted_output_schema = self._encrypt_output_schema(
            request,
            interaction_id=row.id,
            revision=revision,
            output_schema=output_schema,
        )
        now = datetime.now(UTC)
        job.state = "succeeded"
        job.lease_owner = None
        job.lease_until = None
        job.updated_at = now
        row.original_arguments_ciphertext = original.ciphertext
        row.original_arguments_key_id = original.key_id
        row.input_requests_ciphertext = inputs.ciphertext
        row.input_requests_key_id = inputs.key_id
        row.request_state_ciphertext = state.ciphertext
        row.request_state_key_id = state.key_id
        row.schema_digest = interaction_schema_digest(input_requests)
        row.output_schema_digest = output_schema_digest(output_schema)
        row.output_schema_ciphertext = encrypted_output_schema.ciphertext if encrypted_output_schema is not None else None
        row.output_schema_key_id = encrypted_output_schema.key_id if encrypted_output_schema is not None else None
        row.policy_revision = request.policy_revision
        row.credential_generation = request.credential_generation
        row.state = "awaiting_input"
        row.revision = revision
        row.round_count += 1
        row.expires_at = request.expires_at
        row.updated_at = now
        await self._session.flush()
        return InteractionReceipt(interaction_id=row.id, revision=revision)

    async def submit_response(
        self,
        *,
        principal: Principal,
        interaction_id: str,
        revision: int,
        response_idempotency_key: str,
        input_responses: Mapping[str, Any],
    ) -> ResponseClaim:
        digest = _idempotency_digest(
            tenant_id=principal.tenant_id,
            interaction_id=interaction_id,
            revision=revision,
            key=response_idempotency_key,
        )
        row = await self._locked_interaction(
            interaction_id=interaction_id,
            tenant_id=principal.tenant_id,
        )
        if row is None:
            raise InteractionStateError(InteractionErrorCode.NOT_FOUND)
        if row.platform_user_id != principal.platform_user_id or row.external_identity_id != principal.authentication.external_identity_id:
            raise InteractionStateError(InteractionErrorCode.ACTOR_MISMATCH)
        if row.revision != revision:
            raise InteractionStateError(InteractionErrorCode.REVISION_CONFLICT)
        existing = await self._session.scalar(
            sa.select(McpInteractionResumeJob).where(
                McpInteractionResumeJob.interaction_id == row.id,
                McpInteractionResumeJob.revision == revision,
            )
        )
        if existing is not None:
            if existing.response_idempotency_key == digest:
                return ResponseClaim(
                    interaction_id=row.id,
                    revision=revision,
                    status=ResponseClaimStatus.DUPLICATE,
                )
            raise InteractionStateError(InteractionErrorCode.STATE_CONFLICT)
        now = await self._db_now()
        if row.expires_at <= now:
            row.state = "expired"
            row.updated_at = now
            raise InteractionStateError(InteractionErrorCode.EXPIRED)
        if row.state != "awaiting_input":
            raise InteractionStateError(InteractionErrorCode.STATE_CONFLICT)
        input_requests = self._decrypt_mapping(
            row,
            purpose="input_requests",
            ciphertext=row.input_requests_ciphertext,
            key_id=row.input_requests_key_id,
        )
        if interaction_schema_digest(input_requests) != row.schema_digest:
            raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
        responses = normalize_input_responses(
            input_requests=input_requests,
            input_responses=input_responses,
            max_payload_bytes=self._max_payload_bytes,
        )
        encrypted = self._cipher.encrypt(
            tenant_id=row.tenant_id,
            interaction_id=row.id,
            revision=row.revision,
            purpose="input_response",
            value=responses,
        )
        terminal_state = self._response_terminal_state(responses)
        self._session.add(
            McpInteractionResumeJob(
                id=uuid.uuid4().hex,
                interaction_id=row.id,
                tenant_id=row.tenant_id,
                revision=row.revision,
                response_idempotency_key=digest,
                input_response_ciphertext=encrypted.ciphertext,
                input_response_key_id=encrypted.key_id,
                state="succeeded" if terminal_state is not None else "response_ready",
                attempt=0,
                created_at=now,
                updated_at=now,
            )
        )
        row.state = terminal_state or "response_ready"
        row.updated_at = now
        await self._session.flush()
        return ResponseClaim(
            interaction_id=row.id,
            revision=revision,
            status=ResponseClaimStatus.ACCEPTED,
        )

    async def projection(
        self,
        *,
        tenant_id: str,
        platform_user_id: str,
        interaction_id: str,
    ) -> InteractionProjection:
        row = await self._session.scalar(
            sa.select(McpInteraction).where(
                McpInteraction.id == interaction_id,
                McpInteraction.tenant_id == tenant_id,
                McpInteraction.platform_user_id == platform_user_id,
            )
        )
        if row is None:
            raise InteractionStateError(InteractionErrorCode.NOT_FOUND)
        input_requests = self._decrypt_mapping(
            row,
            purpose="input_requests",
            ciphertext=row.input_requests_ciphertext,
            key_id=row.input_requests_key_id,
        )
        if interaction_schema_digest(input_requests) != row.schema_digest:
            raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
        return InteractionProjection(
            interaction_id=row.id,
            revision=row.revision,
            state=row.state,
            input_requests=input_requests,
            expires_at=row.expires_at,
        )

    async def lease_ready(
        self,
        *,
        owner: str,
        lease_seconds: int,
        limit: int,
    ) -> list[InteractionLease]:
        if not owner.strip() or len(owner) > 64:
            raise ValueError("interaction lease owner is invalid")
        now = await self._db_now()
        jobs = list(
            await self._session.scalars(
                sa.select(McpInteractionResumeJob)
                .where(
                    sa.or_(
                        sa.and_(
                            McpInteractionResumeJob.state == "response_ready",
                            sa.or_(
                                McpInteractionResumeJob.next_attempt_at.is_(None),
                                McpInteractionResumeJob.next_attempt_at <= now,
                            ),
                        ),
                        sa.and_(
                            McpInteractionResumeJob.state == "leased",
                            McpInteractionResumeJob.lease_until <= now,
                        ),
                    )
                )
                .order_by(McpInteractionResumeJob.created_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        leases: list[InteractionLease] = []
        for job in jobs:
            row = await self._locked_interaction(
                interaction_id=job.interaction_id,
                tenant_id=job.tenant_id,
            )
            if row is None or row.revision != job.revision:
                self._terminal_job(job, now=now, code=InteractionErrorCode.STATE_CONFLICT)
                continue
            if row.expires_at <= now:
                row.state = "expired"
                row.updated_at = now
                self._terminal_job(job, now=now, code=InteractionErrorCode.EXPIRED)
                continue
            if row.effect == InteractionEffect.SIDE_EFFECT.value:
                row.state = "failed"
                row.updated_at = now
                self._terminal_job(job, now=now, code=InteractionErrorCode.SIDE_EFFECT_BLOCKED)
                continue
            if job.state == "leased" and row.replay_mode != "reusable":
                row.state = "failed"
                row.updated_at = now
                self._terminal_job(job, now=now, code=InteractionErrorCode.REMOTE_RESULT_UNKNOWN)
                continue
            job.state = "leased"
            job.lease_owner = owner
            job.lease_until = now + timedelta(seconds=lease_seconds)
            job.attempt += 1
            job.updated_at = now
            row.state = "resuming"
            row.updated_at = now
            leases.append(
                InteractionLease(
                    job_id=job.id,
                    owner=owner,
                    resume=self._to_resume(row=row, job=job),
                )
            )
        await self._session.flush()
        return leases

    async def complete(
        self,
        *,
        lease: InteractionLease,
        result: object,
    ) -> None:
        row, job = await self._locked_lease(lease)
        output_schema = self._decrypt_output_schema(row)
        normalized_result = normalize_interaction_result(
            result,
            output_schema=output_schema,
            max_payload_bytes=self._max_payload_bytes,
        )
        encrypted = self._cipher.encrypt(
            tenant_id=row.tenant_id,
            interaction_id=row.id,
            revision=row.revision,
            purpose="result",
            value=normalized_result,
        )
        now = datetime.now(UTC)
        row.result_ciphertext = encrypted.ciphertext
        row.result_key_id = encrypted.key_id
        row.state = "completed"
        row.updated_at = now
        job.state = "succeeded"
        job.lease_owner = None
        job.lease_until = None
        job.updated_at = now
        await self._session.flush()

    async def terminal_fail(
        self,
        *,
        lease: InteractionLease,
        code: InteractionErrorCode,
    ) -> None:
        row, job = await self._locked_lease(lease)
        now = datetime.now(UTC)
        row.state = "failed"
        row.updated_at = now
        self._terminal_job(job, now=now, code=code)
        await self._session.flush()

    async def retry(
        self,
        *,
        lease: InteractionLease,
        delay_seconds: int,
        code: InteractionErrorCode,
    ) -> None:
        row, job = await self._locked_lease(lease)
        if row.effect not in {InteractionEffect.READ.value, InteractionEffect.PREPARE.value} or row.replay_mode != "reusable":
            await self.terminal_fail(lease=lease, code=InteractionErrorCode.REMOTE_RESULT_UNKNOWN)
            return
        now = datetime.now(UTC)
        row.state = "response_ready"
        row.updated_at = now
        job.state = "response_ready"
        job.lease_owner = None
        job.lease_until = None
        job.next_attempt_at = now + timedelta(seconds=delay_seconds)
        job.safe_error_code = code.value
        job.updated_at = now
        await self._session.flush()

    async def _locked_lease(
        self,
        lease: InteractionLease,
    ) -> tuple[McpInteraction, McpInteractionResumeJob]:
        job = await self._session.scalar(sa.select(McpInteractionResumeJob).where(McpInteractionResumeJob.id == lease.job_id).with_for_update())
        if job is None or job.state != "leased" or job.lease_owner != lease.owner:
            raise InteractionStateError(InteractionErrorCode.STATE_CONFLICT)
        row = await self._locked_interaction(
            interaction_id=job.interaction_id,
            tenant_id=job.tenant_id,
        )
        if row is None or row.state != "resuming" or row.revision != job.revision:
            raise InteractionStateError(InteractionErrorCode.STATE_CONFLICT)
        return row, job

    def _to_resume(
        self,
        *,
        row: McpInteraction,
        job: McpInteractionResumeJob,
    ) -> InteractionResume:
        original = self._decrypt_mapping(
            row,
            purpose="original_arguments",
            ciphertext=row.original_arguments_ciphertext,
            key_id=row.original_arguments_key_id,
        )
        input_requests = self._decrypt_mapping(
            row,
            purpose="input_requests",
            ciphertext=row.input_requests_ciphertext,
            key_id=row.input_requests_key_id,
        )
        if interaction_schema_digest(input_requests) != row.schema_digest:
            raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
        request_state = self._cipher.decrypt(
            tenant_id=row.tenant_id,
            interaction_id=row.id,
            revision=row.revision,
            purpose="request_state",
            encrypted=EncryptedInteractionPayload(
                ciphertext=row.request_state_ciphertext,
                key_id=row.request_state_key_id,
            ),
        )
        responses = self._decrypt_mapping(
            row,
            purpose="input_response",
            ciphertext=job.input_response_ciphertext,
            key_id=job.input_response_key_id,
        )
        if not isinstance(request_state, str):
            raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
        output_schema = self._decrypt_output_schema(row)
        request = InteractionRequest(
            tenant_id=row.tenant_id,
            platform_user_id=row.platform_user_id,
            external_identity_id=row.external_identity_id,
            identity_revision=row.identity_revision,
            agent_id=row.agent_id,
            agent_revision_id=row.agent_revision_id,
            mcp_server_id=row.mcp_server_id,
            resource_name=row.resource_name,
            resource_uri=row.resource_uri,
            tool_name=row.tool_name,
            original_arguments=original,
            input_requests=input_requests,
            output_schema=output_schema,
            request_state=request_state,
            effect=InteractionEffect(row.effect),
            replay_mode=row.replay_mode,
            policy_revision=row.policy_revision,
            credential_generation=row.credential_generation,
            expires_at=row.expires_at,
            interaction_id=row.id,
            previous_revision=row.revision,
        )
        if request.call_digest != row.call_digest:
            raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
        return InteractionResume(
            interaction_id=row.id,
            revision=row.revision,
            request=request,
            input_responses=responses,
        )

    async def _locked_interaction(
        self,
        *,
        interaction_id: str,
        tenant_id: str,
    ) -> McpInteraction | None:
        return await self._session.scalar(
            sa.select(McpInteraction)
            .where(
                McpInteraction.id == interaction_id,
                McpInteraction.tenant_id == tenant_id,
            )
            .with_for_update()
        )

    async def _db_now(self) -> datetime:
        value = await self._session.scalar(sa.select(sa.func.now()))
        if not isinstance(value, datetime):
            raise RuntimeError("database clock is unavailable")
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _decrypt_mapping(
        self,
        row: McpInteraction,
        *,
        purpose: str,
        ciphertext: str,
        key_id: str,
    ) -> dict[str, Any]:
        value = self._cipher.decrypt(
            tenant_id=row.tenant_id,
            interaction_id=row.id,
            revision=row.revision,
            purpose=purpose,
            encrypted=EncryptedInteractionPayload(
                ciphertext=ciphertext,
                key_id=key_id,
            ),
        )
        if not isinstance(value, dict):
            raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
        return value

    def _encrypt(
        self,
        request: InteractionRequest,
        *,
        interaction_id: str,
        revision: int,
        purpose: str,
        value: object,
    ) -> EncryptedInteractionPayload:
        return self._cipher.encrypt(
            tenant_id=request.tenant_id,
            interaction_id=interaction_id,
            revision=revision,
            purpose=purpose,
            value=value,
        )

    def _encrypt_output_schema(
        self,
        request: InteractionRequest,
        *,
        interaction_id: str,
        revision: int,
        output_schema: Mapping[str, Any] | None,
    ) -> EncryptedInteractionPayload | None:
        if output_schema is None:
            return None
        return self._encrypt(
            request,
            interaction_id=interaction_id,
            revision=revision,
            purpose="output_schema",
            value=dict(output_schema),
        )

    def _decrypt_output_schema(
        self,
        row: McpInteraction,
    ) -> dict[str, Any] | None:
        if row.output_schema_digest is None and row.output_schema_ciphertext is None and row.output_schema_key_id is None:
            return None
        if row.output_schema_digest is None or row.output_schema_ciphertext is None or row.output_schema_key_id is None:
            raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
        output_schema = self._decrypt_mapping(
            row,
            purpose="output_schema",
            ciphertext=row.output_schema_ciphertext,
            key_id=row.output_schema_key_id,
        )
        if output_schema_digest(output_schema) != row.output_schema_digest:
            raise InteractionStateError(InteractionErrorCode.PAYLOAD_INVALID)
        return output_schema

    @staticmethod
    def _same_binding(
        row: McpInteraction,
        request: InteractionRequest,
        *,
        output_schema: Mapping[str, Any] | None,
    ) -> bool:
        return (
            row.platform_user_id == request.platform_user_id
            and row.external_identity_id == request.external_identity_id
            and row.agent_id == request.agent_id
            and row.agent_revision_id == request.agent_revision_id
            and row.mcp_server_id == request.mcp_server_id
            and row.resource_name == request.resource_name
            and row.resource_uri == request.resource_uri
            and row.tool_name == request.tool_name
            and row.call_digest == request.call_digest
            and row.output_schema_digest == output_schema_digest(output_schema)
            and row.effect == request.effect.value
            and row.replay_mode == request.replay_mode
        )

    @staticmethod
    def _response_terminal_state(responses: Mapping[str, Any]) -> str | None:
        actions = {value.get("action") for value in responses.values() if isinstance(value, dict)}
        if "cancel" in actions:
            return "cancelled"
        if "decline" in actions:
            return "declined"
        return None

    @staticmethod
    def _terminal_job(
        job: McpInteractionResumeJob,
        *,
        now: datetime,
        code: InteractionErrorCode,
    ) -> None:
        job.state = "terminal_failed"
        job.lease_owner = None
        job.lease_until = None
        job.safe_error_code = code.value
        job.updated_at = now


__all__ = ["InteractionRepository"]
