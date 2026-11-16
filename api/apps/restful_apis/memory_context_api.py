"""Optional provenance, paginated memory reads, and bounded query context."""

from __future__ import annotations

import asyncio
import logging
from typing import Literal

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.services import memory_context_service
from api.db.db_models import get_async_db
from api.utils.api_utils import Principal, async_current_user
from common.constants import RetCode
from common.exceptions import NotFoundException
from memory.revisions.domain import RevisionConflict
from memory.services.context import SourceContentChangedError
from memory.services.messages import MessageService, RecentMessageCursorStaleError, RecentMessageReadLimitError, SourceMessageBindingError

router = APIRouter()
logger = logging.getLogger(__name__)
MessageType = Literal["raw", "semantic", "episodic", "procedural"]


class QueryContextRequest(BaseModel):
    """Select existing memory data; query context never edits stored memories."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["query"] = "query"
    query: str = Field(min_length=1, max_length=8192)
    budget_tokens: int = Field(default=2048, ge=128, le=32768, strict=True)
    max_items: int = Field(default=5, ge=1, le=100, strict=True)
    candidate_limit: int = Field(default=20, ge=1, le=100, strict=True)
    source_excerpt_chars: int = Field(default=512, ge=0, le=8192, strict=True)
    include_sources: bool = Field(default=True, strict=True)
    user_id: str | None = Field(default=None, max_length=256)
    agent_id: str | None = Field(default=None, max_length=256)
    session_id: str | None = Field(default=None, max_length=256)
    similarity_threshold: float = Field(default=0.2, ge=0, le=1)
    keywords_similarity_weight: float = Field(default=0.7, ge=0, le=1)

    @field_validator("query")
    @classmethod
    def nonblank_query(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value


def _result(data: object, *, status_code: int = 200, retcode: RetCode = RetCode.SUCCESS, retmsg: str = "success") -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"retcode": int(retcode), "retmsg": retmsg, "data": data})


def _read_error(error: Exception) -> JSONResponse:
    if isinstance(error, NotFoundException):
        return _result(False, status_code=404, retcode=RetCode.NOT_FOUND, retmsg=str(error.msg))
    if isinstance(error, RevisionConflict):
        return _result(False, status_code=409, retcode=RetCode.CONFLICT, retmsg=error.code)
    if isinstance(error, (SourceContentChangedError, RecentMessageCursorStaleError)):
        return _result(False, status_code=409, retcode=RetCode.CONFLICT, retmsg=str(error))
    if isinstance(error, RecentMessageReadLimitError):
        return _result(False, status_code=422, retcode=RetCode.ARGUMENT_ERROR, retmsg=str(error))
    if isinstance(error, ValueError) and not isinstance(error, SourceMessageBindingError):
        return _result(False, status_code=422, retcode=RetCode.ARGUMENT_ERROR, retmsg=str(error))
    logger.error("Memory read failed (%s)", type(error).__name__)
    return _result(False, status_code=500, retcode=RetCode.SERVER_ERROR, retmsg="Internal server error")


@router.get("/memories/{memory_id}/capabilities", summary="Read available memory query capabilities")
async def memory_capabilities(memory_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    try:
        binding = await memory_context_service.require_memory(db, memory_id, actor_id=user.id)
        recent = await asyncio.to_thread(MessageService.get_recent_read_capabilities, tenant_id=binding.tenant_id, memory_id=memory_id)
        return _result(
            {
                "recent_messages": {"filters": ["message_type", "user_id", "agent_id", "session_id", "status"], **recent},
                "sources": {"verification": "provenance_only", "snapshot_only": True, "max_excerpt_chars": 8192, "continuation_requires_authorization": True},
                "structured_sources": {
                    "supported": recent.get("coordinated_writes_supported", False),
                    "schema_version": 1,
                    "max_segments": 32,
                    "max_content_bytes": 65535,
                    "max_content_tokens": 8192,
                    "declared_roles": ["user", "assistant", "tool", "unknown"],
                    "roles_are_caller_declared": True,
                    "submitter_identity_is_authenticated": True,
                    "speaker_identity_is_verified": False,
                    "content_is_verified": False,
                    "automatic_extraction": False,
                    "provenance_default_includes_segments": False,
                    "legacy_roles": "role_unknown",
                    "event_time_is_caller_declared": True,
                    "snapshot_only": True,
                },
                "context": {"modes": ["query"], "max_budget_tokens": 32768, "max_candidates": 100, "token_count_is_estimate": True, "mutates_memory": False},
                "startup_context": {
                    "supported": recent.get("coordinated_writes_supported", False),
                    "request_generated": True,
                    "persistent_cache": False,
                    "model_calls": False,
                    "requires_human_confirmed": True,
                    "scope_is_exact": True,
                    "budget_tokens_min": 1024,
                    "budget_tokens_max": 32768,
                    "max_items": 16,
                    "max_entry_ids": 32,
                    "max_inventory_limit": 128,
                    "physical_read_limit": 256,
                    "version_bound_sources": True,
                    "source_offset_unit": "unicode_codepoint",
                },
                "verification": {
                    "supported": recent.get("coordinated_writes_supported", False),
                    "requires_managed_entry": True,
                    "results": ["human_confirmed", "disputed", "needs_review"],
                    "methods": ["source_comparison", "external_check", "other"],
                    "automatic_verification": False,
                    "mutates_assessment": False,
                    "model_calls": False,
                    "snapshot_only": True,
                    "max_sources": 32,
                    "max_evidence_items": 8,
                    "max_applicability_chars": 1024,
                    "max_notes_chars": 2048,
                    "max_evidence_description_chars": 1024,
                    "max_evidence_reference_chars": 1024,
                    "max_revocation_reason_chars": 1024,
                    "max_history_limit": 50,
                    "max_todo_limit": 20,
                    "todo_scan_limit": 64,
                    "todo_physical_read_limit": 256,
                },
                "maintenance": {
                    "target_recommendations": {
                        "supported": recent.get("coordinated_writes_supported", False),
                        "retrieval_modes": ["hybrid", "lexical"],
                        "max_input_sources": 8,
                        "max_targets": 8,
                        "max_closure_sources": 32,
                        "semantic_limit": 64,
                        "lexical_scan_limit": 128,
                        "max_budget_tokens": 8192,
                        "max_query_budget_tokens": 4096,
                        "registered_current_only": True,
                        "mutates_memory": False,
                        "automatic_selection": False,
                    },
                    "supported": recent.get("coordinated_writes_supported", False),
                    "modes": ["dream"],
                    "candidate_review_required": True,
                    "actions": ["add", "patch", "noop"],
                    "requires_registered_sources": True,
                    "automatic_publication": False,
                    "execution": "persistent_background",
                    "worker_required": True,
                    "automatic_scheduling": False,
                    "operation_inbox": True,
                    "max_sources": 32,
                    "max_candidates": 32,
                    "max_publication_candidates": 16,
                    "discoveries": {
                        "supported": recent.get("coordinated_writes_supported", False),
                        "registered_active_only": True,
                        "max_sources": 256,
                        "max_batch_sources": 32,
                        "max_runs_per_request": 8,
                        "expected_preview_hash_required": True,
                        "token_fit": "not_checked",
                        "segmentation": {
                            "supported": recent.get("coordinated_writes_supported", False),
                            "default_mode": "whole",
                            "splitter_version": "unicode-boundary-v1",
                            "fragment_token_limit": {"min": 256, "max": 8192, "default": 2048},
                            "max_parts_per_source": {"min": 1, "max": 64, "default": 32},
                            "max_total_fragments": 256,
                            "context_codepoints": 64,
                            "offset_unit": "unicode_codepoint",
                        },
                    },
                },
                "revisions": {
                    "supported": recent.get("managed_revisions_supported", False),
                    "managed": recent.get("managed", False),
                    "activation_scope": "memory_bank",
                    "ordinary_writes_after_activation": recent.get("coordinated_writes_supported", False),
                    "coordinated_state_changes": True,
                    "managed_delete_supported": recent.get("managed_delete_supported", False),
                    "adoption_required": True,
                    "max_content_bytes": 65535,
                    "max_content_tokens": 8192,
                    "max_reason_chars": 1024,
                    "manual_assessment": "user_asserted",
                    "publication_state": "operation",
                    "history": True,
                },
                "source_withdrawals": {"supported": recent.get("managed_revisions_supported", False), "requires_registered_source": True, "permanent": True, "supply_and_cleanup_separate": True},
                "writes": {
                    "supported": recent.get("coordinated_writes_supported", False),
                    "append": recent.get("coordinated_writes_supported", False),
                    "automatic_extraction": recent.get("coordinated_writes_supported", False),
                    "fifo": recent.get("coordinated_writes_supported", False),
                    "publication_state": "operation",
                    "queue_replay": "stable_task_id",
                    "untracked_legacy_tasks": "operator_attestation",
                },
                "deletions": {
                    "supported": recent.get("managed_delete_supported", False),
                    "requires_writer_drain": True,
                    "audit_tombstone": True,
                    "physical_erasure_certified": False,
                    "publication_state": "operation",
                },
            }
        )
    except Exception as error:
        return _read_error(error)


@router.get("/memories/{memory_id}/messages", summary="Read a stable page of matching memory messages")
async def recent_messages(
    memory_id: str,
    limit: int = Query(default=10, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=2048),
    message_type: MessageType | None = None,
    user_id: str | None = Query(default=None, max_length=256),
    agent_id: str = Query(default="", max_length=256),
    session_id: str = Query(default="", max_length=256),
    status: bool | None = None,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
) -> JSONResponse:
    try:
        result = await memory_context_service.recent_messages(
            db, memory_id, actor_id=user.id, limit=limit, cursor=cursor, message_type=message_type, subject_id=user_id, agent_id=agent_id, session_id=session_id, status=status
        )
        return _result(result)
    except Exception as error:
        return _read_error(error)


@router.get("/memories/{memory_id}/sources/{source_id}", summary="Read an authorized bounded raw-source excerpt")
async def source_excerpt(
    memory_id: str,
    source_id: int = Path(ge=1),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=512, ge=1, le=8192),
    content_sha256: str | None = Query(default=None, pattern="^[0-9a-f]{64}$"),
    user_id: str | None = Query(default=None, max_length=256),
    agent_id: str | None = Query(default=None, max_length=256),
    session_id: str | None = Query(default=None, max_length=256),
    expected_source_revision: int | None = Query(default=None, ge=1, le=2**63 - 1),
    expected_source_generation: int | None = Query(default=None, ge=1, le=2**63 - 1),
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
) -> JSONResponse:
    version_bound = expected_source_revision is not None or expected_source_generation is not None
    if version_bound:
        from api.apps.restful_apis.memory_revision_api import _command_error
        from api.apps.restful_apis.memory_startup_api import no_store
        from api.apps.services.memory_startup_service import startup
        from memory.revisions.domain import RevisionValidationError

        try:
            if expected_source_revision is None or expected_source_generation is None or content_sha256 is None or user_id is None or agent_id is None or session_id is None:
                raise RevisionValidationError("startup_source_preconditions_incomplete")
            bound_result = await (await startup(db, memory_id, actor_id=user.id)).source_excerpt(
                memory_id,
                source_id,
                expected_source_revision=expected_source_revision,
                expected_source_generation=expected_source_generation,
                content_sha256=content_sha256,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                offset=offset,
                limit=limit,
            )
            return no_store(_result(bound_result))
        except Exception as error:
            return no_store(_command_error(error))
    try:
        result = await memory_context_service.source_excerpt(
            db, memory_id, source_id, actor_id=user.id, offset=offset, limit=limit, content_sha256=content_sha256, subject_id=user_id, agent_id=agent_id, session_id=session_id
        )
        return _result(result)
    except Exception as error:
        return _read_error(error)


@router.post("/memories/{memory_id}/context", summary="Build read-only query context within an estimated token budget")
async def query_context(memory_id: str, request_body: QueryContextRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    options = request_body.model_dump(exclude={"mode", "query", "user_id"})
    try:
        result = await memory_context_service.query_context(db, memory_id, request_body.query, actor_id=user.id, subject_id=request_body.user_id, **options)
        return _result(result)
    except Exception as error:
        return _read_error(error)
