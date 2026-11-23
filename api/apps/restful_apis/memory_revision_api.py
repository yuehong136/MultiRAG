"""Explicit memory adoption, revision previews, history, and source withdrawals."""

from __future__ import annotations

import logging
from typing import Annotated, Any, Self

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import get_async_db
from api.identity.principal import AuthenticationSource
from api.utils.api_utils import Principal, async_current_user
from common.constants import RetCode
from common.exceptions import NotFoundException
from memory.revisions.domain import RevisionConflict, RevisionNotFound, RevisionPersistenceUnknown, RevisionValidationError
from memory.revisions.identifiers import wire_message_identifiers

router = APIRouter()
logger = logging.getLogger(__name__)

OperationId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
RevisionNumber = Annotated[int, Field(strict=True, ge=1, le=2**63 - 1)]


class MemoryCommand(BaseModel):
    """Commands never accept tenant, actor, model, or verification overrides."""

    model_config = ConfigDict(extra="forbid")
    operation_id: OperationId


class AdoptEntryRequest(MemoryCommand):
    legacy_message_id: RevisionNumber

    @field_validator("legacy_message_id", mode="before")
    @classmethod
    def decimal_message_id(cls, value: Any) -> Any:
        if isinstance(value, str) and len(value) <= 19 and value.isascii() and value.isdecimal() and value == str(int(value)):
            return int(value)
        return value


class AdoptSourceRequest(MemoryCommand):
    expected_content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MemoryAppendRequest(MemoryCommand):
    user_input: str = Field(max_length=65535)
    agent_response: str = Field(max_length=65535)
    agent_id: str = Field(max_length=256)
    session_id: str = Field(max_length=256)
    user_id: str = Field(default="", max_length=256)

    @field_validator("agent_id", "session_id", "user_id")
    @classmethod
    def bounded_identity(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 256:
            raise ValueError("Identity must fit 256 UTF-8 bytes")
        return value

    @model_validator(mode="after")
    def bounded_conversation(self) -> Self:
        content = f"User Input: {self.user_input}\nAgent Response: {self.agent_response}"
        if len(content.encode("utf-8")) > 65535:
            raise ValueError("Recorded conversation must fit 65535 UTF-8 bytes")
        return self


class ExactTextPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    old_text: str = Field(min_length=1, max_length=65535)
    new_text: str = Field(max_length=65535)


class ReviseEntryRequest(MemoryCommand):
    expected_revision: RevisionNumber
    content: str | None = Field(default=None, max_length=65535)
    patch: ExactTextPatch | None = None
    reason: str = Field(min_length=1, max_length=1024)
    dry_run: bool = Field(default=False, strict=True)

    @field_validator("content")
    @classmethod
    def bounded_content(cls, value: str | None) -> str | None:
        if value is not None and (not value.strip() or len(value.encode("utf-8")) > 65535):
            raise ValueError("content must be nonblank and at most 65535 UTF-8 bytes")
        return value

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value

    @model_validator(mode="after")
    def one_edit(self) -> Self:
        if (self.content is None) == (self.patch is None):
            raise ValueError("Provide either content or one exact text patch")
        return self


class WithdrawSourceRequest(MemoryCommand):
    expected_source_revision: RevisionNumber
    reason: str = Field(min_length=1, max_length=1024)

    @field_validator("reason")
    @classmethod
    def nonblank_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value


def _wire_data(value: Any, key: str = "") -> Any:
    """Keep physical identifiers exact in JavaScript clients."""
    return wire_message_identifiers(value, key)


def _result(data: Any, *, status_code: int = 200, retcode: RetCode = RetCode.SUCCESS, retmsg: str = "success") -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"retcode": int(retcode), "retmsg": retmsg, "data": _wire_data(data)})


def _command_result(data: dict[str, Any]) -> JSONResponse:
    complete = (
        data.get("dry_run") is True
        or data.get("index_sync_status") == "ready"
        or data.get("status") in {"ready", "succeeded", "completed", "failed", "conflict", "rejected", "stale", "canceled", "redacted"}
    )
    if data.get("kind") == "append_raw" and data.get("queue_state") not in {"submitted", "not_required"}:
        complete = data.get("status") in {"failed", "conflict", "rejected", "stale", "canceled", "redacted"}
    return _result(data, status_code=200 if complete else 202)


def _command_error(error: Exception, *, operation_id: str | None = None) -> JSONResponse:
    if isinstance(error, (NotFoundException, RevisionNotFound)):
        return _result(False, status_code=404, retcode=RetCode.NOT_FOUND, retmsg="Memory resource not found.")
    if isinstance(error, RevisionConflict):
        return _result({"error_code": error.code}, status_code=409, retcode=RetCode.CONFLICT, retmsg=error.code)
    if isinstance(error, RevisionValidationError):
        return _result({"error_code": error.code}, status_code=422, retcode=RetCode.ARGUMENT_ERROR, retmsg=error.code)
    if isinstance(error, RevisionPersistenceUnknown):
        return _result(
            {"operation_id": operation_id or error.operation_id, "error_code": error.code, "requires_reconciliation": True},
            status_code=503,
            retcode=RetCode.SERVER_ERROR,
            retmsg="Operation commit is unconfirmed; read its state before retrying.",
        )
    logger.error("Memory revision request failed (%s)", type(error).__name__)
    return _result(False, status_code=500, retcode=RetCode.SERVER_ERROR, retmsg="Internal server error")


