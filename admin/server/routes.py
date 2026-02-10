import logging
from datetime import datetime
from pydantic import BaseModel, Field, ConfigDict
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from auth import AdminAuth, admin_manager, login_admin, logout_admin
from responses import APIResponse, success_response, error_response
from services import UserMgr, ServiceMgr, UserServiceMgr, SettingsMgr
from roles import RoleMgr
from api.common.exceptions import AdminException
from api.db.db_models import get_db
from common.versions import get_multirag_version


# ========== 请求/响应模型定义 ==========

class LoginRequest(BaseModel):
    """管理员登录请求"""
    email: str = Field(..., description="管理员邮箱")
    password: str = Field(..., description="加密后的密码")

    model_config = ConfigDict(json_schema_extra={
        "example": {
            "email": "admin@datav.com",
            "password": "encrypted_password_string"
        }
    })


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


class ActivateStatusUpdate(BaseModel):
    """用户激活状态更新请求"""
    activate_status: str = Field(..., description="激活状态 ('on' 或 'off')")

    model_config = ConfigDict(json_schema_extra={
        "example": {"activate_status": "on"}
    })


class ServiceResponse(BaseModel):
    """服务响应信息"""
    id: int
    name: str
    host: str
    port: int | str
    service_type: str
    status: str | None = None
    extra: dict | None = None

    model_config = ConfigDict(from_attributes=True, extra='allow')


# ========== 路由定义 ==========

admin_router = APIRouter(tags=["admin"])


@admin_router.get(
    "/ping",
    summary="健康检查",
    description="简单的健康检查端点，返回 PONG"
)
def ping():
    """健康检查端点"""
    return success_response("PONG")


@admin_router.post(
    "/login",
    summary="管理员登录",
    description="管理员登录接口，返回JWT token用于后续请求认证"
)
def admin_login(request: LoginRequest, db: Session = Depends(get_db)):
    """管理员登录"""
    try:
        return login_admin(db, request.email, request.password)
    except AdminException as e:
        return error_response(e.message, e.code)
    except Exception as e:
        logging.exception(f"Login error: {e}")
        return error_response(str(e), 500)


@admin_router.get(
    "/logout",
    summary="管理员登出",
    description="管理员登出，使当前 token 失效"
)
def admin_logout(user=Depends(admin_manager), db: Session = Depends(get_db)):
    """管理员登出"""
    try:
        logout_admin(db, user)
        return success_response(True, "Logout successfully")
    except Exception as e:
        logging.exception(f"Logout error: {e}")
        return error_response(str(e), 500)


@admin_router.get(
    "/auth",
    response_model=APIResponse[None],
    summary="验证管理员身份",
    description="验证当前用户是否具有管理员权限（兼容旧版 HTTP Basic Auth）"
)
def auth_admin(auth: AdminAuth) -> APIResponse[None]:
    """
    验证管理员身份（旧接口，使用 HTTP Basic Auth）
    
    ⚠️ 已废弃：建议使用 JWT token 认证
    新客户端应该使用 /login 获取 token，然后在请求头中携带 token
    """
    try:
        username, db = auth
        return success_response(None, "Admin is authorized", 0)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.get(
    "/verify",
    response_model=APIResponse[None],
    summary="验证 JWT token 有效性",
    description="验证当前 JWT token 是否有效且用户具有管理员权限"
)
def verify_admin(user=Depends(admin_manager)) -> APIResponse[None]:
    """验证 JWT token 有效性（推荐使用）"""
    try:
        return success_response(None, f"Admin {user.email} is authorized", 0)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.get(
    "/users",
    response_model=APIResponse[list[UserResponse]],
    summary="获取所有用户",
    description="获取系统中所有用户的列表"
)
def list_users(user=Depends(admin_manager), db: Session = Depends(get_db)) -> APIResponse[list[UserResponse]]:
    """获取所有用户列表"""
    try:
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
def create_user(user_data: UserCreate, user=Depends(admin_manager), db: Session = Depends(get_db)) -> APIResponse[dict]:
    """创建新用户"""
    try:
        res = UserMgr.create_user(
            db,
            user_data.username,
            user_data.password,
            user_data.role
        )
        if res["success"]:
            user_info = res["user_info"]
            # 移除密码字段（不返回敏感信息）
            if "password" in user_info:
                user_info.pop("password")
            logging.info(f"User created successfully: {user_info}")
            return success_response(user_info, "User created successfully")
        else:
            logging.warning(f"Failed to create user: {res}")
            return error_response("create user failed")

    except AdminException as e:
        logging.warning(f"AdminException in create_user: {e.message}")
        return error_response(e.message, e.code)
    except Exception as e:
        logging.exception(f"Exception in create_user: {e}")
        return error_response(str(e), 500)

