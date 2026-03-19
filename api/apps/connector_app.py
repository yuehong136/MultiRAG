# coding=utf-8
"""
@project: multirag
@Author：龙
@file： connector_app.py
@date：2025/12/19 16:00
@desc: 数据源连接器管理接口
"""
import json
import logging
import time
import uuid
from html import escape
from typing import Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from google_auth_oauthlib.flow import Flow
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.apps import manager
from api.db import InputType
from common.constants import TaskStatus
from api.db.db_models import get_db
from api.db.services.connector_service import ConnectorService, Connector2KbService, SyncLogsService
from api.utils.api_utils import get_json_result, get_data_error_result, server_error_response
from common.misc_utils import get_uuid
from common.constants import RetCode
from common.data_source.config import GOOGLE_DRIVE_WEB_OAUTH_REDIRECT_URI, GMAIL_WEB_OAUTH_REDIRECT_URI, BOX_WEB_OAUTH_REDIRECT_URI, DocumentSource
from common.data_source.google_util.constant import WEB_OAUTH_POPUP_TEMPLATE, GOOGLE_SCOPES
from core.utils.redis_conn import REDIS_CONN
from box_sdk_gen import BoxOAuth, OAuthConfig, GetAuthorizeUrlOptions

router = APIRouter()


# ==================== Web OAuth 常量和辅助函数 ====================

WEB_FLOW_TTL_SECS = 15 * 60  # 15分钟


def _web_state_cache_key(flow_id: str, source_type: str | None = None) -> str:
    """生成 OAuth 状态缓存 key

    根据 source_type 生成对应的前缀，支持 google-drive、gmail、box 等。
    """
    prefix = f"{source_type}_web_flow_state"
    return f"{prefix}:{flow_id}"


def _web_result_cache_key(flow_id: str, source_type: str | None = None) -> str:
    """生成 OAuth 结果缓存 key

    与 _web_state_cache_key 使用相同的逻辑。
    """
    prefix = f"{source_type}_web_flow_result"
    return f"{prefix}:{flow_id}"


def _load_credentials(payload: str | dict[str, Any]) -> dict[str, Any]:
    """加载并解析 Google 凭证 JSON"""
    if isinstance(payload, dict):
        return payload
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError("Invalid Google credentials JSON.") from exc


def _get_web_client_config(credentials: dict[str, Any]) -> dict[str, Any]:
    """从凭证中获取 web 客户端配置"""
    web_section = credentials.get("web")
    if not isinstance(web_section, dict):
        raise ValueError("Google OAuth JSON must include a 'web' client configuration to use browser-based authorization.")
    return {"web": web_section}


def _render_web_oauth_popup(flow_id: str, success: bool, message: str, source: str = "drive") -> HTMLResponse:
    """渲染 OAuth 弹窗页面"""
    status = "success" if success else "error"
    auto_close = "window.close();" if success else ""
    escaped_message = escape(message)
    #   Drive: multirag-google-drive-oauth
    #   Gmail: multirag-gmail-oauth
    payload_type = f"multirag-{source}-oauth"
    payload_json = json.dumps(
        {
            "type": payload_type,
            "status": status,
            "flowId": flow_id or "",
            "message": message,
        }
    )
    html = WEB_OAUTH_POPUP_TEMPLATE.format(
        title=f"Google {source.capitalize()} Authorization",
        heading="Authorization complete" if success else "Authorization failed",
        message=escaped_message,
        payload_json=payload_json,
        auto_close=auto_close,
    )
    return HTMLResponse(content=html, status_code=200)


# ==================== 请求体模型定义 ====================

class SetConnectorRequest(BaseModel):
    """创建或更新连接器请求"""
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


class LinkKbRequest(BaseModel):
    """关联知识库请求"""
    kb_ids: list[str]


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


# ==================== 接口定义 ====================

