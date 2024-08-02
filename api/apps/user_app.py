# coding=utf-8
"""
@project: multirag
@Author：龙
@file： user_app.py
@date：2024/7/15 16:50
@desc: 用户管理接口
"""

import json
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from fastapi.security import OAuth2PasswordRequestForm
from fastapi_login import LoginManager
from fastapi_login.exceptions import InvalidCredentialsException

from api.apps import manager
from api.db.database import get_db
from api.db.db_models import TenantLLM
from api.db.services.llm_service import TenantLLMService, LLMService
from api.db.services.user_service import UserService, TenantService, UserTenantService, pwd_context
from api.db.services.file_service import FileService
from api.db import UserTenantRole, LLMType, FileType, StatusEnum, TaskStatus
from api.utils import get_uuid, get_format_time, download_img, current_timestamp, datetime_format
from api.settings import RetCode, GITHUB_OAUTH, FEISHU_OAUTH, CHAT_MDL, EMBEDDING_MDL, ASR_MDL, IMAGE2TEXT_MDL, PARSERS, \
    API_KEY, LLM_FACTORY, LLM_BASE_URL, RERANK_MDL, stat_logger
from api.utils.api_utils import get_json_result, server_error_response, validate_request, cors_response

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

    name: Optional[str] = None
    """租户的名称。"""

    llm_id: Optional[str] = None
    """大语言模型的ID。"""

    embd_id: Optional[str] = None
    """嵌入模型的ID。"""

    asr_id: Optional[str] = None
    """语音识别模型的ID。"""

    img2txt_id: Optional[str] = None
    """图像转文本模型的ID。"""

    rerank_id: Optional[str] = None
    """重新排序模型的ID。"""

    parser_ids: Optional[str] = None
    """解析器的ID。"""

@router.post("/login", summary="登录")
async def login(request: LoginRequest, db: Session = Depends(get_db)):
    """
    登录

    该接口用于用户登录。

    参数:
    - request: LoginRequest对象，包含用户的登录信息
        - username: str 用户名，通常是用户的电子邮件地址
        - password: str 用户的密码
    - db: Session 数据库会话对象

    返回:
    - 成功时返回包含访问令牌和用户信息的JSON结果
    - 失败时返回错误信息
    """
    email = request.username
    users = UserService.query(db, email=email)
    if not users:
        return get_json_result(
            data=False, retcode=RetCode.AUTHENTICATION_ERROR, retmsg=f'This Email is not registered!')

    password = request.password
    user = UserService.query_user(db, email, password)
    if user:
        response_data = user.to_dict()
        user.access_token = get_uuid()
        user.update_time = current_timestamp()
        user.update_date = datetime_format(datetime.now())
        db.add(user)
        try:
            db.commit()
            msg = "Welcome back!"
            access_token = manager.create_access_token(data={"sub": email})
            return cors_response(data=response_data, auth=access_token, retmsg=msg)
        except Exception as e:
            db.rollback()
            raise HTTPException(status_code=500, detail=f"Login error: {str(e)}")
    else:
        return get_json_result(data=False, retcode=RetCode.AUTHENTICATION_ERROR,
                               retmsg='Email and Password do not match!')

@router.get("/github_callback", summary="GitHub 回调")
async def github_callback(code: str, db: Session = Depends(get_db)):
    """
    GitHub 回调

    该接口用于处理GitHub OAuth登录回调。

    参数:
    - code: str 从GitHub获取的授权码
    - db: Session 数据库会话对象

    返回:
    - 成功时返回包含访问令牌的JSON结果
    - 失败时返回错误信息
    """
    import requests
    res = requests.post(GITHUB_OAUTH["url"], data={
        "client_id": GITHUB_OAUTH["client_id"],
        "client_secret": GITHUB_OAUTH["secret_key"],
        "code": code
    }, headers={"Accept": "application/json"})
    res = res.json()
    if "error" in res:
        return HTTPException(status_code=400, detail=res["error_description"])

    if "user:email" not in res["scope"].split(","):
        return HTTPException(status_code=400, detail="user:email not in scope")

    userinfo = user_info_from_github(res["access_token"])
    user_id = get_uuid()
    user = UserService.query(db, email=userinfo["email"])
    if not user:
        try:
            avatar = download_img(userinfo["avatar_url"])
            user = user_register(db, user_id, {
                "access_token": res["access_token"],
                "email": userinfo["email"],
                "avatar": avatar,
                "nickname": userinfo["login"],
                "last_login_time": get_format_time(),
                "is_superuser": False,
            })
            if not user:
                raise HTTPException(status_code=500, detail="Register user failure.")
            access_token = manager.create_access_token(data={"sub": user.email})
            return {"auth": access_token}
        except Exception as e:
            rollback_user_registration(db, user_id)
            stat_logger.exception(e)
            return HTTPException(status_code=500, detail=str(e))
    access_token = manager.create_access_token(data={"sub": user.email})
    return {"auth": access_token}

