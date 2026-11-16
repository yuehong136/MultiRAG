"""Explicit maintenance input, analysis, candidate review and batch publication."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.restful_apis.memory_revision_api import MemoryCommand, RevisionNumber, _command_error, _result
from api.apps.services.memory_target_schemas import TargetPreconditions
from api.db.db_models import get_async_db
from api.utils.api_utils import Principal, async_current_user

router = APIRouter()


def decimal_identity(value: Any) -> Any:
    if isinstance(value, str) and len(value) <= 19 and value.isascii() and value.isdecimal() and value == str(int(value)):
        return int(value)
    return value


MessageIdentity = Annotated[int, BeforeValidator(decimal_identity), Field(strict=True, ge=1, le=2**63 - 1)]
LogicalIdentity = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class CreateMaintenanceRequest(MemoryCommand):
    source_ids: list[MessageIdentity] = Field(min_length=1, max_length=32)
    entry_ids: list[LogicalIdentity] = Field(default_factory=list, max_length=32)
    policy: Literal["general", "preferences", "decisions", "procedures"] = "general"
    input_budget_tokens: int = Field(default=16384, ge=1024, le=32768, strict=True)
    output_budget_tokens: int = Field(default=4096, ge=256, le=8192, strict=True)
    target_preconditions: TargetPreconditions | None = None


class ReviewCandidateRequest(MemoryCommand):
    expected_revision: RevisionNumber
    decision: Literal["approve", "reject"]


class SelectedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: LogicalIdentity
    expected_revision: RevisionNumber


class PublishCandidatesRequest(MemoryCommand):
    candidates: list[SelectedCandidate] = Field(min_length=1, max_length=16)


def job_result(value: dict[str, Any]) -> JSONResponse:
    return _result(value, status_code=202 if value.get("state") in {"pending", "running", "cancel_requested"} else 200)


@router.post("/memories/{memory_id}/maintenance-jobs", summary="Snapshot an explicit source range for review-only memory analysis")
async def create_job(memory_id: str, body: CreateMaintenanceRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_maintenance_service import jobs

    try:
        service = await jobs(db, memory_id, actor_id=user.id)
        return job_result(await service.create(memory_id, **body.model_dump()))
    except Exception as error:
        return _command_error(error, operation_id=body.operation_id)


@router.get("/memories/{memory_id}/maintenance-jobs", summary="List authorized maintenance jobs without running them")
async def list_jobs(memory_id: str, limit: int = Query(default=20, ge=1, le=100), db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_maintenance_service import jobs

    try:
        return _result(await (await jobs(db, memory_id, actor_id=user.id)).list_jobs(memory_id, limit=limit))
    except Exception as error:
        return _command_error(error)


@router.get("/memories/{memory_id}/maintenance-jobs/{job_id}", summary="Read maintenance state and coverage without advancing work")
async def get_job(memory_id: str, job_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_maintenance_service import jobs

    try:
        return _result(await (await jobs(db, memory_id, actor_id=user.id)).get(memory_id, job_id))
    except Exception as error:
        return _command_error(error)


@router.get("/memories/{memory_id}/maintenance-jobs/{job_id}/candidates", summary="Read candidate diffs, provenance and review state")
async def get_candidates(memory_id: str, job_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_maintenance_service import jobs

    try:
        return _result(await (await jobs(db, memory_id, actor_id=user.id)).candidates(memory_id, job_id))
    except Exception as error:
        return _command_error(error)


@router.post("/memories/{memory_id}/maintenance-jobs/{job_id}/runs", summary="Queue durable analysis of the saved input snapshot without publishing memory")
async def run_job(memory_id: str, job_id: str, body: MemoryCommand, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_maintenance_service import executions

    try:
        return job_result(await (await executions(db, memory_id, actor_id=user.id)).submit(memory_id, job_id, operation_id=body.operation_id))
    except Exception as error:
        return _command_error(error, operation_id=body.operation_id)


@router.post("/memories/{memory_id}/maintenance-jobs/{job_id}/candidates/{candidate_id}/reviews", summary="Approve or reject one exact candidate revision")
async def review_candidate(
    memory_id: str, job_id: str, candidate_id: str, body: ReviewCandidateRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)
) -> JSONResponse:
    from api.apps.services.memory_maintenance_service import jobs

    try:
        return _result(await (await jobs(db, memory_id, actor_id=user.id)).review(memory_id, job_id, candidate_id, **body.model_dump()))
    except Exception as error:
        return _command_error(error, operation_id=body.operation_id)


@router.post("/memories/{memory_id}/maintenance-jobs/{job_id}/publications", summary="Publish an approved candidate selection with one SQL decision")
async def publish_candidates(memory_id: str, job_id: str, body: PublishCandidatesRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_maintenance_service import publications

    try:
        value = await (await publications(db, memory_id, actor_id=user.id)).publish(memory_id, job_id, **body.model_dump())
        ready = value.get("status") == "ready" and value.get("index_sync_status") == "ready"
        terminal = value.get("status") in {"stale", "canceled", "failed", "rejected"}
        return _result(value, status_code=200 if ready or terminal else 202)
    except Exception as error:
        return _command_error(error, operation_id=body.operation_id)


@router.post("/memories/{memory_id}/maintenance-jobs/{job_id}/cancellations", summary="Cancel candidate analysis without undoing published memory")
async def cancel_job(memory_id: str, job_id: str, body: MemoryCommand, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_maintenance_service import jobs

    try:
        return job_result(await (await jobs(db, memory_id, actor_id=user.id)).cancel(memory_id, job_id, operation_id=body.operation_id))
    except Exception as error:
        return _command_error(error, operation_id=body.operation_id)