@router.post("/set", summary="创建或更新连接器", response_description="连接器信息")
def set_connector(
    request: SetConnectorRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### POST `/set` 创建或更新连接器

    **功能描述**:
    创建新的数据源连接器或更新现有连接器的配置。

    ---

    ### 请求体 (Request Body)

    | 字段          | 类型   | 必填 | 描述                           |
    |---------------|--------|------|--------------------------------|
    | `id`          | string | 否   | 连接器ID，不传则创建新连接器   |
    | `name`        | string | 是*  | 连接器名称（创建时必填）       |
    | `source`      | string | 是*  | 数据源类型（创建时必填）       |
    | `config`      | object | 是   | 数据源配置信息                 |
    | `refresh_freq`| int    | 否   | 刷新频率（分钟），默认60       |
    | `prune_freq`  | int    | 否   | 修剪频率（分钟），默认0        |
    | `timeout_secs`| int    | 否   | 超时时间（秒），默认3600       |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "id": "connector_id",
            "name": "My Connector",
            "source": "notion",
            ...
        }
    }
    ```
    """
    try:
        req = request.model_dump(exclude_none=True)

        if req.get("id"):
            # 更新现有连接器
            conn = {
                fld: req[fld]
                for fld in ["prune_freq", "refresh_freq", "config", "timeout_secs"]
                if fld in req
            }
            ConnectorService.update_by_id(db, req["id"], conn)
        else:
            # 创建新连接器
            if not req.get("name") or not req.get("source"):
                return get_data_error_result(retmsg="创建连接器时 name 和 source 为必填项")

            req["id"] = get_uuid()
            conn = {
                "id": req["id"],
                "tenant_id": user.id,
                "name": req["name"],
                "source": req["source"],
                "input_type": InputType.POLL,
                "config": req.get("config", {}),
                "refresh_freq": int(req.get("refresh_freq", 5)),
                "prune_freq": int(req.get("prune_freq", 720)),
                "timeout_secs": int(req.get("timeout_secs", 60 * 29)),
                "status": TaskStatus.SCHEDULE
            }
            ConnectorService.insert(db, **conn)

        time.sleep(1)  # 等待数据库写入完成
        connector = ConnectorService.get_by_id(db, req["id"])
        if not connector:
            return get_data_error_result(retmsg="创建连接器失败")

        return get_json_result(data=connector.to_dict())
    except Exception as e:
        return server_error_response(e)


@router.get("/list", summary="获取连接器列表", response_description="连接器列表")
def list_connector(
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### GET `/list` 获取连接器列表

    **功能描述**:
    获取当前用户的所有数据源连接器列表。

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": [
            {
                "id": "connector_id",
                "name": "My Connector",
                "source": "notion",
                "status": "schedule"
            },
            ...
        ]
    }
    ```
    """
    try:
        connectors = ConnectorService.list(db, user.id)
        return get_json_result(data=connectors)
    except Exception as e:
        return server_error_response(e)


@router.get("/{connector_id}", summary="获取连接器详情", response_description="连接器详情")
def get_connector(
    connector_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### GET `/{connector_id}` 获取连接器详情

    **功能描述**:
    根据连接器ID获取连接器的详细信息。

    ---

    ### 路径参数

    | 参数          | 类型   | 描述       |
    |---------------|--------|------------|
    | `connector_id`| string | 连接器ID   |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "id": "connector_id",
            "name": "My Connector",
            "source": "notion",
            "config": {...},
            ...
        }
    }
    ```
    """
    try:
        connector = ConnectorService.get_by_id(db, connector_id)
        if not connector:
            return get_data_error_result(retmsg="找不到该连接器")
        return get_json_result(data=connector.to_dict())
    except Exception as e:
        return server_error_response(e)


