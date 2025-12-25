# coding=utf-8
"""
@project: multirag
@Author：龙
@file： admin_auth.py
@date：2024/10/14
@desc: SQLAdmin 认证中间件
"""
import logging

from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request
from sqlalchemy.orm import Session

from api.db.db_models import SessionLocal
from api.db.services import UserService
from common import settings


class AdminAuth(AuthenticationBackend):
    """
    SQLAdmin 认证后端
    
    实现了基于数据库的用户认证
    可通过配置控制是否需要超级管理员权限
    """
    
    async def login(self, request: Request) -> bool:
        """
        处理登录请求
        
        Args:
            request: Starlette Request对象
            
        Returns:
            bool: 登录是否成功
        """
        form = await request.form()
        email = form.get("username")  # SQLAdmin默认使用username字段
        password = form.get("password")
        
        logging.info(f"Admin login attempt for: {email}")
        
        db: Session = SessionLocal()
        try:
            # 使用现有的UserService验证用户
            user = UserService.query_user(db, email, password)
            
            if not user:
                logging.warning(f"Login failed for {email}: user not found or password incorrect")
                return False
            
            # 检查用户状态
            if not user.is_active:
                logging.warning(f"Login failed for {email}: user is not active")
                return False
            
            # 根据配置检查权限
            require_superuser = settings.ADMIN_REQUIRE_SUPERUSER
            
            if require_superuser:
                # 严格模式：只允许超级管理员
                if not user.is_superuser:
                    logging.warning(f"Login failed for {email}: user is not a superuser (require_superuser=True)")
                    return False
            else:
                # 宽松模式：所有激活用户都可以访问
                pass
            
            # 登录成功，将用户信息存储到session中
            request.session.update({
                "admin_user_id": user.id,
                "admin_user_email": user.email,
                "admin_user_name": user.nickname,
                "admin_is_superuser": user.is_superuser or False
            })
            
            logging.info(f"Admin user {user.email} logged in successfully")
            return True
                
        except Exception as e:
            logging.exception(f"Admin login error for {email}: {e}")
            return False
        finally:
            db.close()
    
    async def logout(self, request: Request) -> bool:
        """
        处理登出请求
        
        Args:
            request: Starlette Request对象
            
        Returns:
            bool: 登出是否成功
        """
        request.session.clear()
        return True
    
    async def authenticate(self, request: Request) -> bool:
        """
        验证用户是否已登录
        
        Args:
            request: Starlette Request对象
            
        Returns:
            bool: True表示认证成功，False表示认证失败
        """
        # 检查session中是否有用户信息
        admin_user_id = request.session.get("admin_user_id")
        
        if not admin_user_id:
            return False
        
        # 验证用户状态
        db: Session = SessionLocal()
        try:
            from api.db.db_models import User
            user = db.query(User).filter(User.id == admin_user_id).first()
            
            if not user or not user.is_active:
                request.session.clear()
                return False
            
            # 根据配置检查权限
            if settings.ADMIN_REQUIRE_SUPERUSER and not user.is_superuser:
                request.session.clear()
                return False
            
            # 认证成功
            return True
            
        except Exception as e:
            logging.exception(f"Admin authentication error: {e}")
            request.session.clear()
            return False
        finally:
            db.close()
