import logging
import string
import os
import re
import secrets
import time
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.apps import manager
from api.db.db_models import TenantLLM, get_db
from api.db.services.llm_service import get_init_tenant_llm
from api.db.services.tenant_llm_service import TenantLLMService
from api.utils.tenant_utils import ensure_tenant_model_id_for_params
from api.db.services.user_service import UserService, TenantService, UserTenantService
from api.utils.api_utils import get_json_result, server_error_response, get_data_error_result
from api.db.services.file_service import FileService
from api.db import UserTenantRole, FileType
from api.apps.auth import get_auth_client
from api.utils.web_utils import (
    send_email_html,
    OTP_LENGTH,
    OTP_TTL_SECONDS,
    ATTEMPT_LIMIT,
    ATTEMPT_LOCK_SECONDS,
    RESEND_COOLDOWN_SECONDS,
    otp_keys,
    hash_code,
    captcha_key,
    verified_key,
)
from common.misc_utils import download_img, get_uuid
from common import settings
from common.connection_utils import construct_response
from common.constants import RetCode
from common.time_utils import current_timestamp, datetime_format, get_format_time
from core.utils.redis_conn import REDIS_CONN


router = APIRouter()


class LoginRequest(BaseModel):
    username: str
    """用户名，通常是用户的电子邮件地址。"""

    password: str
    """用户的密码。"""


class RegisterRequest(BaseModel):
    email: str
    """用户的电子邮件地址。"""

    nickname: str
    """用户的昵称。"""

    password: str
    """用户的密码。"""


class SetTenantInfoRequest(BaseModel):
    tenant_id: str
    """租户的唯一标识符。"""

    name: str | None = None
    """租户的名称。"""

    llm_id: str | None = None
    """大语言模型的ID。"""

    embd_id: str | None = None
    """嵌入模型的ID。"""

    asr_id: str | None = None
    """语音识别模型的ID。"""

    img2txt_id: str | None = None
    """图像转文本模型的ID。"""

    rerank_id: str | None = None
    """重新排序模型的ID。"""

    tts_id: str | None = None
    """文本转语音模型的ID。"""


class UserUpdateRequest(BaseModel):
    password: str = Field(None, description="Old password")
    new_password: str = Field(None, description="New password")
    email: str = Field(None, description="User's email")
    status: str = Field(None, description="User status")
    is_superuser: bool = Field(None, description="Superuser flag")
    login_channel: str = Field(None, description="Login channel")
    avatar: str = Field(None, description="User's avatar (base64 encoded)")
    nickname: str = Field(None, description="User's nickname")
    timezone: str = Field(None, description="User's timezone")


class SendOtpRequest(BaseModel):
    """发送OTP请求模型"""
    email: str = Field(..., description="用户邮箱地址")
    captcha: str = Field(..., description="图片验证码")


class VerifyOtpRequest(BaseModel):
    """验证OTP请求模型"""
    email: str = Field(..., description="用户邮箱地址")
    otp: str = Field(..., description="邮箱验证码")


class ResetPasswordRequest(BaseModel):
    """重置密码请求模型"""
    email: str = Field(..., description="用户邮箱地址")
    new_password: str = Field(..., description="新密码")
    confirm_new_password: str = Field(..., description="确认新密码")


@router.get("/login/channels", summary="获取登录渠道")
def get_login_channels():
    """
    获取所有支持的认证渠道

    该接口用于获取系统支持的所有OAuth登录渠道信息。

    返回:
    - 成功时返回包含所有渠道信息的JSON结果
    - 失败时返回错误信息
    """
    try:
        channels = []
        for channel, config in settings.OAUTH_CONFIG.items():
            channels.append({
                "channel": channel,
                "display_name": config.get("display_name", channel.title()),
                "icon": config.get("icon", "sso"),
            })
        return get_json_result(data=channels)
    except Exception as e:
        logging.exception(e)
        return get_json_result(
            data=[],
            retmsg=f"Load channels failure, error: {str(e)}",
            retcode=RetCode.EXCEPTION_ERROR
        )


