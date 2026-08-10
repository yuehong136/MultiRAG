"""Channel-owned branching over MultiRAG Dialog and Canvas sessions.

The MultiRAG completion services intentionally know nothing about Channel
operations.  This module gives Channel executions copy-on-write semantics:
run against a private candidate session, then atomically promote it only after
the target stream completed successfully.
"""

from __future__ import annotations

import hashlib
import json
import time
from copy import deepcopy
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.channel_execution.errors import TargetExecutionFailedError
from api.channel_execution.models import ExecutionOperation
from api.channel_execution.reasoning import strip_reasoning
from api.channel_execution.session_models import PreparedCanvasExecution, PreparedDialogExecution
from api.db.db_models import API4Conversation, Conversation
from common.misc_utils import get_uuid

_REGENERATE_SAFE_COMPONENTS = {
    "A2UI",
    "Agent",
    "Begin",
    "Categorize",
    "DataOperations",
    "ExitLoop",
    "HTMLReport",
    "Iteration",
    "IterationItem",
    "LLM",
    "ListOperations",
    "Loop",
    "LoopItem",
    "Message",
    "Retrieval",
    "StringTransform",
    "Switch",
    "VariableAggregator",
    "VariableAssigner",
}

_CANDIDATE_NAME = "[channel-candidate]"
_CANDIDATE_MAX_AGE_MS = 24 * 60 * 60 * 1000


