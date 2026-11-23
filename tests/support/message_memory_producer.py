"""Real Message/Canvas consumers with explicit trusted test identities."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from agent.canvas import Canvas
from api.identity.principal import AuthenticatedActor, AuthenticationContext, AuthenticationSource, IdentityAssurance, TenantMembershipEvidence, build_principal_from_authenticated_actor
from api.identity.run_context import RunContext


def memory_run_context(tenant_id: str, user_id: str, *, mcp_read_only: bool = True, agent_id: str | None = None) -> RunContext:
    principal = build_principal_from_authenticated_actor(
        actor=AuthenticatedActor(platform_user_id=user_id),
        membership=TenantMembershipEvidence(platform_user_id=user_id, tenant_id=tenant_id),
        authentication=AuthenticationContext(source=AuthenticationSource.WEB_SESSION, assurance=IdentityAssurance.AUTHENTICATED, validated_at=datetime(2026, 11, 17, tzinfo=UTC)),
    )
    return RunContext(tenant_id=tenant_id, principal=principal, mcp_read_only=mcp_read_only, agent_id=agent_id, agent_revision_id="test-revision" if agent_id is not None else None)


def message_memory_dsl(memory_ids: list[str], *, mode: str = "plain", structured_memory: bool = False, content: str = "Answer", turn: int = 0) -> dict[str, Any]:
    return {
        "components": {
            "begin": {"obj": {"component_name": "Begin", "params": {}}, "downstream": ["Message:answer"], "upstream": []},
            "Message:answer": {
                "obj": {
                    "component_name": "Message",
                    "params": {
                        "content": ["{% if 1 %}" + content + "{% endif %}" if mode == "template" else content],
                        "stream": mode != "plain",
                        "memory_ids": memory_ids,
                        "structured_memory": structured_memory,
                        "user_id": "forged-dsl-subject",
                    },
                },
                "downstream": [],
                "upstream": ["begin"],
                "parent_id": None,
            },
        },
        "globals": {"sys.query": "Question", "sys.user_id": "forged-global-subject", "sys.conversation_turns": turn, "sys.files": [], "sys.history": []},
        "path": [],
        "history": [],
        "retrieval": [],
    }


def message_memory_canvas(
    memory_ids: list[str], *, context: RunContext | None, task_id: str, mode: str = "plain", structured_memory: bool = False, content: str = "Answer", turn: int = 0, agent_id: str = "memory-agent"
) -> Canvas:
    return Canvas(
        json.dumps(message_memory_dsl(memory_ids, mode=mode, structured_memory=structured_memory, content=content, turn=turn)),
        tenant_id=context.tenant_id if context is not None else "legacy-tenant",
        task_id=task_id,
        canvas_id=agent_id,
        run_context=context,
    )


def iteration_memory_canvas(memory_ids: list[str], *, context: RunContext, task_id: str, stream: bool = False) -> Canvas:
    dsl = message_memory_dsl(memory_ids, structured_memory=True)
    child = dsl["components"]["Message:answer"]
    child.update(parent_id="Iteration:outer", upstream=["IterationItem:outer"])
    child["obj"]["params"].update(content=["item:{item}"], stream=stream)
    dsl["components"]["begin"]["downstream"] = ["Iteration:outer"]
    dsl["components"].update(
        {
            "Iteration:outer": {"obj": {"component_name": "Iteration", "params": {"items_ref": "env.items"}}, "upstream": ["begin"], "downstream": []},
            "IterationItem:outer": {"obj": {"component_name": "IterationItem", "params": {}}, "parent_id": "Iteration:outer", "upstream": [], "downstream": ["Message:answer"]},
        }
    )
    dsl["globals"]["env.items"] = ["first", "second"]
    return Canvas(json.dumps(dsl), tenant_id=context.tenant_id, task_id=task_id, canvas_id="memory-agent", run_context=context)
