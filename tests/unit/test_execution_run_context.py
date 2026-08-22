"""Contracts for immutable Principal propagation through one execution run."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from agent.canvas import Graph
from agent.component.agent_with_tools import Agent, AgentParam
from agent.component.llm import LLM
from api.identity.contracts import (
    ExternalIdentityRecord,
    IdentityResolutionResult,
    IdentityResolutionStatus,
    UserMembershipRecord,
)
from api.identity.principal import (
    AuthenticationContext,
    AuthenticationSource,
    IdentityAssurance,
    Principal,
    build_principal_from_resolved_identity,
)
from api.identity.run_context import RunContext


def _principal(*, tenant_id: str = "tenant-secret", user_id: str = "user-secret") -> Principal:
    verified_at = datetime(2026, 8, 13, tzinfo=UTC)
    return build_principal_from_resolved_identity(
        result=IdentityResolutionResult(
            status=IdentityResolutionStatus.RESOLVED,
            identity=ExternalIdentityRecord(
                id="identity-secret",
                tenant_id=tenant_id,
                user_id=user_id,
                provider="feishu",
                provider_tenant_key="provider-tenant-secret",
                subject_type="user_id",
                subject_value="provider-user-secret",
                state="active",
                verified_at=verified_at,
                last_seen_at=verified_at,
                identity_revision=1,
            ),
            membership=UserMembershipRecord(
                user_id=user_id,
                tenant_id=tenant_id,
                role="normal",
            ),
        ),
        authentication=AuthenticationContext(
            source=AuthenticationSource.ENTERPRISE_IDENTITY,
            assurance=IdentityAssurance.DIRECTORY_VERIFIED,
            validated_at=verified_at,
            assurance_verified_at=verified_at,
            provider="feishu",
            external_identity_id="identity-secret",
        ),
    )


def test_run_context_is_immutable_consistent_and_repr_safe() -> None:
    principal = _principal()
    context = RunContext(tenant_id=principal.tenant_id, principal=principal)

    assert context.platform_user_id == "user-secret"
    assert context.principal is principal
    rendered = repr(context)
    assert "tenant-secret" not in rendered
    assert "user-secret" not in rendered
    assert "provider-user-secret" not in rendered

    with pytest.raises(ValueError, match="inconsistent"):
        RunContext(tenant_id="other-tenant", principal=principal)
    with pytest.raises((AttributeError, TypeError)):
        context.tenant_id = "other-tenant"  # type: ignore[misc]


def test_explicit_no_link_context_has_no_platform_user() -> None:
    context = RunContext(tenant_id="tenant-secret", principal=None)

    assert context.platform_user_id is None
    assert "tenant-secret" not in repr(context)


def test_graph_exposes_run_context_before_component_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = _principal()
    run_context = RunContext(tenant_id=principal.tenant_id, principal=principal)
    observed: list[RunContext | None] = []

    class _Param:
        def update(self, values: dict[str, object]) -> None:
            self.values = values

        def check(self) -> None:
            return None

        def __str__(self) -> str:
            return "{}"

    class _Component:
        component_name = "Probe"

        def __init__(self, graph: Graph, _component_id: str, _param: _Param) -> None:
            observed.append(graph.get_run_context())

        def __str__(self) -> str:
            return json.dumps({"component_name": "Probe", "params": {}})

    def _component_class(name: str) -> type[_Param] | type[_Component]:
        return _Param if name.endswith("Param") else _Component

    monkeypatch.setattr("agent.canvas.component_class", _component_class)
    graph = Graph(
        json.dumps(
            {
                "components": {
                    "probe": {
                        "obj": {"component_name": "Probe", "params": {}},
                        "downstream": [],
                        "upstream": [],
                    }
                },
                "path": [],
            }
        ),
        tenant_id=principal.tenant_id,
        run_context=run_context,
    )

    assert observed == [run_context]
    assert graph.get_run_context() is run_context
    serialized = str(graph)
    assert "user-secret" not in serialized
    assert "provider-user-secret" not in serialized


def test_graph_rejects_run_context_from_another_tenant() -> None:
    principal = _principal()
    run_context = RunContext(tenant_id=principal.tenant_id, principal=principal)

    with pytest.raises(ValueError, match="inconsistent"):
        Graph(
            json.dumps({"components": {}, "path": []}),
            tenant_id="other-tenant",
            run_context=run_context,
        )


def test_agent_passes_the_same_run_context_into_mcp_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    principal = _principal()
    run_context = RunContext(tenant_id=principal.tenant_id, principal=principal)
    canvas = Graph(
        json.dumps({"components": {}, "path": []}),
        tenant_id=principal.tenant_id,
        run_context=run_context,
    )
    canvas.tool_use_callback = lambda *_args, **_kwargs: None
    observed: list[dict[str, object]] = []
    interaction_handler = object()

    class _Provider:
        resource_name = "search-service"

        def is_authorized(self, _tool_name: str) -> bool:
            return True

    provider = _Provider()

    class _DBContext:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *_args: object) -> None:
            return None

    class _ChatModel:
        def bind_tools(self, *_args: object) -> None:
            return None

    class _MCPSession:
        def __init__(
            self,
            _server: object,
            _variables: object,
            _custom_header: object,
            *,
            call_context: object | None = None,
            credential_provider: object | None = None,
            interaction_handler: object | None = None,
            legacy_interaction_tools: frozenset[str] = frozenset(),
            tool_output_schemas: dict[str, dict[str, object]] | None = None,
        ) -> None:
            observed.append(
                {
                    "call_context": call_context,
                    "credential_provider": credential_provider,
                    "interaction_handler": interaction_handler,
                    "legacy_interaction_tools": legacy_interaction_tools,
                    "tool_output_schemas": tool_output_schemas,
                }
            )
            self._mcp_server = _server
            self.delegated_resource_name = "search-service"

        def wait_ready(self, *, timeout: float) -> bool:
            del timeout
            return True

    def _fake_llm_init(self: Agent, graph: Graph, component_id: str, param: AgentParam) -> None:
        self._canvas = graph
        self._id = component_id
        self._param = param
        self.chat_mdl = _ChatModel()
        self.imgs = []

    monkeypatch.setattr(LLM, "__init__", _fake_llm_init)
    monkeypatch.setattr("agent.component.agent_with_tools.db_connection", lambda: _DBContext())
    monkeypatch.setattr(
        "agent.component.agent_with_tools.get_model_config_by_type_and_name",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "agent.component.agent_with_tools.TenantLLMService.llm_id2llm_type",
        lambda *_args, **_kwargs: "chat",
    )
    monkeypatch.setattr(
        "agent.component.agent_with_tools.LLMBundle",
        lambda *_args, **_kwargs: _ChatModel(),
    )
    monkeypatch.setattr(
        "agent.component.agent_with_tools.MCPServerService.get_by_id",
        lambda _db, server_id: SimpleNamespace(id=server_id, variables={}),
    )
    monkeypatch.setattr("agent.component.agent_with_tools.MCPToolCallSession", _MCPSession)
    monkeypatch.setattr(
        "agent.component.agent_with_tools.resolve_mcp_credential_provider",
        lambda **_kwargs: provider,
    )
    monkeypatch.setattr(
        "agent.component.agent_with_tools.resolve_mcp_interaction_handler",
        lambda: interaction_handler,
    )

    param = AgentParam()
    param.llm_id = "fixture-model"
    tool_meta = {
        "name": "search",
        "description": "Search",
        "inputSchema": {"type": "object", "properties": {}},
        "outputSchema": {
            "type": "object",
            "properties": {"result": {"type": "string"}},
        },
        "_meta": {"com.ofmcp/interaction-mode": "ask-before-effect"},
    }
    param.mcp = [
        {"mcp_id": "mcp-1", "tools": {"search": tool_meta}},
        {"mcp_id": "mcp-2", "tools": {"search": tool_meta}},
    ]
    agent = Agent(canvas, "agent-1", param)

    assert observed == [
        {
            "call_context": run_context,
            "credential_provider": provider,
            "interaction_handler": interaction_handler,
            "legacy_interaction_tools": frozenset({"search"}),
            "tool_output_schemas": {"search": tool_meta["outputSchema"]},
        },
        {
            "call_context": run_context,
            "credential_provider": provider,
            "interaction_handler": interaction_handler,
            "legacy_interaction_tools": frozenset({"search"}),
            "tool_output_schemas": {"search": tool_meta["outputSchema"]},
        },
    ]
    assert set(agent.tools) == {"search_0", "search_1"}
    assert {binding.original_name for binding in agent.tools.values()} == {"search"}
    assert {binding.mcp_server_id for binding in agent.tools.values()} == {"mcp-1", "mcp-2"}


def test_run_context_requires_agent_and_published_revision_as_one_server_owned_pair() -> None:
    principal = _principal()

    context = RunContext(
        tenant_id=principal.tenant_id,
        principal=principal,
        agent_id="agent-a",
        agent_revision_id="release-a",
    )
    assert context.agent_id == "agent-a"
    assert context.agent_revision_id == "release-a"

    with pytest.raises(ValueError, match="execution target"):
        RunContext(
            tenant_id=principal.tenant_id,
            principal=principal,
            agent_id="agent-a",
        )