@router.get("/{connector_id}/logs", summary="获取同步日志", response_description="同步日志列表")
def list_logs(
    connector_id: str,
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(15, ge=1, le=100, description="每页数量"),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### GET `/{connector_id}/logs` 获取同步日志

    **功能描述**:
    获取指定连接器的同步任务日志列表。

    ---

    ### 路径参数

    | 参数          | 类型   | 描述       |
    |---------------|--------|------------|
    | `connector_id`| string | 连接器ID   |

    ### 查询参数

    | 参数       | 类型 | 必填 | 描述             |
    |------------|------|------|------------------|
    | `page`     | int  | 否   | 页码，默认1      |
    | `page_size`| int  | 否   | 每页数量，默认15 |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": [
            {
                "id": "log_id",
                "connector_id": "connector_id",
                "status": "done",
                "new_docs_indexed": 10,
                ...
            },
            ...
        ]
    }
    ```
    """
    try:
        arr, total = SyncLogsService.list_sync_tasks(db, connector_id, page, page_size)
        return get_json_result(data={"total": total, "logs": arr})
    except Exception as e:
        return server_error_response(e)


@router.put("/{connector_id}/resume", summary="恢复或暂停连接器", response_description="操作结果")
def resume(
    connector_id: str,
    request: ResumeConnectorRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### PUT `/{connector_id}/resume` 恢复或暂停连接器

    **功能描述**:
    恢复或暂停连接器的同步调度任务。

    ---

    ### 路径参数

    | 参数          | 类型   | 描述       |
    |---------------|--------|------------|
    | `connector_id`| string | 连接器ID   |

    ### 请求体 (Request Body)

    | 字段     | 类型    | 必填 | 描述                              |
    |----------|---------|------|-----------------------------------|
    | `resume` | boolean | 否   | true=恢复调度，false=暂停，默认true |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": true
    }
    ```
    """
    try:
        req = request.model_dump()
        if req.get("resume"):
            ConnectorService.resume(db, connector_id, TaskStatus.SCHEDULE)
        else:
            ConnectorService.resume(db, connector_id, TaskStatus.CANCEL)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post("/{connector_id}/link", summary="关联知识库", response_description="操作结果")
def link_kb(
    connector_id: str,
    request: LinkKbRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### POST `/{connector_id}/link` 关联知识库

    **功能描述**:
    将连接器与一个或多个知识库进行关联，同步的文档将被导入到关联的知识库中。

    ---

    ### 路径参数

    | 参数          | 类型   | 描述       |
    |---------------|--------|------------|
    | `connector_id`| string | 连接器ID   |

    ### 请求体 (Request Body)

    | 字段     | 类型          | 必填 | 描述               |
    |----------|---------------|------|--------------------|
    | `kb_ids` | list[string]  | 是   | 要关联的知识库ID列表 |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": true
    }
    ```

    #### 错误响应 (500)
    ```json
    {
        "retcode": 500,
        "retmsg": "关联过程中的错误信息",
        "data": false
    }
    ```
    """
    try:
        req = request.model_dump()
        errors = Connector2KbService.link_kb(db, connector_id, req["kb_ids"], user.id)
        if errors:
            return get_json_result(data=False, retmsg=errors, retcode=RetCode.SERVER_ERROR)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.put("/{connector_id}/rebuild", summary="重建连接器", response_description="操作结果")
