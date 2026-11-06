import logging
import re
import secrets
import string
from base64 import b64decode
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, BeforeValidator, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.apps import manager
from api.apps.auth import get_auth_client
from api.db import UserAccountKind
from api.db.db_models import User, get_async_db
from api.db.services import password_recovery_service as recovery
from api.db.services.user_service import TenantService, UserService
from api.utils.api_utils import Principal, async_current_user, get_data_error_result, get_json_result, server_error_response
from api.utils.crypt import decrypt
from api.utils.nickname_validation import normalize_nickname
from api.utils.password_recovery import check_origin as check_recovery_origin
from api.utils.password_recovery import clear_token as clear_recovery_token
from api.utils.password_recovery import request_token as recovery_request_token
from api.utils.password_recovery import set_token as set_recovery_token
from api.utils.tenant_utils import ensure_tenant_model_id_for_params
from api.utils.web_utils import (
    OTP_LENGTH,
    send_email_html,
)
from common import settings
from common.connection_utils import construct_response
from common.constants import RetCode
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp, datetime_format, get_format_time

router = APIRouter()

Nickname = Annotated[str, BeforeValidator(normalize_nickname)]


class LoginRequest(BaseModel):
    username: str
    """用户名，通常是用户的电子邮件地址。"""

    password: str
    """用户的密码。"""


class RegisterRequest(BaseModel):
    email: str
    """用户的电子邮件地址。"""

    nickname: Nickname
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
    nickname: Nickname = Field(None, description="User's nickname; 1–100 Unicode code points after trimming")
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


@router.get("/auth/login/channels", summary="获取登录渠道")
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
            channels.append(
                {
                    "channel": channel,
                    "display_name": config.get("display_name", channel.title()),
                    "icon": config.get("icon", "sso"),
                }
            )
        return get_json_result(data=channels)
    except Exception as e:
        logging.exception(e)
        return get_json_result(data=[], retmsg=f"Load channels failure, error: {e!s}", retcode=RetCode.EXCEPTION_ERROR)


@router.post("/auth/login", summary="登录")
async def login(request: LoginRequest, db: AsyncSession = Depends(get_async_db)):
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
    if settings.DISABLE_PASSWORD_LOGIN:
        return get_json_result(data=False, retcode=RetCode.FORBIDDEN, retmsg="Password login is disabled")
    email = request.username
    users = await db.run_sync(lambda s: UserService.query(s, email=email))  # TODO(async-phase4)
    if not users:
        return get_json_result(data=False, retcode=RetCode.AUTHENTICATION_ERROR, retmsg=f"Email: {email} is not registered!")

    password = request.password
    try:
        password = b64decode(decrypt(password)).decode("utf-8")
    except Exception:
        return get_json_result(data=False, retcode=RetCode.SERVER_ERROR, retmsg="Fail to crypt password")

    user = await db.run_sync(lambda s: UserService.query_user(s, email, password))  # TODO(async-phase4)

    if user and hasattr(user, "is_active") and not user.is_active:
        return get_json_result(
            data=False,
            retcode=RetCode.FORBIDDEN,
            retmsg="This account has been disabled, please contact the administrator!",
        )
    elif user:
        response_data = user.to_dict(for_self=True)
        user.access_token = get_uuid()
        current_ts = current_timestamp()
        current_date = datetime_format(datetime.now())
        user.update_time = current_ts
        user.update_date = current_date
        db.add(user)
        try:
            await db.commit()
            msg = "Welcome back!"
            jwt_token = manager.create_access_token(data={"sub": email})
            return construct_response(data=response_data, auth=jwt_token, retmsg=msg)
        except Exception as e:
            await db.rollback()
            raise HTTPException(status_code=500, detail=f"Login error: {e!s}")
    else:
        return get_json_result(data=False, retcode=RetCode.AUTHENTICATION_ERROR, retmsg="Email and password do not match!")


