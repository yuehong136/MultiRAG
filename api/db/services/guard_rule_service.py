"""
@project: multirag
@Author：龙
@file： guard_rule_service.py
@date：2025/01/11 16:50
@desc: AI安全护栏规则管理服务
"""
import hashlib
import logging

from sqlalchemy import and_
from sqlalchemy.orm import Session

from api.db.db_models import GuardRule
from api.db.services.common_service import CommonService
from common.misc_utils import get_uuid


class GuardRuleService(CommonService):
    """AI安全护栏规则管理服务"""
    model = GuardRule

    @classmethod
    def create_rule(cls, db: Session, label_id: str, rule_type: str, content: str,
                   tenant_id: str = None, created_by: str = None, **kwargs) -> str | None:
        """
        创建护栏规则

        Args:
            db: 数据库会话
            label_id: 标签ID
            rule_type: 规则类型
            content: 规则内容
            tenant_id: 租户ID
            created_by: 创建者ID
            **kwargs: 其他参数

        Returns:
            创建成功返回规则ID，失败返回None
        """
        try:
            content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()

            # 检查是否已存在相同规则
            existing = cls.get_rule_by_hash(db, label_id, content_hash)
            if existing:
                logging.warning(f"规则内容已存在，标签ID: {label_id}")
                return None

            rule_data = {
                "id": get_uuid(),
                "label_id": label_id,
                "rule_type": rule_type,
                "content": content,
                "content_hash": content_hash,
                "tenant_id": tenant_id,
                "created_by": created_by,
                "match_mode": kwargs.get("match_mode", "exact"),
                "case_sensitive": kwargs.get("case_sensitive", False),
                "config": kwargs.get("config", {}),
                "weight": kwargs.get("weight", 1.0),
                "priority": kwargs.get("priority", 0),
                "source": kwargs.get("source"),
                "description": kwargs.get("description"),
                "status": kwargs.get("status", "1")
            }

            rule = cls.save(db, **rule_data)
            return rule.id

        except Exception as e:
            logging.error(f"创建规则失败: {e}")
            return None

    @classmethod
    def get_rule_by_hash(cls, db: Session, label_id: str,
                        content_hash: str) -> GuardRule | None:
        """
        根据内容哈希获取规则

        Args:
            db: 数据库会话
            label_id: 标签ID
            content_hash: 内容哈希

        Returns:
            规则对象或None
        """
        try:
            return db.query(cls.model).filter(
                and_(
                    cls.model.label_id == label_id,
                    cls.model.content_hash == content_hash,
                    cls.model.status == "1"
                )
            ).first()
        except Exception as e:
            logging.error(f"获取规则失败: {e}")
            return None

    @classmethod
    def get_rules_by_label(cls, db: Session, label_id: str) -> list[GuardRule]:
        """
        获取标签的规则列表

        Args:
            db: 数据库会话
            label_id: 标签ID

        Returns:
            规则列表
        """
        try:
            return db.query(cls.model).filter(
                and_(
                    cls.model.label_id == label_id,
                    cls.model.status == "1"
                )
            ).order_by(cls.model.priority.desc()).all()
        except Exception as e:
            logging.error(f"获取标签规则失败: {e}")
            return []

    @classmethod
    def get_rules_by_type(cls, db: Session, rule_type: str,
                         tenant_id: str) -> list[GuardRule]:
        """
        根据类型获取规则列表

        Args:
            db: 数据库会话
            rule_type: 规则类型
            tenant_id: 租户ID

        Returns:
            规则列表
        """
        try:
            return db.query(cls.model).filter(
                and_(
                    cls.model.rule_type == rule_type,
                    cls.model.tenant_id == tenant_id,
                    cls.model.status == "1"
                )
            ).order_by(cls.model.priority.desc()).all()
        except Exception as e:
            logging.error(f"获取类型规则失败: {e}")
            return []

    @classmethod
    def init_default_rules(cls, db: Session, label_configs: dict[str, str],
                          tenant_id: str, created_by: str) -> list[str]:
        """
        初始化默认规则

        Args:
            db: 数据库会话
            label_configs: 标签配置 {label_code: label_id}
            tenant_id: 租户ID
            created_by: 创建者ID

        Returns:
            创建的规则ID列表
        """
        # 政治敏感词规则
        political_rules = [
            {"rule_type": "keyword", "content": "政治敏感词1", "weight": 2.0},
            {"rule_type": "keyword", "content": "政治敏感词2", "weight": 1.5},
        ]

        # 色情内容规则
        pornographic_rules = [
            {"rule_type": "keyword", "content": "色情词汇1", "weight": 3.0},
            {"rule_type": "regex", "content": r"色情.*内容", "weight": 2.0},
        ]

        # PII规则
        pii_rules = [
            {"rule_type": "regex", "content": r"1[3-9]\d{9}", "weight": 1.0, "description": "手机号码"},
            {"rule_type": "regex", "content": r"\d{15}|\d{17}[\dXx]", "weight": 1.0, "description": "身份证号"},
            {"rule_type": "regex", "content": r"\d{16,19}", "weight": 1.0, "description": "银行卡号"},
        ]

        # 提示词注入规则
        injection_rules = [
            {"rule_type": "keyword", "content": "ignore previous instructions", "weight": 5.0},
            {"rule_type": "keyword", "content": "system prompt", "weight": 3.0},
        ]

        # 组织规则数据
        rule_groups = [
            ("political_entity", political_rules),
            ("pornographic_adult", pornographic_rules),
            ("phone_number", pii_rules),
            ("prompt_injection", injection_rules)
        ]

        created_ids = []
        for label_code, rules in rule_groups:
            label_id = label_configs.get(label_code)
            if not label_id:
                continue

            for rule_data in rules:
                rule_id = cls.create_rule(
                    db=db,
                    label_id=label_id,
                    tenant_id=tenant_id,
                    created_by=created_by,
                    **rule_data
                )
                if rule_id:
                    created_ids.append(rule_id)

        return created_ids
