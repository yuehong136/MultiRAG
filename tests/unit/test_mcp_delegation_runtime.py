"""EIM-P3 application-lifecycle composition contracts."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.identity.mcp_delegation import runtime
from api.identity.mcp_delegation.service import BoundMcpCredentialProvider
from api.identity.run_context import RunContext


def test_agent_resolution_does_not_read_configuration_before_lifespan_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.reset_mcp_delegation_service()
    monkeypatch.setattr(
        runtime,
        "get_app_config",
        lambda: (_ for _ in ()).throw(AssertionError("configuration must not be read")),
    )

    assert runtime.resolve_mcp_credential_provider(mcp_server=object(), run_context=None) is None


def test_lifespan_activation_publishes_one_immutable_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime.reset_mcp_delegation_service()
    observed: list[tuple[object, object]] = []
    provider = object.__new__(BoundMcpCredentialProvider)

    class Service:
        def bind(self, *, mcp_server: object, run_context: object) -> object:
            observed.append((mcp_server, run_context))
            return provider

    service = Service()
    monkeypatch.setattr(
        runtime,
        "get_app_config",
        lambda: SimpleNamespace(
            identity=SimpleNamespace(
                mcp_delegation=SimpleNamespace(enabled=True),
            ),
        ),
    )
    monkeypatch.setattr(runtime, "get_mcp_delegation_service", lambda: service)
    try:
        runtime.activate_mcp_delegation()
        server = object()
        context = RunContext(tenant_id="tenant-a")

        assert (
            runtime.resolve_mcp_credential_provider(
                mcp_server=server,
                run_context=context,
            )
            is provider
        )
        assert observed == [(server, context)]
    finally:
        runtime._active_service = None