@admin_router.delete(
    "/users/{username}",
    response_model=APIResponse[None],
    summary="删除用户",
    description="删除指定用户及其所有数据"
)
def delete_user(username: str, user=Depends(admin_manager), db: Session = Depends(get_db)) -> APIResponse[None]:
    """删除用户"""
    try:
        res = UserMgr.delete_user(db, username)
        if res["success"]:
            return success_response(None, res["message"])
        else:
            return error_response(res["message"])

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
def change_password(
    username: str,
    password_data: PasswordUpdate,
    user=Depends(admin_manager),
    db: Session = Depends(get_db)
) -> APIResponse[None]:
    """修改用户密码"""
    try:
        msg = UserMgr.update_user_password(db, username, password_data.new_password)
        return success_response(None, msg)

    except AdminException as e:
        return error_response(e.message, e.code)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.put(
    "/users/{username}/activate",
    response_model=APIResponse[None],
    summary="更新用户激活状态",
    description="更新指定用户的激活状态（启用/停用）"
)
def alter_user_activate_status(
    username: str,
    status_data: ActivateStatusUpdate,
    user=Depends(admin_manager),
    db: Session = Depends(get_db)
) -> APIResponse[None]:
    """更新用户激活状态"""
    try:
        msg = UserMgr.update_user_activate_status(db, username, status_data.activate_status)
        return success_response(None, msg)
    except AdminException as e:
        return error_response(e.message, e.code)
    except Exception as e:
        return error_response(str(e), 500)

@admin_router.put(
    "/users/{username}/admin",
    response_model=APIResponse[None],
    summary="授予管理员权限",
    description="授予指定用户管理员权限"
)
def grant_admin(
    username: str,
    user=Depends(admin_manager),
    db: Session = Depends(get_db)
) -> APIResponse[None]:
    """授予管理员权限"""
    try:
        if user.email == username:
            return error_response(f"can't grant current user: {username}", 409)
        msg = UserMgr.grant_admin(db, username)
        return success_response(None, msg)
    except AdminException as e:
        return error_response(e.message, e.code)
    except Exception as e:
        return error_response(str(e), 500)

