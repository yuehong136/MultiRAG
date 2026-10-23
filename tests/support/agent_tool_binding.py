"""Controlled model/DB seams shared by constructor and real MCP probes.

The Agent, Canvas, delegation policy and tool router remain production code.
Only the model, native tool and configuration reads are replaced here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import ExitStack, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

from agent.canvas import Canvas
from agent.component import agent_with_tools, llm
from agent.component.agent_with_tools import Agent, AgentParam
from api.identity.mcp_delegation.contracts import DelegatedServerBinding, DelegatedToolPolicy, DelegationGrant, GrantPolicySnapshot, ToolPolicySnapshot
from api.identity.mcp_delegation.service import McpDelegationService
from api.identity.mcp_issuer.contracts import IssuedMcpAccessToken, McpAccessGrant, McpAccessTokenRequest
from api.identity.principal import AuthenticatedActor, AuthenticationContext, AuthenticationSource, IdentityAssurance, TenantMembershipEvidence, build_principal_from_authenticated_actor
from api.identity.run_context import RunContext

CANONICAL_TOOLS = {"a": ("compat_echo_2", "compat_echo", "compat_echo_0", "compat_fail"), "b": ("compat_echo", "compat_fail")}


class RecordingIssuer:
    def __init__(self, servers: dict[str, SimpleNamespace]) -> None:
        self.audiences = {f"resource-{key}": server.url for key, server in servers.items()}
        self.requests: list[McpAccessTokenRequest] = []

    def resource_audience(self, resource_name: str) -> str | None:
        return self.audiences.get(resource_name)

    def issue(self, request: McpAccessTokenRequest, grant: McpAccessGrant) -> IssuedMcpAccessToken:
        assert request.requested_scopes.issubset(grant.allowed_scopes)
        self.requests.append(request)
        server_id = request.resource_name.removeprefix("resource-")
        return IssuedMcpAccessToken(
            compact=f"fixture-{server_id}-{request.principal.platform_user_id}",
            expires_at=datetime.now(UTC) + timedelta(minutes=5),
            scopes=request.requested_scopes,
            kid="loopback-fixture",
        )


def delegation_service(servers: dict[str, SimpleNamespace]) -> tuple[McpDelegationService, RecordingIssuer]:
    issuer = RecordingIssuer(servers)
    tools = {}
    for name in set().union(*CANONICAL_TOOLS.values()):
        tools[name] = DelegatedToolPolicy(
            canonical_tool_name=name,
            required_scopes=frozenset({"probe:write" if name == "compat_fail" else "probe:read"}),
            effect="side_effect" if name == "compat_fail" else "read",
            replay_mode="single_use" if name == "compat_fail" else "reusable",
            enterprise_subject=None,
            accepted_acr_values=frozenset(),
            required_amr=frozenset(),
        )
    grants = {}
    for user in ("alice", "bob"):
        for server_id in servers:
            resource = f"resource-{server_id}"
            key = ("tenant-probe", user, "agent-probe", "release-probe", resource)
            grants[key] = DelegationGrant(
                tenant_id=key[0],
                platform_user_id=user,
                agent_id=key[2],
                agent_revision_id=key[3],
                resource_name=resource,
                allowed_scopes=frozenset() if user == "bob" and server_id == "a" else frozenset({"probe:read"}),
            )
    return McpDelegationService(
        tool_policy=ToolPolicySnapshot.frozen(policy_revision="binding-probe", scope_registry=frozenset({"probe:read", "probe:write"}), tools=tools),
        grant_policy=GrantPolicySnapshot.frozen(
            grant_revision="binding-grants",
            policy_revision="binding-probe",
            credential_generation=1,
            bindings={key: DelegatedServerBinding(key, f"resource-{key}", server.url) for key, server in servers.items()},
            grants=grants,
        ),
        issuer=issuer,
    ), issuer


@dataclass
class RegisteredAgent:
    agent: Agent
    model: Mock
    native: Mock
    callback: Mock


@contextmanager
def registered_agent(
    servers: dict[str, SimpleNamespace],
    metadata: dict[str, dict[str, Any]],
    service: McpDelegationService,
    *,
    user: str = "alice",
) -> Iterator[RegisteredAgent]:
    principal = build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(platform_user_id=user),
        membership=TenantMembershipEvidence(platform_user_id=user, tenant_id="tenant-probe"),
        authentication=AuthenticationContext(source=AuthenticationSource.WEB_SESSION, assurance=IdentityAssurance.AUTHENTICATED, validated_at=datetime.now(UTC)),
    )
    context = RunContext(tenant_id="tenant-probe", principal=principal, agent_id="agent-probe", agent_revision_id="release-probe")
    canvas = Canvas('{"components":{},"retrieval":{"chunks":[],"doc_aggs":[]}}', tenant_id="tenant-probe", run_context=context)
    model, native, callback = Mock(), Mock(), Mock()
    native.get_meta.return_value = {"type": "function", "function": {"name": "compat_echo", "description": "native", "parameters": {"type": "object"}}}
    native.invoke.return_value = "native-result"
    canvas.tool_use_callback = callback
    param = AgentParam()
    param.llm_id = "controlled-model"
    param.tools = [{"component_name": "controlled-native"}]
    param.mcp = [{"mcp_id": key, "tools": tools} for key, tools in metadata.items()]
    # Capture even sessions created before a constructor failure, for cleanup.
    created_sessions = []
    session_class = agent_with_tools.MCPToolCallSession

    def create_session(*args: Any, **kwargs: Any) -> Any:
        session = session_class(*args, **kwargs)
        created_sessions.append(session)
        return session

    try:
        with ExitStack() as stack:
            for module in (llm, agent_with_tools):
                stack.enter_context(patch.object(module, "db_connection", side_effect=lambda: nullcontext(object())))
                stack.enter_context(patch.object(module, "resolve_model_type", return_value=["chat"]))
                stack.enter_context(patch.object(module, "get_model_config_by_type_and_name", return_value={"id": "controlled-model"}))
                stack.enter_context(patch.object(module, "LLMBundle", return_value=model))
            stack.enter_context(patch.object(Agent, "_load_tool_obj", return_value=native))
            stack.enter_context(patch.object(agent_with_tools.MCPServerService, "get_by_id", side_effect=lambda _db, server_id: servers[server_id]))
            stack.enter_context(patch.object(agent_with_tools, "resolve_mcp_credential_provider", side_effect=service.bind))
            stack.enter_context(patch.object(agent_with_tools, "resolve_mcp_interaction_handler", return_value=None))
            stack.enter_context(patch.object(agent_with_tools, "MCPToolCallSession", side_effect=create_session))
            yield RegisteredAgent(Agent(canvas, "binding-probe", param), model, native, callback)
    finally:
        for session in created_sessions:
            session.close_sync(timeout=3)
        canvas._thread_pool.shutdown(wait=True)


def assert_registration(registered: RegisteredAgent, metadata: dict[str, dict[str, Any]]) -> dict[tuple[str, str], str]:
    """Assert contracts without depending on the chosen numeric suffixes."""
    from common.mcp_tool_call_conn import MCPToolBinding, mcp_tool_metadata_to_openai_tool

    agent = registered.agent
    names = [tool["function"]["name"] for tool in agent.tool_meta]
    assert len(names) == len(set(names)) == len(agent.tools)
    assert set(names) == set(agent.tools)
    assert agent.tools["compat_echo_0"] is registered.native
    assert registered.native.get_meta()["function"]["name"] == "compat_echo"
    registered.model.bind_tools.assert_called_once_with(agent.toolcall_session, agent.tool_meta)
    aliases = {}
    for alias, binding in agent.tools.items():
        if not isinstance(binding, MCPToolBinding):
            continue
        aliases[(binding.mcp_server_id, binding.original_name)] = alias
        assert binding.resource_name == f"resource-{binding.mcp_server_id}"
        assert binding.original_name != "compat_fail"
        original = metadata[binding.mcp_server_id][binding.original_name]
        exported = next(tool for tool in agent.tool_meta if tool["function"]["name"] == alias)
        assert exported == mcp_tool_metadata_to_openai_tool(original, function_name=alias)
    return aliases
