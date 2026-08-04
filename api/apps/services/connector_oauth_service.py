"""
@project: multirag
@file: connector_oauth_service.py
@desc: 数据源 Web OAuth 流程逻辑（Google Drive / Gmail / Box）。

从 ``api/apps/connector_app.py`` 抽出，供 ``restful_apis/connector_api.py`` 与
``connector_app.py`` 两个网关共用——两个路由模块各自被 ``spec_from_file_location``
加载，互相 import 会拿到第二份模块对象。

**回调路径必须两套都活**：``common/data_source/config.py`` 里
``*_WEB_OAUTH_REDIRECT_URI`` 的默认值指向旧的 ``/v1/connector/...``，且这些 URI 已注册
在 Google/Box 的应用后台里，改路径等于让存量授权流当场 404。新增 RESTful 路径只是并列
入口，旧路径继续可用。

本层不返回 HTTP 响应对象：弹窗页只回 HTML 字符串，由网关层包成 ``HTMLResponse``。
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from html import escape
from typing import Any

from box_sdk_gen import BoxOAuth, GetAuthorizeUrlOptions, OAuthConfig
from google_auth_oauthlib.flow import Flow

from common.constants import RetCode
from common.data_source.config import BOX_WEB_OAUTH_REDIRECT_URI, GMAIL_WEB_OAUTH_REDIRECT_URI, GOOGLE_DRIVE_WEB_OAUTH_REDIRECT_URI, DocumentSource
from common.data_source.google_util.constant import GOOGLE_SCOPES, WEB_OAUTH_POPUP_TEMPLATE
from core.utils.redis_conn import REDIS_CONN

logger = logging.getLogger(__name__)

WEB_FLOW_TTL_SECS = 15 * 60  # 15分钟

GOOGLE_SOURCES = ("google-drive", "gmail")


# ==================== 缓存 key 与凭证解析 ====================


def web_state_cache_key(flow_id: str, source_type: str | None = None) -> str:
    """生成 OAuth 状态缓存 key（前缀按 source_type 区分 google-drive / gmail / box）。"""
    return f"{source_type}_web_flow_state:{flow_id}"


def web_result_cache_key(flow_id: str, source_type: str | None = None) -> str:
    """生成 OAuth 结果缓存 key，与 ``web_state_cache_key`` 同构。"""
    return f"{source_type}_web_flow_result:{flow_id}"


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


def render_oauth_popup_html(flow_id: str, success: bool, message: str, source: str = "drive") -> str:
    """渲染 OAuth 弹窗页面 HTML（postMessage 类型：multirag-<source>-oauth）。"""
    status = "success" if success else "error"
    auto_close = "window.close();" if success else ""
    payload_json = json.dumps(
        {
            "type": f"multirag-{source}-oauth",
            "status": status,
            "flowId": flow_id or "",
            "message": message,
        }
    )
    return WEB_OAUTH_POPUP_TEMPLATE.format(
        title=f"Google {source.capitalize()} Authorization",
        heading="Authorization complete" if success else "Authorization failed",
        message=escape(message),
        payload_json=payload_json,
        auto_close=auto_close,
    )


# ==================== Google（Drive / Gmail） ====================


def start_google_oauth(user_id: str, source: str, credentials_payload: str | dict, redirect_uri: str | None) -> tuple[bool, Any, RetCode | None]:
    if source not in GOOGLE_SOURCES:
        return False, "Invalid Google OAuth type.", RetCode.ARGUMENT_ERROR

    if source == "gmail":
        default_redirect_uri = GMAIL_WEB_OAUTH_REDIRECT_URI
        scopes = GOOGLE_SCOPES[DocumentSource.GMAIL]
    else:
        default_redirect_uri = GOOGLE_DRIVE_WEB_OAUTH_REDIRECT_URI
        scopes = GOOGLE_SCOPES[DocumentSource.GOOGLE_DRIVE]

    redirect_uri = redirect_uri or default_redirect_uri
    if isinstance(redirect_uri, str):
        redirect_uri = redirect_uri.strip()
    if not redirect_uri:
        return False, "Google OAuth redirect URI is not configured on the server.", RetCode.SERVER_ERROR

    try:
        credentials = _load_credentials(credentials_payload)
    except ValueError as exc:
        return False, str(exc), RetCode.ARGUMENT_ERROR

    if credentials.get("refresh_token"):
        return False, "Uploaded credentials already include a refresh token.", RetCode.ARGUMENT_ERROR

    try:
        client_config = _get_web_client_config(credentials)
    except ValueError as exc:
        return False, str(exc), RetCode.ARGUMENT_ERROR

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
        logger.exception("Failed to create Google OAuth flow: %s", exc)
        return False, "Failed to initialize Google OAuth flow. Please verify the uploaded client configuration.", RetCode.SERVER_ERROR

    REDIS_CONN.set_obj(
        web_state_cache_key(flow_id, source),
        {
            "user_id": user_id,
            "client_config": client_config,
            "redirect_uri": redirect_uri,
            "created_at": int(time.time()),
        },
        WEB_FLOW_TTL_SECS,
    )
    return True, {"flow_id": flow_id, "authorization_url": authorization_url, "expires_in": WEB_FLOW_TTL_SECS}, None


def handle_google_callback(source: str, state: str | None, code: str | None, error: str | None, error_description: str | None) -> tuple[str, bool, str]:
    """处理 Google OAuth 回调，返回 ``(flow_id, success, message)`` 供网关渲染弹窗。"""
    state_id = state or ""
    err_desc = error_description or error
    default_redirect_uri = GMAIL_WEB_OAUTH_REDIRECT_URI if source == "gmail" else GOOGLE_DRIVE_WEB_OAUTH_REDIRECT_URI
    scopes = GOOGLE_SCOPES[DocumentSource.GMAIL if source == "gmail" else DocumentSource.GOOGLE_DRIVE]

    if not state_id:
        return "", False, "Missing OAuth state parameter."

    state_cache = REDIS_CONN.get(web_state_cache_key(state_id, source))
    if not state_cache:
        return state_id, False, "Authorization session expired. Please restart from the main window."

    state_obj = json.loads(state_cache)
    client_config = state_obj.get("client_config")
    redirect_uri = state_obj.get("redirect_uri", default_redirect_uri)
    if not client_config:
        REDIS_CONN.delete(web_state_cache_key(state_id, source))
        return state_id, False, "Authorization session was invalid. Please retry."

    if error:
        REDIS_CONN.delete(web_state_cache_key(state_id, source))
        return state_id, False, err_desc or "Authorization was cancelled."

    if not code:
        return state_id, False, "Missing authorization code from Google."

    try:
        flow = Flow.from_client_config(client_config, scopes=scopes)
        flow.redirect_uri = redirect_uri
        flow.fetch_token(code=code)
    except Exception as exc:
        logger.exception("Failed to exchange Google OAuth code: %s", exc)
        REDIS_CONN.delete(web_state_cache_key(state_id, source))
        return state_id, False, "Failed to exchange tokens with Google. Please retry."

    REDIS_CONN.set_obj(
        web_result_cache_key(state_id, source),
        {"user_id": state_obj.get("user_id"), "credentials": flow.credentials.to_json()},
        WEB_FLOW_TTL_SECS,
    )
    REDIS_CONN.delete(web_state_cache_key(state_id, source))
    return state_id, True, "Authorization completed successfully."


def poll_google_result(user_id: str, source: str, flow_id: str) -> tuple[bool, Any, RetCode | None]:
    if source not in GOOGLE_SOURCES:
        return False, "Invalid Google OAuth type.", RetCode.ARGUMENT_ERROR

    cache_raw = REDIS_CONN.get(web_result_cache_key(flow_id, source))
    if not cache_raw:
        return False, "Authorization is still pending.", RetCode.RUNNING

    result = json.loads(cache_raw)
    if result.get("user_id") != user_id:
        return False, "You are not allowed to access this authorization result.", RetCode.PERMISSION_ERROR

    REDIS_CONN.delete(web_result_cache_key(flow_id, source))
    return True, {"credentials": result.get("credentials")}, None


# ==================== Box ====================


def start_box_oauth(user_id: str, client_id: str, client_secret: str, redirect_uri: str | None) -> tuple[bool, Any, RetCode | None]:
    if not client_id or not client_secret:
        return False, "Box client_id and client_secret are required.", RetCode.ARGUMENT_ERROR

    redirect_uri = redirect_uri or BOX_WEB_OAUTH_REDIRECT_URI
    flow_id = str(uuid.uuid4())
    box_auth = BoxOAuth(OAuthConfig(client_id=client_id, client_secret=client_secret))
    auth_url = box_auth.get_authorize_url(options=GetAuthorizeUrlOptions(redirect_uri=redirect_uri, state=flow_id))

    REDIS_CONN.set_obj(
        web_state_cache_key(flow_id, "box"),
        {
            "user_id": user_id,
            "auth_url": auth_url,
            "client_id": client_id,
            "client_secret": client_secret,
            "created_at": int(time.time()),
        },
        WEB_FLOW_TTL_SECS,
    )
    return True, {"flow_id": flow_id, "authorization_url": auth_url, "expires_in": WEB_FLOW_TTL_SECS}, None


def handle_box_callback(state: str | None, code: str | None, error: str | None, error_description: str | None) -> tuple[str, bool, str]:
    flow_id = state or ""
    if not flow_id:
        return "", False, "Missing OAuth parameters."
    if not code:
        return flow_id, False, "Missing authorization code from Box."

    state_cache = REDIS_CONN.get(web_state_cache_key(flow_id, "box"))
    if not state_cache:
        return flow_id, False, "Box OAuth session expired or invalid."

    cache_payload = json.loads(state_cache)
    err_desc = error_description or error
    if error:
        REDIS_CONN.delete(web_state_cache_key(flow_id, "box"))
        return flow_id, False, err_desc or "Authorization failed."

    try:
        auth = BoxOAuth(OAuthConfig(client_id=cache_payload.get("client_id"), client_secret=cache_payload.get("client_secret")))
        auth.get_tokens_authorization_code_grant(code)
        token = auth.retrieve_token()
        REDIS_CONN.set_obj(
            web_result_cache_key(flow_id, "box"),
            {
                "user_id": cache_payload.get("user_id"),
                "client_id": cache_payload.get("client_id"),
                "client_secret": cache_payload.get("client_secret"),
                "access_token": token.access_token,
                "refresh_token": token.refresh_token,
            },
            WEB_FLOW_TTL_SECS,
        )
        REDIS_CONN.delete(web_state_cache_key(flow_id, "box"))
        return flow_id, True, "Authorization completed successfully."
    except Exception as exc:
        logger.exception("Failed to exchange Box OAuth code: %s", exc)
        REDIS_CONN.delete(web_state_cache_key(flow_id, "box"))
        return flow_id, False, "Failed to exchange tokens with Box. Please retry."


def poll_box_result(user_id: str, flow_id: str) -> tuple[bool, Any, RetCode | None]:
    cache_blob = REDIS_CONN.get(web_result_cache_key(flow_id, "box"))
    if not cache_blob:
        return False, "Authorization is still pending.", RetCode.RUNNING

    cache_raw = json.loads(cache_blob)
    if cache_raw.get("user_id") != user_id:
        return False, "You are not allowed to access this authorization result.", RetCode.PERMISSION_ERROR

    REDIS_CONN.delete(web_result_cache_key(flow_id, "box"))
    return True, {"credentials": cache_raw}, None
