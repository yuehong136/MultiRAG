"""Search App RESTful API.

Routes are mounted under ``/api/v1`` by ``api.apps.register_page``:
    POST   /searches
    GET    /searches
    GET    /searches/{search_id}
    PUT    /searches/{search_id}
    DELETE /searches/{search_id}
    POST   /searches/{search_id}/completions

The legacy ``/v1/search/*`` endpoints remain in ``api/apps/search_app.py``.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.constants import DATASET_NAME_LIMIT
from api.db.db_models import get_async_db
from api.db.services import duplicate_name
from api.db.services.dialog_service import async_ask
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.search_service import SearchService
from api.db.services.user_service import TenantService, UserTenantService
from api.utils.api_utils import async_current_tenant_id, get_error_data_result, get_result
from common.constants import RetCode, StatusEnum
from common.misc_utils import get_uuid

router = APIRouter()
logger = logging.getLogger(__name__)


class CreateSearchRequest(BaseModel):
    name: Any = Field(..., description="Search app name")
    description: str | None = ""
    search_config: dict[str, Any] | None = None


class UpdateSearchRequest(BaseModel):
    name: Any = Field(..., description="Search app name")
    search_config: Any = Field(..., description="Search config patch")
    description: str | None = None
    avatar: str | None = None


class SearchCompletionRequest(BaseModel):
    question: str
    kb_ids: list[str] | None = None


def _respond(success: bool, result: Any, retcode: RetCode | None = None):
    if success:
        return get_result(data=result)
    if retcode == RetCode.AUTHENTICATION_ERROR:
        return get_result(data=False, retmsg=result, retcode=retcode)
    if retcode == RetCode.OPERATING_ERROR:
        return get_result(data=False, retmsg=result, retcode=retcode)
    return get_error_data_result(retcode=retcode or RetCode.DATA_ERROR, retmsg=result)


def _create_search(db: Session, tenant_id: str, req: dict[str, Any]) -> tuple[bool, Any, RetCode | None]:
    search_name = req.get("name")
    description = req.get("description", "")

    if not isinstance(search_name, str):
        return False, "Search name must be string.", RetCode.DATA_ERROR
    if search_name.strip() == "":
        return False, "Search name can't be empty.", RetCode.DATA_ERROR
    if len(search_name.encode("utf-8")) > 255:
        return False, f"Search name length is {len(search_name)} which is large than 255.", RetCode.DATA_ERROR

    if not TenantService.get_by_id(db, tenant_id):
        return False, "Authorized identity.", RetCode.DATA_ERROR

    search_name = search_name.strip()
    search_name = duplicate_name(
        lambda **kwargs: SearchService.query(db, **kwargs),
        name=search_name,
        tenant_id=tenant_id,
        status=StatusEnum.VALID.value,
    )

    req = dict(req)
    req["id"] = get_uuid()
    req["name"] = search_name
    req["description"] = description
    req["tenant_id"] = tenant_id
    req["created_by"] = tenant_id

    if not SearchService.save(db, **req):
        return False, "Sorry! Data missing!", RetCode.DATA_ERROR
    return True, {"search_id": req["id"]}, None


def _list_searches(
    db: Session,
    tenant_id: str,
    *,
    keywords: str = "",
    page: int = 0,
    page_size: int = 0,
    orderby: str = "create_time",
    desc: bool = True,
    owner_ids: list[str] | None = None,
) -> tuple[bool, Any, RetCode | None]:
    owner_ids = owner_ids or []

    if not owner_ids:
        search_apps, total = SearchService.get_by_tenant_ids(db, [], tenant_id, page, page_size, orderby, desc, keywords)
    else:
        search_apps, _ = SearchService.get_by_tenant_ids(db, owner_ids, tenant_id, 0, 0, orderby, desc, keywords)
        search_apps = [search_app for search_app in search_apps if search_app["tenant_id"] in owner_ids]
        total = len(search_apps)
        if page and page_size:
            search_apps = search_apps[(page - 1) * page_size : page * page_size]

    return True, {"search_apps": search_apps, "total": total}, None


def _get_search_detail(db: Session, tenant_id: str, search_id: str) -> tuple[bool, Any, RetCode | None]:
    tenants = UserTenantService.query(db, user_id=tenant_id)
    for tenant in tenants:
        if SearchService.query(db, tenant_id=tenant.tenant_id, id=search_id):
            break
    else:
        return False, "Has no permission for this operation.", RetCode.OPERATING_ERROR

    search = SearchService.get_detail(db, search_id)
    if not search:
        return False, "Can't find this Search App!", RetCode.DATA_ERROR
    return True, search, None


def _update_search(
    db: Session,
    tenant_id: str,
    search_id: str,
    req: dict[str, Any],
) -> tuple[bool, Any, RetCode | None]:
    if not isinstance(req.get("name"), str):
        return False, "Search name must be string.", RetCode.DATA_ERROR
    if req["name"].strip() == "":
        return False, "Search name can't be empty.", RetCode.DATA_ERROR
    if len(req["name"].encode("utf-8")) > DATASET_NAME_LIMIT:
        return False, f"Search name length is {len(req['name'])} which is large than {DATASET_NAME_LIMIT}", RetCode.DATA_ERROR

    req = dict(req)
    req["name"] = req["name"].strip()

    if not TenantService.get_by_id(db, tenant_id):
        return False, "Authorized identity.", RetCode.DATA_ERROR

    if not SearchService.accessible4deletion(db, search_id, tenant_id):
        return False, "No authorization.", RetCode.AUTHENTICATION_ERROR

    search_apps = SearchService.query(db, tenant_id=tenant_id, id=search_id)
    search_app = search_apps[0] if search_apps else None
    if not search_app:
        return False, f"Cannot find search {search_id}", RetCode.DATA_ERROR

    if req["name"].lower() != search_app.name.lower():
        existing_searches = SearchService.query(db, name=req["name"], tenant_id=tenant_id, status=StatusEnum.VALID.value)
        if existing_searches:
            return False, "Duplicated search name.", RetCode.DATA_ERROR

    current_config = search_app.search_config or {}
    new_config = req.get("search_config")
    if not isinstance(new_config, dict):
        return False, "search_config must be a JSON object", RetCode.DATA_ERROR
    req["search_config"] = {**current_config, **new_config}

    for field in ("search_id", "tenant_id", "created_by", "update_time", "id"):
        req.pop(field, None)

    if not SearchService.update_by_id(db, search_id, req):
        return False, "Failed to update search", RetCode.DATA_ERROR

    updated_search = SearchService.get_by_id(db, search_id)
    if not updated_search:
        return False, "Failed to fetch updated search", RetCode.DATA_ERROR

    return True, updated_search.to_dict(), None


def _delete_search(db: Session, tenant_id: str, search_id: str) -> tuple[bool, Any, RetCode | None]:
    if not SearchService.accessible4deletion(db, search_id, tenant_id):
        return False, "No authorization.", RetCode.AUTHENTICATION_ERROR

    if not SearchService.delete_by_id(db, search_id):
        return False, f"Failed to delete search App {search_id}", RetCode.DATA_ERROR
    return True, True, None


@router.post("/searches", summary="Create search app")
async def create_search(
    request: CreateSearchRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        success, result, retcode = await db.run_sync(lambda s: _create_search(s, tenant_id, request.model_dump()))  # TODO(async-phase4)
        return _respond(success, result, retcode)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/searches", summary="List search apps")
async def list_searches(
    keywords: str = Query("", description="Keyword filter"),
    page: int = Query(0, description="Page number; 0 disables pagination"),
    page_size: int = Query(0, description="Items per page; 0 disables pagination"),
    orderby: str = Query("create_time", description="Sort field"),
    desc: bool = Query(True, description="Sort descending"),
    owner_ids: list[str] | None = Query(None, description="Owner tenant IDs"),
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        success, result, retcode = await db.run_sync(  # TODO(async-phase4)
            lambda s: _list_searches(
                s,
                tenant_id,
                keywords=keywords,
                page=page,
                page_size=page_size,
                orderby=orderby,
                desc=desc,
                owner_ids=owner_ids,
            )
        )
        return _respond(success, result, retcode)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/searches/{search_id}", summary="Get search app")
async def get_search(
    search_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        success, result, retcode = await db.run_sync(lambda s: _get_search_detail(s, tenant_id, search_id))  # TODO(async-phase4)
        return _respond(success, result, retcode)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.put("/searches/{search_id}", summary="Update search app")
async def update_search(
    search_id: str,
    request: UpdateSearchRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        success, result, retcode = await db.run_sync(lambda s: _update_search(s, tenant_id, search_id, request.model_dump(exclude_unset=True)))  # TODO(async-phase4)
        return _respond(success, result, retcode)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.delete("/searches/{search_id}", summary="Delete search app")
async def delete_search(
    search_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        success, result, retcode = await db.run_sync(lambda s: _delete_search(s, tenant_id, search_id))  # TODO(async-phase4)
        return _respond(success, result, retcode)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.post("/searches/{search_id}/completions", summary="Search completion")
async def completions(
    search_id: str,
    request: SearchCompletionRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    """基于 search app 配置的检索问答(SSE 流式)。kb_ids 优先取 search_config,body 兜底。"""
    if not await db.run_sync(lambda s: SearchService.accessible4deletion(s, search_id, tenant_id)):  # TODO(async-phase4)
        return _respond(False, "No authorization.", RetCode.AUTHENTICATION_ERROR)

    search_app = await db.run_sync(lambda s: SearchService.get_detail(s, search_id))  # TODO(async-phase4)
    if not search_app:
        return get_error_data_result(retmsg=f"Cannot find search {search_id}")

    search_config = search_app.get("search_config", {})
    kb_ids = search_config.get("kb_ids") or request.kb_ids or []
    if not kb_ids:
        return get_error_data_result(retmsg="`kb_ids` is required.")

    # kb 归属校验,防止经 body 传任意 kb_ids 越权检索
    for kb_id in kb_ids:
        if not await db.run_sync(lambda s, _kb_id=kb_id: KnowledgebaseService.accessible(s, kb_id=_kb_id, user_id=tenant_id)):  # TODO(async-phase4)
            return get_error_data_result(retmsg=f"You don't own the dataset {kb_id}")

    async def stream_response():
        try:
            async for ans in async_ask(db, request.question, kb_ids, tenant_id, search_config=search_config):
                yield "data:" + json.dumps({"code": 0, "message": "", "data": ans}, ensure_ascii=False) + "\n\n"
        except Exception as e:
            yield (
                "data:"
                + json.dumps(
                    {"code": 500, "message": str(e), "data": {"answer": "**ERROR**: " + str(e), "reference": []}},
                    ensure_ascii=False,
                )
                + "\n\n"
            )
        yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"

    resp = StreamingResponse(stream_response(), media_type="text/event-stream")
    resp.headers["Cache-control"] = "no-cache"
    resp.headers["Connection"] = "keep-alive"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
    return resp
