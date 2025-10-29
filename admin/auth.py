import logging
import uuid
from typing import Annotated
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

from api.common.exceptions import AdminException
from api.db.services import UserService
from api.db.db_models import get_db


security = HTTPBasic()


def check_admin(db: Session, username: str, password: str) -> bool:
    """检查管理员账号是否有效"""
    # 预设的管理员账号（硬编码）
    DEFAULT_ADMIN_EMAIL = "admin@datav.com"
    DEFAULT_ADMIN_PASSWORD = "admin"
    
    users = UserService.query(db, email=username)
    if not users:
        # 只有预设的管理员账号才自动创建
        if username == DEFAULT_ADMIN_EMAIL:
            logging.info(f"Auto-creating default admin account: {DEFAULT_ADMIN_EMAIL}")
            user_info = {
                "id": uuid.uuid1().hex,
                "password": UserService.hash_password(DEFAULT_ADMIN_PASSWORD),  # 使用 bcrypt 哈希
                "nickname": "admin",
                "is_superuser": True,
                "email": DEFAULT_ADMIN_EMAIL,
                "creator": "system",
                "status": "1",
            }
            if not UserService.save(db, **user_info):
                raise AdminException("Can't init admin.", 500)
            
            # 刷新用户列表
            users = UserService.query(db, email=username)
        else:
            logging.warning(f"User {username} not found and not default admin")
            return False

    user = UserService.query_user(db, username, password)
    return user is not None


async def verify_admin(
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
    db: Annotated[Session, Depends(get_db)]
) -> tuple[str, Session]:
    """
    验证管理员身份的依赖函数
    
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
    
    if not check_admin(db, username, password):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Access denied",
            headers={"WWW-Authenticate": "Basic"},
        )
    
    return username, db


# 创建依赖项的类型别名，方便在路由中使用
# 返回 (username, db_session) 元组
AdminAuth = Annotated[tuple[str, Session], Depends(verify_admin)]
