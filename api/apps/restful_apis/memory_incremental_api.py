"""Explicit discovery and bounded submission of incremental maintenance batches."""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.restful_apis.memory_maintenance_api import LogicalIdentity
from api.apps.restful_apis.memory_revision_api import MemoryCommand, _command_error, _result
from api.apps.services.memory_target_schemas import TargetPreconditions
from api.db.db_models import get_async_db
from api.utils.api_utils import Principal, async_current_user

router = APIRouter()


class DiscoveryScope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str | None = Field(default=None, max_length=256)
    agent_id: str | None = Field(default=None, max_length=256)
    session_id: str | None = Field(default=None, max_length=256)


class WholeSegmentation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["whole"] = "whole"


class BoundedSegmentation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["segmented"]
    fragment_token_limit: int = Field(default=2048, ge=256, le=8192, strict=True)
    max_parts_per_source: int = Field(default=32, ge=1, le=64, strict=True)


SegmentationSelection = WholeSegmentation | BoundedSegmentation


class DiscoverySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: DiscoveryScope = Field(default_factory=DiscoveryScope)
    policy: Literal["general", "preferences", "decisions", "procedures"] = "general"
    entry_ids: list[LogicalIdentity] = Field(default_factory=list, max_length=32)
    batch_size: int = Field(default=32, ge=1, le=32, strict=True)
    max_sources: int = Field(default=128, ge=1, le=256, strict=True)
    input_budget_tokens: int = Field(default=16384, ge=1024, le=32768, strict=True)
    output_budget_tokens: int = Field(default=4096, ge=256, le=8192, strict=True)
    segmentation: SegmentationSelection | None = None
    target_preconditions: TargetPreconditions | None = None


class CreateDiscoveryRequest(DiscoverySelection, MemoryCommand):
    expected_preview_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RunDiscoveryRequest(MemoryCommand):
    limit: int = Field(default=1, ge=1, le=8, strict=True)


def discovery_result(value: dict[str, Any]) -> JSONResponse:
    unsettled = value["state"] not in {"settled", "canceled", "stale"} or bool(value["progress"]["running"])
    return _result(value, status_code=202 if unsettled else 200)


@router.post("/memories/{memory_id}/maintenance-discoveries/preview", summary="Preview registered active source versions without writes or analysis")
async def preview_discovery(memory_id: str, body: DiscoverySelection, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_maintenance_service import discoveries

    try:
        return _result(await (await discoveries(db, memory_id, actor_id=user.id)).preview(memory_id, **body.model_dump()))
    except Exception as error:
        return _command_error(error)


@router.post("/memories/{memory_id}/maintenance-discoveries", summary="Reserve a reviewed incremental source selection and fixed child handles")
async def create_discovery(memory_id: str, body: CreateDiscoveryRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_maintenance_service import discoveries

    try:
        return discovery_result(await (await discoveries(db, memory_id, actor_id=user.id)).create(memory_id, **body.model_dump()))
    except Exception as error:
        return _command_error(error, operation_id=body.operation_id)


@router.get("/memories/{memory_id}/maintenance-discoveries", summary="Find persisted discovery handles without advancing work")
async def list_discoveries(
    memory_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    operation_id: str | None = Query(default=None, max_length=128),
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
) -> JSONResponse:
    from api.apps.services.memory_maintenance_service import discoveries

    try:
        return _result(await (await discoveries(db, memory_id, actor_id=user.id)).list_discoveries(memory_id, limit=limit, operation_id=operation_id))
    except Exception as error:
        return _command_error(error)


@router.get("/memories/{memory_id}/maintenance-discoveries/{discovery_id}", summary="Read incremental child progress without advancing work")
async def get_discovery(memory_id: str, discovery_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_maintenance_service import discoveries

    try:
        return _result(await (await discoveries(db, memory_id, actor_id=user.id)).get(memory_id, discovery_id))
    except Exception as error:
        return _command_error(error)


@router.post("/memories/{memory_id}/maintenance-discoveries/{discovery_id}/runs", summary="Create and queue bounded fixed child jobs without publishing candidates")
async def run_discovery(memory_id: str, discovery_id: str, body: RunDiscoveryRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_maintenance_service import discoveries

    try:
        return discovery_result(await (await discoveries(db, memory_id, actor_id=user.id)).run(memory_id, discovery_id, **body.model_dump()))
    except Exception as error:
        return _command_error(error, operation_id=body.operation_id)


@router.post("/memories/{memory_id}/maintenance-discoveries/{discovery_id}/cancellations", summary="Cancel a plan using each existing child cancellation protocol")
async def cancel_discovery(memory_id: str, discovery_id: str, body: MemoryCommand, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_maintenance_service import discoveries

    try:
        return discovery_result(await (await discoveries(db, memory_id, actor_id=user.id)).cancel(memory_id, discovery_id, operation_id=body.operation_id))
    except Exception as error:
        return _command_error(error, operation_id=body.operation_id)
