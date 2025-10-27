from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict
from fastapi import APIRouter
from sqlalchemy.orm import Session

from auth import AdminAuth
from responses import APIResponse, success_response, error_response
from services import UserMgr, ServiceMgr
from exceptions import AdminException


# ========== 请求/响应模型定义 ==========

class UserCreate(BaseModel):
    """创建用户请求"""
    username: str = Field(..., description="用户名")
    password: str = Field(..., min_length=6, description="密码")
    role: str = Field(default="user", description="用户角色")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "username": "test_user",
            "password": "password123",
            "role": "user"
        }
    })


class PasswordUpdate(BaseModel):
    """密码更新请求"""
    new_password: str = Field(..., min_length=6, description="新密码")

    model_config = ConfigDict(json_schema_extra={
        "example": {"new_password": "newpassword123"}
    })


class UserResponse(BaseModel):
    """用户响应信息"""
    email: str
    nickname: str
    create_date: datetime | None = None
    is_active: bool | None = None

    model_config = ConfigDict(from_attributes=True)


class UserDetail(BaseModel):
    """用户详细信息"""
    email: str
    nickname: str
    is_active: bool
    create_date: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ServiceResponse(BaseModel):
    """服务响应信息"""
    id: int
    name: str
    host: str
    port: int
    service_type: str
    extra: dict | None = None

    model_config = ConfigDict(from_attributes=True)


# ========== 路由定义 ==========

admin_router = APIRouter(tags=["admin"])


@admin_router.get(
    "/auth",
    response_model=APIResponse[None],
    summary="验证管理员身份",
    description="验证当前用户是否具有管理员权限"
)
async def auth_admin(auth: AdminAuth) -> APIResponse[None]:
    """验证管理员身份"""
    try:
        username, db = auth
        return success_response(None, "Admin is authorized", 0)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.get(
    "/users",
    response_model=APIResponse[list[UserResponse]],
    summary="获取所有用户",
    description="获取系统中所有用户的列表"
)
async def list_users(auth: AdminAuth) -> APIResponse[list[UserResponse]]:
    """获取所有用户列表"""
    try:
        username, db = auth
        users = UserMgr.get_all_users(db)
        return success_response(users, "Get all users", 0)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.post(
    "/users",
    response_model=APIResponse[dict],
    summary="创建用户",
    description="创建一个新用户"
)
async def create_user(user_data: UserCreate, auth: AdminAuth) -> APIResponse[dict]:
    """创建新用户"""
    try:
        username, db = auth
        user = UserMgr.create_user(
            db,
            user_data.username,
            user_data.password,
            user_data.role
        )
        return success_response(user, "User created successfully", 201)
    except AdminException as e:
        return error_response(e.message, e.code)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.delete(
    "/users/{username}",
    response_model=APIResponse[None],
    summary="删除用户",
    description="删除指定用户及其所有数据"
)
async def delete_user(username: str, auth: AdminAuth) -> APIResponse[None]:
    """删除用户"""
    try:
        admin_username, db = auth
        UserMgr.delete_user(db, username)
        return success_response(None, "User and all data deleted successfully")
    except AdminException as e:
        return error_response(e.message, e.code)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.put(
    "/users/{username}/password",
    response_model=APIResponse[None],
    summary="修改用户密码",
    description="修改指定用户的密码"
)
async def change_password(
    username: str,
    password_data: PasswordUpdate,
    auth: AdminAuth
) -> APIResponse[None]:
    """修改用户密码"""
    try:
        admin_username, db = auth
        UserMgr.update_user_password(db, username, password_data.new_password)
        return success_response(None, "Password updated successfully")
    except AdminException as e:
        return error_response(e.message, e.code)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.get(
    "/users/{username}",
    response_model=APIResponse[UserDetail],
    summary="获取用户详情",
    description="获取指定用户的详细信息"
)
async def get_user_details(username: str, auth: AdminAuth) -> APIResponse[UserDetail]:
    """获取用户详细信息"""
    try:
        admin_username, db = auth
        user_details = UserMgr.get_user_details(db, username)
        return success_response(user_details)
    except AdminException as e:
        return error_response(e.message, e.code)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.get(
    "/services",
    response_model=APIResponse[list[ServiceResponse]],
    summary="获取所有服务",
    description="获取系统中所有服务的列表"
)
async def get_services(auth: AdminAuth) -> APIResponse[list[ServiceResponse]]:
    """获取所有服务列表"""
    try:
        username, db = auth
        services = ServiceMgr.get_all_services()
        return success_response(services, "Get all services", 0)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.get(
    "/service_types/{service_type}",
    response_model=APIResponse[list[ServiceResponse]],
    summary="按类型获取服务",
    description="获取指定类型的所有服务"
)
async def get_services_by_type(
    service_type: str,
    auth: AdminAuth
) -> APIResponse[list[ServiceResponse]]:
    """按类型获取服务"""
    try:
        username, db = auth
        services = ServiceMgr.get_services_by_type(service_type)
        return success_response(services)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.get(
    "/services/{service_id}",
    response_model=APIResponse[ServiceResponse],
    summary="获取服务详情",
    description="获取指定服务的详细信息"
)
async def get_service(service_id: int, auth: AdminAuth) -> APIResponse[ServiceResponse]:
    """获取服务详情"""
    try:
        username, db = auth
        service = ServiceMgr.get_service_details(service_id)
        return success_response(service)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.delete(
    "/services/{service_id}",
    response_model=APIResponse[dict],
    summary="关闭服务",
    description="关闭指定的服务"
)
async def shutdown_service(service_id: int, auth: AdminAuth) -> APIResponse[dict]:
    """关闭服务"""
    try:
        username, db = auth
        result = ServiceMgr.shutdown_service(service_id)
        return success_response(result)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.put(
    "/services/{service_id}",
    response_model=APIResponse[dict],
    summary="重启服务",
    description="重启指定的服务"
)
async def restart_service(service_id: int, auth: AdminAuth) -> APIResponse[dict]:
    """重启服务"""
    try:
        username, db = auth
        result = ServiceMgr.restart_service(service_id)
        return success_response(result)
    except Exception as e:
        return error_response(str(e), 500)
