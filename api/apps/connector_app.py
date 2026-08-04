"""
@project: multirag
@Author：龙
@file： connector_app.py
@date：2025/12/19 16:00
@desc: [Deprecated] 旧的 web 数据源连接器端点 ``/v1/connector/*``。

连接器接口已收编到 ``/api/v1/connectors/*``（``api/apps/restful_apis/connector_api.py``），
业务逻辑统一在 ``api/apps/services/connector_api_service.py``（CRUD）与
``api/apps/services/connector_oauth_service.py``（OAuth 流程）。本模块仅为前端等既有消费方
保留，新代码请直接打 RESTful 端点。

**OAuth 回调这几条不只是过渡兼容**：``common/data_source/config.py`` 里
``*_WEB_OAUTH_REDIRECT_URI`` 的默认值指向本模块的路径，且这些 URI 已注册在 Google/Box
的应用后台，删掉会让存量授权流当场 404。
"""

import time

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.apps import manager
from api.apps.services import connector_api_service, connector_oauth_service
from api.db.db_models import get_db
from api.utils.api_utils import get_data_error_result, get_json_result, server_error_response
from common.constants import RetCode

router = APIRouter()


# ==================== 请求体模型定义 ====================


class SetConnectorRequest(BaseModel):
    """创建或更新连接器请求（新接口拆成了 POST /connectors 与 PATCH /connectors/{id}）"""

    id: str | None = None
    name: str | None = None
    source: str | None = None
    config: dict | None = None
    refresh_freq: int | None = 60  # 刷新频率（分钟）
    prune_freq: int | None = 0  # 修剪频率（分钟）
    timeout_secs: int | None = 3600  # 超时时间（秒）


class ResumeConnectorRequest(BaseModel):
    """恢复/暂停连接器请求"""

    resume: bool = True  # True: 恢复调度, False: 取消调度


class RebuildRequest(BaseModel):
    """重建连接器请求"""

    kb_id: str


class GoogleWebOAuthStartRequest(BaseModel):
    """启动 Google Web OAuth 请求"""

    credentials: str | dict  # Google OAuth 凭证 JSON
    redirect_uri: str | None = None  # 自定义重定向 URI（可选）


class GoogleWebOAuthResultRequest(BaseModel):
    """获取 Google Web OAuth 结果请求"""

    flow_id: str


class BoxWebOAuthStartRequest(BaseModel):
    """启动 Box Web OAuth 请求"""

    client_id: str
    client_secret: str
    redirect_uri: str | None = None


class BoxWebOAuthResultRequest(BaseModel):
    """获取 Box Web OAuth 结果请求"""

    flow_id: str


# ==================== 辅助 ====================


def _respond(success: bool, result, retcode: RetCode | None = None):
    if success:
        return get_json_result(data=result)
    if retcode in (None, RetCode.DATA_ERROR):
        return get_data_error_result(retmsg=result)
    return get_json_result(data=False, retmsg=result, retcode=retcode)


def _auth_error():
    return get_json_result(data=False, retmsg=connector_api_service.AUTH_ERROR, retcode=RetCode.AUTHENTICATION_ERROR)


def _read_after_write(db: Session, connector_id: str):
    time.sleep(connector_api_service.WRITE_SETTLE_SECS)
    return _respond(*connector_api_service.get_connector(db, connector_id))


# ==================== 连接器 CRUD（deprecated） ====================