@router.post("/auth/logout", summary="退出登录")
async def log_out(db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)):
    """
    退出登录

    该接口用于用户退出登录。

    返回:
    - 成功时返回成功退出的JSON结果
    """
    try:
        invalid_token = f"INVALID_{secrets.token_hex(16)}"

        def _invalidate(s: Session) -> bool:
            user_obj = UserService.get_by_id(s, user.id)
            if user_obj is None:
                return False
            user_obj.access_token = invalid_token
            s.add(user_obj)
            return True

        if not await db.run_sync(_invalidate):  # TODO(async-phase4)
            return get_data_error_result(retmsg="User not found!")
        await db.commit()
        return get_json_result(data=True)
    except Exception as exc:
        await db.rollback()
        logging.error("Logout failed to persist invalidation: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="Failed to invalidate access token") from None


@router.get("/auth/login/{channel}", summary="OAuth登录入口")
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
        raise HTTPException(status_code=500, detail=f"OAuth login error: {e!s}")


@router.get("/auth/oauth/{channel}/callback", summary="OAuth回调处理")
async def oauth_callback(
    channel: str,
    code: str,
    request: Request,
    state: str | None = None,
    db: AsyncSession = Depends(get_async_db),
):
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

        # State is mandatory: accepting an omitted state turns the callback
        # into a login-CSRF/account-confusion endpoint.
        stored_state = request.session.pop("oauth_state", None)
        if not state or not isinstance(stored_state, str) or not secrets.compare_digest(stored_state, state):
            return RedirectResponse(url="/?error=invalid_state")

        auth_cli = get_auth_client(channel_config)

        # 获取授权码
        if not code:
            return RedirectResponse(url="/?error=missing_code")

        # 用授权码换取访问令牌
        if hasattr(auth_cli, "async_exchange_code_for_token"):
            token_info = await auth_cli.async_exchange_code_for_token(code)
        else:
            token_info = auth_cli.exchange_code_for_token(code)
        access_token = token_info.get("access_token")
        if not access_token:
            return RedirectResponse(url="/?error=token_failed")

        id_token = token_info.get("id_token")

        # 获取用户信息
        if hasattr(auth_cli, "async_fetch_user_info"):
            user_info = await auth_cli.async_fetch_user_info(access_token, id_token=id_token)
        else:
            user_info = auth_cli.fetch_user_info(access_token, id_token=id_token)
        if not user_info.email:
            return RedirectResponse(url="/?error=email_missing")

        # I1 deliberately does not elevate an email returned by OAuth into a
        # platform identity. Until I6 persists and verifies provider subject
        # bindings, both first-time registration and existing-email reuse are
        # disabled. This avoids silently turning an email attribute into an
        # account-linking credential.
        logging.warning("OAuth identity binding is not available for channel=%s", channel)
        return RedirectResponse(url="/?error=oauth_identity_binding_required")

    except Exception as e:
        logging.exception(e)
        return RedirectResponse(url=f"/?error={e!s}")


@router.patch("/users/me", summary="设置用户信息")
async def setting_user(
    request: UserUpdateRequest,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
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
        current_password = await db.run_sync(  # TODO(async-phase4)
            lambda s: user_obj.password if (user_obj := UserService.get_by_id(s, user.id)) is not None else None
        )
        if current_password is None or not UserService.verify_password(request_data["password"], current_password):
            return get_json_result(data=False, retcode=RetCode.AUTHENTICATION_ERROR, retmsg="Password error!")
        if new_password:
            update_dict["password"] = UserService.hash_password(new_password)

    allowed_fields = ["password", "new_password", "email", "status", "is_superuser", "login_channel", "is_anonymous", "is_active", "is_authenticated", "last_login_time"]

    for field, value in request_data.items():
        if field in allowed_fields:
            continue
        update_dict[field] = value

    try:
        await db.run_sync(lambda s: UserService.update_by_id(s, user.id, update_dict))  # TODO(async-phase4)
        return get_json_result(data=True)
    except Exception as e:
        logging.exception(e)
        return get_json_result(data=False, retmsg="Update failure!", retcode=RetCode.EXCEPTION_ERROR)


@router.get("/users/me", summary="获取用户信息")
async def user_profile(db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)):
    payload = await db.run_sync(  # TODO(async-phase4)
        lambda s: user_obj.to_dict(for_self=True) if (user_obj := UserService.get_by_id(s, user.id)) is not None else None
    )
    if payload is None:
        return get_data_error_result(retmsg="User not found!")
    return get_json_result(data=payload)


def login_user(user):
    # 在FastAPI中，我们使用JWT token来管理用户会话
    # 这里主要是更新用户的最后登录时间等信息
    user.last_login_time = get_format_time()
    current_ts = current_timestamp()
    current_date = datetime_format(datetime.now())
    user.update_time = current_ts
    user.update_date = current_date


def rollback_user_registration(db: Session, user_id: str) -> None:
    # Registration writes now share one transaction, including model identities.
    db.rollback()


def user_register(db: Session, user_id: str, user: dict[str, Any]) -> list[User]:
    from api.db.joint_services.user_account_service import register_user_account

    try:
        created = register_user_account(db, user_id, user)
        db.commit()
        return [created]
    except Exception as exc:
        db.rollback()
        logging.error("User registration transaction failed safely: %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="user_registration_failed") from None


@router.post("/users", summary="注册用户")
async def user_add(request: RegisterRequest, db: AsyncSession = Depends(get_async_db)):
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
        return get_json_result(data=False, retmsg=f"Invalid email address: {email_address}!", retcode=RetCode.OPERATING_ERROR)

    # Check if the email address is already used
    if await db.run_sync(lambda s: UserService.query(s, email=email_address)):  # TODO(async-phase4)
        return get_json_result(data=False, retmsg=f"Email: {email_address} has already registered!", retcode=RetCode.OPERATING_ERROR)

    # Construct user info data
    nickname = req["nickname"]
    try:
        decrypted_password = b64decode(decrypt(req["password"])).decode("utf-8")
    except Exception:
        return get_json_result(data=False, retcode=RetCode.SERVER_ERROR, retmsg="Fail to decrypt password")

    user_dict = {
        "access_token": get_uuid(),
        "email": email_address,
        "account_kind": UserAccountKind.LOCAL.value,
        "nickname": nickname,
        "password": decrypted_password,
        "login_channel": "password",
        "last_login_time": get_format_time(),
        "is_superuser": False,
    }

    user_id = get_uuid()
    try:
        users = await db.run_sync(lambda s: user_register(s, user_id, user_dict))  # TODO(async-phase4)
        if not users:
            raise Exception(f"Fail to register {email_address}.")
        if len(users) > 1:
            raise Exception(f"Same email: {email_address} exists!")
        user = users[0]
        access_token = manager.create_access_token(data={"sub": user.email})
        return construct_response(data=user.to_dict(for_self=True), auth=access_token, retmsg=f"{nickname}, welcome aboard!")
    except Exception as e:
        await db.run_sync(lambda s: rollback_user_registration(s, user_id))  # TODO(async-phase4)
        logging.error("User registration failed safely: %s", type(e).__name__)
        return get_json_result(
            data=False,
            retmsg="User registration failed.",
            retcode=RetCode.EXCEPTION_ERROR,
        )


@router.get("/users/me/models", summary="获取租户信息")
async def tenant_info(user: Principal = Depends(async_current_user), db: AsyncSession = Depends(get_async_db)):
    try:
        tenants = await db.run_sync(lambda s: TenantService.get_info_by(s, user.id))  # TODO(async-phase4)
        if not tenants:
            return get_data_error_result(retmsg="Tenant not found!")
        return get_json_result(data=tenants[0])
    except Exception as e:
        return server_error_response(e)


@router.patch("/users/me/models", summary="设置租户信息")
async def set_tenant_info(
    request: SetTenantInfoRequest,
    _user: Principal = Depends(async_current_user),
    db: AsyncSession = Depends(get_async_db),
):
    req = request.model_dump()
    try:
        tid = req.pop("tenant_id")
        req = await db.run_sync(lambda s: ensure_tenant_model_id_for_params(s, tid, req))  # TODO(async-phase4)
        await db.run_sync(lambda s: TenantService.update_by_id(s, tid, req))  # TODO(async-phase4)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post("/auth/password/forgot/captcha", summary="获取图片验证码")
async def forget_get_captcha(request: Request, email: str, db: AsyncSession = Depends(get_async_db)) -> Response:
    check_recovery_origin(request)
    email = email.strip()
    if not email:
        return get_json_result(data=False, retcode=RetCode.ARGUMENT_ERROR, retmsg="email is required")
    text = "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(OTP_LENGTH))
    try:
        await recovery.set_captcha(db, email, text)
        from captcha.image import ImageCaptcha

        image = ImageCaptcha(width=300, height=120, font_sizes=[50, 60, 70])
        return Response(content=image.generate(text).read(), media_type="image/jpeg", headers={"Cache-Control": "no-store"})
    except recovery.RecoveryError:
        return get_json_result(data=False, retcode=RetCode.DATA_ERROR, retmsg="invalid email or recovery unavailable")
    except Exception as error:
        logging.error("Recovery captcha failed: %s", type(error).__name__)
        return get_json_result(data=False, retcode=RetCode.SERVER_ERROR, retmsg="recovery unavailable")


@router.post("/auth/password/forgot/otp", summary="发送邮箱OTP验证码")
async def forget_send_otp(request: Request, body: SendOtpRequest, db: AsyncSession = Depends(get_async_db)) -> JSONResponse:
    check_recovery_origin(request)
    email, captcha = body.email.strip(), body.captcha.strip()
    if not email or not captcha:
        return get_json_result(data=False, retcode=RetCode.ARGUMENT_ERROR, retmsg="email and captcha required")
    try:
        await recovery.send_otp(db, email, captcha, send_email_html)
    except recovery.RecoveryError:
        return get_json_result(data=False, retcode=RetCode.AUTHENTICATION_ERROR, retmsg="invalid captcha or recovery unavailable")
    except Exception as error:
        logging.error("Recovery send failed: %s", type(error).__name__)
        return get_json_result(data=False, retcode=RetCode.SERVER_ERROR, retmsg="failed to send email")
    return get_json_result(data=True, retcode=RetCode.SUCCESS, retmsg="verification passed, email sent")


@router.post("/auth/password/forgot/otp/verify", summary="验证OTP")
async def forget_verify_otp(request: Request, body: VerifyOtpRequest, db: AsyncSession = Depends(get_async_db)) -> JSONResponse:
    check_recovery_origin(request)
    email, otp = body.email.strip(), body.otp.strip()
    if not email or not otp:
        return get_json_result(data=False, retcode=RetCode.ARGUMENT_ERROR, retmsg="email and otp are required")
    try:
        token = await recovery.verify_otp(db, email, otp)
    except recovery.RecoveryError:
        return get_json_result(data=False, retcode=RetCode.NOT_EFFECTIVE, retmsg="invalid or expired otp")
    except Exception as error:
        logging.error("Recovery verify failed: %s", type(error).__name__)
        return get_json_result(data=False, retcode=RetCode.SERVER_ERROR, retmsg="recovery unavailable")
    response = get_json_result(data=True, retcode=RetCode.SUCCESS, retmsg="otp verified")
    set_recovery_token(response, request, token)
    return response


@router.post("/auth/password/reset", summary="重置密码")
async def forget_reset_password(request: Request, body: ResetPasswordRequest, db: AsyncSession = Depends(get_async_db)) -> JSONResponse:
    check_recovery_origin(request)
    email, password = body.email.strip(), body.new_password
    if not all([email, password, body.confirm_new_password]):
        return get_json_result(data=False, retcode=RetCode.ARGUMENT_ERROR, retmsg="email and passwords are required")
    if password != body.confirm_new_password:
        return get_json_result(data=False, retcode=RetCode.ARGUMENT_ERROR, retmsg="passwords do not match")
    token = recovery_request_token(request)
    if token is None:
        return get_json_result(data=False, retcode=RetCode.AUTHENTICATION_ERROR, retmsg="email not verified")
    try:
        await recovery.reset_password(db, email, password, token)
    except recovery.RecoveryError:
        return get_json_result(data=False, retcode=RetCode.AUTHENTICATION_ERROR, retmsg="email not verified or recovery unavailable")
    except Exception as error:
        logging.error("Recovery reset failed: %s", type(error).__name__)
        return get_json_result(data=False, retcode=RetCode.SERVER_ERROR, retmsg="failed to reset password")
    response = get_json_result(data=True, retcode=RetCode.SUCCESS, retmsg="Password reset successful.")
    clear_recovery_token(response)
    return response