@router.get("/feishu_callback", summary="飞书回调")
async def feishu_callback(code: str, db: Session = Depends(get_db)):
    """
    飞书回调

    该接口用于处理飞书OAuth登录回调。

    参数:
    - code: str 从飞书获取的授权码
    - db: Session 数据库会话对象

    返回:
    - 成功时返回包含访问令牌的JSON结果
    - 失败时返回错误信息
    """
    import requests
    app_access_token_res = requests.post(FEISHU_OAUTH["app_access_token_url"], json={
        "app_id": FEISHU_OAUTH["app_id"],
        "app_secret": FEISHU_OAUTH["app_secret"]
    }, headers={"Content-Type": "application/json; charset=utf-8"})
    app_access_token_res = app_access_token_res.json()
    if app_access_token_res['code'] != 0:
        return HTTPException(status_code=400, detail=app_access_token_res)

    res = requests.post(FEISHU_OAUTH["user_access_token_url"], json={
        "grant_type": FEISHU_OAUTH["grant_type"],
        "code": code
    }, headers={"Content-Type": "application/json; charset=utf-8",
                'Authorization': f"Bearer {app_access_token_res['app_access_token']}"})
    res = res.json()
    if res['code'] != 0:
        return HTTPException(status_code=400, detail=res["message"])

    if "contact:user.email:readonly" not in res["data"]["scope"].split(" "):
        return HTTPException(status_code=400, detail="contact:user.email:readonly not in scope")

    userinfo = user_info_from_feishu(res["data"]["access_token"])
    user_id = get_uuid()
    user = UserService.query(db, email=userinfo["email"])
    if not user:
        try:
            avatar = download_img(userinfo["avatar_url"])
            user = user_register(db, user_id, {
                "access_token": res["data"]["access_token"],
                "email": userinfo["email"],
                "avatar": avatar,
                "nickname": userinfo["en_name"],
                "last_login_time": get_format_time(),
                "is_superuser": False,
            })
            if not user:
                raise HTTPException(status_code=500, detail="Register user failure.")
            access_token = manager.create_access_token(data={"sub": user.email})
            return {"auth": access_token}
        except Exception as e:
            rollback_user_registration(db, user_id)
            stat_logger.exception(e)
            return HTTPException(status_code=500, detail=str(e))
    access_token = manager.create_access_token(data={"sub": user.email})
    return {"auth": access_token}

@router.get("/logout", summary="退出登录")
async def log_out(user=Depends(manager)):
    """
    退出登录

    该接口用于用户退出登录。

    参数:
    - user: 当前用户对象

    返回:
    - 成功时返回成功退出的JSON结果
    """
    user.access_token = ""
    user.save()
    return get_json_result(data=True)

@router.post("/setting", summary="设置用户信息")
async def setting_user(request: Request, db: Session = Depends(get_db), user=Depends(manager)):
    """
    设置用户信息

    该接口用于更新用户信息。

    参数:
    - request: Request 请求对象，包含用户更新信息
    - db: Session 数据库会话对象
    - user: 当前用户对象

    返回:
    - 成功时返回更新成功的JSON结果
    - 失败时返回错误信息
    """
    update_dict = {}
    request_data = await request.json()
    if "password" in request_data:
        new_password = request_data.get("new_password")
        if not pwd_context.verify(user.password, request_data["password"]):
            raise HTTPException(status_code=400, detail='Password error!')
        if new_password:
            update_dict["password"] = pwd_context.hash(new_password)

    for k, v in request_data.items():
        if k not in ["password", "new_password"]:
            update_dict[k] = v

    try:
        UserService.update_by_id(db, user.id, update_dict)
        return get_json_result(data=True)
    except Exception as e:
        stat_logger.exception(e)
        return HTTPException(status_code=500, detail='Update failure!')

