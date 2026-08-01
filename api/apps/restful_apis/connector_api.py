"""Connector RESTful API.

Routes are mounted under ``/api/v1`` by ``api.apps.register_page``:
    POST   /connectors                              新建连接器
    GET    /connectors                              列出本租户连接器
    PATCH  /connectors/{connector_id}               更新调度配置
    GET    /connectors/{connector_id}               连接器详情
    DELETE /connectors/{connector_id}               删除连接器
    GET    /connectors/{connector_id}/logs          同步日志
    POST   /connectors/{connector_id}/resume        恢复/暂停调度
    POST   /connectors/{connector_id}/rebuild       重建与知识库的关联
    POST   /connectors/google/oauth/web/start       启动 Google OAuth
    GET    /connectors/google-drive/oauth/web/callback
    GET    /connectors/gmail/oauth/web/callback
    POST   /connectors/google/oauth/web/result      轮询 Google OAuth 结果
    POST   /connectors/box/oauth/web/start          启动 Box OAuth
    GET    /connectors/box/oauth/web/callback
    POST   /connectors/box/oauth/web/result         轮询 Box OAuth 结果

业务逻辑在 ``api/apps/services/connector_api_service.py``（CRUD）与
``api/apps/services/connector_oauth_service.py``（OAuth 流程）；旧的 ``/v1/connector/*``
端点留在 ``api/apps/connector_app.py`` 并已标 deprecated。

**OAuth 回调新旧路径并存不是冗余**：``*_WEB_OAUTH_REDIRECT_URI`` 的默认值仍指向
``/v1/connector/...``，且这些 URI 注册在 Google/Box 应用后台，单方面改路径会让存量授权
流 404（上游改了路由却没改默认值，默认配置下回调即断）。

鉴权：按 id 定位的操作先过 ``ConnectorService.accessible``——连接器 config 里存着数据源
凭证，缺这一关等于任意登录用户可读写他人凭证。
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.apps.services import connector_api_service, connector_oauth_service
from api.db.db_models import get_async_db
from api.utils.api_utils import Principal, async_current_user, get_data_error_result, get_json_result, server_error_response
from common.constants import RetCode

router = APIRouter()


# ==================== 请求体模型 ====================


class CreateConnectorRequest(BaseModel):
    """新建连接器请求"""

    name: str
    source: str
    config: dict | None = None
    refresh_freq: int | None = 60  # 刷新频率（分钟）
    prune_freq: int | None = 0  # 修剪频率（分钟）
    timeout_secs: int | None = 3600  # 超时时间（秒）


class UpdateConnectorRequest(BaseModel):
    """更新连接器调度配置请求（仅这四个字段可改）"""

    config: dict | None = None
    refresh_freq: int | None = None
    prune_freq: int | None = None
    timeout_secs: int | None = None


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


async def _require_access(db: AsyncSession, connector_id: str, user_id: str) -> bool:
    return await db.run_sync(lambda s: connector_api_service.accessible(s, connector_id, user_id))  # TODO(async-phase4)


def _auth_error():
    return get_json_result(data=False, retmsg=connector_api_service.AUTH_ERROR, retcode=RetCode.AUTHENTICATION_ERROR)


# ==================== 连接器 CRUD ====================


@router.post("/connectors", summary="新建连接器", response_description="连接器信息")
async def create_connector(
    request: CreateConnectorRequest,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    """新建数据源连接器，连接器归当前租户所有。"""
    req = request.model_dump(exclude_none=True)
    try:
        success, result, retcode = await db.run_sync(lambda s: connector_api_service.create_connector(s, req, user.id))  # TODO(async-phase4)
        if not success:
            return _respond(success, result, retcode)

        await asyncio.sleep(connector_api_service.WRITE_SETTLE_SECS)
        return _respond(*await db.run_sync(lambda s: connector_api_service.get_connector(s, result)))  # TODO(async-phase4)
    except Exception as e:
        return server_error_response(e)


@router.get("/connectors", summary="获取连接器列表", response_description="连接器列表")
async def list_connector(
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    """列出当前租户的全部连接器。"""
    try:
        connectors = await db.run_sync(lambda s: connector_api_service.list_connectors(s, user.id))  # TODO(async-phase4)
        return get_json_result(data=connectors)
    except Exception as e:
        return server_error_response(e)


@router.patch("/connectors/{connector_id}", summary="更新连接器", response_description="连接器信息")
async def update_connector(
    connector_id: str,
    request: UpdateConnectorRequest,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    """更新连接器的调度配置（config / refresh_freq / prune_freq / timeout_secs）。"""
    req = request.model_dump(exclude_none=True)
    try:
        if not await _require_access(db, connector_id, user.id):
            return _auth_error()

        success, result, retcode = await db.run_sync(lambda s: connector_api_service.update_connector(s, connector_id, req))  # TODO(async-phase4)
        if not success:
            return _respond(success, result, retcode)

        await asyncio.sleep(connector_api_service.WRITE_SETTLE_SECS)
        return _respond(*await db.run_sync(lambda s: connector_api_service.get_connector(s, connector_id)))  # TODO(async-phase4)
    except Exception as e:
        return server_error_response(e)


@router.get("/connectors/{connector_id}", summary="获取连接器详情", response_description="连接器详情")
async def get_connector(
    connector_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    """获取连接器详情（含数据源配置）。"""
    try:
        if not await _require_access(db, connector_id, user.id):
            return _auth_error()
        return _respond(*await db.run_sync(lambda s: connector_api_service.get_connector(s, connector_id)))  # TODO(async-phase4)
    except Exception as e:
        return server_error_response(e)


@router.delete("/connectors/{connector_id}", summary="删除连接器", response_description="操作结果")
async def rm_connector(
    connector_id: str,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    """删除连接器，删除前先取消其全部同步任务。"""
    try:
        if not await _require_access(db, connector_id, user.id):
            return _auth_error()

        def _remove(s: Session) -> bool:
            return connector_api_service.remove_connector(s, connector_id)

        return get_json_result(data=await db.run_sync(_remove))  # TODO(async-phase4)
    except Exception as e:
        return server_error_response(e)


@router.get("/connectors/{connector_id}/logs", summary="获取同步日志", response_description="同步日志列表")
async def list_logs(
    connector_id: str,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(15, ge=1, description="每页条数"),
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    """分页获取连接器的同步日志。"""
    try:
        if not await _require_access(db, connector_id, user.id):
            return _auth_error()
        logs = await db.run_sync(lambda s: connector_api_service.list_logs(s, connector_id, page, page_size))  # TODO(async-phase4)
        return get_json_result(data=logs)
    except Exception as e:
        return server_error_response(e)


@router.post("/connectors/{connector_id}/resume", summary="恢复或暂停连接器", response_description="操作结果")
async def resume(
    connector_id: str,
    request: ResumeConnectorRequest,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    """恢复（``resume=true``）或取消（``resume=false``）连接器的同步调度。"""
    try:
        if not await _require_access(db, connector_id, user.id):
            return _auth_error()
        resumed = await db.run_sync(lambda s: connector_api_service.resume_connector(s, connector_id, request.resume))  # TODO(async-phase4)
        return get_json_result(data=resumed)
    except Exception as e:
        return server_error_response(e)


@router.post("/connectors/{connector_id}/rebuild", summary="重建连接器", response_description="操作结果")
async def rebuild(
    connector_id: str,
    request: RebuildRequest,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    """重建连接器与指定知识库的关联，重新拉取全量数据。"""
    try:
        if not await _require_access(db, connector_id, user.id):
            return _auth_error()
        return _respond(*await db.run_sync(lambda s: connector_api_service.rebuild_connector(s, connector_id, request.kb_id, user.id)))  # TODO(async-phase4)
    except Exception as e:
        return server_error_response(e)


# ==================== Google OAuth ====================


@router.post("/connectors/google/oauth/web/start", summary="启动 Google OAuth", response_description="OAuth 授权 URL")
async def start_google_web_oauth(
    request: GoogleWebOAuthStartRequest,
    source: str = Query("google-drive", description="OAuth 类型: google-drive 或 gmail"),
    user: Principal = Depends(async_current_user),
):
    """用上传的 client 配置发起浏览器 OAuth，返回授权 URL 与 flow_id。"""
    success, result, retcode = connector_oauth_service.start_google_oauth(user.id, source, request.credentials, request.redirect_uri)
    if not success:
        return get_json_result(retcode=retcode or RetCode.ARGUMENT_ERROR, retmsg=result)
    return get_json_result(data=result)


@router.get("/connectors/google-drive/oauth/web/callback", summary="Google Drive OAuth 回调", response_class=HTMLResponse)
async def google_drive_web_oauth_callback(
    state: str | None = Query(None, description="OAuth state（即 flow_id）"),
    code: str | None = Query(None, description="授权码"),
    error: str | None = Query(None, description="错误码"),
    error_description: str | None = Query(None, description="错误描述"),
):
    """Google Drive 授权回调，渲染向主窗口 postMessage 的弹窗页。"""
    flow_id, success, message = connector_oauth_service.handle_google_callback("google-drive", state, code, error, error_description)
    return HTMLResponse(content=connector_oauth_service.render_oauth_popup_html(flow_id, success, message, "google-drive"), status_code=200)


@router.get("/connectors/gmail/oauth/web/callback", summary="Gmail OAuth 回调", response_class=HTMLResponse)
async def gmail_web_oauth_callback(
    state: str | None = Query(None, description="OAuth state（即 flow_id）"),
    code: str | None = Query(None, description="授权码"),
    error: str | None = Query(None, description="错误码"),
    error_description: str | None = Query(None, description="错误描述"),
):
    """Gmail 授权回调，渲染向主窗口 postMessage 的弹窗页。"""
    flow_id, success, message = connector_oauth_service.handle_google_callback("gmail", state, code, error, error_description)
    return HTMLResponse(content=connector_oauth_service.render_oauth_popup_html(flow_id, success, message, "gmail"), status_code=200)


@router.post("/connectors/google/oauth/web/result", summary="获取 Google OAuth 结果", response_description="OAuth 凭证")
async def poll_google_web_result(
    request: GoogleWebOAuthResultRequest,
    source: str = Query(..., description="OAuth 类型: google-drive 或 gmail"),
    user: Principal = Depends(async_current_user),
):
    """轮询授权结果；未完成返回 RUNNING，完成后返回凭证并清除缓存。"""
    success, result, retcode = connector_oauth_service.poll_google_result(user.id, source, request.flow_id)
    if not success:
        return get_json_result(retcode=retcode or RetCode.ARGUMENT_ERROR, retmsg=result)
    return get_json_result(data=result)


# ==================== Box OAuth ====================


@router.post("/connectors/box/oauth/web/start", summary="启动 Box OAuth", response_description="OAuth 授权 URL")
async def start_box_web_oauth(
    request: BoxWebOAuthStartRequest,
    user: Principal = Depends(async_current_user),
):
    """用 Box 应用的 client_id/secret 发起浏览器 OAuth。"""
    success, result, retcode = connector_oauth_service.start_box_oauth(user.id, request.client_id, request.client_secret, request.redirect_uri)
    if not success:
        return get_json_result(retcode=retcode or RetCode.ARGUMENT_ERROR, retmsg=result)
    return get_json_result(data=result)


@router.get("/connectors/box/oauth/web/callback", summary="Box OAuth 回调", response_class=HTMLResponse)
async def box_web_oauth_callback(
    state: str | None = Query(None, description="OAuth state（即 flow_id）"),
    code: str | None = Query(None, description="授权码"),
    error: str | None = Query(None, description="错误码"),
    error_description: str | None = Query(None, description="错误描述"),
):
    """Box 授权回调，渲染向主窗口 postMessage 的弹窗页。"""
    flow_id, success, message = connector_oauth_service.handle_box_callback(state, code, error, error_description)
    return HTMLResponse(content=connector_oauth_service.render_oauth_popup_html(flow_id, success, message, "box"), status_code=200)


@router.post("/connectors/box/oauth/web/result", summary="获取 Box OAuth 结果", response_description="OAuth 凭证")
async def poll_box_web_result(
    request: BoxWebOAuthResultRequest,
    user: Principal = Depends(async_current_user),
):
    """轮询 Box 授权结果；未完成返回 RUNNING，完成后返回凭证并清除缓存。"""
    success, result, retcode = connector_oauth_service.poll_box_result(user.id, request.flow_id)
    if not success:
        return get_json_result(retcode=retcode or RetCode.ARGUMENT_ERROR, retmsg=result)
    return get_json_result(data=result)
