import json
import logging
import os
import re
from typing import Any

from config import SERVICE_CONFIGS
from sqlalchemy.orm import Session

from api.common.exceptions import AdminException, UserAlreadyExistsError, UserNotFoundError
from api.db.db_models import APIToken
from api.db.joint_services.user_account_service import create_new_user, delete_user_data
from api.db.services import UserService
from api.db.services.api_service import APITokenService
from api.db.services.canvas_service import UserCanvasService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.system_settings_service import SystemSettingsService
from api.db.services.user_service import TenantService, UserTenantService
from api.utils import health_utils
from common.constants import ActiveEnum


class UserMgr:
    @staticmethod
    def get_all_users(db: Session):
        """获取所有用户"""
        # 使用 query 方法获取所有用户（query 方法已经返回列表）
        users = UserService.get_all_users(db)
        result = []
        for user in users:
            result.append(
                {
                    "avatar": user.avatar,
                    "email": user.email,
                    "nickname": user.nickname,
                    "create_date": user.create_date,
                    "is_active": user.is_active,
                    "is_superuser": user.is_superuser,
                }
            )
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
            "avatar": user.avatar,
            "email": user.email,
            "nickname": user.nickname,
            "language": user.language,
            "last_login_time": user.last_login_time,
            "is_active": user.is_active,
            "is_anonymous": user.is_anonymous,
            "login_channel": user.login_channel,
            "status": user.status,
            "is_superuser": user.is_superuser,
            "create_date": user.create_date,
            "update_date": user.update_date,
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
            "nickname": username.split("@")[0],  # 使用邮箱前缀作为昵称
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
            "on": ActiveEnum.ACTIVE.value,  # True
            "off": ActiveEnum.INACTIVE.value,  # False
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

    @staticmethod
    def get_user_api_key(db: Session, username: str) -> list[dict[str, Any]]:
        user_list = UserService.query_user_by_email(db, username)
        if not user_list:
            raise UserNotFoundError(username)
        elif len(user_list) > 1:
            raise AdminException(f"More than one user with username '{username}' found!")
        usr = user_list[0]
        api_tokens = APITokenService.query(db, tenant_id=usr.id)
        return [token.to_dict() for token in api_tokens]

    @staticmethod
    def save_api_key(db: Session, api_key: dict[str, Any]) -> bool:
        try:
            APITokenService.save(db, **api_key)
            return True
        except Exception:
            return False

    @staticmethod
    def delete_api_key(db: Session, username: str, key: str) -> bool:
        user_list = UserService.query_user_by_email(db, username)
        if not user_list:
            raise UserNotFoundError(username)
        elif len(user_list) > 1:
            raise AdminException(f"Exist more than 1 user: {username}!")
        usr = user_list[0]
        deleted_count = APITokenService.filter_delete(db, [APIToken.tenant_id == usr.id, APIToken.token == key])
        return deleted_count > 0


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
        return [{"title": r["title"], "permission": r["permission"], "canvas_category": r["canvas_category"].split("_")[0], "avatar": r["avatar"]} for r in res]

    @staticmethod
    def get_user_tenants(db: Session, username: str) -> list[dict[str, Any]]:
        users = UserService.query_user_by_email(db, username)
        if not users:
            raise UserNotFoundError(username)
        user = users[0]
        return UserTenantService.get_tenants_by_user_id(db, user.id)


