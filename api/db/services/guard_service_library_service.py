# coding=utf-8
"""
@project: multirag
@Author：龙
@file： guard_service_library_service.py
@date：2025/01/11 18:30
@desc: AI安全护栏服务词库关系管理服务
"""
import logging
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from datetime import datetime

from api.db.db_models import GuardServiceLibrary, GuardService, GuardLibrary
from api.db.services.common_service import CommonService
from api.utils import get_uuid


class GuardServiceLibraryService(CommonService):
    """AI安全护栏服务词库关系管理服务"""
    model = GuardServiceLibrary

    @classmethod
    def bind_library_to_service(cls, db: Session, service_id: str, library_id: str,
                               priority: int = 0, enabled: bool = True, 
                               tenant_id: str = None, created_by: str = None,
                               library_type: str = None) -> Optional[str]:
        """
        绑定词库到服务
        
        Args:
            db: 数据库会话
            service_id: 服务ID
            library_id: 词库ID
            priority: 优先级
            enabled: 是否启用
            tenant_id: 租户ID
            created_by: 创建者ID
            library_type: 词库在此服务中的类型
            
        Returns:
            绑定成功返回关系ID，失败返回None
        """
        try:
            # 检查是否已经绑定
            existing = cls.get_binding(db, service_id, library_id)
            if existing:
                logging.warning(f"服务词库关系已存在: {service_id} -> {library_id}")
                return None
            
            binding_data = {
                "id": get_uuid(),
                "service_id": service_id,
                "library_id": library_id,
                "priority": priority,
                "enabled": enabled,
                "library_type": library_type,
                "tenant_id": tenant_id,
                "created_by": created_by,
                "status": "1"
            }
            
            binding = cls.save(db, **binding_data)
            return binding.id
            
        except Exception as e:
            logging.error(f"绑定词库到服务失败: {e}")
            return None

    @classmethod
    def get_binding(cls, db: Session, service_id: str, library_id: str) -> Optional[GuardServiceLibrary]:
        """
        获取服务词库绑定关系
        
        Args:
            db: 数据库会话
            service_id: 服务ID
            library_id: 词库ID
            
        Returns:
            绑定关系对象或None
        """
        try:
            return db.query(cls.model).filter(
                and_(
                    cls.model.service_id == service_id,
                    cls.model.library_id == library_id,
                    cls.model.status == "1"
                )
            ).first()
        except Exception as e:
            logging.error(f"获取服务词库绑定关系失败: {e}")
            return None

    @classmethod
    def get_libraries_by_service(cls, db: Session, service_id: str, 
                               enabled_only: bool = True) -> List[Dict[str, Any]]:
        """
        获取服务绑定的词库列表
        
        Args:
            db: 数据库会话
            service_id: 服务ID
            enabled_only: 是否只返回启用的
            
        Returns:
            词库列表（包含绑定信息）
        """
        try:
            logging.info(f"查询服务词库绑定，service_id: {service_id}, enabled_only: {enabled_only}")
            
            # 执行JOIN查询
            query = db.query(cls.model, GuardLibrary).join(
                GuardLibrary, cls.model.library_id == GuardLibrary.id
            ).filter(
                and_(
                    cls.model.service_id == service_id,
                    cls.model.status == "1"  # 只过滤绑定表status，词库表已改为硬删除
                    # 移除 GuardLibrary.status == "1" 过滤条件
                )
            )
            
            logging.info(f"基础查询条件: service_id={service_id}, binding.status='1' (词库表已改为硬删除，不过滤status)")
            
            if enabled_only:
                query = query.filter(cls.model.enabled == True)
                logging.info("添加了 enabled=True 过滤条件")
            
            results = query.order_by(cls.model.priority.desc()).all()
            logging.info(f"查询到 {len(results)} 条绑定记录")
            
            libraries = []
            for binding, library in results:
                logging.info(f"处理绑定记录: binding_id={binding.id}, library_id={library.id}, library_name={library.name}")
                logging.info(f"绑定信息: enabled={binding.enabled}, binding_library_type={binding.library_type}")
                logging.info(f"词库信息: library_type={library.library_type}, status={library.status}")
                
                library_dict = library.to_dict()
                library_dict["binding"] = {
                    "id": binding.id,
                    "priority": binding.priority,
                    "enabled": binding.enabled,
                    "library_type": binding.library_type,  # 绑定表中的类型
                    "create_time": binding.create_time.isoformat() if hasattr(binding.create_time, 'isoformat') and binding.create_time else str(binding.create_time) if binding.create_time else None
                }
                # 优先使用绑定表中的library_type，如果没有则使用词库表中的
                effective_library_type = binding.library_type or library.library_type
                library_dict["effective_library_type"] = effective_library_type
                logging.info(f"最终有效类型: {effective_library_type}")
                libraries.append(library_dict)
            
            logging.info(f"返回 {len(libraries)} 个词库")
            return libraries
            
        except Exception as e:
            logging.error(f"获取服务词库列表失败: {e}")
            return []

    @classmethod
    def get_services_by_library(cls, db: Session, library_id: str,
                              enabled_only: bool = True) -> List[Dict[str, Any]]:
        """
        获取使用此词库的服务列表
        
        Args:
            db: 数据库会话
            library_id: 词库ID
            enabled_only: 是否只返回启用的
            
        Returns:
            服务列表（包含绑定信息）
        """
        try:
            query = db.query(cls.model, GuardService).join(
                GuardService, cls.model.service_id == GuardService.id
            ).filter(
                and_(
                    cls.model.library_id == library_id,
                    cls.model.status == "1",
                    GuardService.status == "1"
                )
            )
            
            if enabled_only:
                query = query.filter(cls.model.enabled == True)
            
            results = query.order_by(cls.model.priority.desc()).all()
            
            services = []
            for binding, service in results:
                service_dict = service.to_dict()
                service_dict["binding"] = {
                    "id": binding.id,
                    "priority": binding.priority,
                    "enabled": binding.enabled,
                    "create_time": binding.create_time.isoformat() if binding.create_time else None
                }
                services.append(service_dict)
            
            return services
            
        except Exception as e:
            logging.error(f"获取词库服务列表失败: {e}")
            return []

    @classmethod
    def update_binding(cls, db: Session, binding_id: str, 
                      update_data: Dict[str, Any]) -> int:
        """
        更新服务词库绑定关系
        
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
            logging.error(f"更新服务词库绑定关系失败: {e}")
            return False

    @classmethod
    def unbind_library_from_service(cls, db: Session, service_id: str, 
                                  library_id: str) -> int:
        """
        解绑服务词库关系
        
        Args:
            db: 数据库会话
            service_id: 服务ID
            library_id: 词库ID
            
        Returns:
            解绑成功返回True，失败返回False
        """
        try:
            binding = cls.get_binding(db, service_id, library_id)
            if not binding:
                return False
            
            return cls.delete_by_id(db, binding.id) > 0
            
        except Exception as e:
            logging.error(f"解绑服务词库关系失败: {e}")
            return False

    @classmethod
    def batch_bind_libraries(cls, db: Session, service_id: str, 
                           library_ids: List[str], tenant_id: str = None,
                           created_by: str = None, library_type: str = None) -> Dict[str, Any]:
        """
        批量绑定词库到服务
        
        Args:
            db: 数据库会话
            service_id: 服务ID
            library_ids: 词库ID列表
            tenant_id: 租户ID
            created_by: 创建者ID
            library_type: 词库类型（可选，会更新词库的library_type字段）
            
        Returns:
            绑定结果统计
        """
        success_count = 0
        failed_count = 0
        failed_libraries = []
        updated_library_types = []
        
        for library_id in library_ids:
            # 绑定词库到服务，包含library_type
            binding_id = cls.bind_library_to_service(
                db, service_id, library_id, 
                tenant_id=tenant_id, created_by=created_by,
                library_type=library_type
            )
            
            if binding_id:
                success_count += 1
                if library_type:
                    updated_library_types.append(library_id)
            else:
                failed_count += 1
                failed_libraries.append(library_id)
        
        result = {
            "success_count": success_count,
            "failed_count": failed_count,
            "failed_libraries": failed_libraries
        }
        
        # 如果有设置library_type，添加统计信息
        if library_type:
            result["set_library_types_count"] = len(updated_library_types)
            result["set_library_type_ids"] = updated_library_types
        
        return result

    @classmethod
    def batch_unbind_libraries(cls, db: Session, service_id: str, 
                             library_ids: List[str]) -> Dict[str, Any]:
        """
        批量解绑服务词库关系
        
        Args:
            db: 数据库会话
            service_id: 服务ID
            library_ids: 词库ID列表
            
        Returns:
            解绑结果统计
        """
        success_count = 0
        failed_count = 0
        failed_libraries = []
        
        for library_id in library_ids:
            if cls.unbind_library_from_service(db, service_id, library_id):
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
    def get_binding_stats(cls, db: Session, tenant_id: str = None) -> Dict[str, Any]:
        """
        获取服务词库绑定统计
        
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
            
            # 统计每个服务的词库数量
            service_stats = {}
            bindings = query.all()
            
            for binding in bindings:
                service_id = binding.service_id
                if service_id not in service_stats:
                    service_stats[service_id] = {"total": 0, "enabled": 0}
                service_stats[service_id]["total"] += 1
                if binding.enabled:
                    service_stats[service_id]["enabled"] += 1
            
            return {
                "total_bindings": total_bindings,
                "enabled_bindings": enabled_bindings,
                "disabled_bindings": total_bindings - enabled_bindings,
                "service_count": len(service_stats),
                "service_stats": service_stats
            }
            
        except Exception as e:
            logging.error(f"获取绑定统计失败: {e}")
            return {}

    @classmethod
    def set_binding_priority(cls, db: Session, binding_id: str, 
                           priority: int) -> int:
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
    def enable_binding(cls, db: Session, binding_id: str) -> int:
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
    def disable_binding(cls, db: Session, binding_id: str) -> int:
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