@router.post("/login", summary="登录")
def login(request: LoginRequest, db: Session = Depends(get_db)):
    """
    登录

    该接口用于用户登录。

    参数:
    - request: LoginRequest对象，包含用户的登录信息
        - username: str 用户名，通常是用户的电子邮件地址
        - password: str 用户的密码

    返回:
    - 成功时返回包含访问令牌和用户信息的JSON结果
    - 失败时返回错误信息
    """
    email = request.username
    users = UserService.query(db, email=email)
    if not users:
        return get_json_result(data=False,
                               retcode=RetCode.AUTHENTICATION_ERROR,
                               retmsg=f'Email: {email} is not registered!')

    password = request.password
    user = UserService.query_user(db, email, password)

    if user and hasattr(user, 'is_active') and not user.is_active:
        return get_json_result(
            data=False,
            retcode=RetCode.FORBIDDEN,
            retmsg="This account has been disabled, please contact the administrator!",
        )
    elif user:
        response_data = user.to_dict()
        # 更新数据库会话标识（用于load_user验证）
        user.access_token = get_uuid()
        current_ts = current_timestamp()
        current_date = datetime_format(datetime.now())
        user.update_time = current_ts
        user.update_date = current_date
        db.add(user)
        try:
            db.commit()
            msg = "Welcome back!"
            # 生成JWT令牌（用于API请求认证）
            jwt_token = manager.create_access_token(data={"sub": email})
            return construct_response(data=response_data, auth=jwt_token, retmsg=msg)
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Login error: {str(e)}")
    else:
        return get_json_result(data=False,
                               retcode=RetCode.AUTHENTICATION_ERROR,
                               retmsg='Email and password do not match!')


@router.get("/github_callback", summary="GitHub 回调")
def github_callback(code: str, db: Session = Depends(get_db)):
    """
    **Deprecated**, Use `/oauth/callback/<channel>` instead.

    GitHub 回调

    该接口用于处理GitHub OAuth登录回调。

    参数:
    - code: str 从GitHub获取的授权码

    返回:
    - 成功时返回包含访问令牌的JSON结果
    - 失败时返回错误信息
    """
    import requests
    res = requests.post(settings.GITHUB_OAUTH["url"],
                        data={
                            "client_id": settings.GITHUB_OAUTH["client_id"],
                            "client_secret": settings.GITHUB_OAUTH["secret_key"],
                            "code": code},
                        headers={"Accept": "application/json"})
    res = res.json()
    if "error" in res:
        return HTTPException(status_code=400, detail=res["error_description"])

    if "user:email" not in res["scope"].split(","):
        return HTTPException(status_code=400, detail="user:email not in scope")

    userinfo = user_info_from_github(res["access_token"])
    user_id = get_uuid()
    users = UserService.query(db, email=userinfo["email"])
    if not users:
        try:
            avatar = download_img(userinfo["avatar_url"])
            users = user_register(db, user_id, {
                "access_token": get_uuid(),
                "email": userinfo["email"],
                "avatar": avatar,
                "nickname": userinfo["login"],
                "login_channel": "github",
                "last_login_time": get_format_time(),
                "is_superuser": False,
            })
            if not users:
                raise HTTPException(status_code=500, detail="Register user failure.")
            if len(users) > 1:
                raise HTTPException(status_code=500, detail=f"Same email: {userinfo['email']} exists!")

            user = users[0]
            login_user(user)
            db.add(user)
            db.commit()
            return RedirectResponse(url=f"/?auth={user.id}")
        except Exception as e:
            rollback_user_registration(db, user_id)
            logging.exception(e)
            return RedirectResponse(url=f"/?error={str(e)}")

    # User exists, try to log in
    user = users[0]
    user.access_token = get_uuid()
    if user and hasattr(user, 'is_active') and not user.is_active:
        return RedirectResponse(url="/?error=user_inactive")

    login_user(user)
    db.add(user)
    db.commit()
    return RedirectResponse(url=f"/?auth={user.id}")


