"""Durability and fencing contracts for Channel interaction presentations."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from api.channel_execution.interaction_presentations import (
    CallbackLease,
    InteractionCallbackPayload,
    InteractionPresentationError,
    InteractionPresentationErrorCode,
    InteractionPresentationRepository,
)
from api.channel_execution.models import ChannelActor
from api.db.db_models import McpInteractionCallbackReceipt, McpInteractionPresentation
from api.identity.mcp_interactions.contracts import InteractionErrorCode
from api.identity.mcp_interactions.crypto import EncryptedInteractionPayload, InteractionPayloadCipher

_FIELD_ID = "f_aaaaaaaaaaaaaaaaaaaaaaaa"


class _ScriptedSession(AsyncSession):
    def __init__(self, *responses: Any) -> None:
        super().__init__()
        self.responses = list(responses)
        self.added: list[Any] = []
        self.flushes = 0

    def extend(self, *responses: Any) -> None:
        self.responses.extend(responses)

    async def scalar(self, _statement: object) -> Any:
        assert self.responses, "unexpected database scalar call"
        return self.responses.pop(0)

    def add(self, instance: object, *, _warn: bool = True) -> None:
        self.added.append(instance)

    async def flush(self, objects: Any = None) -> None:
        self.flushes += 1


def _now() -> datetime:
    return datetime(2026, 8, 23, 1, 2, 3, tzinfo=UTC)


def _presentation(now: datetime) -> McpInteractionPresentation:
    return McpInteractionPresentation(
        id="presentation00000000000000000001",
        interaction_id="interaction0000000000000000001",
        tenant_id="tenant000000000000000000000001",
        revision=2,
        binding_id="binding00000000000000000000001",
        binding_generation=7,
        provider="feishu",
        provider_account_id="account00000000000000000000001",
        source_event_digest="a" * 64,
        conversation_ref="chat-1",
        presentation_ref="message-1",
        expires_at=now + timedelta(minutes=10),
        response_state="open",
        delivery_kind="form",
        delivery_state="pending",
        delivery_projection={"message": "安全字段", "fields": []},
        form_mapping_ciphertext="v1.encrypted-mapping",
        form_mapping_key_id="mapping-key-id",
        nonce_digest=None,
        delivery_token_digest=None,
        delivery_lease_owner=None,
        delivery_lease_until=None,
        delivery_attempt=0,
        delivery_next_attempt_at=None,
        safe_error_code=None,
        created_at=now,
        updated_at=now,
    )


def _receipt(
    *,
    now: datetime,
    row: McpInteractionPresentation,
    ciphertext: str,
    key_id: str,
) -> McpInteractionCallbackReceipt:
    return McpInteractionCallbackReceipt(
        id="receipt000000000000000000000001",
        presentation_id=row.id,
        binding_id=row.binding_id,
        event_digest="b" * 64,
        payload_digest="c" * 64,
        payload_ciphertext=ciphertext,
        payload_key_id=key_id,
        state="received",
        lease_owner=None,
        lease_until=None,
        attempt=0,
        next_attempt_at=None,
        safe_error_code=None,
        created_at=now,
        updated_at=now,
    )


def _repository(session: _ScriptedSession, cipher: InteractionPayloadCipher) -> InteractionPresentationRepository:
    return InteractionPresentationRepository(session, cipher=cipher)


def _payload(
    *,
    nonce: str,
    event_id: str = "event-1",
    provider: str = "feishu",
    conversation: str = "chat-1",
    message_id: str = "message-1",
    subject: str = "user-1",
    form_value: dict[str, Any] | None = None,
) -> InteractionCallbackPayload:
    return InteractionCallbackPayload(
        event_id=event_id,
        nonce=nonce,
        action="accept",
        message_id=message_id,
        actor=ChannelActor(
            provider=provider,
            subject=subject,
            conversation=conversation,
        ),
        form_value=form_value or {},
    )


async def _claim_delivery(
    *,
    monkeypatch: pytest.MonkeyPatch,
    row: McpInteractionPresentation,
    now: datetime,
    cipher: InteractionPayloadCipher,
) -> tuple[_ScriptedSession, InteractionPresentationRepository, str, str]:
    tokens = iter(("delivery-token-0123456789", "action-nonce-0123456789"))
    monkeypatch.setattr(
        "api.channel_execution.interaction_presentations.secrets.token_urlsafe",
        lambda _size: next(tokens),
    )
    # Candidate selection clock, presentation row, interaction row, and the
    # post-lock wall clock used to issue the lease.
    session = _ScriptedSession(now, row, object(), now)
    repository = _repository(session, cipher)
    delivery = await repository.claim_delivery(
        binding_id=row.binding_id,
        binding_generation=row.binding_generation,
        owner="worker-a",
        lease_seconds=30,
    )
    assert delivery is not None
    assert delivery.delivery_token == "delivery-token-0123456789"
    assert delivery.action_nonce == "action-nonce-0123456789"
    assert "delivery-token-0123456789" not in repr(delivery)
    assert "action-nonce-0123456789" not in repr(delivery)
    return session, repository, delivery.delivery_token, cast(str, delivery.action_nonce)


@pytest.mark.asyncio
async def test_delivery_ack_requires_current_binding_owner_and_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _now()
    row = _presentation(now)
    cipher = InteractionPayloadCipher([b"k" * 32])
    session, repository, token, _ = await _claim_delivery(
        monkeypatch=monkeypatch,
        row=row,
        now=now,
        cipher=cipher,
    )

    for owner, candidate_token in (
        ("worker-b", token),
        ("worker-a", "stale-delivery-token-0000"),
    ):
        session.extend(row, now)
        with pytest.raises(InteractionPresentationError) as exc_info:
            await repository.acknowledge_delivery(
                binding_id=row.binding_id,
                binding_generation=row.binding_generation,
                delivery_id=row.id,
                owner=owner,
                delivery_token=candidate_token,
                success=True,
                safe_error_code=None,
            )
        assert exc_info.value.code is InteractionPresentationErrorCode.DELIVERY_CONFLICT
        assert row.delivery_state == "leased"

    session.extend(None, now)
    with pytest.raises(InteractionPresentationError) as exc_info:
        await repository.acknowledge_delivery(
            binding_id=row.binding_id,
            binding_generation=row.binding_generation + 1,
            delivery_id=row.id,
            owner="worker-a",
            delivery_token=token,
            success=True,
            safe_error_code=None,
        )
    assert exc_info.value.code is InteractionPresentationErrorCode.DELIVERY_CONFLICT

    session.extend(row, now)
    await repository.acknowledge_delivery(
        binding_id=row.binding_id,
        binding_generation=row.binding_generation,
        delivery_id=row.id,
        owner="worker-a",
        delivery_token=token,
        success=True,
        safe_error_code=None,
    )
    assert row.delivery_state == "delivered"
    assert row.delivery_token_digest is None
    assert row.delivery_lease_owner is None
    assert row.delivery_lease_until is None

    session.extend(row, now)
    with pytest.raises(InteractionPresentationError) as exc_info:
        await repository.acknowledge_delivery(
            binding_id=row.binding_id,
            binding_generation=row.binding_generation,
            delivery_id=row.id,
            owner="worker-a",
            delivery_token=token,
            success=True,
            safe_error_code=None,
        )
    assert exc_info.value.code is InteractionPresentationErrorCode.DELIVERY_CONFLICT


@pytest.mark.asyncio
async def test_delivery_ack_rejects_an_expired_lease_before_reclaim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _now()
    row = _presentation(now)
    cipher = InteractionPayloadCipher([b"k" * 32])
    session, repository, token, _ = await _claim_delivery(
        monkeypatch=monkeypatch,
        row=row,
        now=now,
        cipher=cipher,
    )
    row.delivery_lease_until = now - timedelta(microseconds=1)
    session.extend(row, now)

    with pytest.raises(InteractionPresentationError) as exc_info:
        await repository.acknowledge_delivery(
            binding_id=row.binding_id,
            binding_generation=row.binding_generation,
            delivery_id=row.id,
            owner="worker-a",
            delivery_token=token,
            success=True,
            safe_error_code=None,
        )

    assert exc_info.value.code is InteractionPresentationErrorCode.DELIVERY_CONFLICT
    assert row.delivery_state == "leased"
    assert row.delivery_token_digest is not None


@pytest.mark.asyncio
async def test_callback_validates_lineage_nonce_and_persists_before_ack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _now()
    row = _presentation(now)
    cipher = InteractionPayloadCipher([b"k" * 32])
    session, repository, token, nonce = await _claim_delivery(
        monkeypatch=monkeypatch,
        row=row,
        now=now,
        cipher=cipher,
    )
    session.extend(row, now)
    await repository.acknowledge_delivery(
        binding_id=row.binding_id,
        binding_generation=row.binding_generation,
        delivery_id=row.id,
        owner="worker-a",
        delivery_token=token,
        success=True,
        safe_error_code=None,
    )

    for payload in (
        _payload(nonce=nonce, provider="other"),
        _payload(nonce=nonce, conversation="other-chat"),
        _payload(nonce=nonce, message_id="other-message"),
    ):
        session.extend(row)
        with pytest.raises(InteractionPresentationError) as exc_info:
            await repository.receive_callback(
                binding_id=row.binding_id,
                binding_generation=row.binding_generation,
                action_id=row.interaction_id,
                revision=row.revision,
                payload=payload,
            )
        assert exc_info.value.code is InteractionPresentationErrorCode.BINDING_MISMATCH

    session.extend(None)
    with pytest.raises(InteractionPresentationError) as exc_info:
        await repository.receive_callback(
            binding_id=row.binding_id,
            binding_generation=row.binding_generation + 1,
            action_id=row.interaction_id,
            revision=row.revision,
            payload=_payload(nonce=nonce),
        )
    assert exc_info.value.code is InteractionPresentationErrorCode.NOT_FOUND

    session.extend(row, None, now)
    with pytest.raises(InteractionPresentationError) as exc_info:
        await repository.receive_callback(
            binding_id=row.binding_id,
            binding_generation=row.binding_generation,
            action_id=row.interaction_id,
            revision=row.revision,
            payload=_payload(nonce="wrong-action-nonce-00000"),
        )
    assert exc_info.value.code is InteractionPresentationErrorCode.STATE_CONFLICT

    payload = _payload(
        nonce=nonce,
        subject="untrusted-user-subject",
        form_value={_FIELD_ID: ["o_opaque"]},
    )
    session.extend(row, None, now)
    result = await repository.receive_callback(
        binding_id=row.binding_id,
        binding_generation=row.binding_generation,
        action_id=row.interaction_id,
        revision=row.revision,
        payload=payload,
    )
    assert result.status == "accepted"
    assert row.response_state == "received"
    assert row.nonce_digest is None

    receipt = cast(McpInteractionCallbackReceipt, session.added[-1])
    assert receipt.state == "received"
    assert receipt.lease_owner is None
    assert receipt.lease_until is None
    assert "untrusted-user-subject" not in receipt.payload_ciphertext
    decrypted = cipher.decrypt(
        tenant_id=row.tenant_id,
        interaction_id=row.interaction_id,
        revision=row.revision,
        purpose="channel_callback",
        encrypted=EncryptedInteractionPayload(
            ciphertext=receipt.payload_ciphertext,
            key_id=receipt.payload_key_id,
        ),
    )
    assert decrypted == payload.model_dump(mode="json")


@pytest.mark.asyncio
async def test_duplicate_callback_requires_identical_encrypted_receipt_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _now()
    row = _presentation(now)
    cipher = InteractionPayloadCipher([b"k" * 32])
    session, repository, token, nonce = await _claim_delivery(
        monkeypatch=monkeypatch,
        row=row,
        now=now,
        cipher=cipher,
    )
    session.extend(row, now)
    await repository.acknowledge_delivery(
        binding_id=row.binding_id,
        binding_generation=row.binding_generation,
        delivery_id=row.id,
        owner="worker-a",
        delivery_token=token,
        success=True,
        safe_error_code=None,
    )
    payload = _payload(nonce=nonce, form_value={_FIELD_ID: "first"})
    session.extend(row, None, now)
    accepted = await repository.receive_callback(
        binding_id=row.binding_id,
        binding_generation=row.binding_generation,
        action_id=row.interaction_id,
        revision=row.revision,
        payload=payload,
    )
    receipt = cast(McpInteractionCallbackReceipt, session.added[-1])
    assert accepted.status == "accepted"

    session.extend(row, receipt)
    duplicate = await repository.receive_callback(
        binding_id=row.binding_id,
        binding_generation=row.binding_generation,
        action_id=row.interaction_id,
        revision=row.revision,
        payload=payload,
    )
    assert duplicate.status == "duplicate"
    assert len(session.added) == 1

    row.expires_at = now - timedelta(seconds=1)
    session.extend(row, receipt)
    duplicate_after_expiry = await repository.receive_callback(
        binding_id=row.binding_id,
        binding_generation=row.binding_generation,
        action_id=row.interaction_id,
        revision=row.revision,
        payload=payload,
    )
    assert duplicate_after_expiry.status == "duplicate"

    changed = _payload(nonce=nonce, form_value={_FIELD_ID: "changed"})
    session.extend(row, receipt)
    with pytest.raises(InteractionPresentationError) as exc_info:
        await repository.receive_callback(
            binding_id=row.binding_id,
            binding_generation=row.binding_generation,
            action_id=row.interaction_id,
            revision=row.revision,
            payload=changed,
        )
    assert exc_info.value.code is InteractionPresentationErrorCode.STATE_CONFLICT


@pytest.mark.asyncio
async def test_poisoned_durable_receipt_is_rejected_instead_of_hot_looping() -> None:
    now = _now()
    row = _presentation(now)
    row.response_state = "received"
    cipher = InteractionPayloadCipher([b"k" * 32])
    encrypted = cipher.encrypt(
        tenant_id=row.tenant_id,
        interaction_id=row.interaction_id,
        revision=row.revision,
        purpose="channel_callback",
        value={"valid": "shape"},
    )
    receipt = _receipt(
        now=now,
        row=row,
        ciphertext="v1.not-valid-ciphertext",
        key_id=encrypted.key_id,
    )
    session = _ScriptedSession(now, receipt, row, now)

    lease = await _repository(session, cipher).lease_callback(
        owner="callback-worker",
        lease_seconds=30,
    )

    assert lease is None
    assert receipt.state == "rejected"
    assert receipt.safe_error_code == InteractionPresentationErrorCode.PAYLOAD_INVALID.value
    assert row.response_state == "terminal"
    assert row.delivery_kind == "terminal"
    assert row.delivery_state == "pending"


@pytest.mark.asyncio
async def test_invalid_form_response_reopens_with_a_fresh_delivery_nonce(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = _now()
    row = _presentation(now)
    row.response_state = "received"
    cipher = InteractionPayloadCipher([b"k" * 32])
    mapping = {
        "requests": {
            "request-1": {
                _FIELD_ID: {
                    "name": "required_date",
                    "type": "string",
                    "kind": "date",
                    "required": True,
                    "options": {},
                    "min": 0,
                    "max": 1000,
                },
            },
        },
    }
    encrypted_mapping = cipher.encrypt(
        tenant_id=row.tenant_id,
        interaction_id=row.interaction_id,
        revision=row.revision,
        purpose="form_mapping",
        value=mapping,
    )
    row.form_mapping_ciphertext = encrypted_mapping.ciphertext
    row.form_mapping_key_id = encrypted_mapping.key_id
    callback = _payload(
        nonce="action-nonce-0123456789",
        form_value={},
    )
    encrypted_callback = cipher.encrypt(
        tenant_id=row.tenant_id,
        interaction_id=row.interaction_id,
        revision=row.revision,
        purpose="channel_callback",
        value=callback.model_dump(mode="json"),
    )
    receipt = _receipt(
        now=now,
        row=row,
        ciphertext=encrypted_callback.ciphertext,
        key_id=encrypted_callback.key_id,
    )
    session = _ScriptedSession(now, receipt, row, now)

    repository = _repository(session, cipher)
    lease = await repository.lease_callback(
        owner="callback-worker",
        lease_seconds=30,
    )

    assert lease is None
    assert receipt.state == "rejected"
    assert receipt.safe_error_code == InteractionErrorCode.RESPONSE_INVALID.value
    assert row.response_state == "open"
    assert row.delivery_kind == "form"
    assert row.delivery_state == "pending"
    assert row.nonce_digest is None
    assert row.safe_error_code == InteractionErrorCode.RESPONSE_INVALID.value

    tokens = iter(("new-delivery-token-0123456789", "new-action-nonce-0123456789"))
    monkeypatch.setattr(
        "api.channel_execution.interaction_presentations.secrets.token_urlsafe",
        lambda _size: next(tokens),
    )
    session.extend(now, row, object(), now)
    delivery = await repository.claim_delivery(
        binding_id=row.binding_id,
        binding_generation=row.binding_generation,
        owner="worker-a",
        lease_seconds=30,
    )

    assert delivery is not None
    assert delivery.action_nonce == "new-action-nonce-0123456789"
    assert delivery.action_nonce != callback.nonce
    assert delivery.safe_error_code == InteractionErrorCode.RESPONSE_INVALID.value


@pytest.mark.asyncio
async def test_callback_finalizer_rejects_an_expired_lease_fence() -> None:
    now = _now()
    row = _presentation(now)
    row.response_state = "received"
    cipher = InteractionPayloadCipher([b"k" * 32])
    encrypted = cipher.encrypt(
        tenant_id=row.tenant_id,
        interaction_id=row.interaction_id,
        revision=row.revision,
        purpose="channel_callback",
        value={"valid": "shape"},
    )
    receipt = _receipt(
        now=now,
        row=row,
        ciphertext=encrypted.ciphertext,
        key_id=encrypted.key_id,
    )
    receipt.state = "leased"
    receipt.lease_owner = "callback-worker"
    receipt.lease_until = now - timedelta(microseconds=1)
    receipt.attempt = 2
    lease = CallbackLease(
        receipt_id=receipt.id,
        owner="callback-worker",
        attempt=receipt.attempt,
        presentation_id=row.id,
        binding_id=row.binding_id,
        binding_generation=row.binding_generation,
        provider_account_id=row.provider_account_id,
        interaction_id=row.interaction_id,
        revision=row.revision,
        actor=ChannelActor(provider="feishu", subject="user-1", conversation="chat-1"),
        action="accept",
        input_responses={},
        idempotency_key=receipt.event_digest,
    )
    session = _ScriptedSession(receipt, row, now)

    with pytest.raises(InteractionPresentationError) as exc_info:
        await _repository(session, cipher).mark_callback_claimed(lease)

    assert exc_info.value.code is InteractionPresentationErrorCode.STATE_CONFLICT
    assert receipt.state == "leased"


@pytest.mark.asyncio
async def test_callback_finalizer_rejects_a_mismatched_presentation_fence() -> None:
    now = _now()
    row = _presentation(now)
    row.response_state = "received"
    cipher = InteractionPayloadCipher([b"k" * 32])
    encrypted = cipher.encrypt(
        tenant_id=row.tenant_id,
        interaction_id=row.interaction_id,
        revision=row.revision,
        purpose="channel_callback",
        value={"valid": "shape"},
    )
    receipt = _receipt(
        now=now,
        row=row,
        ciphertext=encrypted.ciphertext,
        key_id=encrypted.key_id,
    )
    receipt.state = "leased"
    receipt.lease_owner = "callback-worker"
    receipt.lease_until = now + timedelta(seconds=30)
    receipt.attempt = 2
    lease = CallbackLease(
        receipt_id=receipt.id,
        owner="callback-worker",
        attempt=receipt.attempt,
        presentation_id=row.id,
        binding_id=row.binding_id,
        binding_generation=row.binding_generation,
        provider_account_id=row.provider_account_id,
        interaction_id=row.interaction_id,
        revision=row.revision,
        actor=ChannelActor(
            provider="feishu",
            subject="user-1",
            conversation="chat-1",
        ),
        action="accept",
        input_responses={},
        idempotency_key=receipt.event_digest,
    )
    session = _ScriptedSession(receipt, None, now)

    with pytest.raises(InteractionPresentationError) as exc_info:
        await _repository(session, cipher).mark_callback_claimed(
            replace(lease, presentation_id="different-presentation"),
        )

    assert exc_info.value.code is InteractionPresentationErrorCode.STATE_CONFLICT
    assert receipt.state == "leased"


def test_callback_payload_is_bounded_and_nonaccept_actions_carry_no_form() -> None:
    actor = ChannelActor(provider="feishu", subject="user-1", conversation="chat-1")
    with pytest.raises(ValidationError):
        InteractionCallbackPayload(
            event_id="event-1",
            nonce="action-nonce-0123456789",
            action="decline",
            message_id="message-1",
            actor=actor,
            form_value={_FIELD_ID: "not-allowed"},
        )
    with pytest.raises(ValidationError):
        InteractionCallbackPayload(
            event_id="event-1",
            nonce="action-nonce-0123456789",
            message_id="message-1",
            actor=actor,
            form_value={_FIELD_ID: "x" * 65_536},
        )

    for form_value in (
        {"not-an-opaque-field": "value"},
        {_FIELD_ID: {"nested": "value"}},
        {_FIELD_ID: ["option-a", "option-a"]},
    ):
        with pytest.raises(ValidationError):
            InteractionCallbackPayload(
                event_id="event-1",
                nonce="action-nonce-0123456789",
                action="accept",
                message_id="message-1",
                actor=actor,
                form_value=form_value,
            )
