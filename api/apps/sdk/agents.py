import json
import logging
import time
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from api.db import CanvasCategory
from api.db.db_models import get_db
from api.db.services.canvas_service import UserCanvasService
from api.db.services.user_canvas_version import UserCanvasVersionService
from common.constants import RetCode
from common.misc_utils import get_uuid
from api.utils.api_utils import get_error_data_result, get_result, token_required

router = APIRouter()


class CreateAgentRequest(BaseModel):
    title: str
    dsl: dict[str, Any] | str


class UpdateAgentRequest(BaseModel):
    title: str | None = None
    dsl: dict[str, Any] | str | None = None


class DeleteAgentsRequest(BaseModel):
    ids: list[str] | None = None


@router.get("/agents", summary="获取代理列表")
def list_agents(
    id: str | None = Query(None),
    title: str | None = Query(None),
    page: int = Query(1),
    page_size: int = Query(30),
    orderby: str = Query("update_time"),
    desc: bool = Query(True),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    获取代理列表
    
    Args:
        id: 代理ID过滤
        title: 代理标题过滤
        page: 页码
        page_size: 每页数量
        orderby: 排序字段
        desc: 是否降序
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        代理列表
    """
    if id or title:
        canvas = UserCanvasService.query(db, id=id, title=title, user_id=tenant_id)
        if not canvas:
            return get_error_data_result(retmsg="The agent doesn't exist.")
    
    canvas = UserCanvasService.get_list(db, tenant_id, page, page_size, orderby, desc, id, title)
    return get_result(data=canvas)


@router.post("/agents", summary="创建代理")
def create_agent(
    request: CreateAgentRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    创建新的代理
    
    Args:
        request: 代理创建参数
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        创建结果
    """
    req = request.model_dump()
    req["user_id"] = tenant_id

    if req.get("dsl") is not None:
        if not isinstance(req["dsl"], str):
            req["dsl"] = json.dumps(req["dsl"], ensure_ascii=False)
        req["dsl"] = json.loads(req["dsl"])
    else:
        return get_error_data_result(retmsg="No DSL data in request.")

    if req.get("title") is not None:
        req["title"] = req["title"].strip()
    else:
        return get_error_data_result(retmsg="No title in request.")

    if UserCanvasService.query(db, user_id=tenant_id, title=req["title"]):
        return get_error_data_result(retmsg=f"Agent with title {req['title']} already exists.")

    agent_id = get_uuid()
    req["id"] = agent_id

    if not UserCanvasService.save(db, **req):
        return get_error_data_result(retmsg="Fail to create agent.")

    UserCanvasVersionService.insert(
        db,
        user_canvas_id=agent_id,
        title="{0}_{1}".format(req["title"], time.strftime("%Y_%m_%d_%H_%M_%S")),
        dsl=req["dsl"]
    )

    return get_result(data=True)


@router.put("/agents/{agent_id}", summary="更新代理")
def update_agent(
    agent_id: str,
    request: UpdateAgentRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    更新代理
    
    Args:
        agent_id: 代理ID
        request: 更新请求参数
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        更新结果
    """
    req = request.model_dump(exclude_unset=True)
    req["user_id"] = tenant_id

    if req.get("dsl") is not None:
        if not isinstance(req["dsl"], str):
            req["dsl"] = json.dumps(req["dsl"], ensure_ascii=False)
        req["dsl"] = json.loads(req["dsl"])
    
    if req.get("title") is not None:
        req["title"] = req["title"].strip()

    if not UserCanvasService.query(db, user_id=tenant_id, id=agent_id):
        return get_error_data_result(retmsg="Only owner of canvas authorized for this operation.")

    UserCanvasService.update_by_id(db, agent_id, req)

    if req.get("dsl") is not None:
        UserCanvasVersionService.insert(
            db,
            user_canvas_id=agent_id,
            title="{0}_{1}".format(req["title"], time.strftime("%Y_%m_%d_%H_%M_%S")),
            dsl=req["dsl"]
        )
        UserCanvasVersionService.delete_all_versions(db, agent_id)

    return get_result(data=True)


@router.delete("/agents/{agent_id}", summary="删除代理")
def delete_agent(
    agent_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    删除代理
    
    Args:
        agent_id: 代理ID
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        删除结果
    """
    if not UserCanvasService.query(db, user_id=tenant_id, id=agent_id):
        return get_error_data_result(retmsg="Only owner of canvas authorized for this operation.")

    UserCanvasService.delete_by_id(db, agent_id)
    return get_result(data=True)


class WebhookRequest(BaseModel):
    id: str
    query: str | None = None
    files: list | None = None
    user_id: str | None = None


@router.post("/webhook/{agent_id}", summary="Webhook触发代理")
def webhook(
    agent_id: str,
    request: WebhookRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    通过Webhook触发代理执行
    
    Args:
        agent_id: 代理ID
        request: Webhook请求参数
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        SSE流式响应
    """
    req = request.model_dump()
    if not UserCanvasService.accessible(db, req["id"], tenant_id):
        return get_error_data_result(retmsg='Only owner of canvas authorized for this operation.')

    cvs = UserCanvasService.get_by_id(db, req["id"])
    if not cvs:
        return get_error_data_result(retmsg="canvas not found.")

    if not isinstance(cvs.dsl, str):
        cvs.dsl = json.dumps(cvs.dsl, ensure_ascii=False)

    if cvs.canvas_category == CanvasCategory.DataFlow:
        return get_error_data_result(retmsg="Dataflow can not be triggered by webhook.")

    try:
        canvas = Canvas(cvs.dsl, tenant_id, agent_id)
    except Exception as e:
        return get_error_data_result(retmsg=str(e))

    def sse():
        nonlocal canvas
        try:
            for ans in canvas.run(
                query=req.get("query", ""),
                files=req.get("files", []),
                user_id=req.get("user_id", tenant_id),
                webhook_payload=req
            ):
                yield "data:" + json.dumps(ans, ensure_ascii=False) + "\n\n"

            cvs.dsl = json.loads(str(canvas))
            UserCanvasService.update_by_id(db, req["id"], cvs.to_dict())
        except Exception as e:
            logging.exception(e)
            yield "data:" + json.dumps({"code": 500, "message": str(e), "data": False}, ensure_ascii=False) + "\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )
