"""Explicit model-backed artifact commands; GET never generates or applies."""

from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.restful_apis.memory_revision_api import MemoryCommand, _command_error, _result
from api.apps.restful_apis.memory_startup_api import no_store
from api.apps.services.memory_compaction_service import compactions
from api.db.db_models import get_async_db
from api.utils.api_utils import Principal, async_current_user
from memory.compaction.domain import source_identity

router = APIRouter()


class CompactionRequest(MemoryCommand):
    source_ids: list[str] = Field(min_length=1, max_length=32)
    prefix_count: int = Field(strict=True, ge=1, le=32)
    input_budget_tokens: int = Field(default=16384, strict=True, ge=1024, le=32768)
    output_budget_tokens: int = Field(default=1024, strict=True, ge=256, le=8192)
    budget_tokens: int = Field(default=4096, strict=True, ge=1024, le=32768)

    @field_validator("source_ids", mode="before")
    @classmethod
    def ordered_exact_sources(cls, value: Any) -> Any:
        if not isinstance(value, list):
            raise ValueError("Ordered source IDs are required")
        return [source_identity(item) for item in value]


class EmptyCompactionCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/memories/{memory_id}/context-compactions")
async def generate(memory_id: str, body: CompactionRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    try:
        return no_store(_result(await (await compactions(db, memory_id, actor_id=user.id)).generate(memory_id, **body.model_dump())))
    except Exception as error:
        return no_store(_command_error(error, operation_id=body.operation_id))


@router.get("/memories/{memory_id}/context-compaction-operations/{operation_id}")
async def operation(memory_id: str, operation_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    try:
        return no_store(_result(await (await compactions(db, memory_id, actor_id=user.id)).get(memory_id, operation_id)))
    except Exception as error:
        return no_store(_command_error(error, operation_id=operation_id))


@router.post("/memories/{memory_id}/context-compaction-operations/{operation_id}/cancellations")
async def cancel(memory_id: str, operation_id: str, body: EmptyCompactionCommand, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    try:
        return no_store(_result(await (await compactions(db, memory_id, actor_id=user.id)).cancel(memory_id, operation_id)))
    except Exception as error:
        return no_store(_command_error(error, operation_id=operation_id))


class RetirementCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_preview_sha256: str = Field(strict=True, pattern=r"^[a-f0-9]{64}$")


@router.get("/memories/{memory_id}/context-compactions/inventory")
async def inventory(memory_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    try:
        return no_store(_result(await (await compactions(db, memory_id, actor_id=user.id)).retention_inventory(memory_id)))
    except Exception as error:
        return no_store(_command_error(error))


@router.get("/memories/{memory_id}/context-compactions/{artifact_id}/retention-preview")
async def retention_preview(memory_id: str, artifact_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    try:
        return no_store(_result(await (await compactions(db, memory_id, actor_id=user.id)).retention_preview(memory_id, artifact_id)))
    except Exception as error:
        return no_store(_command_error(error))


@router.post("/memories/{memory_id}/context-compactions/{artifact_id}/retirements")
async def retire(memory_id: str, artifact_id: str, body: RetirementCommand, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    try:
        return no_store(_result(await (await compactions(db, memory_id, actor_id=user.id)).retire(memory_id, artifact_id, **body.model_dump())))
    except Exception as error:
        return no_store(_command_error(error))


@router.get("/memories/{memory_id}/context-compactions/{artifact_id}")
async def artifact(memory_id: str, artifact_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    try:
        return no_store(_result(await (await compactions(db, memory_id, actor_id=user.id)).by_id(memory_id, artifact_id)))
    except Exception as error:
        return no_store(_command_error(error))


@router.get("/memories/{memory_id}/context-compactions")
async def artifacts(
    memory_id: str, limit: int = Query(20, ge=1, le=50), cursor: str | None = None, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)
) -> JSONResponse:
    try:
        return no_store(_result(await (await compactions(db, memory_id, actor_id=user.id)).list_artifacts(memory_id, limit=limit, cursor=cursor)))
    except Exception as error:
        return no_store(_command_error(error))
