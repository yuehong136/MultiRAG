"""Structured RAW submissions and version-bound, explicitly requested provenance."""

from __future__ import annotations

from typing import Literal, Self

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.restful_apis.memory_revision_api import MemoryCommand, _command_error, _command_result, _result
from api.apps.restful_apis.memory_startup_api import no_store
from api.apps.services import memory_source_service as service
from api.db.db_models import get_async_db
from api.identity.principal import AuthenticationSource
from api.utils.api_utils import Principal, async_current_user
from memory.structured_sources import source_envelope

router = APIRouter()


class DeclaredSourceSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant", "tool", "unknown"]
    content: str = Field(strict=True, min_length=1, max_length=65535)


class StructuredSourceRequest(MemoryCommand):
    user_id: str = Field(default="", strict=True, max_length=256)
    agent_id: str = Field(strict=True, max_length=256)
    session_id: str = Field(strict=True, max_length=256)
    segments: list[DeclaredSourceSegment] = Field(min_length=1, max_length=32)
    event_time: str | None = Field(default=None, strict=True, max_length=40)

    @model_validator(mode="after")
    def canonical_business_input(self) -> Self:
        if any(len(value.encode("utf-8")) > 256 for value in (self.user_id, self.agent_id, self.session_id)):
            raise ValueError("Identity must fit 256 UTF-8 bytes")
        try:
            source_envelope([item.model_dump() for item in self.segments], self.event_time)
        except Exception as error:
            from memory.revisions.domain import RevisionValidationError

            if isinstance(error, RevisionValidationError):
                raise ValueError(error.code) from error
            raise
        return self


@router.post("/memories/{memory_id}/sources", summary="Record declared source segments without automatic extraction")
async def append_source(memory_id: str, request_body: StructuredSourceRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    arguments = request_body.model_dump()
    if user.authentication.source is not AuthenticationSource.SDK_API_TOKEN:
        arguments["user_id"] = user.id
    try:
        return no_store(_command_result(await service.append_source(db, memory_id, actor_id=user.id, **arguments)))
    except Exception as error:
        return no_store(_command_error(error, operation_id=request_body.operation_id))


@router.get("/memories/{memory_id}/sources/{source_id}/provenance", summary="Read source submission metadata; full segments require explicit opt-in")
async def get_provenance(
    memory_id: str,
    source_id: int = Path(ge=1, le=2**63 - 1),
    include_segments: bool = Query(default=False),
    expected_source_revision: int | None = Query(default=None, ge=1, le=2**63 - 1),
    expected_source_generation: int | None = Query(default=None, ge=1, le=2**63 - 1),
    content_sha256: str | None = Query(default=None, pattern="^[0-9a-f]{64}$"),
    expected_provenance_sha256: str | None = Query(default=None, pattern="^[0-9a-f]{64}$"),
    user_id: str | None = Query(default=None, max_length=256),
    agent_id: str | None = Query(default=None, max_length=256),
    session_id: str | None = Query(default=None, max_length=256),
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
) -> JSONResponse:
    try:
        result = await service.get_provenance(
            db,
            memory_id,
            source_id,
            actor_id=user.id,
            include_segments=include_segments,
            expected_source_revision=expected_source_revision,
            expected_source_generation=expected_source_generation,
            content_sha256=content_sha256,
            expected_provenance_sha256=expected_provenance_sha256,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
        )
        return no_store(_result(result))
    except Exception as error:
        return no_store(_command_error(error))