class ServiceMgr:
    @staticmethod
    def get_all_services():
        """获取所有服务配置"""
        doc_engine = os.getenv("DOC_ENGINE", "milvus")

        result = []
        configs = SERVICE_CONFIGS.configs
        for service_id, config in enumerate(configs):
            config_dict = config.to_dict()
            if config_dict["service_type"] == "retrieval":
                if config_dict["extra"]["retrieval_type"] != doc_engine:
                    continue
            try:
                service_detail = ServiceMgr.get_service_details(service_id)
                if "status" in service_detail:
                    config_dict["status"] = service_detail["status"]
                else:
                    config_dict["status"] = "timeout"
            except Exception as e:
                logging.warning(f"Can't get service details, error: {e}")
                config_dict["status"] = "timeout"
            if not config_dict["host"]:
                config_dict["host"] = "-"
            if not config_dict["port"]:
                config_dict["port"] = "-"
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

        # exclude retrieval service if retrieval_type is not matched
        doc_engine = os.getenv("DOC_ENGINE", "milvus")
        if service_config.service_type == "retrieval":
            retrieval_type = service_config.to_dict().get("extra", {}).get("retrieval_type")
            if retrieval_type != doc_engine:
                raise AdminException(f"invalid service_index: {service_idx}")

        # 获取基本配置信息
        result = service_config.to_dict()

        # 添加 service_name 字段以兼容客户端
        result["service_name"] = service_config.name

        # 调用健康检查函数获取状态和详细信息
        try:
            detail_func = getattr(health_utils, service_config.detail_func_name)
            health_info = detail_func()

            # 设置状态
            if "status" in health_info:
                result["status"] = health_info["status"]
            else:
                result["status"] = "alive"

            # 将健康检查的其他信息合并到 extra 字段
            if "extra" not in result:
                result["extra"] = {}

            # 将健康检查信息（除了 status）放入 extra
            for key, value in health_info.items():
                if key != "status":
                    result["extra"][key] = value

        except Exception:
            result["status"] = "timeout"
            if "extra" not in result:
                result["extra"] = {}

        return result

    @staticmethod
    def shutdown_service(service_id: int):
        """关闭服务"""
        raise AdminException("shutdown_service: not implemented", 501)

    @staticmethod
    def restart_service(service_id: int):
        """重启服务"""
        raise AdminException("restart_service: not implemented", 501)


class SettingsMgr:
    @staticmethod
    def get_all(db: Session) -> list[dict]:
        settings = SystemSettingsService.get_all(db)
        return [
            {
                "name": setting.name,
                "source": setting.source,
                "data_type": setting.data_type,
                "value": setting.value,
            }
            for setting in settings
        ]

    @staticmethod
    def get_by_name(db: Session, name: str) -> list[dict]:
        settings = SystemSettingsService.get_by_name(db, name)
        if not settings:
            raise AdminException(f"Can't get setting: {name}")
        return [
            {
                "name": setting.name,
                "source": setting.source,
                "data_type": setting.data_type,
                "value": setting.value,
            }
            for setting in settings
        ]

    @staticmethod
    def update_by_name(db: Session, name: str, value: str):
        settings = SystemSettingsService.get_by_name(db, name)
        if len(settings) == 1:
            setting = settings[0]
            setting.value = value
            setting_dict = setting.to_dict()
            SystemSettingsService.update_by_name(db, name, setting_dict)
        elif len(settings) > 1:
            raise AdminException(f"Can't update more than 1 setting: {name}")
        else:
            # 不存在则新建（沙箱 provider 配置等新键首次写入时走到这里）
            if name.startswith("sandbox."):
                data_type = "json"
            elif name.endswith(".enabled"):
                data_type = "boolean"
            else:
                data_type = "string"
            SystemSettingsService.save(
                db,
                name=name,
                value=str(value),
                source="admin",
                data_type=data_type,
            )


class ConfigMgr:
    @staticmethod
    def get_all():
        result = []
        configs = SERVICE_CONFIGS.configs
        for config in configs:
            config_dict = config.to_dict()
            result.append(config_dict)
        return result


class EnvironmentsMgr:
    @staticmethod
    def get_all():
        result = []

        result.append({"env": "DOC_ENGINE", "value": os.getenv("DOC_ENGINE")})
        result.append({"env": "DEFAULT_SUPERUSER_EMAIL", "value": os.getenv("DEFAULT_SUPERUSER_EMAIL", "admin@datav.com")})
        result.append({"env": "DB_TYPE", "value": os.getenv("DB_TYPE", "postgresql")})
        result.append({"env": "DEVICE", "value": os.getenv("DEVICE", "cpu")})
        result.append({"env": "STORAGE_IMPL", "value": os.getenv("STORAGE_IMPL", "MINIO")})

        return result


