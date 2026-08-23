"""HTTP contract tests for the private Channel execution endpoint."""

from __future__ import annotations

from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from time import sleep
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.requests import Request

from api.channel_execution.dependencies import (
    DenyAllWorkloadAuthenticator,
    StaticBearerWorkloadAuthenticator,
    get_channel_execution_service,
    require_channel_workload,
)
from api.channel_execution.errors import ChannelStateUnavailableError
from api.channel_execution.models import (
    ChannelActor,
    ChannelExecutionCommand,
    ExecutionEvent,
    ExternalIdentityAssertion,
    ExternalIdentityIdentifier,
    WorkloadIdentity,
)
from api.channel_runtime.tokens import derive_binding_workload_token
from api.identity.enterprise_subjects.service import EnterpriseSubjectService
from api.identity_adapters.channel_runtime import IdentityProviderRegistry
from common.app_config import EnterpriseSubjectResolutionConfig


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
            yield ExecutionEvent(
                event="message_completed",
                content="authoritative ##0$$ answer",
                session_id="session-1",
            )

        return _events()


def _payload() -> dict[str, object]:
    return {
        "event_id": "evt-1",
        "conversation_key": "feishu:chat:user",
        "message": {"type": "text", "content": "hello"},
        "actor": {"provider": "feishu", "subject": "ou-1", "conversation": "oc-1"},
    }


