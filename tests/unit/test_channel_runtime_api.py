"""Contract tests for the authenticated private Channel runtime control API."""

from __future__ import annotations

import logging
from typing import Any

from api.channel_control.dependencies import get_runtime_control_service
from api.channel_control.service import ChannelCredentialUnavailable, ResolvedRuntimeBindingSpec
from api.channel_execution.dependencies import DenyAllWorkloadAuthenticator, require_channel_workload
from api.channel_execution.models import WorkloadIdentity


class _RuntimeService:
    def __init__(self) -> None:
        self.reports: list[dict[str, Any]] = []

    async def list_desired_runtimes(self) -> list[dict[str, Any]]:
        return [{"binding_id": "binding-1", "provider": "feishu", "generation": 4}]

    async def resolve_runtime_binding(self, binding_id: str) -> ResolvedRuntimeBindingSpec:
        assert binding_id == "binding-1"
        return ResolvedRuntimeBindingSpec(
            binding_id=binding_id,
            provider="feishu",
            generation=4,
            public_config={"domain": "feishu", "allowed_open_ids": ["ou-user"]},
            credentials={"app_id": "cli-app", "app_secret": "app-secret-private"},
        )

    async def report_runtime(self, **kwargs: Any) -> None:
        self.reports.append(kwargs)


def _authenticate_runner() -> WorkloadIdentity:
    return WorkloadIdentity(subject="runner-unit")


def test_runtime_control_routes_require_workload_authentication(client) -> None:
    client.app.dependency_overrides[require_channel_workload] = DenyAllWorkloadAuthenticator().authenticate

    desired = client.get("/api/v1/internal/channel-runtimes/desired")
    config = client.get("/api/v1/internal/channel-bindings/binding-1/runtime-config")
    report = client.put(
        "/api/v1/internal/channel-bindings/binding-1/runtime-status",
        json={"observed_generation": 1, "state": "starting", "runner_id": "runner-unit"},
    )

    assert desired.status_code == 401
    assert config.status_code == 401
    assert report.status_code == 401


def test_desired_runtime_contract_contains_no_credentials_or_execution_target(client) -> None:
    service = _RuntimeService()
    client.app.dependency_overrides[require_channel_workload] = _authenticate_runner
    client.app.dependency_overrides[get_runtime_control_service] = lambda: service

    response = client.get("/api/v1/internal/channel-runtimes/desired")

    assert response.status_code == 200
    assert response.json() == {"items": [{"binding_id": "binding-1", "provider": "feishu", "generation": 4}]}
    serialized = response.text
    for forbidden in (
        "app_secret",
        "tenant_id",
        "target_id",
        "target_type",
        "revision_id",
        "target_revision_id",
        "session_id",
        "policy",
    ):
        assert forbidden not in serialized


def test_runtime_config_releases_only_provider_connection_material_to_authenticated_runner(
    client,
    caplog,
) -> None:
    service = _RuntimeService()
    client.app.dependency_overrides[require_channel_workload] = _authenticate_runner
    client.app.dependency_overrides[get_runtime_control_service] = lambda: service
    caplog.set_level(logging.DEBUG)

    response = client.get("/api/v1/internal/channel-bindings/binding-1/runtime-config")

    assert response.status_code == 200
    assert response.json() == {
        "binding_id": "binding-1",
        "provider": "feishu",
        "generation": 4,
        "public_config": {"domain": "feishu", "allowed_open_ids": ["ou-user"]},
        "credential": {"app_id": "cli-app", "app_secret": "app-secret-private"},
    }
    for forbidden in (
        "tenant_id",
        "target_id",
        "target_type",
        "revision_id",
        "target_revision_id",
        "session_id",
        "policy",
    ):
        assert forbidden not in response.text
    assert "app-secret-private" not in caplog.text


def test_runtime_status_rejects_trusted_context_overrides_and_forwards_only_report_fields(client) -> None:
    service = _RuntimeService()
    client.app.dependency_overrides[require_channel_workload] = _authenticate_runner
    client.app.dependency_overrides[get_runtime_control_service] = lambda: service
    base_payload = {
        "observed_generation": 4,
        "state": "connected",
        "runner_id": "runner-unit",
        "connected_at": "2026-07-31T08:00:00Z",
        "last_error_code": None,
    }

    for field in (
        "tenant_id",
        "target_id",
        "target_type",
        "revision_id",
        "target_revision_id",
        "session_id",
    ):
        response = client.put(
            "/api/v1/internal/channel-bindings/binding-1/runtime-status",
            json={**base_payload, field: "attacker-controlled"},
        )
        assert response.status_code == 422

    response = client.put(
        "/api/v1/internal/channel-bindings/binding-1/runtime-status",
        json=base_payload,
    )

    assert response.status_code == 204
    assert len(service.reports) == 1
    assert set(service.reports[0]) == {
        "binding_id",
        "observed_generation",
        "state",
        "runner_id",
        "heartbeat_at",
        "connected_at",
        "last_error_code",
    }
    assert service.reports[0]["binding_id"] == "binding-1"
    assert service.reports[0]["observed_generation"] == 4


def test_runtime_config_secret_store_failure_is_sanitized(client, caplog) -> None:
    class _UnavailableService(_RuntimeService):
        async def resolve_runtime_binding(self, binding_id: str) -> ResolvedRuntimeBindingSpec:
            del binding_id
            raise ChannelCredentialUnavailable

    client.app.dependency_overrides[require_channel_workload] = _authenticate_runner
    client.app.dependency_overrides[get_runtime_control_service] = _UnavailableService
    caplog.set_level(logging.DEBUG)

    response = client.get("/api/v1/internal/channel-bindings/binding-1/runtime-config")

    assert response.status_code == 503
    assert response.json() == {"detail": "CHANNEL_SECRET_STORE_UNAVAILABLE"}
    assert "secret" not in caplog.text.lower()
