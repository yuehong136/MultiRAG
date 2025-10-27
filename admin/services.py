from sqlalchemy.orm import Session
from api.db.services import UserService
from exceptions import AdminException
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
        """获取用户详情"""
        users = UserService.query(db, email=username)
        if not users:
            raise AdminException(f"User '{username}' not found", 404)
        
        user = users[0]
        return {
            'email': user.email,
            'nickname': user.nickname,
            'is_active': user.is_active,
            'create_date': user.create_date
        }

    @staticmethod
    def create_user(db: Session, username: str, password: str, role: str = "user"):
        """创建用户"""
        # 检查用户是否已存在
        existing_users = UserService.query(db, email=username)
        if existing_users:
            raise AdminException(f"User '{username}' already exists", 409)
        
        # 创建用户
        import uuid
        
        user_info = {
            "id": uuid.uuid1().hex,
            "password": UserService.hash_password(password),  # 使用 bcrypt 哈希
            "nickname": username.split('@')[0],  # 使用邮箱前缀作为昵称
            "email": username,
            "creator": "admin",
            "status": "1",
        }
        
        if not UserService.save(db, **user_info):
            raise AdminException("Failed to create user", 500)
        
        return {"email": username, "message": "User created successfully"}

    @staticmethod
    def delete_user(db: Session, username: str):
        """删除用户"""
        users = UserService.query(db, email=username)
        if not users:
            raise AdminException(f"User '{username}' not found", 404)
        
        user = users[0]
        
        # 防止删除超级管理员账户
        if hasattr(user, 'is_superuser') and user.is_superuser:
            raise AdminException("Cannot delete superuser account", 403)
        
        # 标记为删除状态
        UserService.delete_user(db, [user.id], {"status": "0"})
        db.commit()

    @staticmethod
    def update_user_password(db: Session, username: str, new_password: str):
        """更新用户密码"""
        users = UserService.query(db, email=username)
        if not users:
            raise AdminException(f"User '{username}' not found", 404)
        
        user = users[0]
        UserService.update_user(db, user.id, {"password": UserService.hash_password(new_password)})
        db.commit()


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