def _sanitize_messages(messages: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    sanitized = deepcopy(messages or [])
    for message in sanitized:
        if message.get("role") != "assistant":
            continue
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = strip_reasoning(content)
    return sanitized


def _rewind_latest_turn(
    messages: list[dict[str, Any]],
    references: list[dict[str, Any]] | None,
    question: str,
) -> None:
    """Remove exactly one matching completed tail turn."""

    if len(messages) < 2:
        raise LookupError("No completed turn is available for regeneration")
    user_message, assistant_message = messages[-2:]
    if user_message.get("role") != "user" or assistant_message.get("role") != "assistant" or user_message.get("content") != question:
        raise LookupError("Only the latest matching turn can be regenerated")
    user_id = user_message.get("id")
    assistant_id = assistant_message.get("id")
    if user_id and assistant_id and user_id != assistant_id:
        raise LookupError("The latest conversation turn is inconsistent")
    del messages[-2:]
    if references:
        references.pop()


def _canvas_history(messages: list[dict[str, Any]]) -> list[list[str | dict[str, Any]]]:
    history: list[list[str | dict[str, Any]]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            continue
        a2ui = message.get("a2ui")
        value: str | dict[str, Any]
        if isinstance(a2ui, dict):
            value = {"content": content, "a2ui": deepcopy(a2ui)}
        else:
            value = content
        history.append([role, value])
    return history


def _assert_canvas_regeneration_safe(dsl: dict[str, Any]) -> None:
    components = dsl.get("components")
    if not isinstance(components, dict):
        raise PermissionError("Canvas regeneration requires a supported component graph")
    for component in components.values():
        if not isinstance(component, dict):
            raise PermissionError("Canvas regeneration requires a supported component graph")
        obj = component.get("obj")
        if not isinstance(obj, dict):
            raise PermissionError("Canvas regeneration requires a supported component graph")
        name = obj.get("component_name")
        if not isinstance(name, str) or name not in _REGENERATE_SAFE_COMPONENTS:
            raise PermissionError("Canvas regeneration is unavailable for workflows with external tools")
        params = obj.get("params")
        if name == "Message":
            if not isinstance(params, dict) or params.get("output_format") or params.get("memory_ids"):
                raise PermissionError("Canvas regeneration is unavailable for messages with persistent outputs")
            continue
        if name != "Agent":
            continue
        if not isinstance(params, dict) or params.get("tools") or params.get("mcp"):
            raise PermissionError("Canvas regeneration is unavailable for tool-enabled agents")


def canvas_regeneration_is_safe(dsl: dict[str, Any] | str | None) -> bool:
    """Whether a published Canvas graph is safe to replay from visible history."""

    try:
        parsed = json.loads(dsl) if isinstance(dsl, str) else deepcopy(dsl)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    if not isinstance(parsed, dict):
        return False
    try:
        _assert_canvas_regeneration_safe(parsed)
    except PermissionError:
        return False
    return True


def _prepare_canvas_dsl(
    dsl: dict[str, Any] | str | None,
    messages: list[dict[str, Any]],
    *,
    validate_replay: bool,
) -> dict[str, Any]:
    if isinstance(dsl, str):
        parsed = json.loads(dsl)
    else:
        parsed = deepcopy(dsl)
    if not isinstance(parsed, dict):
        raise ValueError("Canvas DSL must be an object")
    if validate_replay:
        _assert_canvas_regeneration_safe(parsed)

    history = _canvas_history(messages)
    parsed["history"] = history
    globals_ = parsed.get("globals")
    if not isinstance(globals_, dict):
        globals_ = {}
        parsed["globals"] = globals_
    globals_["sys.history"] = [f"{role}: {value.get('content', '') if isinstance(value, dict) else value}" for role, value in history]
    globals_["sys.conversation_turns"] = sum(role == "user" for role, _value in history)
    return parsed


def _fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _dialog_fingerprint(row: Conversation) -> str:
    return _fingerprint(
        {
            "message": row.message or [],
            "reference": row.reference or [],
        }
    )


def _canvas_fingerprint(row: API4Conversation) -> str:
    return _fingerprint(
        {
            "message": row.message or [],
            "reference": row.reference,
            "dsl": row.dsl,
            "round": row.round,
        }
    )


def _has_visible_assistant(messages: list[dict[str, Any]]) -> bool:
    if not messages or messages[-1].get("role") != "assistant":
        return False
    content = messages[-1].get("content")
    return isinstance(content, str) and bool(strip_reasoning(content))


class SqlAlchemyCanvasHistoryTransaction:
    """Canvas-owned candidate transaction around MultiRAG completion."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def prepare(
        self,
        *,
        target_id: str,
        session_id: str | None,
        question: str,
        operation: ExecutionOperation,
    ) -> PreparedCanvasExecution:
        await _prune_stale_candidates(self._db, API4Conversation, target_id)
        if session_id is None:
            if operation == "regenerate":
                raise LookupError("A session is required for regeneration")
            return PreparedCanvasExecution(None, None, None)

        source = await self._db.get(API4Conversation, session_id)
        if source is None or source.dialog_id != target_id:
            raise LookupError("Session not found")
        messages = _sanitize_messages(source.message)
        references = deepcopy(source.reference)
        if operation == "regenerate":
            _rewind_latest_turn(messages, None, question)
        dsl = _prepare_canvas_dsl(
            source.dsl,
            messages,
            validate_replay=operation == "regenerate",
        )
        candidate_id = get_uuid()
        candidate = API4Conversation(
            id=candidate_id,
            name=_CANDIDATE_NAME,
            dialog_id=source.dialog_id,
            user_id=candidate_id,
            exp_user_id=candidate_id,
            message=messages,
            reference=references,
            tokens=source.tokens,
            source=source.source,
            dsl=dsl,
            duration=source.duration,
            round=source.round,
            thumb_up=source.thumb_up,
            errors=source.errors,
            version_title=source.version_title,
        )
        self._db.add(candidate)
        await _commit(self._db)
        return PreparedCanvasExecution(
            session_id,
            candidate_id,
            _canvas_fingerprint(source),
        )

    async def commit(
        self,
        prepared: PreparedCanvasExecution,
        generated_session_id: str,
    ) -> str:
        if prepared.execution_session_id and generated_session_id != prepared.execution_session_id:
            raise TargetExecutionFailedError()

        if prepared.public_session_id is None:
            row = await _locked_one(self._db, API4Conversation, generated_session_id)
            messages = _sanitize_messages(row.message)
            if not _has_visible_assistant(messages):
                raise TargetExecutionFailedError()
            row.message = messages
            row.dsl = _prepare_canvas_dsl(row.dsl, messages, validate_replay=False)
            await _commit(self._db)
            return generated_session_id

        public, candidate = await _locked_pair(
            self._db,
            API4Conversation,
            prepared.public_session_id,
            generated_session_id,
        )
        if _canvas_fingerprint(public) != prepared.source_fingerprint:
            raise TargetExecutionFailedError()
        messages = _sanitize_messages(candidate.message)
        if not _has_visible_assistant(messages):
            raise TargetExecutionFailedError()
        public.message = messages
        public.reference = deepcopy(candidate.reference)
        public.dsl = _prepare_canvas_dsl(candidate.dsl, messages, validate_replay=False)
        public.tokens = candidate.tokens
        public.duration = candidate.duration
        public.round = candidate.round
        public.thumb_up = candidate.thumb_up
        public.errors = candidate.errors
        await self._db.delete(candidate)
        await _commit(self._db)
        return prepared.public_session_id

    async def abort(
        self,
        prepared: PreparedCanvasExecution,
        generated_session_id: str | None,
    ) -> None:
        await _abort_candidate(
            self._db,
            API4Conversation,
            prepared.public_session_id,
            prepared.execution_session_id,
            generated_session_id,
        )


class SqlAlchemyDialogHistoryTransaction:
    """Dialog-owned candidate transaction around MultiRAG completion."""

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def prepare(
        self,
        *,
        target_id: str,
        session_id: str | None,
        question: str,
        operation: ExecutionOperation,
    ) -> PreparedDialogExecution:
        await _prune_stale_candidates(self._db, Conversation, target_id)
        if session_id is None:
            if operation == "regenerate":
                raise LookupError("A session is required for regeneration")
            return PreparedDialogExecution(None, None, None, False)

        source = await self._db.get(Conversation, session_id)
        if source is None or source.dialog_id != target_id:
            raise LookupError("Session not found")
        messages = _sanitize_messages(source.message)
        references = deepcopy(source.reference or [])
        if operation == "regenerate":
            _rewind_latest_turn(messages, references, question)
        candidate_id = get_uuid()
        candidate = Conversation(
            id=candidate_id,
            dialog_id=source.dialog_id,
            name=_CANDIDATE_NAME,
            message=messages,
            reference=references,
            user_id=candidate_id,
        )
        self._db.add(candidate)
        await _commit(self._db)
        return PreparedDialogExecution(
            session_id,
            candidate_id,
            _dialog_fingerprint(source),
            True,
        )

    async def commit(
        self,
        prepared: PreparedDialogExecution,
        generated_session_id: str,
    ) -> str:
        if prepared.execution_session_id and generated_session_id != prepared.execution_session_id:
            raise TargetExecutionFailedError()

        if prepared.public_session_id is None:
            row = await _locked_one(self._db, Conversation, generated_session_id)
            messages = _sanitize_messages(row.message)
            if prepared.require_visible_answer and not _has_visible_assistant(messages):
                raise TargetExecutionFailedError()
            row.message = messages
            await _commit(self._db)
            return generated_session_id

        public, candidate = await _locked_pair(
            self._db,
            Conversation,
            prepared.public_session_id,
            generated_session_id,
        )
        if _dialog_fingerprint(public) != prepared.source_fingerprint:
            raise TargetExecutionFailedError()
        messages = _sanitize_messages(candidate.message)
        if prepared.require_visible_answer and not _has_visible_assistant(messages):
            raise TargetExecutionFailedError()
        public.message = messages
        public.reference = deepcopy(candidate.reference)
        await self._db.delete(candidate)
        await _commit(self._db)
        return prepared.public_session_id

    async def abort(
        self,
        prepared: PreparedDialogExecution,
        generated_session_id: str | None,
    ) -> None:
        await _abort_candidate(
            self._db,
            Conversation,
            prepared.public_session_id,
            prepared.execution_session_id,
            generated_session_id,
        )


async def _abort_candidate(
    db: AsyncSession,
    model: type[Any],
    public_session_id: str | None,
    execution_session_id: str | None,
    generated_session_id: str | None,
) -> None:
    candidate_id = execution_session_id or generated_session_id
    if not candidate_id or candidate_id == public_session_id:
        return
    try:
        await db.rollback()
        candidate = await db.get(model, candidate_id)
        if candidate is None:
            return
        await db.delete(candidate)
        await db.commit()
    except Exception:
        await db.rollback()


async def _locked_one(db: AsyncSession, model: type[Any], session_id: str) -> Any:
    stmt = select(model).where(model.id == session_id).with_for_update().execution_options(populate_existing=True)
    row = (await db.scalars(stmt)).one_or_none()
    if row is None:
        raise LookupError("Session not found")
    return row


async def _prune_stale_candidates(
    db: AsyncSession,
    model: type[Any],
    target_id: str,
) -> None:
    cutoff = int(time.time() * 1000) - _CANDIDATE_MAX_AGE_MS
    conditions = [
        model.dialog_id == target_id,
        model.name == _CANDIDATE_NAME,
        model.user_id == model.id,
        model.update_time < cutoff,
    ]
    if model is API4Conversation:
        conditions.append(model.exp_user_id == model.id)
    stmt = delete(model).where(*conditions).execution_options(synchronize_session=False)
    await db.execute(stmt)
    await _commit(db)


async def _locked_pair(
    db: AsyncSession,
    model: type[Any],
    public_session_id: str,
    candidate_session_id: str,
) -> tuple[Any, Any]:
    stmt = select(model).where(model.id.in_([public_session_id, candidate_session_id])).with_for_update().execution_options(populate_existing=True)
    rows = {row.id: row for row in (await db.scalars(stmt)).all()}
    public = rows.get(public_session_id)
    candidate = rows.get(candidate_session_id)
    if public is None or candidate is None:
        raise LookupError("Session not found")
    return public, candidate


async def _commit(db: AsyncSession) -> None:
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise
