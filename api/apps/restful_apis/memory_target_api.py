"""Explicit bounded target recall; it never creates a maintenance job."""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from api.apps.restful_apis.memory_revision_api import _command_error, _result
from api.apps.services.memory_target_schemas import RecommendTargetsRequest
from api.db.db_models import get_async_db
from api.utils.api_utils import Principal, async_current_user

router = APIRouter()


@router.post("/memories/{memory_id}/maintenance-targets/recommendations", summary="Read bounded related current entries for explicit user selection")
async def recommend_targets(memory_id: str, body: RecommendTargetsRequest, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> JSONResponse:
    from api.apps.services.memory_maintenance_service import targets

    try:
        return _result(await (await targets(db, memory_id, actor_id=user.id)).recommend(memory_id, **body.model_dump()))
    except Exception as error:
        return _command_error(error)