@router.post("/memories/{memory_id}/messages", summary="Append a recorded conversation with a stable operation ID")
async def append_message(memory_id: str, request_body: MemoryAppendRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services import memory_revision_service as service

    message = request_body.model_dump(exclude={"operation_id"})
    if user.authentication.source is not AuthenticationSource.SDK_API_TOKEN:
        message["user_id"] = user.id
    try:
        return _command_result(await service.append_message(db, memory_id, message, actor_id=user.id, operation_id=request_body.operation_id))
    except Exception as error:
        return _command_error(error, operation_id=request_body.operation_id)


@router.post("/memories/{memory_id}/deletions", summary="Stop, drain and reconcile a memory-bank deletion")
async def delete_memory(memory_id: str, request_body: MemoryCommand, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services import memory_revision_service as service

    try:
        return _command_result(await service.delete_memory(db, memory_id, actor_id=user.id, operation_id=request_body.operation_id))
    except Exception as error:
        return _command_error(error, operation_id=request_body.operation_id)


@router.post("/memories/{memory_id}/entries", summary="Explicitly adopt a derived memory for versioned editing")
async def adopt_entry(memory_id: str, request_body: AdoptEntryRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services import memory_revision_service as service

    try:
        return _command_result(await service.adopt_entry(db, memory_id, actor_id=user.id, **request_body.model_dump()))
    except Exception as error:
        return _command_error(error, operation_id=request_body.operation_id)


@router.get("/memories/{memory_id}/messages/{message_id}/entry", summary="Find a managed entry without adopting or changing it")
async def find_entry(memory_id: str, message_id: int = Path(ge=1, le=2**63 - 1), db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services import memory_revision_service as service

    try:
        return _result(await service.find_entry_by_message(db, memory_id, message_id, actor_id=user.id))
    except Exception as error:
        return _command_error(error)


@router.get("/memories/{memory_id}/entries/{entry_id}", summary="Read the current managed entry and publication state")
async def get_entry(memory_id: str, entry_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services import memory_revision_service as service

    try:
        return _result(await service.get_entry(db, memory_id, entry_id, actor_id=user.id))
    except Exception as error:
        return _command_error(error)


@router.get("/memories/{memory_id}/entries/{entry_id}/revisions", summary="Read an authorized page of revision history")
async def list_revisions(
    memory_id: str,
    entry_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=2048),
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
) -> JSONResponse:
    from api.apps.services import memory_revision_service as service

    try:
        return _result(await service.list_revisions(db, memory_id, entry_id, actor_id=user.id, limit=limit, cursor=cursor))
    except Exception as error:
        return _command_error(error)


@router.post("/memories/{memory_id}/entries/{entry_id}/revisions", summary="Preview or submit a version-checked memory correction")
async def revise_entry(memory_id: str, entry_id: str, request_body: ReviseEntryRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services import memory_revision_service as service

    try:
        return _command_result(await service.revise_entry(db, memory_id, entry_id, actor_id=user.id, **request_body.model_dump()))
    except Exception as error:
        return _command_error(error, operation_id=request_body.operation_id)


@router.get("/memories/{memory_id}/operations/{operation_id}", summary="Read a mutation operation without advancing it")
async def get_operation(memory_id: str, operation_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services import memory_revision_service as service

    try:
        return _result(await service.get_operation(db, memory_id, operation_id, actor_id=user.id))
    except Exception as error:
        return _command_error(error)


@router.post("/memories/{memory_id}/operations/{operation_id}/reconciliations", summary="Explicitly reconcile an operation using its original physical target")
async def reconcile_operation(memory_id: str, operation_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services import memory_revision_service as service

    try:
        return _command_result(await service.reconcile_operation(db, memory_id, operation_id, actor_id=user.id))
    except Exception as error:
        return _command_error(error, operation_id=operation_id)


@router.get("/memories/{memory_id}/sources/{source_id}/state", summary="Read current source management state without registering it")
async def get_source_state(memory_id: str, source_id: int = Path(ge=1, le=2**63 - 1), db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services import memory_revision_service as service

    try:
        return _result(await service.get_source_state(db, memory_id, source_id, actor_id=user.id))
    except Exception as error:
        return _command_error(error)


@router.post("/memories/{memory_id}/sources/{source_id}/adoptions", summary="Explicitly register a hash-checked raw source")
async def adopt_source(
    memory_id: str, request_body: AdoptSourceRequest, source_id: int = Path(ge=1, le=2**63 - 1), db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)
) -> JSONResponse:
    from api.apps.services import memory_revision_service as service

    try:
        return _command_result(await service.adopt_source(db, memory_id, source_id, actor_id=user.id, **request_body.model_dump()))
    except Exception as error:
        return _command_error(error, operation_id=request_body.operation_id)


@router.post("/memories/{memory_id}/sources/{source_id}/withdrawals", summary="Withdraw a versioned source from normal memory supply")
async def withdraw_source(
    memory_id: str, request_body: WithdrawSourceRequest, source_id: int = Path(ge=1, le=2**63 - 1), db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)
) -> JSONResponse:
    from api.apps.services import memory_revision_service as service

    try:
        return _command_result(await service.withdraw_source(db, memory_id, source_id, actor_id=user.id, **request_body.model_dump()))
    except Exception as error:
        return _command_error(error, operation_id=request_body.operation_id)