@router.get("/feishu_callback", summary="飞书回调")
def feishu_callback(code: str, db: Session = Depends(get_db)):
    """
    飞书回调

    该接口用于处理飞书OAuth登录回调。

    参数:
    - code: str 从飞书获取的授权码

    返回:
    - 成功时返回包含访问令牌的JSON结果
    - 失败时返回错误信息
    """
    import requests
    app_access_token_res = requests.post(settings.FEISHU_OAUTH["app_access_token_url"], json={
        "app_id": settings.FEISHU_OAUTH["app_id"],
        "app_secret": settings.FEISHU_OAUTH["app_secret"]
    }, headers={"Content-Type": "application/json; charset=utf-8"})
    app_access_token_res = app_access_token_res.json()
    if app_access_token_res['code'] != 0:
        return HTTPException(status_code=400, detail=app_access_token_res)

    res = requests.post(settings.FEISHU_OAUTH["user_access_token_url"], json={
        "grant_type": settings.FEISHU_OAUTH["grant_type"],
        "code": code
    }, headers={"Content-Type": "application/json; charset=utf-8",
                'Authorization': f"Bearer {app_access_token_res['app_access_token']}"})
    res = res.json()
    if res['code'] != 0:
        return HTTPException(status_code=400, detail=res["message"])

    if "contact:user.email:readonly" not in res["data"]["scope"].split():
        return HTTPException(status_code=400, detail="contact:user.email:readonly not in scope")

    userinfo = user_info_from_feishu(res["data"]["access_token"])
    user_id = get_uuid()
    users = UserService.query(db, email=userinfo["email"])
    if not users:
        try:
            avatar = download_img(userinfo["avatar_url"])
            users = user_register(db, user_id, {
                "access_token": get_uuid(),
                "email": userinfo["email"],
                "avatar": avatar,
                "nickname": userinfo["en_name"],
                "login_channel": "feishu",
                "last_login_time": get_format_time(),
                "is_superuser": False,
            })
            if not users:
                raise HTTPException(status_code=500, detail="Register user failure.")
            if len(users) > 1:
                raise HTTPException(status_code=500, detail=f"Same email: {userinfo['email']} exists!")

            user = users[0]
            login_user(user)
            db.add(user)
            db.commit()
            return RedirectResponse(url=f"/?auth={user.id}")
        except Exception as e:
            rollback_user_registration(db, user_id)
            logging.exception(e)
            return RedirectResponse(url=f"/?error={str(e)}")

    # User exists, try to log in
    user = users[0]
    user.access_token = get_uuid()
    if user and hasattr(user, 'is_active') and not user.is_active:
        return RedirectResponse(url="/?error=user_inactive")
    login_user(user)
    db.add(user)
    db.commit()
    return RedirectResponse(url=f"/?auth={user.id}")


@router.get("/logout", summary="退出登录")
def log_out(db: Session = Depends(get_db), user=Depends(manager)):
    """
    退出登录

    该接口用于用户退出登录。

    返回:
    - 成功时返回成功退出的JSON结果
    """
    try:
        user.access_token = f"INVALID_{secrets.token_hex(16)}"
        db.add(user)
        db.commit()
        return get_json_result(data=True)
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"An error occurred: {str(e)}")


@router.get("/login/{channel}", summary="OAuth登录入口")
def oauth_login(channel: str, request: Request):
    """
    OAuth登录入口

    该接口用于启动指定渠道的OAuth登录流程。

    参数:
    - channel: str OAuth渠道名称（如github、feishu等）

    返回:
    - 重定向到OAuth授权页面
    """
    try:
        channel_config = settings.OAUTH_CONFIG.get(channel)
        if not channel_config:
            raise HTTPException(status_code=400, detail=f"Invalid channel name: {channel}")

        auth_cli = get_auth_client(channel_config)

        # 生成状态参数并存储到session
        state = get_uuid()
        request.session["oauth_state"] = state

        auth_url = auth_cli.get_authorization_url(state)

        return RedirectResponse(url=auth_url)
    except Exception as e:
        logging.exception(e)
        raise HTTPException(status_code=500, detail=f"OAuth login error: {str(e)}")


