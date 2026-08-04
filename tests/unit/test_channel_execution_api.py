"""HTTP contract tests for the private Channel execution endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from starlette.requests import Request

from api.channel_execution.dependencies import (
    DenyAllWorkloadAuthenticator,
    StaticBearerWorkloadAuthenticator,
    get_channel_execution_service,
    require_channel_workload,
)
from api.channel_execution.models import ChannelExecutionCommand, ExecutionEvent, WorkloadIdentity
from api.channel_runtime.tokens import derive_binding_workload_token


class _RouteService:
    async def execute(
        self,
        *,
        binding_id: str,
        workload: WorkloadIdentity,
        command: ChannelExecutionCommand,
    ) -> AsyncIterator[ExecutionEvent]:
        assert binding_id == "binding-1"
        assert workload.subject == "runner-unit"
        assert command.event_id == "evt-1"

        async def _events() -> AsyncIterator[ExecutionEvent]:
            yield ExecutionEvent(event="message_delta", content="answer", session_id="session-1")
            yield ExecutionEvent(event="message_completed", session_id="session-1")

        return _events()


def _payload() -> dict[str, object]:
    return {
        "event_id": "evt-1",
        "conversation_key": "feishu:chat:user",
        "message": {"type": "text", "content": "hello"},
        "actor": {"provider": "feishu", "subject": "ou-1", "conversation": "oc-1"},
    }


def test_internal_route_requires_workload_authentication(client) -> None:
    client.app.dependency_overrides[require_channel_workload] = DenyAllWorkloadAuthenticator().authenticate

    response = client.post(
        "/api/v1/internal/channel-bindings/binding-1/executions",
        headers={"Idempotency-Key": "evt-1"},
        json=_payload(),
    )

    assert response.status_code == 401
    assert "Unauthorized channel runtime" in response.text


def test_internal_route_rejects_trusted_fields_and_idempotency_mismatch(client) -> None:
    client.app.dependency_overrides[require_channel_workload] = lambda: WorkloadIdentity(subject="runner-unit")
    client.app.dependency_overrides[get_channel_execution_service] = lambda: _RouteService()

    injected = {**_payload(), "tenant_id": "attacker", "target_id": "other"}
    response = client.post(
        "/api/v1/internal/channel-bindings/binding-1/executions",
        headers={"Idempotency-Key": "evt-1"},
        json=injected,
    )
    assert response.status_code == 422

    response = client.post(
        "/api/v1/internal/channel-bindings/binding-1/executions",
        headers={"Idempotency-Key": "different"},
        json=_payload(),
    )
    assert response.status_code == 400


def test_internal_route_returns_only_sanitized_sse_contract(client) -> None:
    client.app.dependency_overrides[require_channel_workload] = lambda: WorkloadIdentity(subject="runner-unit")
    client.app.dependency_overrides[get_channel_execution_service] = lambda: _RouteService()

    response = client.post(
        "/api/v1/internal/channel-bindings/binding-1/executions",
        headers={"Idempotency-Key": "evt-1"},
        json=_payload(),
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert 'data:{"event":"message_delta","content":"answer","session_id":"session-1"}' in response.text
    assert 'data:{"event":"message_completed","session_id":"session-1"}' in response.text
    assert "data:[DONE]" in response.text
    assert "trace" not in response.text


async def test_static_bearer_authenticator_uses_constant_time_credential(monkeypatch) -> None:
    seen: list[tuple[str, str]] = []

    def _compare(left: str, right: str) -> bool:
        seen.append((left, right))
        return left == right

    monkeypatch.setattr("api.channel_execution.dependencies.secrets.compare_digest", _compare)
    authenticator = StaticBearerWorkloadAuthenticator(SecretStr("token-unit"), subject="runner-unit")
    valid = Request({"type": "http", "headers": [(b"authorization", b"Bearer token-unit")]})

    identity = await authenticator.authenticate(valid)

    assert identity == WorkloadIdentity(subject="runner-unit")
    assert seen == [("token-unit", "token-unit")]

    invalid = Request({"type": "http", "headers": [(b"authorization", b"Bearer wrong")]})
    try:
        await authenticator.authenticate(invalid)
    except HTTPException as exc:
        assert exc.status_code == 401
        assert "wrong" not in exc.detail
    else:
        raise AssertionError("invalid workload credential was accepted")


async def test_static_bearer_authenticator_scopes_child_token_to_binding_generation() -> None:
    master_token = "master-token-unit"
    authenticator = StaticBearerWorkloadAuthenticator(SecretStr(master_token), subject="runner-unit")
    child_token = derive_binding_workload_token(
        master_token,
        binding_id="binding-1",
        generation=7,
    )
    request = Request(
        {
            "type": "http",
            "path_params": {"binding_id": "binding-1"},
            "headers": [
                (b"authorization", f"Bearer {child_token}".encode()),
                (b"x-channel-binding-generation", b"7"),
            ],
        }
    )

    identity = await authenticator.authenticate(request)

    assert identity == WorkloadIdentity(
        subject="runner-unit",
        binding_id="binding-1",
        binding_generation=7,
    )

    wrong_binding = Request(
        {
            "type": "http",
            "path_params": {"binding_id": "binding-2"},
            "headers": [
                (b"authorization", f"Bearer {child_token}".encode()),
                (b"x-channel-binding-generation", b"7"),
            ],
        }
    )
    with pytest.raises(HTTPException) as raised:
        await authenticator.authenticate(wrong_binding)
    assert raised.value.status_code == 401

    oversized_generation = Request(
        {
            "type": "http",
            "path_params": {"binding_id": "binding-1"},
            "headers": [
                (b"authorization", f"Bearer {child_token}".encode()),
                (b"x-channel-binding-generation", str(2**63).encode()),
            ],
        }
    )
    with pytest.raises(HTTPException) as raised:
        await authenticator.authenticate(oversized_generation)
    assert raised.value.status_code == 401
