"""
@project: multirag
@Author：龙
@file： guard_dimension_service.py
@date：2025/01/11 16:00
@desc: AI安全护栏维度管理服务
"""
import logging
from typing import Any

from sqlalchemy import and_
from sqlalchemy.orm import Session

from api.db.db_models import GuardDimension
from api.db.services.common_service import CommonService
from common.misc_utils import get_uuid


class GuardDimensionService(CommonService):
    """AI安全护栏维度管理服务"""
    model = GuardDimension

    @classmethod
    def create_dimension(cls, db: Session, code: str, name: str,
                        description: str | None = None, tenant_id: str | None = None,
                        created_by: str | None = None, **kwargs) -> str | None:
        """
        创建护栏维度

        Args:
            db: 数据库会话
            code: 维度代码
            name: 维度名称
            description: 维度描述
            tenant_id: 租户ID
            created_by: 创建者ID
            **kwargs: 其他参数

        Returns:
            创建成功返回维度ID，失败返回None
        """
        try:
            # 检查代码是否已存在
            existing = cls.get_dimension_by_code(db, code, tenant_id)
            if existing:
                logging.warning(f"维度代码 {code} 已存在，租户: {tenant_id}")
                raise ValueError(f"维度代码 {code} 已存在")

            dimension_data = {
                "id": get_uuid(),
                "code": code,
                "name": name,
                "description": description,
                "tenant_id": tenant_id,
                "created_by": created_by,
                "enabled": kwargs.get("enabled", True),
                "config": kwargs.get("config", {}),
                "sort_order": kwargs.get("sort_order", 0),
                "status": kwargs.get("status", "1")
            }

            dimension = cls.save(db, **dimension_data)
            return dimension.id

        except Exception as e:
            logging.error(f"创建维度失败: {e}")
            return None

    @classmethod
    def get_dimension_by_code(cls, db: Session, code: str,
                             tenant_id: str) -> GuardDimension | None:
        """
        根据代码获取维度

        Args:
            db: 数据库会话
            code: 维度代码
            tenant_id: 租户ID

        Returns:
            维度对象或None
        """
        try:
            return db.query(cls.model).filter(
                and_(
                    cls.model.code == code,
                    cls.model.tenant_id == tenant_id,
                    cls.model.status == "1"
                )
            ).first()
        except Exception as e:
            logging.error(f"获取维度失败: {e}")
            return None

    @classmethod
    def get_enabled_dimensions(cls, db: Session, tenant_id: str) -> list[GuardDimension]:
        """
        获取启用的维度列表

        Args:
            db: 数据库会话
            tenant_id: 租户ID

        Returns:
            启用的维度列表
        """
        try:
            return db.query(cls.model).filter(
                and_(
                    cls.model.tenant_id == tenant_id,
                    cls.model.enabled == True,
                    cls.model.status == "1"
                )
            ).order_by(cls.model.sort_order.asc()).all()
        except Exception as e:
            logging.error(f"获取启用维度失败: {e}")
            return []

    @classmethod
    def get_dimensions_by_tenant(cls, db: Session, tenant_id: str,
                                enabled_only: bool = False) -> list[GuardDimension]:
        """
        获取租户的维度列表

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            enabled_only: 是否只返回启用的维度

        Returns:
            维度列表
        """
        try:
            query = db.query(cls.model).filter(
                and_(
                    cls.model.tenant_id == tenant_id,
                    cls.model.status == "1"
                )
            )

            if enabled_only:
                query = query.filter(cls.model.enabled == True)

            return query.order_by(cls.model.sort_order.asc()).all()
        except Exception as e:
            logging.error(f"获取租户维度失败: {e}")
            return []

    @classmethod
    def update_dimension(cls, db: Session, dimension_id: str,
                        update_data: dict[str, Any]) -> int:
        """
        更新维度信息

        Args:
            db: 数据库会话
            dimension_id: 维度ID
            update_data: 更新数据

        Returns:
            更新成功返回True，失败返回False
        """
        try:
            return cls.update_by_id(db, dimension_id, update_data)
        except Exception as e:
            logging.error(f"更新维度失败: {e}")
            return False

    @classmethod
    def delete_dimension(cls, db: Session, dimension_id: str) -> int:
        """
        删除维度（物理删除）

        Args:
            db: 数据库会话
            dimension_id: 维度ID

        Returns:
            删除成功返回True，失败返回False
        """
        try:
            return cls.delete_by_id(db, dimension_id) > 0
        except Exception as e:
            logging.error(f"删除维度失败: {e}")
            return False

    @classmethod
    def toggle_dimension_status(cls, db: Session, dimension_id: str) -> int:
        """
        切换维度启用状态

        Args:
            db: 数据库会话
            dimension_id: 维度ID

        Returns:
            切换成功返回True，失败返回False
        """
        try:
            dimension = cls.get_by_id(db, dimension_id)
            if not dimension:
                return False

            new_enabled = not dimension.enabled
            return cls.update_by_id(db, dimension_id, {"enabled": new_enabled})

        except Exception as e:
            logging.error(f"切换维度状态失败: {e}")
            return False

    @classmethod
    def get_dimension_stats(cls, db: Session, tenant_id: str) -> dict[str, Any]:
        """
        获取维度统计信息

        Args:
            db: 数据库会话
            tenant_id: 租户ID

        Returns:
            统计信息字典
        """
        try:
            dimensions = cls.get_dimensions_by_tenant(db, tenant_id)
            enabled_count = len([d for d in dimensions if d.enabled])

            return {
                "total_dimensions": len(dimensions),
                "enabled_dimensions": enabled_count,
                "disabled_dimensions": len(dimensions) - enabled_count,
                "dimension_codes": [d.code for d in dimensions if d.enabled]
            }
        except Exception as e:
            logging.error(f"获取维度统计失败: {e}")
            return {}

    @classmethod
    def init_default_dimensions(cls, db: Session, tenant_id: str,
                               created_by: str) -> list[str]:
        """
        初始化默认维度

        Args:
            db: 数据库会话
            tenant_id: 租户ID
            created_by: 创建者ID

        Returns:
            创建的维度ID列表
        """
        default_dimensions = [
            {
                "code": "CONTENT_COMPLIANCE",
                "name": "内容合规",
                "description": "检测政治、色情、暴力等不合规内容",
                "sort_order": 1,
                "config": {
                    "detection_types": ["political", "pornographic", "violent"],
                    "default_action": "block",
                    "risk_threshold": 80
                }
            },
            {
                "code": "SENSITIVE_CONTENT",
                "name": "敏感内容",
                "description": "检测个人隐私信息、敏感数据等",
                "sort_order": 2,
                "config": {
                    "detection_types": ["pii", "credentials", "financial"],
                    "default_action": "replace",
                    "risk_threshold": 70
                }
            },
            {
                "code": "PROMPT_ATTACK",
                "name": "提示词攻击",
                "description": "检测提示词注入、越狱等攻击行为",
                "sort_order": 3,
                "config": {
                    "detection_types": ["injection", "jailbreak", "manipulation"],
                    "default_action": "block",
                    "risk_threshold": 90
                }
            }
        ]

        created_ids = []
        for dimension_data in default_dimensions:
            dimension_id = cls.create_dimension(
                db=db,
                tenant_id=tenant_id,
                created_by=created_by,
                **dimension_data
            )
            if dimension_id:
                created_ids.append(dimension_id)

        return created_ids
