# coding=utf-8
"""
@project: multirag
@Author：龙
@file： guard_label_service.py
@date：2025/01/11 16:10
@desc: AI安全护栏标签管理服务
"""
import logging
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import and_

from api.db.db_models import GuardLabel
from api.db.services.common_service import CommonService
from api.utils import get_uuid


class GuardLabelService(CommonService):
    """AI安全护栏标签管理服务"""
    model = GuardLabel

    @classmethod
    def create_label(cls, db: Session, dimension_id: str, code: str, name: str,
                    description: str = None, tenant_id: str = None,
                    created_by: str = None, **kwargs) -> Optional[str]:
        """
        创建护栏标签
        
        Args:
            db: 数据库会话
            dimension_id: 维度ID
            code: 标签代码
            name: 标签名称
            description: 标签描述
            tenant_id: 租户ID
            created_by: 创建者ID
            **kwargs: 其他参数
            
        Returns:
            创建成功返回标签ID，失败返回None
        """
        try:
            # 检查代码是否已存在
            existing = cls.get_label_by_code(db, code, tenant_id)
            if existing:
                logging.warning(f"标签代码 {code} 已存在，租户: {tenant_id}")
                return None
                
            label_data = {
                "id": get_uuid(),
                "dimension_id": dimension_id,
                "code": code,
                "name": name,
                "description": description,
                "tenant_id": tenant_id,
                "created_by": created_by,
                "cloud_label": kwargs.get("cloud_label"),
                "cloud_label_type": kwargs.get("cloud_label_type", "string"),
                "detection_ranges": kwargs.get("detection_ranges", []),
                "enabled": kwargs.get("enabled", True),
                "risk_score": kwargs.get("risk_score", 70.0),
                "risk_level": kwargs.get("risk_level", 3),
                "sensitive_level": kwargs.get("sensitive_level"),
                "action": kwargs.get("action", "warn"),
                "action_config": kwargs.get("action_config", {}),
                "sort_order": kwargs.get("sort_order", 0),
                "status": kwargs.get("status", "1")
            }
            
            label = cls.save(db, **label_data)
            return label.id
            
        except Exception as e:
            logging.error(f"创建标签失败: {e}")
            return None

    @classmethod
    def get_label_by_code(cls, db: Session, code: str, 
                         tenant_id: str) -> Optional[GuardLabel]:
        """
        根据代码获取标签
        
        Args:
            db: 数据库会话
            code: 标签代码
            tenant_id: 租户ID
            
        Returns:
            标签对象或None
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
            logging.error(f"获取标签失败: {e}")
            return None

    @classmethod
    def get_labels_by_dimension(cls, db: Session, dimension_id: str,
                               tenant_id: str, enabled_only: bool = False) -> List[GuardLabel]:
        """
        获取维度下的标签列表
        
        Args:
            db: 数据库会话
            dimension_id: 维度ID
            tenant_id: 租户ID
            enabled_only: 是否只返回启用的标签
            
        Returns:
            标签列表
        """
        try:
            query = db.query(cls.model).filter(
                and_(
                    cls.model.dimension_id == dimension_id,
                    cls.model.tenant_id == tenant_id,
                    cls.model.status == "1"
                )
            )
            
            if enabled_only:
                query = query.filter(cls.model.enabled == True)
                
            return query.order_by(cls.model.sort_order.asc()).all()
        except Exception as e:
            logging.error(f"获取维度标签失败: {e}")
            return []

    @classmethod
    def get_labels_by_tenant(cls, db: Session, tenant_id: str,
                            enabled_only: bool = False) -> List[GuardLabel]:
        """
        获取租户的标签列表
        
        Args:
            db: 数据库会话
            tenant_id: 租户ID
            enabled_only: 是否只返回启用的标签
            
        Returns:
            标签列表
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
            logging.error(f"获取租户标签失败: {e}")
            return []

    @classmethod
    def get_labels_by_codes(cls, db: Session, codes: List[str],
                           tenant_id: str) -> List[GuardLabel]:
        """
        根据代码列表获取标签
        
        Args:
            db: 数据库会话
            codes: 标签代码列表
            tenant_id: 租户ID
            
        Returns:
            标签列表
        """
        try:
            return db.query(cls.model).filter(
                and_(
                    cls.model.code.in_(codes),
                    cls.model.tenant_id == tenant_id,
                    cls.model.enabled == True,
                    cls.model.status == "1"
                )
            ).all()
        except Exception as e:
            logging.error(f"批量获取标签失败: {e}")
            return []

    @classmethod
    def update_label(cls, db: Session, label_id: str, 
                    update_data: Dict[str, Any]) -> bool:
        """
        更新标签信息
        
        Args:
            db: 数据库会话
            label_id: 标签ID
            update_data: 更新数据
            
        Returns:
            更新成功返回True，失败返回False
        """
        try:
            return cls.update_by_id(db, label_id, update_data)
        except Exception as e:
            logging.error(f"更新标签失败: {e}")
            return False

    @classmethod
    def delete_label(cls, db: Session, label_id: str) -> bool:
        """
        删除标签（物理删除）
        
        Args:
            db: 数据库会话
            label_id: 标签ID
            
        Returns:
            删除成功返回True，失败返回False
        """
        try:
            return cls.delete_by_id(db, label_id) > 0
        except Exception as e:
            logging.error(f"删除标签失败: {e}")
            return False

    @classmethod
    def toggle_label_status(cls, db: Session, label_id: str) -> bool:
        """
        切换标签启用状态
        
        Args:
            db: 数据库会话
            label_id: 标签ID
            
        Returns:
            切换成功返回True，失败返回False
        """
        try:
            label = cls.get_by_id(db, label_id)
            if not label:
                return False
                
            new_enabled = not label.enabled
            return cls.update_by_id(db, label_id, {"enabled": new_enabled})
            
        except Exception as e:
            logging.error(f"切换标签状态失败: {e}")
            return False

    @classmethod
    def get_label_stats(cls, db: Session, tenant_id: str) -> Dict[str, Any]:
        """
        获取标签统计信息
        
        Args:
            db: 数据库会话
            tenant_id: 租户ID
            
        Returns:
            统计信息字典
        """
        try:
            labels = cls.get_labels_by_tenant(db, tenant_id)
            enabled_count = len([l for l in labels if l.enabled])
            
            # 按维度统计
            dimension_stats = {}
            for label in labels:
                dim_id = label.dimension_id
                if dim_id not in dimension_stats:
                    dimension_stats[dim_id] = {"total": 0, "enabled": 0}
                dimension_stats[dim_id]["total"] += 1
                if label.enabled:
                    dimension_stats[dim_id]["enabled"] += 1
            
            return {
                "total_labels": len(labels),
                "enabled_labels": enabled_count,
                "disabled_labels": len(labels) - enabled_count,
                "dimension_stats": dimension_stats,
                "label_codes": [l.code for l in labels if l.enabled]
            }
        except Exception as e:
            logging.error(f"获取标签统计失败: {e}")
            return {}

    @classmethod
    def init_default_labels(cls, db: Session, dimension_configs: Dict[str, str],
                           tenant_id: str, created_by: str) -> List[str]:
        """
        初始化默认标签
        
        Args:
            db: 数据库会话
            dimension_configs: 维度配置 {dimension_code: dimension_id}
            tenant_id: 租户ID
            created_by: 创建者ID
            
        Returns:
            创建的标签ID列表
        """
        # 内容合规标签
        content_compliance_labels = [
            {
                "code": "political_entity",
                "name": "政治实体",
                "description": "检测政治相关敏感内容",
                "cloud_label": "political_entity",
                "risk_score": 90.0,
                "risk_level": 5,
                "action": "block",
                "sort_order": 1
            },
            {
                "code": "pornographic_adult",
                "name": "色情内容",
                "description": "检测色情、成人内容",
                "cloud_label": "pornographic_adult",
                "risk_score": 95.0,
                "risk_level": 5,
                "action": "block",
                "sort_order": 2
            },
            {
                "code": "violent_extremists",
                "name": "暴力极端",
                "description": "检测暴力、极端主义内容",
                "cloud_label": "violent_extremists",
                "risk_score": 85.0,
                "risk_level": 4,
                "action": "block",
                "sort_order": 3
            }
        ]
        
        # 敏感内容标签
        sensitive_content_labels = [
            {
                "code": "bank_card_cn",
                "name": "银行卡号",
                "description": "检测中国银行卡号",
                "cloud_label": "bank_card_cn",
                "risk_score": 80.0,
                "risk_level": 4,
                "sensitive_level": "S3",
                "action": "replace",
                "action_config": {"replacement": "[银行卡号]"},
                "sort_order": 1
            },
            {
                "code": "phone_number",
                "name": "手机号码",
                "description": "检测手机号码",
                "cloud_label": "phone_number",
                "risk_score": 70.0,
                "risk_level": 3,
                "sensitive_level": "S2",
                "action": "replace",
                "action_config": {"replacement": "[手机号]"},
                "sort_order": 2
            },
            {
                "code": "id_card_cn",
                "name": "身份证号",
                "description": "检测中国身份证号",
                "cloud_label": "id_card_cn",
                "risk_score": 85.0,
                "risk_level": 4,
                "sensitive_level": "S3",
                "action": "replace",
                "action_config": {"replacement": "[身份证号]"},
                "sort_order": 3
            }
        ]
        
        # 提示词攻击标签
        prompt_attack_labels = [
            {
                "code": "prompt_injection",
                "name": "提示词注入",
                "description": "检测提示词注入攻击",
                "cloud_label": "Prompt Injection",
                "cloud_label_type": "phrase",
                "risk_score": 95.0,
                "risk_level": 5,
                "action": "block",
                "sort_order": 1
            },
            {
                "code": "jailbreak_attempt",
                "name": "越狱尝试",
                "description": "检测AI越狱尝试",
                "cloud_label": "Jailbreak",
                "cloud_label_type": "phrase",
                "risk_score": 90.0,
                "risk_level": 5,
                "action": "block",
                "sort_order": 2
            }
        ]
        
        # 组织标签数据
        label_groups = [
            ("CONTENT_COMPLIANCE", content_compliance_labels),
            ("SENSITIVE_CONTENT", sensitive_content_labels),
            ("PROMPT_ATTACK", prompt_attack_labels)
        ]
        
        created_ids = []
        for dimension_code, labels in label_groups:
            dimension_id = dimension_configs.get(dimension_code)
            if not dimension_id:
                continue
                
            for label_data in labels:
                label_id = cls.create_label(
                    db=db,
                    dimension_id=dimension_id,
                    tenant_id=tenant_id,
                    created_by=created_by,
                    **label_data
                )
                if label_id:
                    created_ids.append(label_id)
                    
        return created_ids