class SandboxMgr:
    """沙箱 provider 配置与操作管理。"""

    # Provider 注册表与元数据
    PROVIDER_REGISTRY = {
        "self_managed": {
            "name": "Self-Managed",
            "description": "On-premise deployment using Daytona/Docker",
            "tags": ["self-hosted", "low-latency", "secure"],
        },
        "aliyun_codeinterpreter": {
            "name": "Aliyun Code Interpreter",
            "description": "Aliyun Function Compute Code Interpreter - Code execution in serverless microVMs",
            "tags": ["saas", "cloud", "scalable", "aliyun"],
        },
        "e2b": {
            "name": "E2B",
            "description": "E2B Cloud - Code Execution Sandboxes",
            "tags": ["saas", "fast", "global"],
        },
    }

    @staticmethod
    def list_providers():
        """列出所有可用的沙箱 provider。"""
        return [{"id": provider_id, **metadata} for provider_id, metadata in SandboxMgr.PROVIDER_REGISTRY.items()]

    @staticmethod
    def get_provider_config_schema(provider_id: str):
        """获取指定 provider 的配置 schema。"""
        from agent.sandbox.providers import (
            AliyunCodeInterpreterProvider,
            E2BProvider,
            SelfManagedProvider,
        )

        schemas = {
            "self_managed": SelfManagedProvider.get_config_schema(),
            "aliyun_codeinterpreter": AliyunCodeInterpreterProvider.get_config_schema(),
            "e2b": E2BProvider.get_config_schema(),
        }

        if provider_id not in schemas:
            raise AdminException(f"Unknown provider: {provider_id}")

        return schemas.get(provider_id, {})

    @staticmethod
    def get_config(db: Session):
        """获取当前沙箱配置。"""
        try:
            # 当前激活的 provider 类型
            provider_type_settings = SystemSettingsService.get_by_name(db, "sandbox.provider_type")
            if not provider_type_settings:
                # 未配置时返回默认
                provider_type = "self_managed"
            else:
                provider_type = provider_type_settings[0].value

            # provider 专属配置
            provider_config_settings = SystemSettingsService.get_by_name(db, f"sandbox.{provider_type}")
            if not provider_config_settings:
                provider_config = {}
            else:
                try:
                    provider_config = json.loads(provider_config_settings[0].value)
                except json.JSONDecodeError:
                    provider_config = {}

            return {
                "provider_type": provider_type,
                "config": provider_config,
            }
        except Exception as e:
            raise AdminException(f"Failed to get sandbox config: {e!s}")

    @staticmethod
    def set_config(db: Session, provider_type: str, config: dict, set_active: bool = True):
        """
        设置沙箱 provider 配置。

        Args:
            db: 数据库会话
            provider_type: provider 标识（如 "self_managed"、"e2b"）
            config: provider 配置字典
            set_active: 为 True 时同时切换激活 provider；为 False 时仅更新配置不切换。默认 True。

        Returns:
            含更新后 provider_type 与 config 的字典
        """
        from agent.sandbox.providers import (
            AliyunCodeInterpreterProvider,
            E2BProvider,
            SelfManagedProvider,
        )

        try:
            # 校验 provider 类型
            if provider_type not in SandboxMgr.PROVIDER_REGISTRY:
                raise AdminException(f"Unknown provider type: {provider_type}")

            # 取 schema 做校验
            schema = SandboxMgr.get_provider_config_schema(provider_type)

            # 按 schema 校验配置
            for field_name, field_schema in schema.items():
                if field_schema.get("required", False) and field_name not in config:
                    raise AdminException(f"Required field '{field_name}' is missing")

                # 类型校验
                if field_name in config:
                    field_type = field_schema.get("type")
                    if field_type == "integer":
                        if not isinstance(config[field_name], int):
                            raise AdminException(f"Field '{field_name}' must be an integer")
                    elif field_type == "string":
                        if not isinstance(config[field_name], str):
                            raise AdminException(f"Field '{field_name}' must be a string")
                    elif field_type == "bool":
                        if not isinstance(config[field_name], bool):
                            raise AdminException(f"Field '{field_name}' must be a boolean")

                    # 整型范围校验
                    if field_type == "integer" and field_name in config:
                        min_val = field_schema.get("min")
                        max_val = field_schema.get("max")
                        if min_val is not None and config[field_name] < min_val:
                            raise AdminException(f"Field '{field_name}' must be >= {min_val}")
                        if max_val is not None and config[field_name] > max_val:
                            raise AdminException(f"Field '{field_name}' must be <= {max_val}")

            # provider 自定义校验
            provider_classes = {
                "self_managed": SelfManagedProvider,
                "aliyun_codeinterpreter": AliyunCodeInterpreterProvider,
                "e2b": E2BProvider,
            }
            provider = provider_classes[provider_type]()
            is_valid, error_msg = provider.validate_config(config)
            if not is_valid:
                raise AdminException(f"Provider validation failed: {error_msg}")

            # set_active 为 True 时才更新 provider_type
            if set_active:
                SettingsMgr.update_by_name(db, "sandbox.provider_type", provider_type)

            # 始终更新 provider 配置
            config_json = json.dumps(config)
            SettingsMgr.update_by_name(db, f"sandbox.{provider_type}", config_json)

            return {"provider_type": provider_type, "config": config}
        except AdminException:
            raise
        except Exception as e:
            raise AdminException(f"Failed to set sandbox config: {e!s}")

    @staticmethod
    def test_connection(provider_type: str, config: dict):
        """
        通过执行一段简单 Python 脚本测试与沙箱 provider 的连通性。

        会创建一个临时沙箱实例并运行测试代码，验证：
        - 凭证有效
        - 沙箱可创建
        - 代码可正常执行

        Args:
            provider_type: provider 标识
            config: provider 配置字典

        Returns:
            含 stdout、stderr、exit_code、execution_time 的测试结果字典
        """
        try:
            from agent.sandbox.providers import (
                AliyunCodeInterpreterProvider,
                E2BProvider,
                SelfManagedProvider,
            )

            # 按类型实例化 provider
            provider_classes = {
                "self_managed": SelfManagedProvider,
                "aliyun_codeinterpreter": AliyunCodeInterpreterProvider,
                "e2b": E2BProvider,
            }

            if provider_type not in provider_classes:
                raise AdminException(f"Unknown provider type: {provider_type}")

            provider = provider_classes[provider_type]()

            # 用配置初始化
            if not provider.initialize(config):
                raise AdminException(f"Failed to initialize provider '{provider_type}'")

            # 创建临时测试实例
            instance = provider.create_instance(template="python")

            # multirag 各 provider 创建成功的 status 不一致：self_managed/e2b 为 "running"，aliyun 为 "READY"
            if not instance or instance.status not in ("READY", "running"):
                raise AdminException(f"Failed to create sandbox instance. Status: {instance.status if instance else 'None'}")

            # 行使基本 Python 功能的测试代码
            test_code = """
# Test basic Python functionality
import sys
import json
import math

print("Python version:", sys.version)
print("Platform:", sys.platform)

# Test basic calculations
result = 2 + 2
print(f"2 + 2 = {result}")

# Test JSON operations
data = {"test": "data", "value": 123}
print(f"JSON dump: {json.dumps(data)}")

# Test math operations
print(f"Math.sqrt(16) = {math.sqrt(16)}")

# Test error handling
try:
    x = 1 / 1
    print("Division test: OK")
except Exception as e:
    print(f"Error: {e}")

# Return success indicator
print("TEST_PASSED")
"""

            # 带超时执行测试代码
            execution_result = provider.execute_code(
                instance_id=instance.instance_id,
                code=test_code,
                language="python",
                timeout=10,  # 10 秒超时
            )

            # 清理测试实例（multirag/ragflow base 均为 destroy_instance）
            try:
                provider.destroy_instance(instance.instance_id)
                logging.info(f"Cleaned up test instance {instance.instance_id}")
            except Exception as cleanup_error:
                logging.warning(f"Failed to cleanup test instance {instance.instance_id}: {cleanup_error}")

            # 组装结果信息
            success = execution_result.exit_code == 0 and "TEST_PASSED" in execution_result.stdout

            message_parts = [f"Test {(success and 'PASSED') or 'FAILED'}", f"Exit code: {execution_result.exit_code}", f"Execution time: {execution_result.execution_time:.2f}s"]

            if execution_result.stdout.strip():
                stdout_preview = execution_result.stdout.strip()[:200]
                message_parts.append(f"Output: {stdout_preview}...")

            if execution_result.stderr.strip():
                stderr_preview = execution_result.stderr.strip()[:200]
                message_parts.append(f"Errors: {stderr_preview}...")

            message = " | ".join(message_parts)

            return {
                "success": success,
                "message": message,
                "details": {
                    "exit_code": execution_result.exit_code,
                    "execution_time": execution_result.execution_time,
                    "stdout": execution_result.stdout,
                    "stderr": execution_result.stderr,
                },
            }

        except AdminException:
            raise
        except Exception as e:
            import traceback

            error_details = traceback.format_exc()
            raise AdminException(f"Connection test failed: {e!s}\n\nStack trace:\n{error_details}")
