"""Explicit read-only startup package generation, never implicit prompt authority."""

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.restful_apis.memory_revision_api import _command_error, _result
from api.db.db_models import get_async_db
from api.utils.api_utils import Principal, async_current_user
from memory.verification.domain import identifier

router = APIRouter()


class StartupContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_id: str = Field(max_length=256)
    agent_id: str = Field(max_length=256)
    session_id: str | None = Field(default=None, max_length=256)
    entry_ids: list[str] | None = Field(default=None, min_length=1, max_length=32)
    budget_tokens: int = Field(default=4096, strict=True, ge=1024, le=32768)
    max_items: int = Field(default=8, strict=True, ge=1, le=16)
    inventory_limit: int = Field(default=64, strict=True, ge=1, le=128)

    @field_validator("user_id", "agent_id", "session_id")
    @classmethod
    def bounded_scope(cls, value: str | None) -> str | None:
        if value is not None and len(value.encode("utf-8")) > 256:
            raise ValueError("Scope must fit 256 UTF-8 bytes")
        return value

    @field_validator("entry_ids")
    @classmethod
    def exact_entries(cls, value: list[str] | None) -> list[str] | None:
        if value is not None:
            if len(set(value)) != len(value):
                raise ValueError("Duplicate startup entry")
            for key in value:
                identifier(key)
        return value


def no_store(response: JSONResponse) -> JSONResponse:
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/memories/{memory_id}/startup-context", summary="Read a fresh, bounded startup memory-data package")
async def startup_context(memory_id: str, body: StartupContextRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_startup_service import startup

    try:
        result: dict[str, Any] = await (await startup(db, memory_id, actor_id=user.id)).build(memory_id, **body.model_dump())
        return no_store(_result(result))
    except Exception as error:
        return no_store(_command_error(error))
