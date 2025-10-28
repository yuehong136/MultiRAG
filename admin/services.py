import re

from sqlalchemy.orm import Session

from api.db import ActiveEnum
from api.db.joint_services.user_account_service import create_new_user
from api.db.services import UserService
from api.db.services.canvas_service import UserCanvasService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import TenantService, UserTenantService
from exceptions import AdminException, UserAlreadyExistsError, UserNotFoundError
from config import SERVICE_CONFIGS


class UserMgr:
    @staticmethod
    def get_all_users(db: Session):
        """获取所有用户"""
        # 使用 query 方法获取所有用户（query 方法已经返回列表）
        users = UserService.query(db)
        result = []
        for user in users:
            result.append({
                'email': user.email,
                'nickname': user.nickname,
                'create_date': user.create_date,
                'is_active': user.is_active
            })
        return result

    @staticmethod
    def get_user_details(db: Session, username: str):
        """
        获取用户详细信息
        
        Args:
            db: 数据库会话
            username: 用户邮箱
            
        Returns:
            dict: 用户详细信息
            
        Raises:
            UserNotFoundError: 用户不存在
            AdminException: 存在多个同名用户
        """
        # use email to query
        users = UserService.query_user_by_email(db, username)
        if not users:
            raise UserNotFoundError(username)
        elif len(users) > 1:
            raise AdminException(f"Exist more than 1 user: {username}!")
        
        user = users[0]
        return {
            'email': user.email,
            'nickname': user.nickname,
            'language': user.language,
            'last_login_time': user.last_login_time,
            'is_authenticated': user.is_authenticated,
            'is_active': user.is_active,
            'is_anonymous': user.is_anonymous,
            'login_channel': user.login_channel,
            'status': user.status,
            'is_superuser': user.is_superuser,
            'create_date': user.create_date,
            'update_date': user.update_date
        }

    @staticmethod
    def create_user(db: Session, username: str, password: str, role: str = "user") -> dict:
        """
        创建新用户
        
        Args:
            db: 数据库会话
            username: 用户邮箱
            password: 原始密码（未加密）
            role: 用户角色 ('admin' 或 'user')
            
        Returns:
            dict: {"success": bool, "user_info": dict}
        """
        # Validate the email address
        if not re.match(r"^[\w\._-]+@([\w_-]+\.)+[\w-]{2,}$", username):
            raise AdminException(f"Invalid email address: {username}!")
        # Check if the email address is already used
        if UserService.query(db, email=username):
            raise UserAlreadyExistsError(username)
        # Construct user info data
        user_info_dict = {
            "email": username,
            "nickname": username.split('@')[0],  # 使用邮箱前缀作为昵称
            "password": password,  # 传入原始密码，UserService.save 会自动哈希
            "login_channel": "password",
            "is_superuser": role == "admin",
        }
        return create_new_user(db, user_info_dict)

    @staticmethod
    def delete_user(db: Session, username: str):
        """
        删除用户及其所有关联数据
        
        Args:
            db: 数据库会话
            username: 用户邮箱
            
        级联删除内容：
        - User 记录
        - Tenant 记录
        - UserTenant 关系记录
        - TenantLLM 配置记录
        - File 记录（包括所有文件和文件夹）
        - Knowledgebase 记录及相关文档
        """
        import logging
        from api.db.db_models import TenantLLM
        from api.db.services.knowledgebase_service import KnowledgebaseService
        from api.db.services.file_service import FileService
        
        users = UserService.query(db, email=username)
        if not users:
            raise AdminException(f"User '{username}' not found", 404)
        
        user = users[0]
        user_id = user.id
        
        # 防止删除超级管理员账户
        if hasattr(user, 'is_superuser') and user.is_superuser:
            raise AdminException("Cannot delete superuser account", 403)
        
        try:
            logging.info(f"Starting cascade delete for user: {username} (id: {user_id})")
            
            # 1. 删除知识库（会级联删除相关文档、向量等）
            try:
                kbs = KnowledgebaseService.query(db, tenant_id=user_id)
                if kbs:
                    logging.info(f"Deleting {len(kbs)} knowledgebases for user {username}")
                    for kb in kbs:
                        KnowledgebaseService.delete_by_id(db, kb.id)
            except Exception as e:
                logging.warning(f"Error deleting knowledgebases: {e}")
            
            # 2. 删除文件和文件夹
            try:
                files = db.query(FileService.model).filter(
                    FileService.model.tenant_id == user_id
                ).all()
                if files:
                    logging.info(f"Deleting {len(files)} files for user {username}")
                    for file in files:
                        FileService.delete_by_id(db, file.id)
            except Exception as e:
                logging.warning(f"Error deleting files: {e}")
            
            # 3. 删除租户 LLM 配置
            try:
                deleted_count = db.query(TenantLLM).filter(
                    TenantLLM.tenant_id == user_id
                ).delete(synchronize_session=False)
                if deleted_count > 0:
                    logging.info(f"Deleted {deleted_count} tenant LLM configs for user {username}")
                db.commit()
            except Exception as e:
                logging.warning(f"Error deleting tenant LLM configs: {e}")
                db.rollback()
            
            # 4. 删除用户-租户关系
            try:
                user_tenants = UserTenantService.query(db, user_id=user_id)
                if user_tenants:
                    logging.info(f"Deleting {len(user_tenants)} user-tenant relationships for user {username}")
                    for ut in user_tenants:
                        UserTenantService.delete_by_id(db, ut.id)
            except Exception as e:
                logging.warning(f"Error deleting user-tenant relationships: {e}")
            
            # 5. 删除租户
            try:
                TenantService.delete_by_id(db, user_id)
                logging.info(f"Deleted tenant for user {username}")
            except Exception as e:
                logging.warning(f"Error deleting tenant: {e}")
            
            # 6. 最后删除用户记录
            UserService.delete_by_id(db, user_id)
            logging.info(f"User {username} and all related data deleted successfully")
            
        except Exception as e:
            db.rollback()
            logging.exception(f"Error during cascade delete for user {username}: {e}")
            raise AdminException(f"Failed to delete user: {str(e)}", 500)

    @staticmethod
    def update_user_password(db: Session, username: str, new_password: str) -> str:
        """
        更新用户密码
        
        Args:
            db: 数据库会话
            username: 用户邮箱
            new_password: 新密码（原始密码，未加密）
            
        Returns:
            str: 操作结果消息
        """
        # use email to find user. check exist and unique.
        user_list = UserService.query_user_by_email(db, username)
        if not user_list:
            raise UserNotFoundError(username)
        elif len(user_list) > 1:
            raise AdminException(f"Exist more than 1 user: {username}!")
        
        # check new_password different from old.
        usr = user_list[0]
        
        # 使用 bcrypt 验证新密码是否与旧密码相同
        if UserService.verify_password(new_password, usr.password):
            return "Same password, no need to update!"
        
        # update password (传入原始密码，update_user_password 内部会自动哈希)
        UserService.update_user_password(db, usr.id, new_password)
        return "Password updated successfully!"

    @staticmethod
    def update_user_activate_status(db: Session, username: str, activate_status: str) -> str:
        """
        更新用户激活状态
        
        Args:
            db: 数据库会话
            username: 用户邮箱
            activate_status: 激活状态 ('on' 或 'off')
            
        Returns:
            str: 操作结果消息
        """
        # use email to find user. check exist and unique.
        user_list = UserService.query_user_by_email(db, username)
        if not user_list:
            raise UserNotFoundError(username)
        elif len(user_list) > 1:
            raise AdminException(f"Exist more than 1 user: {username}!")
        
        # check activate status different from new
        usr = user_list[0]
        
        # format activate_status before handle
        _activate_status = activate_status.lower()
        
        # 映射到 bool 类型 (ActiveEnum.ACTIVE.value = True, ActiveEnum.INACTIVE.value = False)
        status_map = {
            'on': ActiveEnum.ACTIVE.value,    # True
            'off': ActiveEnum.INACTIVE.value,  # False
        }
        
        # 检查是否是有效的状态值
        if _activate_status not in status_map:
            raise AdminException(f"Invalid activate_status: {activate_status}. Must be 'on' or 'off'.")
        
        target_status = status_map[_activate_status]
        
        # 检查是否与当前状态相同
        if target_status == usr.is_active:
            return f"User activate status is already {_activate_status}!"
        
        # update is_active
        UserService.update_user(db, usr.id, {"is_active": target_status})
        return f"Turn {_activate_status} user activate status successfully!"

