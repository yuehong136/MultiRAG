"""
@project: multirag
@Author：龙
@file： guard_label_library_service.py
@date：2025/01/11 18:35
@desc: AI安全护栏标签词库关系管理服务
"""
import logging
from typing import Any

from sqlalchemy import and_
from sqlalchemy.orm import Session

from api.db.db_models import GuardLabel, GuardLabelLibrary, GuardLibrary
from api.db.services.common_service import CommonService
from common.misc_utils import get_uuid


class GuardLabelLibraryService(CommonService):
    """AI安全护栏标签词库关系管理服务"""
    model = GuardLabelLibrary

    @classmethod
    def bind_library_to_label(cls, db: Session, label_id: str, library_id: str,
                             priority: int = 0, enabled: bool = True,
                             tenant_id: str = None, created_by: str = None) -> str | None:
        """
        绑定词库到标签

        Args:
            db: 数据库会话
            label_id: 标签ID
            library_id: 词库ID
            priority: 优先级
            enabled: 是否启用
            tenant_id: 租户ID
            created_by: 创建者ID

        Returns:
            绑定成功返回关系ID，失败返回None
        """
        try:
            # 检查是否已经绑定
            existing = cls.get_binding(db, label_id, library_id)
            if existing:
                logging.warning(f"标签词库关系已存在: {label_id} -> {library_id}")
                return None

            binding_data = {
                "id": get_uuid(),
                "label_id": label_id,
                "library_id": library_id,
                "priority": priority,
                "enabled": enabled,
                "tenant_id": tenant_id,
                "created_by": created_by,
                "status": "1"
            }

            binding = cls.save(db, **binding_data)
            return binding.id

        except Exception as e:
            logging.error(f"绑定词库到标签失败: {e}")
            return None

    @classmethod
    def get_binding(cls, db: Session, label_id: str, library_id: str) -> GuardLabelLibrary | None:
        """
        获取标签词库绑定关系

        Args:
            db: 数据库会话
            label_id: 标签ID
            library_id: 词库ID

        Returns:
            绑定关系对象或None
        """
        try:
            return db.query(cls.model).filter(
                and_(
                    cls.model.label_id == label_id,
                    cls.model.library_id == library_id,
                    cls.model.status == "1"
                )
            ).first()
        except Exception as e:
            logging.error(f"获取标签词库绑定关系失败: {e}")
            return None

    @classmethod
    def get_libraries_by_label(cls, db: Session, label_id: str,
                              enabled_only: bool = True) -> list[dict[str, Any]]:
        """
        获取标签绑定的词库列表

        Args:
            db: 数据库会话
            label_id: 标签ID
            enabled_only: 是否只返回启用的

        Returns:
            词库列表（包含绑定信息）
        """
        try:
            query = db.query(cls.model, GuardLibrary).join(
                GuardLibrary, cls.model.library_id == GuardLibrary.id
            ).filter(
                and_(
                    cls.model.label_id == label_id,
                    cls.model.status == "1",
                    GuardLibrary.status == "1"
                )
            )

            if enabled_only:
                query = query.filter(cls.model.enabled == True)

            results = query.order_by(cls.model.priority.desc()).all()

            libraries = []
            for binding, library in results:
                library_dict = library.to_dict()
                library_dict["binding"] = {
                    "id": binding.id,
                    "priority": binding.priority,
                    "enabled": binding.enabled,
                    "create_time": binding.create_time.isoformat() if binding.create_time else None
                }
                libraries.append(library_dict)

            return libraries

        except Exception as e:
            logging.error(f"获取标签词库列表失败: {e}")
            return []

    @classmethod
    def get_labels_by_library(cls, db: Session, library_id: str,
                             enabled_only: bool = True) -> list[dict[str, Any]]:
        """
        获取使用此词库的标签列表

        Args:
            db: 数据库会话
            library_id: 词库ID
            enabled_only: 是否只返回启用的

        Returns:
            标签列表（包含绑定信息）
        """
        try:
            query = db.query(cls.model, GuardLabel).join(
                GuardLabel, cls.model.label_id == GuardLabel.id
            ).filter(
                and_(
                    cls.model.library_id == library_id,
                    cls.model.status == "1",
                    GuardLabel.status == "1"
                )
            )

            if enabled_only:
                query = query.filter(cls.model.enabled == True)

            results = query.order_by(cls.model.priority.desc()).all()

            labels = []
            for binding, label in results:
                label_dict = label.to_dict()
                label_dict["binding"] = {
                    "id": binding.id,
                    "priority": binding.priority,
                    "enabled": binding.enabled,
                    "create_time": binding.create_time.isoformat() if binding.create_time else None
                }
                labels.append(label_dict)

            return labels

        except Exception as e:
            logging.error(f"获取词库标签列表失败: {e}")
            return []

    @classmethod
    def update_binding(cls, db: Session, binding_id: str,
                      update_data: dict[str, Any]) -> bool:
        """
        更新标签词库绑定关系

        Args:
            db: 数据库会话
            binding_id: 绑定关系ID
            update_data: 更新数据

        Returns:
            更新成功返回True，失败返回False
        """
        try:
            return cls.update_by_id(db, binding_id, update_data)
        except Exception as e:
            logging.error(f"更新标签词库绑定关系失败: {e}")
            return False

    @classmethod
    def unbind_library_from_label(cls, db: Session, label_id: str,
                                 library_id: str) -> bool:
        """
        解绑标签词库关系

        Args:
            db: 数据库会话
            label_id: 标签ID
            library_id: 词库ID

        Returns:
            解绑成功返回True，失败返回False
        """
        try:
            binding = cls.get_binding(db, label_id, library_id)
            if not binding:
                return False

            return cls.delete_by_id(db, binding.id) > 0

        except Exception as e:
            logging.error(f"解绑标签词库关系失败: {e}")
            return False

    @classmethod
    def batch_bind_libraries(cls, db: Session, label_id: str,
                           library_ids: list[str], tenant_id: str = None,
                           created_by: str = None) -> dict[str, Any]:
        """
        批量绑定词库到标签

        Args:
            db: 数据库会话
            label_id: 标签ID
            library_ids: 词库ID列表
            tenant_id: 租户ID
            created_by: 创建者ID

        Returns:
            绑定结果统计
        """
        success_count = 0
        failed_count = 0
        failed_libraries = []

        for library_id in library_ids:
            binding_id = cls.bind_library_to_label(
                db, label_id, library_id,
                tenant_id=tenant_id, created_by=created_by
            )

            if binding_id:
                success_count += 1
            else:
                failed_count += 1
                failed_libraries.append(library_id)

        return {
            "success_count": success_count,
            "failed_count": failed_count,
            "failed_libraries": failed_libraries
        }

    @classmethod
    def batch_unbind_libraries(cls, db: Session, label_id: str,
                             library_ids: list[str]) -> dict[str, Any]:
        """
        批量解绑标签词库关系

        Args:
            db: 数据库会话
            label_id: 标签ID
            library_ids: 词库ID列表

        Returns:
            解绑结果统计
        """
        success_count = 0
        failed_count = 0
        failed_libraries = []

        for library_id in library_ids:
            if cls.unbind_library_from_label(db, label_id, library_id):
                success_count += 1
            else:
                failed_count += 1
                failed_libraries.append(library_id)

        return {
            "success_count": success_count,
            "failed_count": failed_count,
            "failed_libraries": failed_libraries
        }

    @classmethod
    def get_binding_stats(cls, db: Session, tenant_id: str = None) -> dict[str, Any]:
        """
        获取标签词库绑定统计

        Args:
            db: 数据库会话
            tenant_id: 租户ID

        Returns:
            统计信息字典
        """
        try:
            query = db.query(cls.model).filter(cls.model.status == "1")

            if tenant_id:
                query = query.filter(cls.model.tenant_id == tenant_id)

            total_bindings = query.count()
            enabled_bindings = query.filter(cls.model.enabled == True).count()

            # 统计每个标签的词库数量
            label_stats = {}
            bindings = query.all()

            for binding in bindings:
                label_id = binding.label_id
                if label_id not in label_stats:
                    label_stats[label_id] = {"total": 0, "enabled": 0}
                label_stats[label_id]["total"] += 1
                if binding.enabled:
                    label_stats[label_id]["enabled"] += 1

            return {
                "total_bindings": total_bindings,
                "enabled_bindings": enabled_bindings,
                "disabled_bindings": total_bindings - enabled_bindings,
                "label_count": len(label_stats),
                "label_stats": label_stats
            }

        except Exception as e:
            logging.error(f"获取绑定统计失败: {e}")
            return {}

    @classmethod
    def set_binding_priority(cls, db: Session, binding_id: str,
                           priority: int) -> bool:
        """
        设置绑定关系优先级

        Args:
            db: 数据库会话
            binding_id: 绑定关系ID
            priority: 优先级

        Returns:
            设置成功返回True，失败返回False
        """
        try:
            return cls.update_by_id(db, binding_id, {"priority": priority})
        except Exception as e:
            logging.error(f"设置绑定优先级失败: {e}")
            return False

    @classmethod
    def enable_binding(cls, db: Session, binding_id: str) -> bool:
        """
        启用绑定关系

        Args:
            db: 数据库会话
            binding_id: 绑定关系ID

        Returns:
            启用成功返回True，失败返回False
        """
        try:
            return cls.update_by_id(db, binding_id, {"enabled": True})
        except Exception as e:
            logging.error(f"启用绑定关系失败: {e}")
            return False

    @classmethod
    def disable_binding(cls, db: Session, binding_id: str) -> bool:
        """
        禁用绑定关系

        Args:
            db: 数据库会话
            binding_id: 绑定关系ID

        Returns:
            禁用成功返回True，失败返回False
        """
        try:
            return cls.update_by_id(db, binding_id, {"enabled": False})
        except Exception as e:
            logging.error(f"禁用绑定关系失败: {e}")
            return False

    @classmethod
    def get_library_usage_by_dimensions(cls, db: Session, library_id: str,
                                      tenant_id: str = None) -> dict[str, Any]:
        """
        获取词库在各个维度下的使用情况

        Args:
            db: 数据库会话
            library_id: 词库ID
            tenant_id: 租户ID

        Returns:
            维度使用统计
        """
        try:
            # 获取使用此词库的标签
            labels = cls.get_labels_by_library(db, library_id, enabled_only=False)

            # 统计各维度使用情况
            dimension_stats = {}

            for label_data in labels:
                label = db.query(GuardLabel).filter(
                    GuardLabel.id == label_data["id"]
                ).first()

                if label and label.dimension_id:
                    dimension_id = label.dimension_id
                    if dimension_id not in dimension_stats:
                        dimension_stats[dimension_id] = {
                            "total_labels": 0,
                            "enabled_labels": 0,
                            "labels": []
                        }

                    dimension_stats[dimension_id]["total_labels"] += 1
                    if label_data["binding"]["enabled"]:
                        dimension_stats[dimension_id]["enabled_labels"] += 1

                    dimension_stats[dimension_id]["labels"].append({
                        "label_id": label.id,
                        "label_name": label.name,
                        "label_code": label.code,
                        "enabled": label_data["binding"]["enabled"],
                        "priority": label_data["binding"]["priority"]
                    })

            return {
                "library_id": library_id,
                "total_dimensions": len(dimension_stats),
                "dimension_stats": dimension_stats
            }

        except Exception as e:
            logging.error(f"获取词库维度使用统计失败: {e}")
            return {}

    @classmethod
    def sync_library_to_all_labels_in_dimension(cls, db: Session, library_id: str,
                                               dimension_id: str, tenant_id: str = None,
                                               created_by: str = None) -> dict[str, Any]:
        """
        将词库同步到指定维度的所有标签

        Args:
            db: 数据库会话
            library_id: 词库ID
            dimension_id: 维度ID
            tenant_id: 租户ID
            created_by: 创建者ID

        Returns:
            同步结果统计
        """
        try:
            # 获取维度下的所有标签
            labels = db.query(GuardLabel).filter(
                and_(
                    GuardLabel.dimension_id == dimension_id,
                    GuardLabel.status == "1"
                )
            ).all()

            success_count = 0
            failed_count = 0
            skipped_count = 0

            for label in labels:
                # 检查是否已经绑定
                existing = cls.get_binding(db, label.id, library_id)
                if existing:
                    skipped_count += 1
                    continue

                # 创建绑定
                binding_id = cls.bind_library_to_label(
                    db, label.id, library_id,
                    tenant_id=tenant_id, created_by=created_by
                )

                if binding_id:
                    success_count += 1
                else:
                    failed_count += 1

            return {
                "success_count": success_count,
                "failed_count": failed_count,
                "skipped_count": skipped_count,
                "total_labels": len(labels)
            }

        except Exception as e:
            logging.error(f"同步词库到维度标签失败: {e}")
            return {"error": str(e)}