@router.post(
    "/set",
    summary="[Deprecated] 创建或更新连接器（请改用 POST /api/v1/connectors 或 PATCH /api/v1/connectors/{connector_id}）",
    response_description="连接器信息",
    deprecated=True,
)
def set_connector(request: SetConnectorRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req = request.model_dump(exclude_none=True)
    try:
        if req.get("id"):
            connector_id = req["id"]
            if not connector_api_service.accessible(db, connector_id, user.id):
                return _auth_error()
            success, result, retcode = connector_api_service.update_connector(db, connector_id, req)
        else:
            success, result, retcode = connector_api_service.create_connector(db, req, user.id)
        if not success:
            return _respond(success, result, retcode)
        return _read_after_write(db, result)
    except Exception as e:
        return server_error_response(e)


@router.get("/list", summary="[Deprecated] 获取连接器列表（请改用 GET /api/v1/connectors）", response_description="连接器列表", deprecated=True)
def list_connector(db: Session = Depends(get_db), user=Depends(manager)):
    try:
        return get_json_result(data=connector_api_service.list_connectors(db, user.id))
    except Exception as e:
        return server_error_response(e)


@router.get(
    "/{connector_id}",
    summary="[Deprecated] 获取连接器详情（请改用 GET /api/v1/connectors/{connector_id}）",
    response_description="连接器详情",
    deprecated=True,
)
def get_connector(connector_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        if not connector_api_service.accessible(db, connector_id, user.id):
            return _auth_error()
        return _respond(*connector_api_service.get_connector(db, connector_id))
    except Exception as e:
        return server_error_response(e)


@router.get(
    "/{connector_id}/logs",
    summary="[Deprecated] 获取同步日志（请改用 GET /api/v1/connectors/{connector_id}/logs）",
    response_description="同步日志列表",
    deprecated=True,
)
def list_logs(
    connector_id: str,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(15, ge=1, description="每页条数"),
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    try:
        if not connector_api_service.accessible(db, connector_id, user.id):
            return _auth_error()
        return get_json_result(data=connector_api_service.list_logs(db, connector_id, page, page_size))
    except Exception as e:
        return server_error_response(e)


@router.put(
    "/{connector_id}/resume",
    summary="[Deprecated] 恢复或暂停连接器（请改用 POST /api/v1/connectors/{connector_id}/resume）",
    response_description="操作结果",
    deprecated=True,
)
def resume(connector_id: str, request: ResumeConnectorRequest, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        if not connector_api_service.accessible(db, connector_id, user.id):
            return _auth_error()
        return get_json_result(data=connector_api_service.resume_connector(db, connector_id, request.resume))
    except Exception as e:
        return server_error_response(e)


@router.put(
    "/{connector_id}/rebuild",
    summary="[Deprecated] 重建连接器（请改用 POST /api/v1/connectors/{connector_id}/rebuild）",
    response_description="操作结果",
    deprecated=True,
)
def rebuild(connector_id: str, request: RebuildRequest, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        if not connector_api_service.accessible(db, connector_id, user.id):
            return _auth_error()
        return _respond(*connector_api_service.rebuild_connector(db, connector_id, request.kb_id, user.id))
    except Exception as e:
        return server_error_response(e)


@router.post(
    "/{connector_id}/rm",
    summary="[Deprecated] 删除连接器（请改用 DELETE /api/v1/connectors/{connector_id}）",
    response_description="操作结果",
    deprecated=True,
)
def rm_connector(connector_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        if not connector_api_service.accessible(db, connector_id, user.id):
            return _auth_error()
        return get_json_result(data=connector_api_service.remove_connector(db, connector_id))
    except Exception as e:
        return server_error_response(e)


# ==================== Google OAuth（deprecated；回调路径见模块头说明） ====================


@router.post(
    "/google/oauth/web/start",
    summary="[Deprecated] 启动 Google OAuth（请改用 POST /api/v1/connectors/google/oauth/web/start）",
    response_description="OAuth 授权 URL",
    deprecated=True,
)
def start_google_web_oauth(
    request: GoogleWebOAuthStartRequest,
    source: str = Query("google-drive", description="OAuth 类型: google-drive 或 gmail"),
    user=Depends(manager),
):
    success, result, retcode = connector_oauth_service.start_google_oauth(user.id, source, request.credentials, request.redirect_uri)
    if not success:
        return get_json_result(retcode=retcode or RetCode.ARGUMENT_ERROR, retmsg=result)
    return get_json_result(data=result)


@router.get(
    "/google-drive/oauth/web/callback",
    summary="[Deprecated] Google Drive OAuth 回调（注册在 Google 后台的 redirect_uri 仍指向此路径）",
    response_class=HTMLResponse,
    deprecated=True,
)
def google_drive_web_oauth_callback(
    state: str | None = Query(None, description="OAuth state（即 flow_id）"),
    code: str | None = Query(None, description="授权码"),
    error: str | None = Query(None, description="错误码"),
    error_description: str | None = Query(None, description="错误描述"),
):
    flow_id, success, message = connector_oauth_service.handle_google_callback("google-drive", state, code, error, error_description)
    return HTMLResponse(content=connector_oauth_service.render_oauth_popup_html(flow_id, success, message, "google-drive"), status_code=200)


@router.get(
    "/gmail/oauth/web/callback",
    summary="[Deprecated] Gmail OAuth 回调（注册在 Google 后台的 redirect_uri 仍指向此路径）",
    response_class=HTMLResponse,
    deprecated=True,
)
def gmail_web_oauth_callback(
    state: str | None = Query(None, description="OAuth state（即 flow_id）"),
    code: str | None = Query(None, description="授权码"),
    error: str | None = Query(None, description="错误码"),
    error_description: str | None = Query(None, description="错误描述"),
):
    flow_id, success, message = connector_oauth_service.handle_google_callback("gmail", state, code, error, error_description)
    return HTMLResponse(content=connector_oauth_service.render_oauth_popup_html(flow_id, success, message, "gmail"), status_code=200)


@router.post(
    "/google/oauth/web/result",
    summary="[Deprecated] 获取 Google OAuth 结果（请改用 POST /api/v1/connectors/google/oauth/web/result）",
    response_description="OAuth 凭证",
    deprecated=True,
)
def poll_google_web_result(
    request: GoogleWebOAuthResultRequest,
    source: str = Query(..., description="OAuth 类型: google-drive 或 gmail"),
    user=Depends(manager),
):
    success, result, retcode = connector_oauth_service.poll_google_result(user.id, source, request.flow_id)
    if not success:
        return get_json_result(retcode=retcode or RetCode.ARGUMENT_ERROR, retmsg=result)
    return get_json_result(data=result)


# ==================== Box OAuth（deprecated） ====================


@router.post(
    "/box/oauth/web/start",
    summary="[Deprecated] 启动 Box OAuth（请改用 POST /api/v1/connectors/box/oauth/web/start）",
    response_description="OAuth 授权 URL",
    deprecated=True,
)
def start_box_web_oauth(request: BoxWebOAuthStartRequest, user=Depends(manager)):
    success, result, retcode = connector_oauth_service.start_box_oauth(user.id, request.client_id, request.client_secret, request.redirect_uri)
    if not success:
        return get_json_result(retcode=retcode or RetCode.ARGUMENT_ERROR, retmsg=result)
    return get_json_result(data=result)


@router.get(
    "/box/oauth/web/callback",
    summary="[Deprecated] Box OAuth 回调（注册在 Box 后台的 redirect_uri 仍指向此路径）",
    response_class=HTMLResponse,
    deprecated=True,
)
def box_web_oauth_callback(
    state: str | None = Query(None, description="OAuth state（即 flow_id）"),
    code: str | None = Query(None, description="授权码"),
    error: str | None = Query(None, description="错误码"),
    error_description: str | None = Query(None, description="错误描述"),
):
    flow_id, success, message = connector_oauth_service.handle_box_callback(state, code, error, error_description)
    return HTMLResponse(content=connector_oauth_service.render_oauth_popup_html(flow_id, success, message, "box"), status_code=200)


@router.post(
    "/box/oauth/web/result",
    summary="[Deprecated] 获取 Box OAuth 结果（请改用 POST /api/v1/connectors/box/oauth/web/result）",
    response_description="OAuth 凭证",
    deprecated=True,
)
def poll_box_web_result(request: BoxWebOAuthResultRequest, user=Depends(manager)):
    success, result, retcode = connector_oauth_service.poll_box_result(user.id, request.flow_id)
    if not success:
        return get_json_result(retcode=retcode or RetCode.ARGUMENT_ERROR, retmsg=result)
    return get_json_result(data=result)
