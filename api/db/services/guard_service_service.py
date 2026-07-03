"""
@project: multirag
@Author：龙
@file： guard_service_service.py
@date：2025/01/11 16:40
@desc: AI安全护栏服务配置管理服务
"""

import logging
from typing import Any

from sqlalchemy import and_
from sqlalchemy.orm import Session

from api.db.db_models import GuardService
from api.db.services.common_service import CommonService
from common.misc_utils import get_uuid


class GuardServiceService(CommonService):
    """AI安全护栏服务配置管理服务"""

    model = GuardService

    @classmethod
    def create_service(cls, db: Session, code: str, name: str, description: str = None, tenant_id: str = None, created_by: str = None, **kwargs) -> str | None:
        """
        创建AI服务配置

        Args:
            db: 数据库会话
            code: 服务代码
            name: 服务名称
            description: 服务描述
            tenant_id: 租户ID
            created_by: 创建者ID
            **kwargs: 其他参数

        Returns:
            创建成功返回服务ID，失败返回None
        """
        try:
            # 检查代码是否已存在
            existing = cls.get_service_by_code(db, code, tenant_id)
            if existing:
                logging.warning(f"服务代码 {code} 已存在，租户: {tenant_id}")
                return None

            service_data = {
                "id": get_uuid(),
                "code": code,
                "name": name,
                "description": description,
                "tenant_id": tenant_id,
                "created_by": created_by,
                "service_type": kwargs.get("service_type", "api"),
                "enabled_dimensions": kwargs.get("enabled_dimensions", []),
                "enabled_labels": kwargs.get("enabled_labels", []),
                "policy_config": kwargs.get("policy_config", {}),
                "cache_enabled": kwargs.get("cache_enabled", True),
                "timeout_ms": kwargs.get("timeout_ms", 1000),
                "total_requests": 0,
                "blocked_requests": 0,
                "status": kwargs.get("status", "1"),
            }

            service = cls.save(db, **service_data)
            return service.id

        except Exception as e:
            logging.error(f"创建服务配置失败: {e}")
            return None

    @classmethod
    def get_service_by_code(cls, db: Session, code: str, tenant_id: str) -> GuardService | None:
        """
        根据代码获取服务配置

        Args:
            db: 数据库会话
            code: 服务代码
            tenant_id: 租户ID

        Returns:
            服务配置对象或None
        """
        try:
            return db.query(cls.model).filter(and_(cls.model.code == code, cls.model.tenant_id == tenant_id, cls.model.status == "1")).first()
        except Exception as e:
            logging.error(f"获取服务配置失败: {e}")
            return None

    @classmethod
    def get_services_by_tenant(cls, db: Session, tenant_id: str) -> list[GuardService]:
        """
        获取租户的服务配置列表

        Args:
            db: 数据库会话
            tenant_id: 租户ID

        Returns:
            服务配置列表
        """
        try:
            return db.query(cls.model).filter(and_(cls.model.tenant_id == tenant_id, cls.model.status == "1")).order_by(cls.model.create_time.desc()).all()
        except Exception as e:
            logging.error(f"获取租户服务配置失败: {e}")
            return []

    @classmethod
    def update_service(cls, db: Session, service_id: str, update_data: dict[str, Any]) -> int:
        """
        更新服务配置

        Args:
            db: 数据库会话
            service_id: 服务ID
            update_data: 更新数据

        Returns:
            返回受影响的行数，失败返回0
        """
        try:
            return cls.update_by_id(db, service_id, update_data)
        except Exception as e:
            logging.error(f"更新服务配置失败: {e}")
            return 0

    @classmethod
    def increment_request_count(cls, db: Session, service_id: str, blocked: bool = False) -> int:
        """
        增加请求统计

        Args:
            db: 数据库会话
            service_id: 服务ID
            blocked: 是否被拦截

        Returns:
            返回受影响的行数，服务不存在或失败返回0
        """
        try:
            service = cls.get_by_id(db, service_id)
            if not service:
                return 0

            update_data = {"total_requests": service.total_requests + 1}

            if blocked:
                update_data["blocked_requests"] = service.blocked_requests + 1

            return cls.update_by_id(db, service_id, update_data)
        except Exception as e:
            logging.error(f"更新请求统计失败: {e}")
            return 0

    @classmethod
    def init_default_services(cls, db: Session, tenant_id: str, created_by: str) -> list[str]:
        """
        初始化默认服务配置

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            created_by: 创建者ID

        Returns:
            创建的服务ID列表
        """
        default_services = [
            {
                "code": "query_security_check",
                "name": "查询安全检测",
                "description": "用户查询内容的安全检测",
                "service_type": "api",
                "enabled_dimensions": ["CONTENT_COMPLIANCE", "PROMPT_ATTACK"],
                "enabled_labels": ["political_entity", "prompt_injection"],
                "policy_config": {"risk_threshold": 80, "default_action": "block", "enable_cache": True},
                "timeout_ms": 1000,
            },
            {
                "code": "response_security_check",
                "name": "响应安全检测",
                "description": "AI响应内容的安全检测",
                "service_type": "api",
                "enabled_dimensions": ["CONTENT_COMPLIANCE", "SENSITIVE_CONTENT"],
                "enabled_labels": ["political_entity", "bank_card_cn", "phone_number"],
                "policy_config": {"risk_threshold": 70, "default_action": "replace", "enable_cache": True},
                "timeout_ms": 1500,
            },
        ]

        created_ids = []
        for service_data in default_services:
            service_id = cls.create_service(db=db, tenant_id=tenant_id, created_by=created_by, **service_data)
            if service_id:
                created_ids.append(service_id)

        return created_ids
