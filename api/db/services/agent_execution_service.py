"""Trusted Web execution targets and immutable session provenance.

Keep published identifiers separate from development snapshots. Runtime state
can evolve; a session never replaces its original executable configuration.
"""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import AgentExecutionOrigin, API4Conversation
from api.identity.principal import (
    AuthenticatedActor,
    AuthenticationSource,
    Principal,
    TenantMembershipEvidence,
    build_principal_from_authenticated_actor,
)
from api.identity.run_context import DraftExecutionTarget, RunContext


def snapshot_digest(dsl: dict[str, Any]) -> str:
    encoded = json.dumps(dsl, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def principal_for_resource_tenant(principal: Principal, tenant_id: str) -> Principal:
    """Preserve caller authentication after proving live resource membership."""
    if principal.tenant_id != tenant_id:
        if principal.authentication.source not in {AuthenticationSource.WEB_SESSION, AuthenticationSource.SDK_API_TOKEN}:
            raise PermissionError("Run identity does not belong to the resource tenant.")
        principal = build_principal_from_authenticated_actor(
            actor=AuthenticatedActor(principal.platform_user_id, principal.display_name),
            membership=TenantMembershipEvidence(principal.platform_user_id, tenant_id),
            authentication=principal.authentication,
        )
    return principal


def execution_context(*, principal: Principal, tenant_id: str, agent_id: str, dsl: str | dict[str, Any], published_revision_id: str | None = None) -> RunContext:
    """Call only after proving live ownership/membership in the resource tenant."""
    principal = principal_for_resource_tenant(principal, tenant_id)
    if published_revision_id is not None:
        return RunContext(tenant_id=tenant_id, principal=principal, agent_id=agent_id, agent_revision_id=published_revision_id, mcp_read_only=True)
    snapshot = json.loads(dsl) if isinstance(dsl, str) else dsl
    return RunContext(tenant_id=tenant_id, principal=principal, draft_target=DraftExecutionTarget(agent_id, snapshot_digest(snapshot)), mcp_read_only=True)


def restore_execution_snapshot(snapshot: dict[str, Any], runtime_dsl: str | dict[str, Any]) -> str:
    """Restore immutable configuration while retaining server-persisted values.

    Component definitions, routing and parameter configuration always come from
    the origin. Only conversation state and input/output values come from the
    runtime DSL; titles and a later release are never authorization evidence.
    """
    runtime = json.loads(runtime_dsl) if isinstance(runtime_dsl, str) else runtime_dsl
    restored = copy.deepcopy(snapshot)
    for key in ("path", "history", "retrieval", "memory", "globals"):
        if key in runtime:
            restored[key] = copy.deepcopy(runtime[key])
    for component_id, component in restored.get("components", {}).items():
        current = runtime.get("components", {}).get(component_id, {})
        params = component["obj"]["params"]
        runtime_params = current.get("obj", {}).get("params", {})
        outputs = params.setdefault("outputs", {})
        for name, value in runtime_params.get("outputs", {}).items():
            if name not in outputs and isinstance(value, dict) and "value" in value:
                outputs[name] = {"type": value.get("type", "string"), "value": None}
        for key in ("inputs", "outputs"):
            for name, definition in params.get(key, {}).items():
                value = runtime_params.get(key, {}).get(name)
                if isinstance(definition, dict) and isinstance(value, dict) and "value" in value:
                    definition["value"] = copy.deepcopy(value["value"])
    return json.dumps(restored, ensure_ascii=False)


async def save_agent_session(db: AsyncSession, conversation: dict[str, Any], *, context: RunContext, snapshot: dict[str, Any]) -> dict[str, Any]:
    """Persist session and its original executable snapshot in one transaction."""
    if context.principal is None or (context.agent_id is None and context.draft_target is None):
        raise PermissionError("Session execution context is incomplete.")
    if context.agent_id is not None:
        agent_id = context.agent_id
    else:
        assert context.draft_target is not None
        agent_id = context.draft_target.agent_id
    if conversation["dialog_id"] != agent_id:
        raise PermissionError("Session execution target is inconsistent.")
    try:
        row = API4Conversation(**conversation)
        db.add(row)
        # Explicit ordering works without an ORM relationship and keeps both rows
        # in the same transaction. A failed origin insert rolls the session back.
        await db.flush()
        db.add(
            AgentExecutionOrigin(
                id=row.id,
                tenant_id=context.tenant_id,
                platform_user_id=context.principal.platform_user_id,
                agent_id=agent_id,
                execution_mode="published" if context.agent_revision_id is not None else "draft",
                agent_revision_id=context.agent_revision_id,
                snapshot_digest=snapshot_digest(snapshot),
                snapshot_dsl=copy.deepcopy(snapshot),
            )
        )
        await db.commit()
        await db.refresh(row)
        return row.to_dict()
    except BaseException:
        await db.rollback()
        raise