def _structured_identity() -> dict[str, object]:
    return {
        "provider": "feishu",
        "provider_tenant_key": "tenant-external",
        "identifiers": [
            {"kind": "open_id", "value": "ou-external"},
            {"kind": "user_id", "value": "user-external"},
            {"kind": "union_id", "value": "on-external"},
        ],
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
    assert 'data:{"event":"message_completed","content":"authoritative ##0$$ answer","session_id":"session-1"}' in response.text
    assert "data:[DONE]" in response.text
    assert "trace" not in response.text


def test_internal_route_tolerates_structured_identity_but_keeps_legacy_actor_authoritative(client) -> None:
    class _ToleratingRouteService(_RouteService):
        async def execute(
            self,
            *,
            binding_id: str,
            workload: WorkloadIdentity,
            command: ChannelExecutionCommand,
        ) -> AsyncIterator[ExecutionEvent]:
            assert command.actor.subject == "ou-1"
            assert command.actor.identity == ExternalIdentityAssertion(
                provider="feishu",
                provider_tenant_key="tenant-external",
                identifiers=(
                    ExternalIdentityIdentifier(kind="open_id", value="ou-external"),
                    ExternalIdentityIdentifier(kind="user_id", value="user-external"),
                    ExternalIdentityIdentifier(kind="union_id", value="on-external"),
                ),
            )
            return await super().execute(
                binding_id=binding_id,
                workload=workload,
                command=command,
            )

    client.app.dependency_overrides[require_channel_workload] = lambda: WorkloadIdentity(subject="runner-unit")
    client.app.dependency_overrides[get_channel_execution_service] = lambda: _ToleratingRouteService()
    payload = _payload()
    payload["actor"] = {**payload["actor"], "identity": _structured_identity()}

    response = client.post(
        "/api/v1/internal/channel-bindings/binding-1/executions",
        headers={"Idempotency-Key": "evt-1"},
        json=payload,
    )

    assert response.status_code == 200


def test_legacy_actor_dump_does_not_gain_an_identity_null_field() -> None:
    actor = ChannelActor(provider="feishu", subject="ou-1", conversation="oc-1")

    assert actor.model_dump(mode="json") == {
        "provider": "feishu",
        "subject": "ou-1",
        "conversation": "oc-1",
    }


@pytest.mark.parametrize(
    "identity",
    [
        {
            **_structured_identity(),
            "tenant_id": "attacker-selected-tenant",
        },
        {
            **_structured_identity(),
            "provider_account_key": "attacker-selected-app",
        },
        {
            **_structured_identity(),
            "app_id": "attacker-selected-app",
        },
        {
            **_structured_identity(),
            "principal_id": "attacker-selected-principal",
        },
        {
            **_structured_identity(),
            "role": "owner",
        },
        {
            **_structured_identity(),
            "scopes": ["admin"],
        },
        {
            **_structured_identity(),
            "audience": "internal-resource",
        },
        {
            **_structured_identity(),
            "confirmed": True,
        },
        {
            **_structured_identity(),
            "access_token": "attacker-token",
        },
        {
            **_structured_identity(),
            "identifiers": [
                {
                    "kind": "open_id",
                    "value": "ou-external",
                    "tenant_id": "attacker-selected-tenant",
                }
            ],
        },
        {
            **_structured_identity(),
            "identifiers": [
                {"kind": "open_id", "value": "ou-one"},
                {"kind": "open_id", "value": "ou-two"},
            ],
        },
        {
            **_structured_identity(),
            "identifiers": [
                {"kind": "open_id", "value": "ou-one"},
                {"kind": "open_id ", "value": "ou-two"},
            ],
        },
        {
            **_structured_identity(),
            "identifiers": [],
        },
        {
            **_structured_identity(),
            "identifiers": [{"kind": f"provider_id_{index}", "value": f"external-{index}"} for index in range(9)],
        },
        {
            **_structured_identity(),
            "identifiers": [{"kind": "k" * 65, "value": "external"}],
        },
        {
            **_structured_identity(),
            "identifiers": [{"kind": "open_id", "value": "v" * 256}],
        },
        {
            **_structured_identity(),
            "provider": "p" * 65,
        },
        {
            **_structured_identity(),
            "provider_tenant_key": "t" * 256,
        },
    ],
    ids=[
        "tenant",
        "provider-account",
        "app-id",
        "principal",
        "role",
        "scopes",
        "audience",
        "confirmation",
        "token",
        "identifier-extra",
        "duplicate-kind",
        "kind-whitespace",
        "identifiers-empty",
        "identifiers-over-limit",
        "kind-over-limit",
        "value-over-limit",
        "provider-over-limit",
        "provider-tenant-over-limit",
    ],
)
def test_internal_route_rejects_identity_authority_smuggling_and_duplicate_kinds(
    client,
    identity: dict[str, object],
) -> None:
    client.app.dependency_overrides[require_channel_workload] = lambda: WorkloadIdentity(subject="runner-unit")
    client.app.dependency_overrides[get_channel_execution_service] = lambda: _RouteService()
    payload = _payload()
    payload["actor"] = {**payload["actor"], "identity": identity}

    response = client.post(
        "/api/v1/internal/channel-bindings/binding-1/executions",
        headers={"Idempotency-Key": "evt-1"},
        json=payload,
    )

    assert response.status_code == 422


def test_internal_route_accepts_unknown_identifier_kind_for_later_provider_resolution(client) -> None:
    class _UnknownKindRouteService(_RouteService):
        async def execute(
            self,
            *,
            binding_id: str,
            workload: WorkloadIdentity,
            command: ChannelExecutionCommand,
        ) -> AsyncIterator[ExecutionEvent]:
            assert command.actor.identity is not None
            assert command.actor.identity.identifiers[0].kind == "provider_future_id"
            return await super().execute(
                binding_id=binding_id,
                workload=workload,
                command=command,
            )

    client.app.dependency_overrides[require_channel_workload] = lambda: WorkloadIdentity(subject="runner-unit")
    client.app.dependency_overrides[get_channel_execution_service] = lambda: _UnknownKindRouteService()
    payload = _payload()
    payload["actor"] = {
        **payload["actor"],
        "identity": {
            "provider": "feishu",
            "identifiers": [{"kind": "provider_future_id", "value": "future-external"}],
        },
    }

    response = client.post(
        "/api/v1/internal/channel-bindings/binding-1/executions",
        headers={"Idempotency-Key": "evt-1"},
        json=payload,
    )

    assert response.status_code == 200


def test_internal_route_rejects_mismatched_legacy_and_structured_providers(client) -> None:
    client.app.dependency_overrides[require_channel_workload] = lambda: WorkloadIdentity(subject="runner-unit")
    client.app.dependency_overrides[get_channel_execution_service] = lambda: _RouteService()
    payload = _payload()
    payload["actor"] = {
        **payload["actor"],
        "identity": {**_structured_identity(), "provider": "dingtalk"},
    }

    response = client.post(
        "/api/v1/internal/channel-bindings/binding-1/executions",
        headers={"Idempotency-Key": "evt-1"},
        json=payload,
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "field",
    [
        "tenant_id",
        "target_id",
        "principal_id",
        "provider_account_key",
        "app_id",
        "role",
        "scopes",
        "audience",
        "confirmed",
        "access_token",
    ],
)
def test_internal_route_rejects_actor_level_authority_smuggling(
    client,
    field: str,
) -> None:
    client.app.dependency_overrides[require_channel_workload] = lambda: WorkloadIdentity(subject="runner-unit")
    client.app.dependency_overrides[get_channel_execution_service] = lambda: _RouteService()
    payload = _payload()
    payload["actor"] = {**payload["actor"], field: "attacker-selected"}

    response = client.post(
        "/api/v1/internal/channel-bindings/binding-1/executions",
        headers={"Idempotency-Key": "evt-1"},
        json=payload,
    )

    assert response.status_code == 422


def test_structured_identity_does_not_make_legacy_subject_optional(client) -> None:
    client.app.dependency_overrides[require_channel_workload] = lambda: WorkloadIdentity(subject="runner-unit")
    client.app.dependency_overrides[get_channel_execution_service] = lambda: _RouteService()
    payload = _payload()
    payload["actor"] = {
        "provider": "feishu",
        "conversation": "oc-1",
        "identity": _structured_identity(),
    }

    response = client.post(
        "/api/v1/internal/channel-bindings/binding-1/executions",
        headers={"Idempotency-Key": "evt-1"},
        json=payload,
    )

    assert response.status_code == 422


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


def test_identity_provider_registry_cold_start_is_atomic_across_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.channel_execution import dependencies

    callers = 12
    barrier = Barrier(callers)
    session_factory = object()
    registry_builds = 0
    provider_builds = 0

    class _Provider:
        async def resolve(self, context, assertion):
            del context, assertion
            raise AssertionError("not exercised")

        async def refresh(self, context, provider_user_id):
            del context, provider_user_id
            raise AssertionError("not exercised")

    def _build(_session_factory: object) -> IdentityProviderRegistry:
        nonlocal registry_builds, provider_builds
        assert _session_factory is session_factory
        registry_builds += 1

        def _provider_factory() -> _Provider:
            nonlocal provider_builds
            provider_builds += 1
            # Widen the cold-miss window so this asserts synchronization,
            # rather than accidentally passing under the GIL.
            sleep(0.02)
            return _Provider()

        return IdentityProviderRegistry({"feishu": _provider_factory})

    dependencies._reset_identity_provider_registry_for_testing()
    monkeypatch.setattr(
        dependencies,
        "_require_async_session_factory",
        lambda: session_factory,
    )
    monkeypatch.setattr(dependencies, "_build_identity_provider_registry", _build)

    def _cold_get() -> tuple[int, int]:
        barrier.wait()
        registry = dependencies.get_identity_provider_registry()
        provider = registry.get("feishu")
        assert provider is not None
        return id(registry), id(provider)

    try:
        with ThreadPoolExecutor(max_workers=callers) as executor:
            identities = list(executor.map(lambda _index: _cold_get(), range(callers)))

        assert len({registry_id for registry_id, _ in identities}) == 1
        assert len({provider_id for _, provider_id in identities}) == 1
        assert registry_builds == 1
        assert provider_builds == 1
    finally:
        dependencies._reset_identity_provider_registry_for_testing()


@pytest.mark.parametrize("enabled", [False, True])
def test_enterprise_subject_composition_is_explicitly_feature_gated(
    monkeypatch: pytest.MonkeyPatch,
    enabled: bool,
) -> None:
    from api.channel_execution import dependencies

    config = EnterpriseSubjectResolutionConfig(enabled=enabled)
    monkeypatch.setattr(
        dependencies,
        "get_app_config",
        lambda: SimpleNamespace(
            identity=SimpleNamespace(enterprise_subject_resolution=config),
        ),
    )

    service = dependencies._build_enterprise_subject_service(async_sessionmaker())

    if enabled:
        assert isinstance(service, EnterpriseSubjectService)
    else:
        assert service is None


def test_unknown_enterprise_subject_resolver_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from api.channel_execution import dependencies

    config = EnterpriseSubjectResolutionConfig(
        enabled=True,
        resolver="unregistered_resolver",
    )
    monkeypatch.setattr(
        dependencies,
        "get_app_config",
        lambda: SimpleNamespace(
            identity=SimpleNamespace(enterprise_subject_resolution=config),
        ),
    )

    with pytest.raises(ChannelStateUnavailableError):
        dependencies._build_enterprise_subject_service(async_sessionmaker())