@admin_router.delete(
    "/users/{username}/admin",
    response_model=APIResponse[None],
    summary="撤销管理员权限",
    description="撤销指定用户管理员权限"
)
def revoke_admin(
    username: str,
    user=Depends(admin_manager),
    db: Session = Depends(get_db)
) -> APIResponse[None]:
    """撤销管理员权限"""
    try:
        if user.email == username:
            return error_response(f"can't grant current user: {username}", 409)
        msg = UserMgr.revoke_admin(db, username)
        return success_response(None, msg)
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
def get_user_details(username: str, user=Depends(admin_manager), db: Session = Depends(get_db)) -> APIResponse[UserDetail]:
    """获取用户详细信息"""
    try:
        user_details = UserMgr.get_user_details(db, username)
        return success_response(user_details)
    except AdminException as e:
        return error_response(e.message, e.code)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.get(
    "/users/{username}/datasets",
    response_model=APIResponse[list[dict]],
    summary="获取用户的知识库列表",
    description="获取指定用户的所有知识库"
)
def get_user_datasets(username: str, user=Depends(admin_manager), db: Session = Depends(get_db)) -> APIResponse[list[dict]]:
    """获取用户的知识库列表"""
    try:
        datasets_list = UserServiceMgr.get_user_datasets(db, username)
        return success_response(datasets_list)
    except AdminException as e:
        return error_response(e.message, e.code)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.get(
    "/users/{username}/agents",
    response_model=APIResponse[list[dict]],
    summary="获取用户的Agent列表",
    description="获取指定用户的所有Agent"
)
def get_user_agents(username: str, user=Depends(admin_manager), db: Session = Depends(get_db)) -> APIResponse[list[dict]]:
    """获取用户的Agent列表"""
    try:
        agents_list = UserServiceMgr.get_user_agents(db, username)
        return success_response(agents_list)
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
def get_services(user=Depends(admin_manager)) -> APIResponse[list[ServiceResponse]]:
    """获取所有服务列表"""
    try:
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
def get_services_by_type(
    service_type: str,
    user=Depends(admin_manager)
) -> APIResponse[list[ServiceResponse]]:
    """按类型获取服务"""
    try:
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
def get_service(service_id: int, user=Depends(admin_manager)) -> APIResponse[ServiceResponse]:
    """获取服务详情"""
    try:
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
def shutdown_service(service_id: int, user=Depends(admin_manager)) -> APIResponse[dict]:
    """关闭服务"""
    try:
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
def restart_service(service_id: int, user=Depends(admin_manager)) -> APIResponse[dict]:
    """重启服务"""
    try:
        result = ServiceMgr.restart_service(service_id)
        return success_response(result)
    except Exception as e:
        return error_response(str(e), 500)


class RoleCreate(BaseModel):
    """创建角色请求"""
    role_name: str = Field(..., description="角色名称")
    description: str = Field(default="", description="角色描述")


@admin_router.post(
    "/roles",
    response_model=APIResponse[dict],
    summary="创建角色",
    description="创建一个新角色"
)
def create_role(role_data: RoleCreate, user=Depends(admin_manager)) -> APIResponse[dict]:
    """创建角色"""
    try:
        res = RoleMgr.create_role(role_data.role_name, role_data.description)
        return success_response(res)
    except Exception as e:
        return error_response(str(e), 500)


class RoleUpdate(BaseModel):
    """更新角色请求"""
    description: str = Field(..., description="角色描述")


@admin_router.put(
    "/roles/{role_name}",
    response_model=APIResponse[dict],
    summary="更新角色",
    description="更新角色描述"
)
def update_role(role_name: str, role_data: RoleUpdate, user=Depends(admin_manager)) -> APIResponse[dict]:
    """更新角色"""
    try:
        res = RoleMgr.update_role_description(role_name, role_data.description)
        return success_response(res)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.delete(
    "/roles/{role_name}",
    response_model=APIResponse[dict],
    summary="删除角色",
    description="删除指定角色"
)
def delete_role(role_name: str, user=Depends(admin_manager)) -> APIResponse[dict]:
    """删除角色"""
    try:
        res = RoleMgr.delete_role(role_name)
        return success_response(res)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.get(
    "/roles",
    response_model=APIResponse[list[dict]],
    summary="获取所有角色",
    description="获取系统中所有角色的列表"
)
def list_roles(user=Depends(admin_manager)) -> APIResponse[list[dict]]:
    """获取所有角色"""
    try:
        res = RoleMgr.list_roles()
        return success_response(res)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.get(
    "/roles/{role_name}/permission",
    response_model=APIResponse[dict],
    summary="获取角色权限",
    description="获取指定角色的权限配置"
)
def get_role_permission(role_name: str, user=Depends(admin_manager)) -> APIResponse[dict]:
    """获取角色权限"""
    try:
        res = RoleMgr.get_role_permission(role_name)
        return success_response(res)
    except Exception as e:
        return error_response(str(e), 500)


class PermissionGrant(BaseModel):
    """授予权限请求"""
    actions: list[str] = Field(..., description="操作列表")
    resource: str = Field(..., description="资源名称")


