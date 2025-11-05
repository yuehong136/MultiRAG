import logging
import uuid
import secrets
from datetime import datetime, timedelta
from typing import Annotated
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi_login import LoginManager
from sqlalchemy.orm import Session

from api import settings
from api.common.exceptions import AdminException, UserNotFoundError
from api.db.services import UserService
from api.db.db_models import get_db, SessionLocal
from api.db import ActiveEnum, StatusEnum
from api.utils.crypt import decrypt
from api.utils import get_uuid, current_timestamp, datetime_format, get_format_time


security = HTTPBasic()

# ============================================================================
# 关键：在模块级别初始化 settings
# 
# 原因：
# 1. LoginManager 需要 SECRET_KEY 才能创建
# 2. 模块导入时就会执行这些代码
# 3. 必须在创建 admin_manager 之前确保 settings 已初始化
# ============================================================================
if settings.SECRET_KEY is None:
    logging.info("Initializing settings at module level for admin auth...")
    settings.init_settings()

# 创建 admin 专用的 LoginManager
admin_manager = LoginManager(settings.SECRET_KEY, token_url='/api/v1/admin/login', default_expiry=timedelta(days=1))


@admin_manager.user_loader()
def load_admin_user(email: str, db: Session = None):
    """
    加载管理员用户（用于 JWT token 验证）
    """
    if db is None:
        db = SessionLocal()
        close_db = True
    else:
        close_db = False

    try:
        users = UserService.query(db, email=email, status=StatusEnum.VALID.value)
        if not users:
            return None
        
        user = users[0]
        
        # 检查是否是管理员
        if not user.is_superuser:
            logging.warning(f"User {email} is not admin")
            return None
        
        # 检查是否被登出
        if user.access_token and user.access_token.startswith("INVALID_"):
            logging.warning(f"Admin {email} has been logged out")
            return None
        
        # 检查是否被禁用
        if user.is_active == ActiveEnum.INACTIVE.value:
            logging.warning(f"Admin {email} is inactive")
            return None

        return user
    except Exception as e:
        logging.error(f"load_admin_user got exception {e}", exc_info=True)
        return None
    finally:
        if close_db:
            db.close()


def init_default_admin(db: Session):
    """初始化默认管理员账号"""
    DEFAULT_ADMIN_EMAIL = "admin@datav.com"
    DEFAULT_ADMIN_PASSWORD = "admin"
    
    users = UserService.query(db, is_superuser=True)
    if not users:
        logging.info(f"Creating default admin account: {DEFAULT_ADMIN_EMAIL}")
        user_info = {
            "id": uuid.uuid1().hex,
            "password": DEFAULT_ADMIN_PASSWORD,  # ← 直接传明文，UserService.save 会自动哈希
            "nickname": "admin",
            "is_superuser": True,
            "email": DEFAULT_ADMIN_EMAIL,
            "creator": "system",
            "status": "1",
        }
        if not UserService.save(db, **user_info):
            raise AdminException("Can't init admin.", 500)
        logging.info("Default admin account created successfully")
    elif not any([u.is_active == ActiveEnum.ACTIVE.value for u in users]):
        raise AdminException("No active admin. Please update 'is_active' in db manually.", 500)


def login_admin(db: Session, email: str, password: str):
    """
    管理员登录
    
    :param db: 数据库会话
    :param email: 管理员邮箱
    :param password: 加密后的密码（客户端已加密）
    """
    import base64
    
    users = UserService.query(db, email=email)
    if not users:
        raise UserNotFoundError(email)
    
    # 解密客户端发送的密码
    try:
        # Step 1: RSA 解密
        decrypted_base64 = decrypt(password)
        # Step 2: Base64 解码（客户端加密前做了 base64 编码）
        plain_password = base64.b64decode(decrypted_base64).decode('utf-8')
    except Exception as e:
        logging.error(f"Failed to decrypt password: {e}", exc_info=True)
        raise AdminException("Password decryption failed!")
    
    # 验证密码
    logging.info(f"Verifying password for {email}")
    user = UserService.query_user(db, email, plain_password)
    if not user:
        logging.warning(f"Password verification failed for {email}")
        # 检查用户是否存在但密码不匹配
        test_user = users[0]
        logging.debug(f"User exists: {test_user.email}, checking password hash...")
        raise AdminException("Email and password do not match!")
    
    # 检查是否是管理员
    if not user.is_superuser:
        raise AdminException("Not admin", 403)
    
    # 检查是否被禁用
    if user.is_active == ActiveEnum.INACTIVE.value:
        raise AdminException(f"Admin {email} is inactive", 403)
    
    # 更新 access_token
    user.access_token = get_uuid()
    user.update_time = current_timestamp()
    user.update_date = datetime_format(datetime.now())
    user.last_login_time = get_format_time()
    db.add(user)
    db.commit()
    
    # 生成 JWT token
    jwt_token = admin_manager.create_access_token(data={"sub": email})
    
    resp_data = user.to_dict()
    msg = "Welcome back!"
    
    # 返回标准格式（code/message）并设置 Authorization header
    from fastapi.responses import JSONResponse
    from fastapi.encoders import jsonable_encoder
    
    response = JSONResponse(content=jsonable_encoder({
        "code": 0,
        "message": msg,
        "data": resp_data
    }))
    response.headers["Authorization"] = jwt_token
    return response


def logout_admin(db: Session, user):
    """
    管理员登出
    
    :param db: 数据库会话
    :param user: 当前用户对象
    """
    user.access_token = f"INVALID_{secrets.token_hex(16)}"
    user.update_time = current_timestamp()
    user.update_date = datetime_format(datetime.now())
    db.add(user)
    db.commit()
    return True


def check_admin(db: Session, username: str, password: str) -> bool:
    """检查管理员账号是否有效（兼容旧的 Basic Auth）"""
    user = UserService.query_user(db, username, password)
    if user and user.is_superuser:
        return True
    return False


async def verify_admin(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    db: Annotated[Session, Depends(get_db)]
) -> tuple[str, Session]:
    """
    验证管理员身份的依赖函数（兼容旧版 HTTP Basic Auth）
    
    Args:
        credentials: HTTP Basic 认证凭据
        db: 数据库会话
        
    Returns:
        tuple[str, Session]: 用户名和数据库会话
        
    Raises:
        HTTPException: 认证失败时抛出
    """
    username = credentials.username
    password = credentials.password
    
    if not username or not password:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    
    # 添加异常处理，捕获验证过程中的错误
    try:
        if not check_admin(db, username, password):
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Access denied",
            )
    except HTTPException:
        raise  # 重新抛出 HTTPException
    except Exception as e:
        # 捕获其他异常并统一返回
        logging.error(f"Error during admin verification: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(e),
        )
    
    return username, db


# 创建依赖项的类型别名，方便在路由中使用
# 返回 (username, db_session) 元组
AdminAuth = Annotated[tuple[str, Session], Depends(verify_admin)]