@router.get("/info", summary="获取用户信息")
async def user_info(user=Depends(manager)):
    """
    获取用户信息

    该接口用于获取当前登录用户的信息。

    参数:
    - user: 当前用户对象

    返回:
    - 成功时返回包含用户信息的JSON结果
    """
    return get_json_result(data=user.to_dict())

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
    """
    用户注册

    该函数用于注册新用户。

    参数:
    - db: Session 数据库会话对象
    - user_id: str 用户的唯一标识符
    - user: dict 用户信息字典

    返回:
    - 成功时返回注册的用户对象
    - 失败时引发HTTP异常
    """
    user["id"] = user_id
    tenant = {
        "id": user_id,
        "name": user["nickname"] + "‘s Kingdom",
        "llm_id": CHAT_MDL,
        "embd_id": EMBEDDING_MDL,
        "asr_id": ASR_MDL,
        "parser_ids": PARSERS,
        "img2txt_id": IMAGE2TEXT_MDL,
        "rerank_id": RERANK_MDL
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
    tenant_llm = []
    for llm in LLMService.query(db, fid=LLM_FACTORY):
        tenant_llm.append({
            "tenant_id": user_id,
            "llm_factory": LLM_FACTORY,
            "llm_name": llm.llm_name,
            "mdl_type": llm.mdl_type,
            "api_key": API_KEY,
            "api_base": LLM_BASE_URL
        })

    try:
        if not UserService.save(db, **user):
            return
        TenantService.insert(db, **tenant)
        UserTenantService.insert(db, **usr_tenant)
        TenantLLMService.insert_many(db, tenant_llm)
        FileService.insert(db, file)
        db.commit()
        return UserService.query(db, email=user["email"])
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error during user registration: {str(e)}")

@router.post("/register", summary="注册用户")
async def user_add(request: RegisterRequest, db: Session = Depends(get_db)):
    """
    注册用户

    该接口用于注册新用户。

    参数:
    - request: RegisterRequest对象，包含用户的注册信息
        - email: str 用户的电子邮件地址
        - nickname: str 用户的昵称
        - password: str 用户的密码
    - db: Session 数据库会话对象

    返回:
    - 成功时返回包含访问令牌和用户信息的JSON结果
    - 失败时返回错误信息
    """
    req = request.model_dump()
    if UserService.query(db, email=req["email"]):
        return get_json_result(
            data=False, retmsg=f'Email: {req["email"]} has already registered!', retcode=RetCode.OPERATING_ERROR)
    if not re.match(r"^[\w\._-]+@([\w_-]+\.)+[\w-]{2,4}$", req["email"]):
        return get_json_result(data=False, retmsg=f'Invalid e-mail: {req["email"]}!', retcode=RetCode.OPERATING_ERROR)

    user_dict = {
        "access_token": get_uuid(),
        "email": req["email"],
        "nickname": req["nickname"],
        "password": req["password"],
        "last_login_time": get_format_time(),
        "is_superuser": False,
    }

    user_id = get_uuid()
    try:
        users = user_register(db, user_id, user_dict)
        if not users:
            raise HTTPException(status_code=500, detail="Register user failure.")
        user = users[0]
        access_token = manager.create_access_token(data={"sub": user.email})
        return cors_response(data=user.to_dict(), auth=access_token, retmsg="Welcome aboard!")
    except Exception as e:
        rollback_user_registration(db, user_id)
        stat_logger.exception(e)
        raise HTTPException(status_code=500, detail=f'User registration failure: {str(e)}')

@router.get("/tenant_info", summary="获取租户信息")
async def tenant_info(user=Depends(manager), db: Session = Depends(get_db)):
    """
    获取租户信息

    该接口用于获取当前登录用户的租户信息。

    参数:
    - user: 当前用户对象
    - db: Session 数据库会话对象

    返回:
    - 成功时返回包含租户信息的JSON结果
    """
    try:
        tenants = TenantService.get_by_user_id(db, user.id)
        return get_json_result(data=tenants)
    except Exception as e:
        return server_error_response(e)

@router.post("/set_tenant_info", summary="设置租户信息")
async def set_tenant_info(request: SetTenantInfoRequest, user=Depends(manager), db: Session = Depends(get_db)):
    """
    设置租户信息

    该接口用于更新租户信息。

    参数:
    - request: SetTenantInfoRequest对象，包含租户的更新信息
        - tenant_id: str 租户的唯一标识符
        - name: Optional[str] 租户的名称
        - llm_id: Optional[str] 大语言模型的ID
        - embd_id: Optional[str] 嵌入模型的ID
        - asr_id: Optional[str] 语音识别模型的ID
        - img2txt_id: Optional[str] 图像转文本模型的ID
        - rerank_id: Optional[str] 重新排序模型的ID
        - parser_ids: Optional[str] 解析器的ID
    - user: 当前用户对象
    - db: Session 数据库会话对象

    返回:
    - 成功时返回更新成功的JSON结果
    - 失败时返回错误信息
    """
    req = request.model_dump()
    try:
        tid = req["tenant_id"]
        del req["tenant_id"]
        TenantService.update_by_id(db, tid, req)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)

def user_info_from_github(access_token: str):
    """
    从GitHub获取用户信息

    该函数用于从GitHub获取用户信息。

    参数:
    - access_token: str GitHub访问令牌

    返回:
    - 用户信息字典
    """
    import requests
    headers = {"Accept": "application/json", 'Authorization': f"token {access_token}"}
    res = requests.get(f"https://api.github.com/user", headers=headers)
    user_info = res.json()
    email_info = requests.get(f"https://api.github.com/user/emails", headers=headers).json()
    user_info["email"] = next((email for email in email_info if email['primary']), None)["email"]
    return user_info

def user_info_from_feishu(access_token: str):
    """
    从飞书获取用户信息

    该函数用于从飞书获取用户信息。

    参数:
    - access_token: str 飞书访问令牌

    返回:
    - 用户信息字典
    """
    import requests
    headers = {"Content-Type": "application/json; charset=utf-8", 'Authorization': f"Bearer {access_token}"}
    res = requests.get("https://open.feishu.cn/open-apis/authen/v1/user_info", headers=headers)
    user_info = res.json()["data"]
    user_info["email"] = None if user_info.get("email") == "" else user_info["email"]
    return user_info
