"""Bounded Message source-operation identity and non-content result metadata."""

from __future__ import annotations

import hashlib
import json
from typing import TYPE_CHECKING, Any

from api.identity.run_context import RunContext
from memory.revisions.domain import RevisionValidationError
from memory.revisions.identifiers import wire_message_identifiers

if TYPE_CHECKING:
    from agent.component.message import Message

_RESULT_FIELDS = frozenset(
    {
        "memory_id",
        "operation_id",
        "kind",
        "status",
        "index_sync_status",
        "queue_state",
        "raw_source_id",
        "extraction_task_id",
        "error_code",
        "requires_reconciliation",
        "can_reconcile",
        "retryable",
        "writer_busy",
        "needs_operator_reconciliation",
    }
)


def source_operation_id(component: Message) -> str:
    """Bind one server execution slot, never create a new ID from changed content."""
    canvas: Any = component._canvas
    context = canvas.get_run_context()
    if not isinstance(context, RunContext) or context.principal is None or not context.platform_user_id:
        raise PermissionError("Structured memory requires an authenticated run.")
    coordinates = (canvas.task_id, canvas._id, component._id)
    if any(not isinstance(value, str) or not value or len(value.encode("utf-8")) > 256 for value in coordinates):
        raise RevisionValidationError("memory_execution_identity_unavailable")
    turn = canvas.globals.get("sys.conversation_turns")
    if type(turn) is not int or turn < 1:
        raise RevisionValidationError("memory_execution_turn_unavailable")
    parents: list[list[str | int]] = []
    visited: set[str] = set()
    parent: Any = component.get_parent()
    while parent is not None:
        if not isinstance(parent._id, str) or parent._id in visited or len(visited) >= 32:
            raise RevisionValidationError("memory_execution_parent_unavailable")
        visited.add(parent._id)
        if parent.component_name.lower() in {"iteration", "loop"}:
            controller = canvas.get_component_obj(parent.get_start())
            index = getattr(controller, "_idx", None)
            if type(index) is not int or index < 1:
                raise RevisionValidationError("memory_execution_iteration_unavailable")
            parents.append([parent._id, index])
        parent = parent.get_parent()
    descriptor = ["message-source", 1, context.tenant_id, context.platform_user_id, *coordinates, turn, parents]
    digest = hashlib.sha256(json.dumps(descriptor, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return "message-source:" + digest


def operation_metadata(operations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Never copy source bodies, credentials or model payloads into node outputs."""
    return wire_message_identifiers([{key: value for key, value in operation.items() if key in _RESULT_FIELDS and (value is None or type(value) in {str, bool, int})} for operation in operations])
