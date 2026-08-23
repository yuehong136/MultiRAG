"""Contract tests for the durable private Channel interaction API."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from api.channel_execution.dependencies import (
    DenyAllWorkloadAuthenticator,
    get_interaction_delivery_lease_seconds,
    get_interaction_presentation_service,
    require_channel_workload,
    require_interaction_presentation_service,
)
from api.channel_execution.interaction_presentations import (
    CallbackReceiptResult,
    InteractionCallbackPayload,
    InteractionDelivery,
    InteractionPresentationError,
    InteractionPresentationErrorCode,
)
from api.channel_execution.models import WorkloadIdentity

_BINDING_ID = "binding-1"
_ACTION_ID = "interaction-1"
_REVISION = 2
_DELIVERY_ID = "delivery-1"
_DELIVERY_TOKEN = "delivery-token-one-time"
_ACTION_NONCE = "action-nonce-one-time"
_FIELD_ID = "f_aaaaaaaaaaaaaaaaaaaaaaaa"


class _PresentationService:
    def __init__(self) -> None:
        self.claim_result: InteractionDelivery | None = InteractionDelivery(
            delivery_id=_DELIVERY_ID,
            delivery_token=_DELIVERY_TOKEN,
            action_id=_ACTION_ID,
            revision=_REVISION,
            kind="form",
            presentation_ref="om-presented-message",
            projection={
                "message": "需要补充信息",
                "fields": [{"name": _FIELD_ID, "kind": "text", "label": "原因"}],
            },
            action_nonce=_ACTION_NONCE,
            expires_at=datetime(2026, 8, 23, 12, 0, tzinfo=UTC),
        )
        self.callback_result = CallbackReceiptResult(status="accepted")
        self.claims: list[dict[str, Any]] = []
        self.acks: list[dict[str, Any]] = []
        self.callbacks: list[dict[str, Any]] = []
        self.claim_error: InteractionPresentationError | None = None
        self.ack_error: InteractionPresentationError | None = None
        self.callback_error: InteractionPresentationError | None = None

    async def claim_delivery(self, **kwargs: Any) -> InteractionDelivery | None:
        self.claims.append(kwargs)
        if self.claim_error is not None:
            raise self.claim_error
        return self.claim_result

    async def acknowledge_delivery(self, **kwargs: Any) -> None:
        self.acks.append(kwargs)
        if self.ack_error is not None:
            raise self.ack_error

    async def receive_callback(self, **kwargs: Any) -> CallbackReceiptResult:
        self.callbacks.append(kwargs)
        if self.callback_error is not None:
            raise self.callback_error
        return self.callback_result


def _authenticate_runner() -> WorkloadIdentity:
    return WorkloadIdentity(
        subject="runner-unit",
        binding_id=_BINDING_ID,
        binding_generation=4,
    )


def _callback_payload() -> dict[str, object]:
    return {
        "event_id": "callback-event-1",
        "nonce": _ACTION_NONCE,
        "action": "accept",
        "message_id": "om-presented-message",
        "actor": {
            "provider": "feishu",
            "subject": "ou-user",
            "conversation": "oc-chat",
            "identity": {
                "provider": "feishu",
                "provider_tenant_key": "tenant-external",
                "identifiers": [
                    {"kind": "open_id", "value": "ou-user"},
                    {"kind": "union_id", "value": "on-user"},
                ],
            },
        },
        "form_value": {_FIELD_ID: "approved"},
    }


def _override_private_dependencies(client, service: _PresentationService) -> None:
    client.app.dependency_overrides[require_channel_workload] = _authenticate_runner
    client.app.dependency_overrides[get_interaction_presentation_service] = lambda: service
    client.app.dependency_overrides[require_interaction_presentation_service] = lambda: service
    client.app.dependency_overrides[get_interaction_delivery_lease_seconds] = lambda: 17


def test_interaction_routes_require_workload_authentication(client) -> None:
    client.app.dependency_overrides[require_channel_workload] = DenyAllWorkloadAuthenticator().authenticate

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/interaction-deliveries/claim",
        json={"owner": "worker-a"},
    )

    assert response.status_code == 401
    assert response.json()["message"] == "Unauthorized channel runtime."


def test_interaction_routes_require_exact_binding_generation_scope(client) -> None:
    service = _PresentationService()
    client.app.dependency_overrides[require_channel_workload] = lambda: WorkloadIdentity(
        subject="runner-unit",
        binding_id="binding-other",
        binding_generation=4,
    )
    client.app.dependency_overrides[get_interaction_presentation_service] = lambda: service
    client.app.dependency_overrides[require_interaction_presentation_service] = lambda: service

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/interaction-deliveries/claim",
        json={"owner": "worker-a"},
    )

    assert response.status_code == 401
    assert service.claims == []


def test_claim_returns_only_one_time_delivery_material_and_safe_projection(client) -> None:
    service = _PresentationService()
    _override_private_dependencies(client, service)

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/interaction-deliveries/claim",
        json={
            "owner": "worker-a",
            "action_id": _ACTION_ID,
            "revision": _REVISION,
        },
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "delivery_id": _DELIVERY_ID,
        "delivery_token": _DELIVERY_TOKEN,
        "action_id": _ACTION_ID,
        "revision": _REVISION,
        "kind": "form",
        "presentation_ref": "om-presented-message",
        "projection": {
            "message": "需要补充信息",
            "fields": [{"name": _FIELD_ID, "kind": "text", "label": "原因"}],
        },
        "action_nonce": _ACTION_NONCE,
        "expires_at": "2026-08-23T12:00:00Z",
        "safe_error_code": None,
    }
    assert service.claims == [
        {
            "binding_id": _BINDING_ID,
            "binding_generation": 4,
            "owner": "worker-a",
            "lease_seconds": 17,
            "action_id": _ACTION_ID,
            "revision": _REVISION,
        }
    ]
    for forbidden in (
        "tenant_id",
        "principal",
        "requestState",
        "original_arguments",
        "nonce_digest",
        "access_token",
    ):
        assert forbidden not in response.text


def test_claim_returns_204_when_no_delivery_is_ready(client) -> None:
    service = _PresentationService()
    service.claim_result = None
    _override_private_dependencies(client, service)

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/interaction-deliveries/claim",
        json={"owner": "worker-a"},
    )

    assert response.status_code == 204
    assert response.content == b""


def test_claim_rejects_authority_smuggling_and_partial_action_scope(client) -> None:
    service = _PresentationService()
    _override_private_dependencies(client, service)
    endpoint = f"/api/v1/internal/channel-bindings/{_BINDING_ID}/interaction-deliveries/claim"

    for payload in (
        {"owner": "worker-a", "tenant_id": "attacker"},
        {"owner": "worker-a", "principal_id": "attacker"},
        {"owner": "worker-a", "requestState": {"approved": True}},
        {"owner": "worker-a", "action_id": _ACTION_ID},
    ):
        response = client.post(endpoint, json=payload)
        assert response.status_code == 422
        assert "attacker" not in response.text

    assert service.claims == []


def test_private_interaction_body_is_rejected_before_unbounded_buffering(client) -> None:
    service = _PresentationService()
    _override_private_dependencies(client, service)

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/interaction-deliveries/claim",
        content=b"x" * 4_097,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "INTERACTION_REQUEST_INVALID"
    assert service.claims == []


def test_delivery_ack_forwards_exact_lease_and_never_echoes_token(client) -> None:
    service = _PresentationService()
    _override_private_dependencies(client, service)

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/interaction-deliveries/{_DELIVERY_ID}/ack",
        json={
            "owner": "worker-a",
            "delivery_token": _DELIVERY_TOKEN,
            "success": False,
            "safe_error_code": "FEISHU_CARD_UPDATE_RETRY",
        },
    )

    assert response.status_code == 204
    assert response.content == b""
    assert _DELIVERY_TOKEN not in response.text
    assert service.acks == [
        {
            "binding_id": _BINDING_ID,
            "binding_generation": 4,
            "delivery_id": _DELIVERY_ID,
            "owner": "worker-a",
            "delivery_token": _DELIVERY_TOKEN,
            "success": False,
            "safe_error_code": "FEISHU_CARD_UPDATE_RETRY",
        }
    ]


def test_callback_is_durably_receipted_before_a_sanitized_202_ack(client) -> None:
    service = _PresentationService()
    _override_private_dependencies(client, service)

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/interactions/{_ACTION_ID}/revisions/{_REVISION}/callbacks",
        json=_callback_payload(),
    )

    assert response.status_code == 202
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {"status": "accepted"}
    assert len(service.callbacks) == 1
    received = service.callbacks[0]
    assert received["binding_id"] == _BINDING_ID
    assert received["binding_generation"] == 4
    assert received["action_id"] == _ACTION_ID
    assert received["revision"] == _REVISION
    assert isinstance(received["payload"], InteractionCallbackPayload)
    assert "ou-user" not in response.text
    assert _ACTION_NONCE not in response.text


def test_duplicate_callback_is_still_acknowledged_without_echoing_payload(client) -> None:
    service = _PresentationService()
    service.callback_result = CallbackReceiptResult(status="duplicate")
    _override_private_dependencies(client, service)

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/interactions/{_ACTION_ID}/revisions/{_REVISION}/callbacks",
        json=_callback_payload(),
    )

    assert response.status_code == 202
    assert response.json() == {"status": "duplicate"}
    assert "form_value" not in response.text


def test_callback_rejects_trusted_fields_before_the_durable_service(client) -> None:
    service = _PresentationService()
    _override_private_dependencies(client, service)
    endpoint = f"/api/v1/internal/channel-bindings/{_BINDING_ID}/interactions/{_ACTION_ID}/revisions/{_REVISION}/callbacks"

    for field, value in (
        ("tenant_id", "attacker"),
        ("principal_id", "attacker"),
        ("requestState", {"approved": True}),
        ("original_arguments", {"tool": "dangerous"}),
    ):
        response = client.post(endpoint, json={**_callback_payload(), field: value})
        assert response.status_code == 422
        assert _ACTION_NONCE not in response.text
        assert "attacker" not in response.text

    assert service.callbacks == []


def test_presentation_errors_are_stable_and_do_not_echo_callback_secrets(client) -> None:
    service = _PresentationService()
    service.callback_error = InteractionPresentationError(InteractionPresentationErrorCode.BINDING_MISMATCH)
    _override_private_dependencies(client, service)

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/interactions/{_ACTION_ID}/revisions/{_REVISION}/callbacks",
        json=_callback_payload(),
    )

    assert response.status_code == 404
    assert _ACTION_NONCE not in response.text
    assert "ou-user" not in response.text