@admin_router.post(
    "/roles/{role_name}/permission",
    response_model=APIResponse[dict],
    summary="授予角色权限",
    description="为指定角色授予权限"
)
def grant_role_permission(
    role_name: str,
    perm_data: PermissionGrant,
    user=Depends(admin_manager)
) -> APIResponse[dict]:
    """授予角色权限"""
    try:
        res = RoleMgr.grant_role_permission(role_name, perm_data.actions, perm_data.resource)
        return success_response(res)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.delete(
    "/roles/{role_name}/permission",
    response_model=APIResponse[dict],
    summary="撤销角色权限",
    description="撤销指定角色的权限"
)
def revoke_role_permission(
    role_name: str,
    perm_data: PermissionGrant,
    user=Depends(admin_manager)
) -> APIResponse[dict]:
    """撤销角色权限"""
    try:
        res = RoleMgr.revoke_role_permission(role_name, perm_data.actions, perm_data.resource)
        return success_response(res)
    except Exception as e:
        return error_response(str(e), 500)


class UserRoleUpdate(BaseModel):
    """更新用户角色请求"""
    role_name: str = Field(..., description="角色名称")


@admin_router.put(
    "/users/{user_name}/role",
    response_model=APIResponse[dict],
    summary="更新用户角色",
    description="更新指定用户的角色"
)
def update_user_role(
    user_name: str,
    role_data: UserRoleUpdate,
    user=Depends(admin_manager)
) -> APIResponse[dict]:
    """更新用户角色"""
    try:
        res = RoleMgr.update_user_role(user_name, role_data.role_name)
        return success_response(res)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.get(
    "/users/{user_name}/permission",
    response_model=APIResponse[dict],
    summary="获取用户权限",
    description="获取指定用户的权限配置"
)
def get_user_permission(user_name: str, user=Depends(admin_manager)) -> APIResponse[dict]:
    """获取用户权限"""
    try:
        res = RoleMgr.get_user_permission(user_name)
        return success_response(res)
    except Exception as e:
        return error_response(str(e), 500)


class VariableSet(BaseModel):
    """设置系统变量请求"""
    var_name: str = Field(..., description="变量名称")
    var_value: str = Field(..., description="变量值")

    model_config = ConfigDict(json_schema_extra={
        "example": {"var_name": "enable_whitelist", "var_value": "true"}
    })


class VariableGet(BaseModel):
    """查询系统变量请求"""
    var_name: str = Field(..., description="变量名称")

    model_config = ConfigDict(json_schema_extra={
        "example": {"var_name": "enable_whitelist"}
    })


@admin_router.put(
    "/variables",
    response_model=APIResponse[None],
    summary="设置系统变量",
    description="设置或更新一个系统变量的值"
)
def set_variable(
    data: VariableSet,
    user=Depends(admin_manager),
    db: Session = Depends(get_db)
) -> APIResponse[None]:
    """设置系统变量"""
    try:
        SettingsMgr.update_by_name(db, data.var_name, data.var_value)
        return success_response(None, "Set variable successfully")
    except AdminException as e:
        return error_response(e.message, e.code)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.get(
    "/variables",
    response_model=APIResponse[list[dict]],
    summary="获取系统变量",
    description="获取所有系统变量或按名称查询"
)
def get_variable(
    var_name: str | None = None,
    user=Depends(admin_manager),
    db: Session = Depends(get_db)
) -> APIResponse[list[dict]]:
    """获取系统变量"""
    try:
        if var_name:
            res = SettingsMgr.get_by_name(db, var_name)
        else:
            res = SettingsMgr.get_all(db)
        return success_response(res)
    except AdminException as e:
        return error_response(e.message, e.code)
    except Exception as e:
        return error_response(str(e), 500)


@admin_router.get(
    "/version",
    response_model=APIResponse[dict],
    summary="获取版本信息",
    description="获取 MultiRAG 版本信息"
)
def show_version(user=Depends(admin_manager)) -> APIResponse[dict]:
    """获取版本信息"""
    try:
        res = {"version": get_multirag_version()}
        return success_response(res)
    except Exception as e:
        return error_response(str(e), 500)
