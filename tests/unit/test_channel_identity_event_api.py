"""HTTP contract tests for the private Channel identity-event endpoint."""

from __future__ import annotations

from typing import Any

import pytest

from api.channel_execution.dependencies import (
    get_channel_directory_event_service,
    require_channel_workload,
)
from api.channel_execution.models import WorkloadIdentity
from api.channels.identity_events import ChannelIdentityEvent
from api.identity.directory_events import DirectoryEventError, DirectoryEventErrorCode

_BINDING_ID = "binding-1"
_EVENT_ID = "directory-event-1"


class _DirectoryEventService:
    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []
        self.error: DirectoryEventError | None = None

    async def receive(self, **kwargs: Any) -> object:
        self.received.append(kwargs)
        if self.error is not None:
            raise self.error
        return object()


def _workload() -> WorkloadIdentity:
    return WorkloadIdentity(
        subject="runner-unit",
        binding_id=_BINDING_ID,
        binding_generation=7,
    )


def _payload() -> dict[str, object]:
    return {
        "version": 1,
        "event_type": "contact.user.updated_v3",
        "event_id": _EVENT_ID,
        "event_at": "2026-08-24T06:00:00.123Z",
        "observed_app_id": "app-observed",
        "observed_tenant_key": "tenant-observed",
        "subject": {
            "identifiers": [
                {"kind": "open_id", "value": "ou-observed"},
                {"kind": "user_id", "value": "user-observed"},
            ],
            "directory_status": "inactive",
        },
    }


def _override(client: Any, service: _DirectoryEventService) -> None:
    client.app.dependency_overrides[require_channel_workload] = _workload
    client.app.dependency_overrides[get_channel_directory_event_service] = lambda: service


def test_private_identity_event_returns_only_no_content(client: Any) -> None:
    service = _DirectoryEventService()
    _override(client, service)

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/identity-events",
        headers={"Idempotency-Key": _EVENT_ID},
        json=_payload(),
    )

    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["cache-control"] == "private, no-store"
    assert len(service.received) == 1
    received = service.received[0]
    assert received["binding_id"] == _BINDING_ID
    assert received["binding_generation"] == 7
    event = received["event"]
    assert isinstance(event, ChannelIdentityEvent)
    assert event.observed_app_id == "app-observed"
    assert event.observed_tenant_key == "tenant-observed"


def test_private_identity_event_rejects_binding_generation_mismatch(client: Any) -> None:
    service = _DirectoryEventService()
    client.app.dependency_overrides[require_channel_workload] = lambda: WorkloadIdentity(
        subject="runner-unit",
        binding_id="other-binding",
        binding_generation=7,
    )
    client.app.dependency_overrides[get_channel_directory_event_service] = lambda: service

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/identity-events",
        headers={"Idempotency-Key": _EVENT_ID},
        json=_payload(),
    )

    assert response.status_code == 401
    assert service.received == []


@pytest.mark.parametrize(
    "payload",
    [
        {**_payload(), "tenant_id": "attacker-tenant"},
        {**_payload(), "provider_account_id": "attacker-account"},
        {**_payload(), "provider_account_revision": 999},
    ],
)
def test_private_identity_event_rejects_authority_smuggling_without_echo(
    client: Any,
    payload: dict[str, object],
) -> None:
    service = _DirectoryEventService()
    _override(client, service)

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/identity-events",
        headers={"Idempotency-Key": _EVENT_ID},
        json=payload,
    )

    assert response.status_code == 422
    assert "attacker" not in response.text
    assert service.received == []


def test_private_identity_event_bounds_and_sanitizes_raw_body(client: Any) -> None:
    service = _DirectoryEventService()
    _override(client, service)
    sentinel = "sensitive-directory-value"

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/identity-events",
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": _EVENT_ID,
        },
        content=(sentinel * 300).encode(),
    )

    assert response.status_code == 422
    assert sentinel not in response.text
    assert service.received == []


@pytest.mark.parametrize("content_length", ["0", "4097", "not-a-number"])
def test_private_identity_event_rejects_invalid_content_length_before_parsing(
    client: Any,
    content_length: str,
) -> None:
    service = _DirectoryEventService()
    _override(client, service)

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/identity-events",
        headers={
            "Content-Length": content_length,
            "Content-Type": "application/json",
            "Idempotency-Key": _EVENT_ID,
        },
        content=b"{}",
    )

    assert response.status_code == 422
    assert service.received == []


def test_private_identity_event_requires_exact_json_content_type(client: Any) -> None:
    service = _DirectoryEventService()
    _override(client, service)

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/identity-events",
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": _EVENT_ID,
        },
        content=b"{}",
    )

    assert response.status_code == 415
    assert service.received == []


@pytest.mark.parametrize("event_id", ["事件一", "event one", "event\nline"])
def test_private_identity_event_rejects_non_header_safe_event_id(
    client: Any,
    event_id: str,
) -> None:
    service = _DirectoryEventService()
    _override(client, service)
    payload = {**_payload(), "event_id": event_id}

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/identity-events",
        headers={"Idempotency-Key": _EVENT_ID},
        json=payload,
    )

    assert response.status_code == 422
    assert event_id not in response.text
    assert service.received == []


def test_private_identity_event_rejects_malformed_binding_without_422_echo(
    client: Any,
) -> None:
    service = _DirectoryEventService()
    _override(client, service)

    response = client.post(
        "/api/v1/internal/channel-bindings/bad%20binding/identity-events",
        headers={"Idempotency-Key": _EVENT_ID},
        json=_payload(),
    )

    assert response.status_code == 404
    assert service.received == []


def test_private_identity_event_requires_matching_idempotency_header(client: Any) -> None:
    service = _DirectoryEventService()
    _override(client, service)

    missing = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/identity-events",
        json=_payload(),
    )
    mismatch = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/identity-events",
        headers={"Idempotency-Key": "different-event"},
        json=_payload(),
    )

    assert missing.status_code == 422
    assert mismatch.status_code == 409
    assert service.received == []


@pytest.mark.parametrize(
    "code",
    [
        DirectoryEventErrorCode.BINDING_NOT_FOUND,
        DirectoryEventErrorCode.BINDING_DISABLED,
        DirectoryEventErrorCode.AUTHORITY_INVALID,
    ],
)
def test_private_identity_event_collapses_authority_failures(
    client: Any,
    code: DirectoryEventErrorCode,
) -> None:
    service = _DirectoryEventService()
    service.error = DirectoryEventError(code)
    _override(client, service)

    response = client.post(
        f"/api/v1/internal/channel-bindings/{_BINDING_ID}/identity-events",
        headers={"Idempotency-Key": _EVENT_ID},
        json=_payload(),
    )

    assert response.status_code == 404
    assert code.value not in response.text
    assert "attacker" not in response.text