@router.get("/oauth/callback/{channel}", summary="OAuth回调处理")
def oauth_callback(channel: str, code: str, state: str = None, request: Request = None, db: Session = Depends(get_db)):
    """
    OAuth回调处理

    该接口用于处理各种OAuth/OIDC渠道的回调，动态处理不同渠道的认证流程。

    参数:
    - channel: str OAuth渠道名称
    - code: str 从OAuth提供商获取的授权码
    - state: str 可选的状态参数，用于防止CSRF攻击

    返回:
    - 成功时重定向到主页并携带认证信息
    - 失败时重定向到错误页面
    """
    try:
        channel_config = settings.OAUTH_CONFIG.get(channel)
        if not channel_config:
            return RedirectResponse(url=f"/?error=invalid_channel_{channel}")

        # 验证状态参数（如果提供了的话）
        if state and request:
            stored_state = request.session.get("oauth_state")
            if not stored_state or stored_state != state:
                return RedirectResponse(url="/?error=invalid_state")
            # 验证成功后删除状态
            request.session.pop("oauth_state", None)

        auth_cli = get_auth_client(channel_config)

        # 获取授权码
        if not code:
            return RedirectResponse(url="/?error=missing_code")

        # 用授权码换取访问令牌
        token_info = auth_cli.exchange_code_for_token(code)
        access_token = token_info.get("access_token")
        if not access_token:
            return RedirectResponse(url="/?error=token_failed")

        id_token = token_info.get("id_token")

        # 获取用户信息
        user_info = auth_cli.fetch_user_info(access_token, id_token=id_token)
        if not user_info.email:
            return RedirectResponse(url="/?error=email_missing")

        # 登录或注册
        users = UserService.query(db, email=user_info.email)
        user_id = get_uuid()

        if not users:
            try:
                try:
                    avatar = download_img(user_info.avatar_url)
                except Exception as e:
                    logging.exception(e)
                    avatar = ""

                users = user_register(
                    db,
                    user_id,
                    {
                        "access_token": get_uuid(),
                        "email": user_info.email,
                        "avatar": avatar,
                        "nickname": user_info.nickname,
                        "login_channel": channel,
                        "last_login_time": get_format_time(),
                        "is_superuser": False,
                    },
                )

                if not users:
                    raise Exception(f"Failed to register {user_info.email}")
                if len(users) > 1:
                    raise Exception(f"Same email: {user_info.email} exists!")

                # 尝试登录
                user = users[0]
                login_user(user)
                db.add(user)
                db.commit()
                return RedirectResponse(url=f"/?auth={user.id}")

            except Exception as e:
                rollback_user_registration(db, user_id)
                logging.exception(e)
                return RedirectResponse(url=f"/?error={str(e)}")

        # 用户已存在，尝试登录
        user = users[0]
        if user and hasattr(user, 'is_active') and not user.is_active:
            return RedirectResponse(url="/?error=user_inactive")
        user.access_token = get_uuid()
        login_user(user)
        db.add(user)
        db.commit()

        return RedirectResponse(url=f"/?auth={user.id}")

    except Exception as e:
        logging.exception(e)
        return RedirectResponse(url=f"/?error={str(e)}")


