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
                "context": {"modes": ["query"], "max_budget_tokens": 32768, "max_candidates": 100, "token_count_is_estimate": True, "mutates_memory": False},
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
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
) -> JSONResponse:
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
