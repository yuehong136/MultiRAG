import logging
import os
import re

from sqlalchemy.orm import Session

from common.constants import ActiveEnum
from api.db.joint_services.user_account_service import create_new_user, delete_user_data
from api.db.services import UserService
from api.db.services.canvas_service import UserCanvasService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import TenantService
from api.utils import health_utils

from api.common.exceptions import AdminException, UserAlreadyExistsError, UserNotFoundError
from config import SERVICE_CONFIGS


class UserMgr:
    @staticmethod
    def get_all_users(db: Session):
        """获取所有用户"""
        # 使用 query 方法获取所有用户（query 方法已经返回列表）
        users = UserService.get_all_users(db)
        result = []
        for user in users:
            result.append({
                'avatar': user.avatar,
                'email': user.email,
                'nickname': user.nickname,
                'create_date': user.create_date,
                'is_active': user.is_active,
                'is_superuser': user.is_superuser,
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
            'avatar': user.avatar,
            'email': user.email,
            'nickname': user.nickname,
            'language': user.language,
            'last_login_time': user.last_login_time,
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
        # use email to delete
        user_list = UserService.query_user_by_email(db, username)
        if not user_list:
            raise UserNotFoundError(username)
        if len(user_list) > 1:
            raise AdminException(f"Exist more than 1 user: {username}!")
        usr = user_list[0]
        return delete_user_data(db, usr.id)

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

    @staticmethod
    def grant_admin(db: Session, username: str) -> str:
        # use email to find user. check exist and unique.
        user_list = UserService.query_user_by_email(db, username)
        if not user_list:
            raise UserNotFoundError(username)
        elif len(user_list) > 1:
            raise AdminException(f"Exist more than 1 user: {username}!")

        usr = user_list[0]
        if usr.is_superuser:
            return f"{usr} is already superuser!"

        UserService.update_user(db, usr.id, {"is_superuser": True})
        return "Grant successfully!"

    @staticmethod
    def revoke_admin(db: Session, username: str) -> str:
        # use email to find user. check exist and unique.
        user_list = UserService.query_user_by_email(db, username)
        if not user_list:
            raise UserNotFoundError(username)
        elif len(user_list) > 1:
            raise AdminException(f"Exist more than 1 user: {username}!")

        usr = user_list[0]
        if not usr.is_superuser:
            return f"{usr} isn't superuser, yet!"

        UserService.update_user(db, usr.id, {"is_superuser": False})
        return "Revoke successfully!"


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
        tenant_ids = [m.tenant_id for m in tenants]
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
        tenant_ids = [m.tenant_id for m in tenants]
        # filter permitted agents and owned agents
        res = UserCanvasService.get_all_agents_by_tenant_ids(db, tenant_ids, usr.id)
        return [{
            'title': r['title'],
            'permission': r['permission'],
            'canvas_category': r['canvas_category'].split('_')[0],
            'avatar': r['avatar']
        } for r in res]

class ServiceMgr:

    @staticmethod
    def get_all_services():
        """获取所有服务配置"""
        doc_engine = os.getenv('DOC_ENGINE', 'milvus')

        result = []
        configs = SERVICE_CONFIGS.configs
        for service_id, config in enumerate(configs):
            config_dict = config.to_dict()
            if config_dict['service_type'] == 'retrieval':
                if config_dict['extra']['retrieval_type'] != doc_engine:
                    continue
            try:
                service_detail = ServiceMgr.get_service_details(service_id)
                if "status" in service_detail:
                    config_dict['status'] = service_detail['status']
                else:
                    config_dict['status'] = 'timeout'
            except Exception as e:
                logging.warning(f"Can't get service details, error: {e}")
                config_dict['status'] = 'timeout'
            if not config_dict['host']:
                config_dict['host'] = '-'
            if not config_dict['port']:
                config_dict['port'] = '-'
            result.append(config_dict)
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
        service_idx = int(service_id)
        configs = SERVICE_CONFIGS.configs

        # 检查索引是否有效
        if service_idx < 0 or service_idx >= len(configs):
            raise AdminException(f"invalid service_index: {service_idx}")

        service_config = configs[service_idx]

        # 获取基本配置信息
        result = service_config.to_dict()
        
        # 添加 service_name 字段以兼容客户端
        result['service_name'] = service_config.name

        # 调用健康检查函数获取状态和详细信息
        try:
            detail_func = getattr(health_utils, service_config.detail_func_name)
            health_info = detail_func()
            
            # 设置状态
            if 'status' in health_info:
                result['status'] = health_info['status']
            else:
                result['status'] = 'alive'
            
            # 将健康检查的其他信息合并到 extra 字段
            if 'extra' not in result:
                result['extra'] = {}
            
            # 将健康检查信息（除了 status）放入 extra
            for key, value in health_info.items():
                if key != 'status':
                    result['extra'][key] = value
                    
        except Exception:
            result['status'] = 'timeout'
            if 'extra' not in result:
                result['extra'] = {}

        return result

    @staticmethod
    def shutdown_service(service_id: int):
        """关闭服务"""
        raise AdminException("shutdown_service: not implemented", 501)

    @staticmethod
    def restart_service(service_id: int):
        """重启服务"""
        raise AdminException("restart_service: not implemented", 501)
