"""Human verification is a version-bound annotation, never model assessment."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.restful_apis.memory_revision_api import MemoryCommand, _command_error, _result
from api.db.db_models import get_async_db
from api.utils.api_utils import Principal, async_current_user
from memory.verification.domain import head_version

router = APIRouter()
HeadVersion = Annotated[int, Field(strict=True, ge=0, le=2**63 - 1)]


class VerificationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    description: str = Field(min_length=1, max_length=1024)
    reference: str = Field(default="", max_length=1024)


class VerificationRequest(MemoryCommand):
    expected_head_version: HeadVersion
    expected_binding_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    result: Literal["human_confirmed", "disputed", "needs_review"]
    method: Literal["source_comparison", "external_check", "other"]
    applicability: str = Field(min_length=1, max_length=1024)
    notes: str = Field(default="", max_length=2048)
    evidence: list[VerificationEvidence] = Field(default_factory=list, max_length=8)
    checked_at: AwareDatetime | None = None

    @field_validator("expected_head_version", mode="before")
    @classmethod
    def exact_head_version(cls, value: Any) -> int:
        return head_version(value)


class VerificationRevokeRequest(MemoryCommand):
    expected_head_version: HeadVersion
    reason: str = Field(min_length=1, max_length=1024)

    @field_validator("expected_head_version", mode="before")
    @classmethod
    def exact_head_version(cls, value: Any) -> int:
        return head_version(value)


@router.get("/memories/{memory_id}/entries/{entry_id}/verification-snapshot")
async def verification_snapshot(memory_id: str, entry_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_verification_service import verifications

    try:
        return _result(await (await verifications(db, memory_id, actor_id=user.id)).snapshot(memory_id, entry_id))
    except Exception as error:
        return _command_error(error)


@router.post("/memories/{memory_id}/entries/{entry_id}/verifications")
async def record_verification(memory_id: str, entry_id: str, body: VerificationRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_verification_service import verifications

    try:
        return _result(await (await verifications(db, memory_id, actor_id=user.id)).record(memory_id, entry_id, **body.model_dump()))
    except Exception as error:
        return _command_error(error, operation_id=body.operation_id)


@router.post("/memories/{memory_id}/verifications/{verification_id}/revocations")
async def revoke_verification(
    memory_id: str, verification_id: str, body: VerificationRevokeRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)
) -> JSONResponse:
    from api.apps.services.memory_verification_service import verifications

    try:
        return _result(await (await verifications(db, memory_id, actor_id=user.id)).revoke(memory_id, verification_id, **body.model_dump()))
    except Exception as error:
        return _command_error(error, operation_id=body.operation_id)


@router.get("/memories/{memory_id}/entries/{entry_id}/verifications")
async def verification_history(
    memory_id: str, entry_id: str, limit: int = Query(20, ge=1, le=50), cursor: str | None = None, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)
) -> JSONResponse:
    from api.apps.services.memory_verification_service import verifications

    try:
        return _result(await (await verifications(db, memory_id, actor_id=user.id)).history(memory_id, entry_id, limit=limit, cursor=cursor))
    except Exception as error:
        return _command_error(error)


@router.get("/memories/{memory_id}/verification-operations/{operation_id}")
async def verification_operation(memory_id: str, operation_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_verification_service import verifications

    try:
        return _result(await (await verifications(db, memory_id, actor_id=user.id)).operation(memory_id, operation_id))
    except Exception as error:
        return _command_error(error)


@router.get("/memories/{memory_id}/verification-todos")
async def verification_todos(
    memory_id: str, limit: int = Query(10, ge=1, le=20), cursor: str | None = None, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)
) -> JSONResponse:
    from api.apps.services.memory_verification_service import verifications

    try:
        return _result(await (await verifications(db, memory_id, actor_id=user.id)).todos(memory_id, limit=limit, cursor=cursor))
    except Exception as error:
        return _command_error(error)