@router.post("/setting", summary="设置用户信息")
def setting_user(request: UserUpdateRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    **功能描述**:
    此接口用于更新用户的个人信息，如密码、邮箱、状态等字段。用户可以通过此接口更新自身信息，密码更新时需验证旧密码。

    ### 请求体 (Request Body):
    - **UserUpdateRequest (模型)**: 包含以下字段：
        - `password` (str, 可选): 用户的旧密码，用于验证身份。
        - `new_password` (str, 可选): 用户的新密码，需在验证旧密码后才能更新。
        - `email` (str, 可选): 用户的电子邮件地址，用于更新联系信息。
        - `status` (str, 可选): 用户的账户状态，例如激活或停用。
        - `is_superuser` (bool, 可选): 标识用户是否为超级用户。
        - `login_channel` (str, 可选): 用户使用的登录渠道，例如电子邮件、Google等。

    ### 响应 (Response):
    - **成功响应 (200)**:
        - `data` (bool): 表示更新操作是否成功。
        - `retcode` (int): 响应码，成功时为200，失败时为其他错误码。
        - `retmsg` (str): 响应信息，如操作成功或失败时的详细说明。

    ### 错误响应:
    - **401: Authentication Error**:
        - 当用户提供的旧密码验证失败时，返回此错误。
    - **500: Update Failure**:
        - 当更新操作失败时，返回此错误，具体错误信息会记录在日志中。

    ### 主要流程:
    1. 从请求体中提取用户提供的信息（如旧密码、新密码、邮箱等）。
    2. 如果提供了旧密码，首先进行密码验证，验证通过后才能进行新密码更新。
    3. 过滤允许更新的字段，并构建更新字典。
    4. 调用数据库服务更新用户信息，如果更新成功则返回成功响应，否则返回失败响应。

    ### 注意事项:
    - **密码更新逻辑**:
        - 系统会先验证用户输入的旧密码，只有在验证通过的情况下才能更新为新密码。新密码会通过加密后存储。
    - **允许更新的字段**:
        - 只有在预定义的允许更新字段列表中的字段才能被更新，确保系统的安全性和一致性。
        - 允许更新的字段包括 `password`, `email`, `status`, `is_superuser`, `login_channel` 等。
    - **异常处理**:
        - 如果更新操作失败，会记录异常信息并返回通用的失败响应，防止敏感信息泄露。
    """
    update_dict = {}
    # 使用 exclude_none=True 排除 None 值，避免将未传入的字段设为 NULL
    request_data = request.model_dump(exclude_none=True)
    if request_data.get("password"):
        new_password = request_data.get("new_password")
        if not UserService.verify_password(request_data["password"], user.password):
            return get_json_result(data=False, retcode=RetCode.AUTHENTICATION_ERROR, retmsg='Password error!')
        if new_password:
            update_dict["password"] = UserService.hash_password(new_password)

    allowed_fields = ["password", "new_password", "email", "status", "is_superuser", "login_channel", "is_anonymous", "is_active", "is_authenticated", "last_login_time"]

    for field, value in request_data.items():
        if field in allowed_fields:
            continue
        update_dict[field] = value

    try:
        UserService.update_by_id(db, user.id, update_dict)
        return get_json_result(data=True)
    except Exception as e:
        logging.exception(e)
        return get_json_result(data=False, retmsg='Update failure!', retcode=RetCode.EXCEPTION_ERROR)


@router.get("/info", summary="获取用户信息")
def user_profile(user=Depends(manager)):
    return get_json_result(data=user.to_dict())


def login_user(user):
    # 在FastAPI中，我们使用JWT token来管理用户会话
    # 这里主要是更新用户的最后登录时间等信息
    user.last_login_time = get_format_time()
    current_ts = current_timestamp()
    current_date = datetime_format(datetime.now())
    user.update_time = current_ts
    user.update_date = current_date


def rollback_user_registration(db: Session, user_id: str):
    try:
        UserService.delete_by_id(db, user_id)
    except Exception as e:
        pass
    try:
        TenantService.delete_by_id(db, user_id)
    except Exception as e:
        pass
    try:
        u = UserTenantService.query(db, tenant_id=user_id)
        if u:
            UserTenantService.delete_by_id(db, u[0].id)
    except Exception as e:
        pass
    try:
        db.query(TenantLLM).filter(TenantLLM.tenant_id == user_id).delete()
    except Exception as e:
        pass


def user_register(db: Session, user_id: str, user: dict):
    user["id"] = user_id
    tenant = {
        "id": user_id,
        "name": user["nickname"] + "‘s Kingdom",
        "llm_id": settings.CHAT_MDL,
        "embd_id": settings.EMBEDDING_MDL,
        "asr_id": settings.ASR_MDL,
        "parser_ids": settings.PARSERS,
        "img2txt_id": settings.IMAGE2TEXT_MDL,
        "rerank_id": settings.RERANK_MDL
    }
    usr_tenant = {
        "tenant_id": user_id,
        "user_id": user_id,
        "invited_by": user_id,
        "role": UserTenantRole.OWNER
    }
    file_id = get_uuid()
    file = {
        "id": file_id,
        "parent_id": file_id,
        "tenant_id": user_id,
        "created_by": user_id,
        "name": "/",
        "type": FileType.FOLDER.value,
        "size": 0,
        "location": "",
    }

    tenant_llm = get_init_tenant_llm(db, user_id)

    try:
        if not UserService.save(db, **user):
            return None
        TenantService.insert(db, **tenant)
        UserTenantService.insert(db, **usr_tenant)
        TenantLLMService.insert_many(db, tenant_llm)
        TenantService.update_by_id(db, user_id, ensure_tenant_model_id_for_params(db, user_id, tenant))
        FileService.insert(db, file)
        db.commit()
        return UserService.query(db, email=user["email"])
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error during user registration: {str(e)}")


@router.post("/register", summary="注册用户")
def user_add(request: RegisterRequest, db: Session = Depends(get_db)):
    if not settings.REGISTER_ENABLED:
        return get_json_result(
            data=False,
            retmsg="User registration is disabled!",
            retcode=RetCode.OPERATING_ERROR,
        )

    req = request.model_dump()
    email_address = req["email"]
    # Validate the email address
    if not re.match(r"^[\w\._-]+@([\w_-]+\.)+[\w-]{2,}$", email_address):
        return get_json_result(data=False,
                               retmsg=f'Invalid email address: {email_address}!',
                               retcode=RetCode.OPERATING_ERROR)

    # Check if the email address is already used
    if UserService.query(db, email=email_address):
        return get_json_result(
            data=False,
            retmsg=f'Email: {email_address} has already registered!',
            retcode=RetCode.OPERATING_ERROR)

    # Construct user info data
    nickname = req["nickname"]
    user_dict = {
        "access_token": get_uuid(),
        "email": email_address,
        "nickname": nickname,
        "password": req["password"],
        "last_login_time": get_format_time(),
        "is_superuser": False,
    }

    user_id = get_uuid()
    try:
        users = user_register(db, user_id, user_dict)
        if not users:
            raise Exception(f'Fail to register {email_address}.')
        if len(users) > 1:
            raise Exception(f'Same email: {email_address} exists!')
        user = users[0]
        access_token = manager.create_access_token(data={"sub": user.email})
        return construct_response(data=user.to_dict(), auth=access_token, retmsg=f"{nickname}, welcome aboard!")
    except Exception as e:
        rollback_user_registration(db, user_id)
        logging.exception(e)
        return get_json_result(data=False,
                               retmsg=f'User registration failure, error: {str(e)}',
                               retcode=RetCode.EXCEPTION_ERROR)


@router.get("/tenant_info", summary="获取租户信息")
def tenant_info(user=Depends(manager), db: Session = Depends(get_db)):
    try:
        tenants = TenantService.get_info_by(db, user.id)
        if not tenants:
            return get_data_error_result(retmsg="Tenant not found!")
        return get_json_result(data=tenants[0])
    except Exception as e:
        return server_error_response(e)


@router.post("/set_tenant_info", summary="设置租户信息")
def set_tenant_info(request: SetTenantInfoRequest, user=Depends(manager), db: Session = Depends(get_db)):
    req = request.model_dump()
    try:
        tid = req.pop("tenant_id")
        req = ensure_tenant_model_id_for_params(db, tid, req)
        TenantService.update_by_id(db, tid, req)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


def user_info_from_github(access_token: str):
    import requests

    headers = {"Accept": "application/json", "Authorization": f"token {access_token}"}
    res = requests.get(
        f"https://api.github.com/user?access_token={access_token}", headers=headers
    )
    user_info = res.json()
    email_info = requests.get(
        f"https://api.github.com/user/emails?access_token={access_token}",
        headers=headers,
    ).json()
    user_info["email"] = next(
        (email for email in email_info if email["primary"] == True), None
    )["email"]
    return user_info


def user_info_from_feishu(access_token: str):
    import requests
    headers = {"Content-Type": "application/json; charset=utf-8", 'Authorization': f"Bearer {access_token}"}
    res = requests.get("https://open.feishu.cn/open-apis/authen/v1/user_info", headers=headers)
    user_info = res.json()["data"]
    user_info["email"] = None if user_info.get("email") == "" else user_info["email"]
    return user_info


@router.get("/forget/captcha", summary="获取图片验证码")
def forget_get_captcha(email: str, db: Session = Depends(get_db)):
    """
    获取图片验证码

    该接口用于生成图片验证码并缓存到 Redis 中。

    参数:
    - email: str 用户的邮箱地址（查询参数）

    返回:
    - 成功时返回 base64 编码的验证码图片 data URI
    - 失败时返回错误信息
    """
    if not email:
        return get_json_result(
            data=False,
            retcode=RetCode.ARGUMENT_ERROR,
            retmsg="email is required"
        )

    users = UserService.query(db, email=email)
    if not users:
        return get_json_result(
            data=False,
            retcode=RetCode.DATA_ERROR,
            retmsg="invalid email"
        )

    # Generate captcha text
    allowed = string.ascii_uppercase + string.digits
    captcha_text = "".join(secrets.choice(allowed) for _ in range(OTP_LENGTH))
    REDIS_CONN.set(captcha_key(email), captcha_text, 60)  # Valid for 60 seconds

    from captcha.image import ImageCaptcha

    image = ImageCaptcha(width=300, height=120, font_sizes=[50, 60, 70])
    img_bytes = image.generate(captcha_text).read()

    return Response(content=img_bytes, media_type="image/jpeg")


@router.post("/forget/otp", summary="发送邮箱OTP验证码")
async def forget_send_otp(request: SendOtpRequest, db: Session = Depends(get_db)):
    """
    发送邮箱OTP验证码
    
    该接口用于验证图片验证码，并生成邮箱OTP验证码发送到用户邮箱。
    
    参数:
    - request: SendOtpRequest对象，包含邮箱和图片验证码
        - email: str 用户的邮箱地址
        - captcha: str 图片验证码
    
    返回:
    - 成功时返回成功消息
    - 失败时返回错误信息
    """
    email = request.email.strip() if request.email else ""
    captcha = request.captcha.strip() if request.captcha else ""
    
    if not email or not captcha:
        return get_json_result(
            data=False, 
            retcode=RetCode.ARGUMENT_ERROR, 
            retmsg="email and captcha required"
        )
    
    users = UserService.query(db, email=email)
    if not users:
        return get_json_result(
            data=False, 
            retcode=RetCode.DATA_ERROR, 
            retmsg="invalid email"
        )
    
    stored_captcha = REDIS_CONN.get(captcha_key(email))
    if not stored_captcha:
        return get_json_result(
            data=False, 
            retcode=RetCode.NOT_EFFECTIVE, 
            retmsg="invalid or expired captcha"
        )
    
    if (stored_captcha or "").strip().lower() != captcha.lower():
        return get_json_result(
            data=False, 
            retcode=RetCode.AUTHENTICATION_ERROR,
            retmsg="invalid or expired captcha"
        )
    
    # Delete captcha to prevent reuse
    REDIS_CONN.delete(captcha_key(email))
    
    k_code, k_attempts, k_last, k_lock = otp_keys(email)
    now = int(time.time())
    last_ts = REDIS_CONN.get(k_last)
    
    if last_ts:
        try:
            elapsed = now - int(last_ts)
        except Exception:
            elapsed = RESEND_COOLDOWN_SECONDS
        
        remaining = RESEND_COOLDOWN_SECONDS - elapsed
        if remaining > 0:
            return get_json_result(
                data=False, 
                retcode=RetCode.NOT_EFFECTIVE,
                retmsg=f"you still have to wait {remaining} seconds"
            )
    
    # Generate OTP (uppercase letters only) and store hashed
    otp = "".join(secrets.choice(string.ascii_uppercase) for _ in range(OTP_LENGTH))
    salt = os.urandom(16)
    code_hash = hash_code(otp, salt)
    
    REDIS_CONN.set(k_code, f"{code_hash}:{salt.hex()}", OTP_TTL_SECONDS)
    REDIS_CONN.set(k_attempts, 0, OTP_TTL_SECONDS)
    REDIS_CONN.set(k_last, now, OTP_TTL_SECONDS)
    REDIS_CONN.delete(k_lock)

    ttl_min = OTP_TTL_SECONDS // 60

    try:
        await send_email_html(
            subject="Your Password Reset Code",
            to_email=email,
            template_key="reset_code",
            code=otp,
            ttl_min=ttl_min,
        )
    except Exception as e:
        logging.exception(e)
        return get_json_result(
            data=False,
            retcode=RetCode.SERVER_ERROR,
            retmsg="failed to send email"
        )

    return get_json_result(
        data=True,
        retcode=RetCode.SUCCESS,
        retmsg="verification passed, email sent"
    )


@router.post("/forget/verify-otp", summary="验证OTP")
def forget_verify_otp(request: VerifyOtpRequest, db: Session = Depends(get_db)):
    """
    验证邮箱OTP验证码

    该接口仅验证OTP，成功后会设置一个短期验证标记。
    然后使用 /forget/reset-password 接口完成密码重置。

    参数:
    - request: VerifyOtpRequest对象
        - email: str 用户的邮箱地址
        - otp: str 邮箱验证码

    返回:
    - 成功时返回 data=True
    - 失败时返回错误信息
    """
    email = request.email.strip() if request.email else ""
    otp = request.otp.strip() if request.otp else ""

    if not all([email, otp]):
        return get_json_result(
            data=False,
            retcode=RetCode.ARGUMENT_ERROR,
            retmsg="email and otp are required"
        )

    users = UserService.query(db, email=email)
    if not users:
        return get_json_result(
            data=False,
            retcode=RetCode.DATA_ERROR,
            retmsg="invalid email"
        )

    # Verify OTP from Redis
    k_code, k_attempts, k_last, k_lock = otp_keys(email)

    if REDIS_CONN.get(k_lock):
        return get_json_result(
            data=False,
            retcode=RetCode.NOT_EFFECTIVE,
            retmsg="too many attempts, try later"
        )

    stored = REDIS_CONN.get(k_code)
    if not stored:
        return get_json_result(
            data=False,
            retcode=RetCode.NOT_EFFECTIVE,
            retmsg="expired otp"
        )

    try:
        stored_hash, salt_hex = str(stored).split(":", 1)
        salt = bytes.fromhex(salt_hex)
    except Exception:
        return get_json_result(
            data=False,
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg="otp storage corrupted"
        )

    # Case-insensitive verification: OTP generated uppercase
    calc = hash_code(otp.upper(), salt)
    if calc != stored_hash:
        # Increment attempts
        try:
            attempts = int(REDIS_CONN.get(k_attempts) or 0) + 1
        except Exception:
            attempts = 1

        REDIS_CONN.set(k_attempts, attempts, OTP_TTL_SECONDS)

        if attempts >= ATTEMPT_LIMIT:
            REDIS_CONN.set(k_lock, int(time.time()), ATTEMPT_LOCK_SECONDS)

        return get_json_result(
            data=False,
            retcode=RetCode.AUTHENTICATION_ERROR,
            retmsg="invalid otp"
        )

    # Success: consume OTP and set verified flag
    REDIS_CONN.delete(k_code)
    REDIS_CONN.delete(k_attempts)
    REDIS_CONN.delete(k_last)
    REDIS_CONN.delete(k_lock)

    # Set verified flag with limited TTL
    try:
        REDIS_CONN.set(verified_key(email), "1", OTP_TTL_SECONDS)
    except Exception:
        return get_json_result(
            data=False,
            retcode=RetCode.SERVER_ERROR,
            retmsg="failed to set verification state"
        )

    return get_json_result(
        data=True,
        retcode=RetCode.SUCCESS,
        retmsg="otp verified"
    )


@router.post("/forget/reset-password", summary="重置密码")
def forget_reset_password(request: ResetPasswordRequest, db: Session = Depends(get_db)):
    """
    OTP验证成功后重置密码

    该接口需要在 /forget/verify-otp 验证成功后调用。

    参数:
    - request: ResetPasswordRequest对象
        - email: str 用户的邮箱地址
        - new_password: str 新密码
        - confirm_new_password: str 确认新密码

    返回:
    - 成功时返回 data=True
    - 失败时返回错误信息
    """
    email = request.email.strip() if request.email else ""
    new_pwd = request.new_password
    new_pwd2 = request.confirm_new_password

    # Check verified flag first
    if not REDIS_CONN.get(verified_key(email)):
        return get_json_result(
            data=False,
            retcode=RetCode.AUTHENTICATION_ERROR,
            retmsg="email not verified"
        )

    if not all([email, new_pwd, new_pwd2]):
        return get_json_result(
            data=False,
            retcode=RetCode.ARGUMENT_ERROR,
            retmsg="email and passwords are required"
        )

    if new_pwd != new_pwd2:
        return get_json_result(
            data=False,
            retcode=RetCode.ARGUMENT_ERROR,
            retmsg="passwords do not match"
        )

    users = UserService.query(db, email=email)
    if not users:
        return get_json_result(
            data=False,
            retcode=RetCode.DATA_ERROR,
            retmsg="invalid email"
        )

    user = users[0]

    # Reset password
    try:
        UserService.update_user_password(db, user.id, new_pwd)
    except Exception as e:
        logging.exception(e)
        return get_json_result(
            data=False,
            retcode=RetCode.EXCEPTION_ERROR,
            retmsg="failed to reset password"
        )

    # Clear verified flag
    try:
        REDIS_CONN.delete(verified_key(email))
    except Exception:
        pass

    return get_json_result(
        data=True,
        retcode=RetCode.SUCCESS,
        retmsg="Password reset successful."
    )