def rebuild(
    connector_id: str,
    request: RebuildRequest,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### PUT `/{connector_id}/rebuild` 重建连接器

    **功能描述**:
    重建连接器与指定知识库的关联，删除已同步的文档并重新开始同步。

    ---

    ### 路径参数

    | 参数          | 类型   | 描述       |
    |---------------|--------|------------|
    | `connector_id`| string | 连接器ID   |

    ### 请求体 (Request Body)

    | 字段     | 类型   | 必填 | 描述           |
    |----------|--------|------|----------------|
    | `kb_id`  | string | 是   | 知识库ID       |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": true
    }
    ```
    """
    try:
        req = request.model_dump()
        err = ConnectorService.rebuild(db, connector_id, req["kb_id"], user.id)
        if err:
            return get_json_result(data=False, retmsg=err, retcode=RetCode.SERVER_ERROR)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post("/{connector_id}/rm", summary="删除连接器", response_description="操作结果")
def rm_connector(
    connector_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    ### POST `/{connector_id}/rm` 删除连接器

    **功能描述**:
    删除指定的数据源连接器。删除前会先取消所有相关的同步任务。

    ---

    ### 路径参数

    | 参数          | 类型   | 描述       |
    |---------------|--------|------------|
    | `connector_id`| string | 连接器ID   |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": true
    }
    ```

    ---

    ### 注意事项

    - 删除连接器会取消所有相关的调度任务
    - 已同步的文档不会被删除，需要手动清理
    """
    try:
        # 先取消所有相关任务
        ConnectorService.resume(db, connector_id, TaskStatus.CANCEL)
        # 删除连接器
        ConnectorService.delete_by_id(db, connector_id)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


# ==================== Google OAuth 接口 ====================

@router.post("/google/oauth/web/start", summary="启动 Google OAuth（统一接口）", response_description="OAuth 授权 URL")
def start_google_web_oauth(
    request: GoogleWebOAuthStartRequest,
    source: str = Query("google-drive", description="OAuth 类型: google-drive 或 gmail"),
    user=Depends(manager)
):
    """
    ### POST `/google/oauth/web/start` 启动 Google OAuth（统一接口）

    **功能描述**:
    启动 Google Drive 或 Gmail 的 Web 端 OAuth 授权流程，返回授权 URL 供前端打开弹窗进行授权。

    ---

    ### 查询参数

    | 参数     | 类型   | 必填 | 描述                              |
    |----------|--------|------|-----------------------------------|
    | `source` | string | 否   | OAuth 类型: google-drive 或 gmail |

    ### 请求体 (Request Body)

    | 字段          | 类型         | 必填 | 描述                              |
    |---------------|--------------|------|-----------------------------------|
    | `credentials` | string/dict  | 是   | Google OAuth 凭证 JSON            |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "flow_id": "uuid",
            "authorization_url": "https://accounts.google.com/o/oauth2/...",
            "expires_in": 900
        }
    }
    ```
    """
    if source not in ("google-drive", "gmail"):
        return get_json_result(retcode=RetCode.ARGUMENT_ERROR, retmsg="Invalid Google OAuth type.")

    if source == "gmail":
        default_redirect_uri = GMAIL_WEB_OAUTH_REDIRECT_URI
        scopes = GOOGLE_SCOPES[DocumentSource.GMAIL]
    else:
        default_redirect_uri = GOOGLE_DRIVE_WEB_OAUTH_REDIRECT_URI
        scopes = GOOGLE_SCOPES[DocumentSource.GOOGLE_DRIVE]

    redirect_uri = request.redirect_uri or default_redirect_uri
    if isinstance(redirect_uri, str):
        redirect_uri = redirect_uri.strip()

    if not redirect_uri:
        return get_json_result(
            retcode=RetCode.SERVER_ERROR,
            retmsg="Google OAuth redirect URI is not configured on the server.",
        )

    raw_credentials = request.credentials
    try:
        credentials = _load_credentials(raw_credentials)
    except ValueError as exc:
        return get_json_result(retcode=RetCode.ARGUMENT_ERROR, retmsg=str(exc))

    if credentials.get("refresh_token"):
        return get_json_result(
            retcode=RetCode.ARGUMENT_ERROR,
            retmsg="Uploaded credentials already include a refresh token.",
        )

    try:
        client_config = _get_web_client_config(credentials)
    except ValueError as exc:
        return get_json_result(retcode=RetCode.ARGUMENT_ERROR, retmsg=str(exc))

    flow_id = str(uuid.uuid4())
    try:
        flow = Flow.from_client_config(client_config, scopes=scopes)
        flow.redirect_uri = redirect_uri
        authorization_url, _ = flow.authorization_url(
            access_type="offline",
            include_granted_scopes="true",
            prompt="consent",
            state=flow_id,
        )
    except Exception as exc:
        logging.exception("Failed to create Google OAuth flow: %s", exc)
        return get_json_result(
            retcode=RetCode.SERVER_ERROR,
            retmsg="Failed to initialize Google OAuth flow. Please verify the uploaded client configuration.",
        )

    cache_payload = {
        "user_id": user.id,
        "client_config": client_config,
        "redirect_uri": redirect_uri,
        "created_at": int(time.time()),
    }
    REDIS_CONN.set_obj(_web_state_cache_key(flow_id, source), cache_payload, WEB_FLOW_TTL_SECS)

    return get_json_result(
        data={
            "flow_id": flow_id,
            "authorization_url": authorization_url,
            "expires_in": WEB_FLOW_TTL_SECS,
        }
    )


@router.get("/google-drive/oauth/web/callback", summary="Google Drive OAuth 回调", response_class=HTMLResponse)
def google_drive_web_oauth_callback(
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """
    ### GET `/google-drive/oauth/web/callback` Google Drive OAuth 回调

    **功能描述**:
    Google Drive OAuth 授权完成后的回调端点，处理授权码并交换令牌。
    此端点由 Google OAuth 重定向调用，不需要用户登录验证。

    ---

    ### 查询参数

    | 参数               | 类型   | 描述                    |
    |--------------------|--------|-------------------------|
    | `state`            | string | OAuth 状态参数 (flow_id) |
    | `code`             | string | 授权码                  |
    | `error`            | string | 错误代码（如果有）      |
    | `error_description`| string | 错误描述（如果有）      |

    ---

    ### 响应 (Response)

    返回 HTML 页面，通过 postMessage 将结果传递给父窗口。
    """
    state_id = state
    source = "google-drive"
    err_desc = error_description or error

    if not state_id:
        return _render_web_oauth_popup("", False, "Missing OAuth state parameter.", source)

    state_cache = REDIS_CONN.get(_web_state_cache_key(state_id, source))
    if not state_cache:
        return _render_web_oauth_popup(state_id, False, "Authorization session expired. Please restart from the main window.", source)

    state_obj = json.loads(state_cache)
    client_config = state_obj.get("client_config")
    redirect_uri = state_obj.get("redirect_uri", GOOGLE_DRIVE_WEB_OAUTH_REDIRECT_URI)
    if not client_config:
        REDIS_CONN.delete(_web_state_cache_key(state_id, source))
        return _render_web_oauth_popup(state_id, False, "Authorization session was invalid. Please retry.", source)

    if error:
        REDIS_CONN.delete(_web_state_cache_key(state_id, source))
        return _render_web_oauth_popup(state_id, False, err_desc or "Authorization was cancelled.", source)

    if not code:
        return _render_web_oauth_popup(state_id, False, "Missing authorization code from Google.", source)

    try:
        flow = Flow.from_client_config(client_config, scopes=GOOGLE_SCOPES[DocumentSource.GOOGLE_DRIVE])
        flow.redirect_uri = redirect_uri
        flow.fetch_token(code=code)
    except Exception as exc:
        logging.exception("Failed to exchange Google OAuth code: %s", exc)
        REDIS_CONN.delete(_web_state_cache_key(state_id, source))
        return _render_web_oauth_popup(state_id, False, "Failed to exchange tokens with Google. Please retry.", source)

    creds_json = flow.credentials.to_json()
    result_payload = {
        "user_id": state_obj.get("user_id"),
        "credentials": creds_json,
    }
    REDIS_CONN.set_obj(_web_result_cache_key(state_id, source), result_payload, WEB_FLOW_TTL_SECS)
    REDIS_CONN.delete(_web_state_cache_key(state_id, source))

    return _render_web_oauth_popup(state_id, True, "Authorization completed successfully.", source)


@router.get("/gmail/oauth/web/callback", summary="Gmail OAuth 回调", response_class=HTMLResponse)
def gmail_web_oauth_callback(
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """
    ### GET `/gmail/oauth/web/callback` Gmail OAuth 回调

    **功能描述**:
    Gmail OAuth 授权完成后的回调端点，处理授权码并交换令牌。
    此端点由 Google OAuth 重定向调用，不需要用户登录验证。

    ---

    ### 查询参数

    | 参数               | 类型   | 描述                    |
    |--------------------|--------|-------------------------|
    | `state`            | string | OAuth 状态参数 (flow_id) |
    | `code`             | string | 授权码                  |
    | `error`            | string | 错误代码（如果有）      |
    | `error_description`| string | 错误描述（如果有）      |

    ---

    ### 响应 (Response)

    返回 HTML 页面，通过 postMessage 将结果传递给父窗口。
    """
    state_id = state
    source = "gmail"
    err_desc = error_description or error

    if not state_id:
        return _render_web_oauth_popup("", False, "Missing OAuth state parameter.", source)

    state_cache = REDIS_CONN.get(_web_state_cache_key(state_id, source))
    if not state_cache:
        return _render_web_oauth_popup(state_id, False, "Authorization session expired. Please restart from the main window.", source)

    state_obj = json.loads(state_cache)
    client_config = state_obj.get("client_config")
    redirect_uri = state_obj.get("redirect_uri", GMAIL_WEB_OAUTH_REDIRECT_URI)
    if not client_config:
        REDIS_CONN.delete(_web_state_cache_key(state_id, source))
        return _render_web_oauth_popup(state_id, False, "Authorization session was invalid. Please retry.", source)

    if error:
        REDIS_CONN.delete(_web_state_cache_key(state_id, source))
        return _render_web_oauth_popup(state_id, False, err_desc or "Authorization was cancelled.", source)

    if not code:
        return _render_web_oauth_popup(state_id, False, "Missing authorization code from Google.", source)

    try:
        flow = Flow.from_client_config(client_config, scopes=GOOGLE_SCOPES[DocumentSource.GMAIL])
        flow.redirect_uri = redirect_uri
        flow.fetch_token(code=code)
    except Exception as exc:
        logging.exception("Failed to exchange Google OAuth code: %s", exc)
        REDIS_CONN.delete(_web_state_cache_key(state_id, source))
        return _render_web_oauth_popup(state_id, False, "Failed to exchange tokens with Google. Please retry.", source)

    creds_json = flow.credentials.to_json()
    result_payload = {
        "user_id": state_obj.get("user_id"),
        "credentials": creds_json,
    }
    REDIS_CONN.set_obj(_web_result_cache_key(state_id, source), result_payload, WEB_FLOW_TTL_SECS)
    REDIS_CONN.delete(_web_state_cache_key(state_id, source))

    return _render_web_oauth_popup(state_id, True, "Authorization completed successfully.", source)


@router.post("/google/oauth/web/result", summary="获取 Google OAuth 结果（统一接口）", response_description="OAuth 凭证")
def poll_google_web_result(
    request: GoogleWebOAuthResultRequest,
    source: str = Query(..., description="OAuth 类型: google-drive 或 gmail"),
    user=Depends(manager)
):
    """
    ### POST `/google/oauth/web/result` 获取 Google OAuth 结果（统一接口）

    **功能描述**:
    轮询获取 Google OAuth 授权的结果。前端在用户完成授权后调用此接口获取凭证。

    ---

    ### 查询参数

    | 参数     | 类型   | 必填 | 描述                              |
    |----------|--------|------|-----------------------------------|
    | `source` | string | 是   | OAuth 类型: google-drive 或 gmail |

    ### 请求体 (Request Body)

    | 字段      | 类型   | 必填 | 描述        |
    |-----------|--------|------|-------------|
    | `flow_id` | string | 是   | OAuth 流程ID |

    ---

    ### 响应 (Response)

    #### 授权完成 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "credentials": "{...}"
        }
    }
    ```

    #### 授权进行中 (102)
    ```json
    {
        "retcode": 102,
        "retmsg": "Authorization is still pending."
    }
    ```

    #### 无权限 (109)
    ```json
    {
        "retcode": 109,
        "retmsg": "You are not allowed to access this authorization result."
    }
    ```
    """
    if source not in ("google-drive", "gmail"):
        return get_json_result(retcode=RetCode.ARGUMENT_ERROR, retmsg="Invalid Google OAuth type.")

    flow_id = request.flow_id
    cache_raw = REDIS_CONN.get(_web_result_cache_key(flow_id, source))
    if not cache_raw:
        return get_json_result(retcode=RetCode.RUNNING, retmsg="Authorization is still pending.")

    result = json.loads(cache_raw)
    if result.get("user_id") != user.id:
        return get_json_result(retcode=RetCode.PERMISSION_ERROR, retmsg="You are not allowed to access this authorization result.")

    REDIS_CONN.delete(_web_result_cache_key(flow_id, source))
    return get_json_result(data={"credentials": result.get("credentials")})


# ==================== Box OAuth 接口 ====================

@router.post("/box/oauth/web/start", summary="启动 Box OAuth", response_description="OAuth 授权 URL")
def start_box_web_oauth(
    request: BoxWebOAuthStartRequest,
    user=Depends(manager)
):
    """
    ### POST `/box/oauth/web/start` 启动 Box OAuth

    **功能描述**:
    启动 Box 的 Web 端 OAuth 授权流程，返回授权 URL 供前端打开弹窗进行授权。

    ---

    ### 请求体 (Request Body)

    | 字段            | 类型   | 必填 | 描述                              |
    |-----------------|--------|------|-----------------------------------|
    | `client_id`     | string | 是   | Box 应用的 Client ID              |
    | `client_secret` | string | 是   | Box 应用的 Client Secret          |
    | `redirect_uri`  | string | 否   | 自定义重定向 URI                  |

    ---

    ### 响应 (Response)

    #### 成功响应 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "flow_id": "uuid",
            "authorization_url": "https://account.box.com/api/oauth2/authorize...",
            "expires_in": 900
        }
    }
    ```
    """
    client_id = request.client_id
    client_secret = request.client_secret
    redirect_uri = request.redirect_uri or BOX_WEB_OAUTH_REDIRECT_URI

    if not client_id or not client_secret:
        return get_json_result(retcode=RetCode.ARGUMENT_ERROR, retmsg="Box client_id and client_secret are required.")

    flow_id = str(uuid.uuid4())

    box_auth = BoxOAuth(
        OAuthConfig(
            client_id=client_id,
            client_secret=client_secret,
        )
    )

    auth_url = box_auth.get_authorize_url(
        options=GetAuthorizeUrlOptions(
            redirect_uri=redirect_uri,
            state=flow_id,
        )
    )

    cache_payload = {
        "user_id": user.id,
        "auth_url": auth_url,
        "client_id": client_id,
        "client_secret": client_secret,
        "created_at": int(time.time()),
    }
    REDIS_CONN.set_obj(_web_state_cache_key(flow_id, "box"), cache_payload, WEB_FLOW_TTL_SECS)

    return get_json_result(
        data={
            "flow_id": flow_id,
            "authorization_url": auth_url,
            "expires_in": WEB_FLOW_TTL_SECS,
        }
    )


@router.get("/box/oauth/web/callback", summary="Box OAuth 回调", response_class=HTMLResponse)
def box_web_oauth_callback(
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    """
    ### GET `/box/oauth/web/callback` Box OAuth 回调

    **功能描述**:
    Box OAuth 授权完成后的回调端点，处理授权码并交换令牌。
    此端点由 Box OAuth 重定向调用，不需要用户登录验证。

    ---

    ### 查询参数

    | 参数               | 类型   | 描述                    |
    |--------------------|--------|-------------------------|
    | `state`            | string | OAuth 状态参数 (flow_id) |
    | `code`             | string | 授权码                  |
    | `error`            | string | 错误代码（如果有）      |
    | `error_description`| string | 错误描述（如果有）      |

    ---

    ### 响应 (Response)

    返回 HTML 页面，通过 postMessage 将结果传递给父窗口。
    """
    flow_id = state
    if not flow_id:
        return _render_web_oauth_popup("", False, "Missing OAuth parameters.", "box")

    if not code:
        return _render_web_oauth_popup(flow_id, False, "Missing authorization code from Box.", "box")

    state_cache = REDIS_CONN.get(_web_state_cache_key(flow_id, "box"))
    if not state_cache:
        return _render_web_oauth_popup(flow_id, False, "Box OAuth session expired or invalid.", "box")

    cache_payload = json.loads(state_cache)

    err_desc = error_description or error
    if error:
        REDIS_CONN.delete(_web_state_cache_key(flow_id, "box"))
        return _render_web_oauth_popup(flow_id, False, err_desc or "Authorization failed.", "box")

    try:
        auth = BoxOAuth(
            OAuthConfig(
                client_id=cache_payload.get("client_id"),
                client_secret=cache_payload.get("client_secret"),
            )
        )

        auth.get_tokens_authorization_code_grant(code)
        token = auth.retrieve_token()

        result_payload = {
            "user_id": cache_payload.get("user_id"),
            "client_id": cache_payload.get("client_id"),
            "client_secret": cache_payload.get("client_secret"),
            "access_token": token.access_token,
            "refresh_token": token.refresh_token,
        }

        REDIS_CONN.set_obj(_web_result_cache_key(flow_id, "box"), result_payload, WEB_FLOW_TTL_SECS)
        REDIS_CONN.delete(_web_state_cache_key(flow_id, "box"))

        return _render_web_oauth_popup(flow_id, True, "Authorization completed successfully.", "box")
    except Exception as exc:
        logging.exception("Failed to exchange Box OAuth code: %s", exc)
        REDIS_CONN.delete(_web_state_cache_key(flow_id, "box"))
        return _render_web_oauth_popup(flow_id, False, "Failed to exchange tokens with Box. Please retry.", "box")


@router.post("/box/oauth/web/result", summary="获取 Box OAuth 结果", response_description="OAuth 凭证")
def poll_box_web_result(
    request: BoxWebOAuthResultRequest,
    user=Depends(manager)
):
    """
    ### POST `/box/oauth/web/result` 获取 Box OAuth 结果

    **功能描述**:
    轮询获取 Box OAuth 授权的结果。前端在用户完成授权后调用此接口获取凭证。

    ---

    ### 请求体 (Request Body)

    | 字段      | 类型   | 必填 | 描述        |
    |-----------|--------|------|-------------|
    | `flow_id` | string | 是   | OAuth 流程ID |

    ---

    ### 响应 (Response)

    #### 授权完成 (200)
    ```json
    {
        "retcode": 0,
        "retmsg": "success",
        "data": {
            "credentials": {...}
        }
    }
    ```

    #### 授权进行中 (102)
    ```json
    {
        "retcode": 102,
        "retmsg": "Authorization is still pending."
    }
    ```

    #### 无权限 (109)
    ```json
    {
        "retcode": 109,
        "retmsg": "You are not allowed to access this authorization result."
    }
    ```
    """
    flow_id = request.flow_id

    cache_blob = REDIS_CONN.get(_web_result_cache_key(flow_id, "box"))
    if not cache_blob:
        return get_json_result(retcode=RetCode.RUNNING, retmsg="Authorization is still pending.")

    cache_raw = json.loads(cache_blob)
    if cache_raw.get("user_id") != user.id:
        return get_json_result(retcode=RetCode.PERMISSION_ERROR, retmsg="You are not allowed to access this authorization result.")

    REDIS_CONN.delete(_web_result_cache_key(flow_id, "box"))

    return get_json_result(data={"credentials": cache_raw})
