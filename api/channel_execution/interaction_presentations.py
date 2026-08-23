"""Durable EIM-U15/CHN-X15 presentation, callback, and delivery state."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Literal

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.channel_execution.interaction_forms import build_form_projection, decode_form_submission
from api.channel_execution.models import ChannelActor, TrustedChannelContext
from api.db.db_models import (
    ChannelBinding,
    ChatChannel,
    IdentityProviderChannelLink,
    McpInteraction,
    McpInteractionCallbackReceipt,
    McpInteractionPresentation,
)
from api.identity.mcp_interactions.contracts import (
    InteractionErrorCode,
    InteractionStateError,
)
from api.identity.mcp_interactions.crypto import (
    EncryptedInteractionPayload,
    InteractionPayloadCipher,
    InteractionPayloadCipherError,
)
from api.identity.mcp_interactions.validation import interaction_schema_digest

_SAFE_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_FORM_FIELD_ID = re.compile(r"^f_[0-9a-f]{24}$")
_TERMINAL_STATES = frozenset({"completed", "declined", "cancelled", "expired", "failed"})
_MAX_CALLBACK_BYTES = 65_536
_MAX_FORM_TEXT_CHARS = 1_000
_MAX_FORM_SELECTIONS = 20


class InteractionPresentationErrorCode(StrEnum):
    NOT_FOUND = "INTERACTION_PRESENTATION_NOT_FOUND"
    BINDING_MISMATCH = "INTERACTION_PRESENTATION_BINDING_MISMATCH"
    STATE_CONFLICT = "INTERACTION_PRESENTATION_STATE_CONFLICT"
    EXPIRED = "INTERACTION_PRESENTATION_EXPIRED"
    PAYLOAD_INVALID = "INTERACTION_PRESENTATION_PAYLOAD_INVALID"
    DELIVERY_CONFLICT = "INTERACTION_PRESENTATION_DELIVERY_CONFLICT"


class InteractionPresentationError(RuntimeError):
    def __init__(self, code: InteractionPresentationErrorCode) -> None:
        self.code = code
        super().__init__("Channel interaction presentation was rejected")


class InteractionRequiredNotice(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action_id: str = Field(min_length=1, max_length=32, repr=False)
    revision: int = Field(gt=0)
    expires_at: datetime


class InteractionCallbackPayload(BaseModel):
    """Bounded untrusted Provider callback persisted before Principal resolution."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    event_id: str = Field(min_length=1, max_length=255, repr=False)
    nonce: str = Field(min_length=16, max_length=255, repr=False)
    action: Literal["accept", "decline", "cancel"] = "accept"
    message_id: str = Field(min_length=1, max_length=255, repr=False)
    actor: ChannelActor = Field(repr=False)
    form_value: dict[str, Any] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def validate_bounded_payload(self) -> InteractionCallbackPayload:
        if self.action != "accept" and self.form_value:
            raise ValueError("non-accept callback cannot carry form values")
        for key, value in self.form_value.items():
            if not isinstance(key, str) or _FORM_FIELD_ID.fullmatch(key) is None:
                raise ValueError("form field names are invalid")
            if isinstance(value, str):
                if len(value) > _MAX_FORM_TEXT_CHARS:
                    raise ValueError("form field value is invalid")
            elif type(value) is bool:
                pass
            elif isinstance(value, list):
                if len(value) > _MAX_FORM_SELECTIONS or len(value) != len(set(value)) or any(not isinstance(item, str) or not item or len(item) > 64 for item in value):
                    raise ValueError("form field value is invalid")
            else:
                raise ValueError("form field value is invalid")
        try:
            encoded = json.dumps(
                self.model_dump(mode="json"),
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
        except (TypeError, ValueError) as exc:
            raise ValueError("callback payload is invalid") from exc
        if len(encoded) > _MAX_CALLBACK_BYTES:
            raise ValueError("callback payload is too large")
        return self


class CallbackReceiptResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["accepted", "duplicate"]


class InteractionDelivery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    delivery_id: str = Field(min_length=1, max_length=32, repr=False)
    delivery_token: str = Field(min_length=16, max_length=255, repr=False)
    action_id: str = Field(min_length=1, max_length=32, repr=False)
    revision: int = Field(gt=0)
    kind: Literal["form", "terminal"]
    presentation_ref: str = Field(min_length=1, max_length=255, repr=False)
    projection: dict[str, Any]
    action_nonce: str | None = Field(default=None, min_length=16, max_length=255, repr=False)
    expires_at: datetime
    safe_error_code: str | None = Field(default=None, pattern=r"^[A-Z][A-Z0-9_]{0,63}$")


@dataclass(frozen=True, slots=True)
class CallbackLease:
    receipt_id: str = field(repr=False)
    owner: str = field(repr=False)
    attempt: int
    presentation_id: str = field(repr=False)
    binding_id: str = field(repr=False)
    binding_generation: int
    provider_account_id: str = field(repr=False)
    interaction_id: str = field(repr=False)
    revision: int
    actor: ChannelActor = field(repr=False)
    action: Literal["accept", "decline", "cancel"]
    input_responses: Mapping[str, Any] = field(repr=False)
    idempotency_key: str = field(repr=False)


def _digest(domain: str, *values: object) -> str:
    material = json.dumps(values, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256((f"multirag.channel-interaction.{domain}.v1\x00" + material).encode()).hexdigest()


def _token_digest(purpose: str, value: str) -> str:
    return _digest(purpose, value)


def _terminal_projection(state: str) -> dict[str, str]:
    messages = {
        "completed": "处理已完成。",
        "declined": "你已拒绝本次请求。",
        "cancelled": "本次请求已取消。",
        "expired": "表单已过期，请重新发起。",
        "failed": "处理未完成，请稍后重试。",
    }
    return {"state": state, "message": messages.get(state, "处理状态已更新。")}


class InteractionPresentationRepository:
    """One short transaction over Channel presentation rows."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        cipher: InteractionPayloadCipher,
    ) -> None:
        self._session = session
        self._cipher = cipher

    async def register(
        self,
        *,
        context: TrustedChannelContext,
        interaction_id: str,
        revision: int,
        source_event_id: str,
        conversation_ref: str,
        presentation_ref: str,
    ) -> InteractionRequiredNotice:
        principal = context.principal
        if principal is None or context.provider != "feishu" or not source_event_id.strip() or not conversation_ref.strip() or not presentation_ref.strip():
            raise InteractionPresentationError(InteractionPresentationErrorCode.BINDING_MISMATCH)
        if len(source_event_id) > 255 or len(conversation_ref) > 255 or len(presentation_ref) > 255:
            raise InteractionPresentationError(InteractionPresentationErrorCode.PAYLOAD_INVALID)
        row = await self._session.scalar(
            sa.select(McpInteraction)
            .where(
                McpInteraction.id == interaction_id,
                McpInteraction.tenant_id == context.tenant_id,
            )
            .with_for_update()
        )
        if row is None:
            raise InteractionPresentationError(InteractionPresentationErrorCode.NOT_FOUND)
        if row.revision != revision or row.state != "awaiting_input" or row.platform_user_id != principal.platform_user_id or row.external_identity_id != principal.authentication.external_identity_id:
            raise InteractionPresentationError(InteractionPresentationErrorCode.STATE_CONFLICT)
        now = await self._db_now()
        if row.expires_at <= now:
            row.state = "expired"
            row.updated_at = now
            raise InteractionPresentationError(InteractionPresentationErrorCode.EXPIRED)
        provider_account_id = await self._linked_provider_account(context)
        source_digest = _digest("source-event", context.binding_id, source_event_id)
        existing = await self._session.scalar(
            sa.select(McpInteractionPresentation).where(
                McpInteractionPresentation.interaction_id == interaction_id,
                McpInteractionPresentation.revision == revision,
            )
        )
        if existing is not None:
            if (
                existing.binding_id != context.binding_id
                or existing.binding_generation != context.binding_generation
                or existing.provider != context.provider
                or existing.provider_account_id != provider_account_id
                or existing.source_event_digest != source_digest
                or existing.conversation_ref != conversation_ref
                or existing.presentation_ref != presentation_ref
            ):
                raise InteractionPresentationError(InteractionPresentationErrorCode.STATE_CONFLICT)
            return InteractionRequiredNotice(
                action_id=existing.interaction_id,
                revision=existing.revision,
                expires_at=existing.expires_at,
            )
        public, encrypted_mapping = self._form_payload(row)
        self._session.add(
            McpInteractionPresentation(
                id=uuid.uuid4().hex,
                interaction_id=row.id,
                tenant_id=row.tenant_id,
                revision=row.revision,
                binding_id=context.binding_id,
                binding_generation=context.binding_generation,
                provider=context.provider,
                provider_account_id=provider_account_id,
                source_event_digest=source_digest,
                conversation_ref=conversation_ref,
                presentation_ref=presentation_ref,
                expires_at=row.expires_at,
                response_state="open",
                delivery_kind="form",
                delivery_state="pending",
                delivery_projection=public,
                form_mapping_ciphertext=encrypted_mapping.ciphertext,
                form_mapping_key_id=encrypted_mapping.key_id,
                delivery_attempt=0,
                created_at=now,
                updated_at=now,
            )
        )
        if row.provider not in {None, context.provider} or row.presentation_ref not in {None, presentation_ref}:
            raise InteractionPresentationError(InteractionPresentationErrorCode.STATE_CONFLICT)
        row.provider = context.provider
        row.presentation_ref = presentation_ref
        row.updated_at = now
        await self._session.flush()
        return InteractionRequiredNotice(action_id=row.id, revision=row.revision, expires_at=row.expires_at)

    async def claim_delivery(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        owner: str,
        lease_seconds: int,
        action_id: str | None = None,
        revision: int | None = None,
    ) -> InteractionDelivery | None:
        if not owner.strip() or len(owner) > 64 or binding_generation < 1 or lease_seconds < 1:
            raise ValueError("interaction delivery lease is invalid")
        candidate_now = await self._db_now()
        filters: list[Any] = [
            McpInteractionPresentation.binding_id == binding_id,
            McpInteractionPresentation.binding_generation == binding_generation,
            sa.or_(
                sa.and_(
                    McpInteractionPresentation.delivery_state == "pending",
                    sa.or_(
                        McpInteractionPresentation.delivery_next_attempt_at.is_(None),
                        McpInteractionPresentation.delivery_next_attempt_at <= candidate_now,
                    ),
                ),
                sa.and_(
                    McpInteractionPresentation.delivery_state == "leased",
                    McpInteractionPresentation.delivery_lease_until <= candidate_now,
                ),
            ),
        ]
        if action_id is not None:
            filters.append(McpInteractionPresentation.interaction_id == action_id)
        if revision is not None:
            filters.append(McpInteractionPresentation.revision == revision)
        row = await self._session.scalar(sa.select(McpInteractionPresentation).where(*filters).order_by(McpInteractionPresentation.created_at).limit(1).with_for_update(skip_locked=True))
        if row is None:
            return None
        interaction = await self._session.scalar(
            sa.select(McpInteraction)
            .where(
                McpInteraction.id == row.interaction_id,
                McpInteraction.tenant_id == row.tenant_id,
            )
            .with_for_update()
        )
        # The interaction row may have remained locked after this delivery was
        # selected. Base expiry and the new lease on the post-lock wall clock.
        now = await self._db_now()
        if interaction is None:
            row.delivery_state = "failed"
            row.safe_error_code = InteractionPresentationErrorCode.NOT_FOUND.value
            row.updated_at = now
            await self._session.flush()
            return None
        if row.delivery_kind == "form" and row.expires_at <= now:
            interaction.state = "expired"
            interaction.updated_at = now
            self._terminalize(row, state="expired", now=now)
        delivery_token = secrets.token_urlsafe(24)
        action_nonce: str | None = None
        if row.delivery_kind == "form":
            if row.response_state != "open":
                raise InteractionPresentationError(InteractionPresentationErrorCode.STATE_CONFLICT)
            action_nonce = secrets.token_urlsafe(24)
            row.nonce_digest = _token_digest("action-nonce", action_nonce)
        else:
            row.nonce_digest = None
        row.delivery_state = "leased"
        row.delivery_lease_owner = owner
        row.delivery_lease_until = now + timedelta(seconds=lease_seconds)
        row.delivery_token_digest = _token_digest("delivery-token", delivery_token)
        row.delivery_attempt += 1
        row.updated_at = now
        await self._session.flush()
        return InteractionDelivery(
            delivery_id=row.id,
            delivery_token=delivery_token,
            action_id=row.interaction_id,
            revision=row.revision,
            kind=row.delivery_kind,
            presentation_ref=row.presentation_ref,
            projection=dict(row.delivery_projection),
            action_nonce=action_nonce,
            expires_at=row.expires_at,
            safe_error_code=row.safe_error_code,
        )

    async def acknowledge_delivery(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        delivery_id: str,
        owner: str,
        delivery_token: str,
        success: bool,
        safe_error_code: str | None,
    ) -> None:
        if safe_error_code is not None and _SAFE_CODE.fullmatch(safe_error_code) is None:
            raise ValueError("safe delivery error code is invalid")
        row = await self._session.scalar(
            sa.select(McpInteractionPresentation)
            .where(
                McpInteractionPresentation.id == delivery_id,
                McpInteractionPresentation.binding_id == binding_id,
                McpInteractionPresentation.binding_generation == binding_generation,
            )
            .with_for_update()
        )
        now = await self._db_now()
        if (
            row is None
            or row.delivery_state != "leased"
            or row.delivery_lease_owner != owner
            or row.delivery_lease_until is None
            or row.delivery_lease_until <= now
            or row.delivery_token_digest is None
            or not secrets.compare_digest(row.delivery_token_digest, _token_digest("delivery-token", delivery_token))
        ):
            raise InteractionPresentationError(InteractionPresentationErrorCode.DELIVERY_CONFLICT)
        row.delivery_lease_owner = None
        row.delivery_lease_until = None
        row.delivery_token_digest = None
        if success:
            row.delivery_state = "delivered"
            row.delivery_next_attempt_at = None
            row.safe_error_code = None
        elif row.delivery_attempt >= 5:
            row.delivery_state = "failed"
            row.safe_error_code = safe_error_code or "CHANNEL_INTERACTION_DELIVERY_FAILED"
        else:
            row.delivery_state = "pending"
            row.delivery_next_attempt_at = now + timedelta(seconds=min(60, 2**row.delivery_attempt))
            row.safe_error_code = safe_error_code or "CHANNEL_INTERACTION_DELIVERY_RETRY"
        row.updated_at = now
        await self._session.flush()

    async def receive_callback(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        action_id: str,
        revision: int,
        payload: InteractionCallbackPayload,
    ) -> CallbackReceiptResult:
        row = await self._session.scalar(
            sa.select(McpInteractionPresentation)
            .where(
                McpInteractionPresentation.interaction_id == action_id,
                McpInteractionPresentation.revision == revision,
                McpInteractionPresentation.binding_id == binding_id,
                McpInteractionPresentation.binding_generation == binding_generation,
            )
            .with_for_update()
        )
        if row is None:
            raise InteractionPresentationError(InteractionPresentationErrorCode.NOT_FOUND)
        if payload.actor.provider != row.provider or payload.actor.conversation != row.conversation_ref or payload.message_id != row.presentation_ref:
            raise InteractionPresentationError(InteractionPresentationErrorCode.BINDING_MISMATCH)
        event_digest = _digest("callback-event", binding_id, payload.event_id)
        payload_json = payload.model_dump(mode="json")
        payload_digest = _digest("callback-payload", payload_json)
        existing = await self._session.scalar(
            sa.select(McpInteractionCallbackReceipt).where(
                McpInteractionCallbackReceipt.binding_id == binding_id,
                McpInteractionCallbackReceipt.event_digest == event_digest,
            )
        )
        if existing is not None:
            if existing.presentation_id != row.id or existing.payload_digest != payload_digest:
                raise InteractionPresentationError(InteractionPresentationErrorCode.STATE_CONFLICT)
            return CallbackReceiptResult(status="duplicate")
        now = await self._db_now()
        if row.expires_at <= now:
            interaction = await self._session.scalar(
                sa.select(McpInteraction)
                .where(
                    McpInteraction.id == row.interaction_id,
                    McpInteraction.tenant_id == row.tenant_id,
                )
                .with_for_update()
            )
            if interaction is not None:
                interaction.state = "expired"
                interaction.updated_at = now
            self._terminalize(row, state="expired", now=now)
            raise InteractionPresentationError(InteractionPresentationErrorCode.EXPIRED)
        if (
            row.response_state != "open"
            or row.delivery_kind != "form"
            or row.delivery_state != "delivered"
            or row.nonce_digest is None
            or not secrets.compare_digest(row.nonce_digest, _token_digest("action-nonce", payload.nonce))
        ):
            raise InteractionPresentationError(InteractionPresentationErrorCode.STATE_CONFLICT)
        encrypted = self._cipher.encrypt(
            tenant_id=row.tenant_id,
            interaction_id=row.interaction_id,
            revision=row.revision,
            purpose="channel_callback",
            value=payload_json,
        )
        self._session.add(
            McpInteractionCallbackReceipt(
                id=uuid.uuid4().hex,
                presentation_id=row.id,
                binding_id=row.binding_id,
                event_digest=event_digest,
                payload_digest=payload_digest,
                payload_ciphertext=encrypted.ciphertext,
                payload_key_id=encrypted.key_id,
                state="received",
                attempt=0,
                created_at=now,
                updated_at=now,
            )
        )
        row.response_state = "received"
        row.nonce_digest = None
        row.updated_at = now
        await self._session.flush()
        return CallbackReceiptResult(status="accepted")

    async def lease_callback(
        self,
        *,
        owner: str,
        lease_seconds: int,
    ) -> CallbackLease | None:
        if not owner.strip() or len(owner) > 64 or lease_seconds < 1:
            raise ValueError("callback lease is invalid")
        candidate_now = await self._db_now()
        receipt = await self._session.scalar(
            sa.select(McpInteractionCallbackReceipt)
            .where(
                sa.or_(
                    sa.and_(
                        McpInteractionCallbackReceipt.state == "received",
                        sa.or_(
                            McpInteractionCallbackReceipt.next_attempt_at.is_(None),
                            McpInteractionCallbackReceipt.next_attempt_at <= candidate_now,
                        ),
                    ),
                    sa.and_(
                        McpInteractionCallbackReceipt.state == "leased",
                        McpInteractionCallbackReceipt.lease_until <= candidate_now,
                    ),
                )
            )
            .order_by(McpInteractionCallbackReceipt.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if receipt is None:
            return None
        presentation = await self._session.scalar(sa.select(McpInteractionPresentation).where(McpInteractionPresentation.id == receipt.presentation_id).with_for_update())
        # Start the callback lease only after both potentially blocking locks.
        now = await self._db_now()
        if presentation is None or presentation.response_state != "received":
            receipt.state = "rejected"
            receipt.safe_error_code = InteractionPresentationErrorCode.STATE_CONFLICT.value
            receipt.lease_owner = None
            receipt.lease_until = None
            receipt.updated_at = now
            await self._session.flush()
            return None
        try:
            payload = self._cipher.decrypt(
                tenant_id=presentation.tenant_id,
                interaction_id=presentation.interaction_id,
                revision=presentation.revision,
                purpose="channel_callback",
                encrypted=EncryptedInteractionPayload(
                    ciphertext=receipt.payload_ciphertext,
                    key_id=receipt.payload_key_id,
                ),
            )
            mapping = self._decrypt_mapping(presentation)
            callback = InteractionCallbackPayload.model_validate(payload)
            input_responses = decode_form_submission(
                mapping=mapping,
                action=callback.action,
                form_value=callback.form_value,
            )
        except InteractionStateError as exc:
            if exc.code is InteractionErrorCode.RESPONSE_INVALID:
                receipt.state = "rejected"
                receipt.safe_error_code = exc.code.value
                receipt.lease_owner = None
                receipt.lease_until = None
                receipt.next_attempt_at = None
                receipt.updated_at = now
                presentation.response_state = "open"
                presentation.delivery_kind = "form"
                presentation.delivery_state = "pending"
                presentation.delivery_lease_owner = None
                presentation.delivery_lease_until = None
                presentation.delivery_token_digest = None
                presentation.delivery_next_attempt_at = None
                presentation.nonce_digest = None
                presentation.safe_error_code = exc.code.value
                presentation.updated_at = now
                await self._session.flush()
                return None
            receipt.state = "rejected"
            receipt.safe_error_code = InteractionPresentationErrorCode.PAYLOAD_INVALID.value
            receipt.lease_owner = None
            receipt.lease_until = None
            receipt.updated_at = now
            self._terminalize(presentation, state="failed", now=now)
            presentation.safe_error_code = InteractionPresentationErrorCode.PAYLOAD_INVALID.value
            await self._session.flush()
            return None
        except (
            InteractionPayloadCipherError,
            TypeError,
            ValueError,
        ):
            receipt.state = "rejected"
            receipt.safe_error_code = InteractionPresentationErrorCode.PAYLOAD_INVALID.value
            receipt.lease_owner = None
            receipt.lease_until = None
            receipt.updated_at = now
            self._terminalize(presentation, state="failed", now=now)
            presentation.safe_error_code = InteractionPresentationErrorCode.PAYLOAD_INVALID.value
            await self._session.flush()
            return None
        receipt.state = "leased"
        receipt.lease_owner = owner
        receipt.lease_until = now + timedelta(seconds=lease_seconds)
        receipt.attempt += 1
        receipt.updated_at = now
        await self._session.flush()
        return CallbackLease(
            receipt_id=receipt.id,
            owner=owner,
            attempt=receipt.attempt,
            presentation_id=presentation.id,
            binding_id=presentation.binding_id,
            binding_generation=presentation.binding_generation,
            provider_account_id=presentation.provider_account_id,
            interaction_id=presentation.interaction_id,
            revision=presentation.revision,
            actor=callback.actor,
            action=callback.action,
            input_responses=input_responses,
            idempotency_key=receipt.event_digest,
        )

    async def mark_callback_claimed(self, lease: CallbackLease) -> None:
        receipt, presentation, now = await self._locked_callback(lease)
        receipt.state = "claimed"
        receipt.lease_owner = None
        receipt.lease_until = None
        receipt.updated_at = now
        presentation.response_state = "claimed"
        presentation.updated_at = now
        await self._session.flush()

    async def reject_callback(
        self,
        lease: CallbackLease,
        *,
        code: str,
        terminal: bool = False,
    ) -> None:
        if _SAFE_CODE.fullmatch(code) is None:
            raise ValueError("callback error code is invalid")
        receipt, presentation, now = await self._locked_callback(lease)
        receipt.state = "rejected"
        receipt.lease_owner = None
        receipt.lease_until = None
        receipt.safe_error_code = code
        receipt.updated_at = now
        if terminal:
            presentation.response_state = "terminal"
            presentation.delivery_kind = "terminal"
            presentation.delivery_projection = _terminal_projection("failed")
        else:
            presentation.response_state = "open"
            presentation.delivery_kind = "form"
        presentation.delivery_state = "pending"
        presentation.delivery_lease_owner = None
        presentation.delivery_lease_until = None
        presentation.delivery_token_digest = None
        presentation.delivery_next_attempt_at = None
        presentation.nonce_digest = None
        presentation.safe_error_code = code
        presentation.updated_at = now
        await self._session.flush()

    async def retry_callback(
        self,
        lease: CallbackLease,
        *,
        code: str,
        delay_seconds: int,
    ) -> None:
        if _SAFE_CODE.fullmatch(code) is None or delay_seconds < 1:
            raise ValueError("callback retry is invalid")
        receipt, presentation, now = await self._locked_callback(lease)
        receipt.lease_owner = None
        receipt.lease_until = None
        receipt.safe_error_code = code
        receipt.updated_at = now
        if receipt.attempt >= 5:
            receipt.state = "rejected"
            self._terminalize(presentation, state="failed", now=now)
            presentation.safe_error_code = code
        else:
            receipt.state = "received"
            receipt.next_attempt_at = now + timedelta(seconds=delay_seconds)
        await self._session.flush()

    async def renew_callback(
        self,
        lease: CallbackLease,
        *,
        lease_seconds: int,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("callback lease duration is invalid")
        receipt, _presentation, now = await self._locked_callback(lease)
        receipt.lease_until = now + timedelta(seconds=lease_seconds)
        receipt.updated_at = now
        await self._session.flush()

    async def reconcile(self, *, limit: int = 20) -> int:
        now = await self._db_now()
        presentations = list(
            await self._session.scalars(
                sa.select(McpInteractionPresentation)
                .where(McpInteractionPresentation.response_state.in_(("open", "received", "claimed")))
                .order_by(McpInteractionPresentation.updated_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        changed = 0
        for presentation in presentations:
            interaction = await self._session.scalar(
                sa.select(McpInteraction)
                .where(
                    McpInteraction.id == presentation.interaction_id,
                    McpInteraction.tenant_id == presentation.tenant_id,
                )
                .with_for_update()
            )
            if interaction is None:
                self._terminalize(presentation, state="failed", now=now)
                presentation.safe_error_code = InteractionPresentationErrorCode.NOT_FOUND.value
                changed += 1
                continue
            if interaction.state == "awaiting_input" and interaction.expires_at <= now and interaction.revision == presentation.revision:
                interaction.state = "expired"
                interaction.updated_at = now
                self._terminalize(presentation, state="expired", now=now)
                changed += 1
                continue
            if interaction.revision > presentation.revision and interaction.state == "awaiting_input" and presentation.response_state == "claimed":
                existing = await self._session.scalar(
                    sa.select(McpInteractionPresentation).where(
                        McpInteractionPresentation.interaction_id == interaction.id,
                        McpInteractionPresentation.revision == interaction.revision,
                    )
                )
                if existing is None:
                    public, encrypted_mapping = self._form_payload(interaction)
                    self._session.add(
                        McpInteractionPresentation(
                            id=uuid.uuid4().hex,
                            interaction_id=interaction.id,
                            tenant_id=interaction.tenant_id,
                            revision=interaction.revision,
                            binding_id=presentation.binding_id,
                            binding_generation=presentation.binding_generation,
                            provider=presentation.provider,
                            provider_account_id=presentation.provider_account_id,
                            source_event_digest=_digest("round", interaction.id, interaction.revision),
                            conversation_ref=presentation.conversation_ref,
                            presentation_ref=presentation.presentation_ref,
                            expires_at=interaction.expires_at,
                            response_state="open",
                            delivery_kind="form",
                            delivery_state="pending",
                            delivery_projection=public,
                            form_mapping_ciphertext=encrypted_mapping.ciphertext,
                            form_mapping_key_id=encrypted_mapping.key_id,
                            delivery_attempt=0,
                            created_at=now,
                            updated_at=now,
                        )
                    )
                presentation.response_state = "terminal"
                presentation.updated_at = now
                changed += 1
                continue
            if interaction.revision == presentation.revision and interaction.state in _TERMINAL_STATES:
                self._terminalize(presentation, state=interaction.state, now=now)
                changed += 1
        await self._session.flush()
        return changed

    async def _linked_provider_account(self, context: TrustedChannelContext) -> str:
        rows = (
            await self._session.execute(
                sa.select(ChannelBinding, ChatChannel, IdentityProviderChannelLink)
                .join(ChatChannel, ChatChannel.id == ChannelBinding.channel_id)
                .join(IdentityProviderChannelLink, IdentityProviderChannelLink.channel_id == ChatChannel.id)
                .where(ChannelBinding.id == context.binding_id)
                .limit(2)
            )
        ).all()
        if len(rows) != 1:
            raise InteractionPresentationError(InteractionPresentationErrorCode.BINDING_MISMATCH)
        binding, channel, link = rows[0]
        if (
            binding.generation != context.binding_generation
            or not binding.enabled
            or channel.status != 1
            or channel.tenant_id != context.tenant_id
            or channel.channel != context.provider
            or link.tenant_id != context.tenant_id
            or link.provider != context.provider
        ):
            raise InteractionPresentationError(InteractionPresentationErrorCode.BINDING_MISMATCH)
        return str(link.provider_account_id)

    def _form_payload(
        self,
        interaction: McpInteraction,
    ) -> tuple[dict[str, Any], EncryptedInteractionPayload]:
        input_requests = self._cipher.decrypt(
            tenant_id=interaction.tenant_id,
            interaction_id=interaction.id,
            revision=interaction.revision,
            purpose="input_requests",
            encrypted=EncryptedInteractionPayload(
                ciphertext=interaction.input_requests_ciphertext,
                key_id=interaction.input_requests_key_id,
            ),
        )
        if not isinstance(input_requests, dict) or interaction_schema_digest(input_requests) != interaction.schema_digest:
            raise InteractionPresentationError(InteractionPresentationErrorCode.PAYLOAD_INVALID)
        try:
            projection, mapping = build_form_projection(
                interaction_id=interaction.id,
                revision=interaction.revision,
                input_requests=input_requests,
            )
        except InteractionStateError as exc:
            raise InteractionPresentationError(InteractionPresentationErrorCode.PAYLOAD_INVALID) from exc
        encrypted_mapping = self._cipher.encrypt(
            tenant_id=interaction.tenant_id,
            interaction_id=interaction.id,
            revision=interaction.revision,
            purpose="form_mapping",
            value=mapping,
        )
        return projection.model_dump(mode="json", exclude_none=True), encrypted_mapping

    def _decrypt_mapping(self, presentation: McpInteractionPresentation) -> dict[str, Any]:
        if presentation.form_mapping_ciphertext is None or presentation.form_mapping_key_id is None:
            raise InteractionPresentationError(InteractionPresentationErrorCode.PAYLOAD_INVALID)
        mapping = self._cipher.decrypt(
            tenant_id=presentation.tenant_id,
            interaction_id=presentation.interaction_id,
            revision=presentation.revision,
            purpose="form_mapping",
            encrypted=EncryptedInteractionPayload(
                ciphertext=presentation.form_mapping_ciphertext,
                key_id=presentation.form_mapping_key_id,
            ),
        )
        if not isinstance(mapping, dict):
            raise InteractionPresentationError(InteractionPresentationErrorCode.PAYLOAD_INVALID)
        return mapping

    async def _locked_callback(
        self,
        lease: CallbackLease,
    ) -> tuple[
        McpInteractionCallbackReceipt,
        McpInteractionPresentation,
        datetime,
    ]:
        receipt = await self._session.scalar(sa.select(McpInteractionCallbackReceipt).where(McpInteractionCallbackReceipt.id == lease.receipt_id).with_for_update())
        if receipt is None:
            raise InteractionPresentationError(InteractionPresentationErrorCode.STATE_CONFLICT)
        presentation = await self._session.scalar(sa.select(McpInteractionPresentation).where(McpInteractionPresentation.id == lease.presentation_id).with_for_update())
        # Validate only after both locks, using a DB wall clock rather than
        # PostgreSQL's transaction-scoped timestamp.
        now = await self._db_now()
        if (
            receipt.state != "leased"
            or receipt.presentation_id != lease.presentation_id
            or receipt.lease_owner != lease.owner
            or receipt.attempt != lease.attempt
            or receipt.lease_until is None
            or receipt.lease_until <= now
        ):
            raise InteractionPresentationError(InteractionPresentationErrorCode.STATE_CONFLICT)
        if presentation is None or presentation.response_state != "received":
            raise InteractionPresentationError(InteractionPresentationErrorCode.STATE_CONFLICT)
        return receipt, presentation, now

    @staticmethod
    def _terminalize(
        presentation: McpInteractionPresentation,
        *,
        state: str,
        now: datetime,
    ) -> None:
        presentation.response_state = "terminal"
        presentation.delivery_kind = "terminal"
        presentation.delivery_state = "pending"
        presentation.delivery_projection = _terminal_projection(state)
        presentation.delivery_lease_owner = None
        presentation.delivery_lease_until = None
        presentation.delivery_token_digest = None
        presentation.delivery_next_attempt_at = None
        presentation.nonce_digest = None
        presentation.safe_error_code = None if state in {"completed", "declined", "cancelled"} else f"INTERACTION_{state.upper()}"
        presentation.updated_at = now

    async def _db_now(self) -> datetime:
        value = await self._session.scalar(sa.select(sa.func.clock_timestamp()))
        if not isinstance(value, datetime):
            return datetime.now(UTC)
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class InteractionPresentationService:
    """Short-session facade shared by private routes and API-local workers."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: InteractionPayloadCipher,
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher

    async def register(
        self,
        *,
        context: TrustedChannelContext,
        interaction_id: str,
        revision: int,
        source_event_id: str,
        conversation_ref: str,
        presentation_ref: str,
    ) -> InteractionRequiredNotice:
        async with self._session_factory() as session, session.begin():
            return await InteractionPresentationRepository(
                session,
                cipher=self._cipher,
            ).register(
                context=context,
                interaction_id=interaction_id,
                revision=revision,
                source_event_id=source_event_id,
                conversation_ref=conversation_ref,
                presentation_ref=presentation_ref,
            )

    async def claim_delivery(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        owner: str,
        lease_seconds: int,
        action_id: str | None = None,
        revision: int | None = None,
    ) -> InteractionDelivery | None:
        async with self._session_factory() as session, session.begin():
            return await InteractionPresentationRepository(
                session,
                cipher=self._cipher,
            ).claim_delivery(
                binding_id=binding_id,
                binding_generation=binding_generation,
                owner=owner,
                lease_seconds=lease_seconds,
                action_id=action_id,
                revision=revision,
            )

    async def acknowledge_delivery(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        delivery_id: str,
        owner: str,
        delivery_token: str,
        success: bool,
        safe_error_code: str | None,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await InteractionPresentationRepository(
                session,
                cipher=self._cipher,
            ).acknowledge_delivery(
                binding_id=binding_id,
                binding_generation=binding_generation,
                delivery_id=delivery_id,
                owner=owner,
                delivery_token=delivery_token,
                success=success,
                safe_error_code=safe_error_code,
            )

    async def receive_callback(
        self,
        *,
        binding_id: str,
        binding_generation: int,
        action_id: str,
        revision: int,
        payload: InteractionCallbackPayload,
    ) -> CallbackReceiptResult:
        async with self._session_factory() as session, session.begin():
            return await InteractionPresentationRepository(
                session,
                cipher=self._cipher,
            ).receive_callback(
                binding_id=binding_id,
                binding_generation=binding_generation,
                action_id=action_id,
                revision=revision,
                payload=payload,
            )

    async def lease_callback(
        self,
        *,
        owner: str,
        lease_seconds: int,
    ) -> CallbackLease | None:
        async with self._session_factory() as session, session.begin():
            return await InteractionPresentationRepository(
                session,
                cipher=self._cipher,
            ).lease_callback(
                owner=owner,
                lease_seconds=lease_seconds,
            )

    async def mark_callback_claimed(self, lease: CallbackLease) -> None:
        async with self._session_factory() as session, session.begin():
            await InteractionPresentationRepository(session, cipher=self._cipher).mark_callback_claimed(lease)

    async def reject_callback(self, lease: CallbackLease, *, code: str, terminal: bool = False) -> None:
        async with self._session_factory() as session, session.begin():
            await InteractionPresentationRepository(session, cipher=self._cipher).reject_callback(lease, code=code, terminal=terminal)

    async def retry_callback(
        self,
        lease: CallbackLease,
        *,
        code: str,
        delay_seconds: int,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await InteractionPresentationRepository(
                session,
                cipher=self._cipher,
            ).retry_callback(
                lease,
                code=code,
                delay_seconds=delay_seconds,
            )

    async def renew_callback(
        self,
        lease: CallbackLease,
        *,
        lease_seconds: int,
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await InteractionPresentationRepository(
                session,
                cipher=self._cipher,
            ).renew_callback(
                lease,
                lease_seconds=lease_seconds,
            )

    async def reconcile(self, *, limit: int = 20) -> int:
        async with self._session_factory() as session, session.begin():
            return await InteractionPresentationRepository(session, cipher=self._cipher).reconcile(limit=limit)


__all__ = [
    "CallbackLease",
    "CallbackReceiptResult",
    "InteractionCallbackPayload",
    "InteractionDelivery",
    "InteractionPresentationError",
    "InteractionPresentationErrorCode",
    "InteractionPresentationRepository",
    "InteractionPresentationService",
    "InteractionRequiredNotice",
]