class UserServiceMgr:

    @staticmethod
    def get_user_datasets(db: Session, username):
        # use email to find user.
        user_list = UserService.query_user_by_email(db, username)
        if not user_list:
            raise UserNotFoundError(username)
        elif len(user_list) > 1:
            raise AdminException(f"Exist more than 1 user: {username}!")
        # find tenants
        usr = user_list[0]
        tenants = TenantService.get_joined_tenants_by_user_id(db, usr.id)
        tenant_ids = [m["tenant_id"] for m in tenants]
        # filter permitted kb and owned kb
        return KnowledgebaseService.get_all_kb_by_tenant_ids(db, tenant_ids, usr.id)

    @staticmethod
    def get_user_agents(db: Session, username):
        # use email to find user.
        user_list = UserService.query_user_by_email(db, username)
        if not user_list:
            raise UserNotFoundError(username)
        elif len(user_list) > 1:
            raise AdminException(f"Exist more than 1 user: {username}!")
        # find tenants
        usr = user_list[0]
        tenants = TenantService.get_joined_tenants_by_user_id(db, usr.id)
        tenant_ids = [m["tenant_id"] for m in tenants]
        # filter permitted agents and owned agents
        return UserCanvasService.get_all_agents_by_tenant_ids(db, tenant_ids, usr.id)

class ServiceMgr:
    @staticmethod
    def get_all_services():
        """获取所有服务配置"""
        result = []
        configs = SERVICE_CONFIGS.configs
        for config in configs:
            result.append(config.to_dict())
        return result

    @staticmethod
    def get_services_by_type(service_type_str: str):
        """按类型获取服务"""
        result = []
        configs = SERVICE_CONFIGS.configs
        for config in configs:
            if config.service_type == service_type_str:
                result.append(config.to_dict())
        return result

    @staticmethod
    def get_service_details(service_id: int):
        """获取服务详情"""
        configs = SERVICE_CONFIGS.configs
        for config in configs:
            if config.id == service_id:
                return config.to_dict()
        raise AdminException(f"Service with id {service_id} not found", 404)

    @staticmethod
    def shutdown_service(service_id: int):
        """关闭服务"""
        raise AdminException("shutdown_service: not implemented", 501)

    @staticmethod
    def restart_service(service_id: int):
        """重启服务"""
        raise AdminException("restart_service: not implemented", 501)
