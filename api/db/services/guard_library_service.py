# coding=utf-8
"""
@project: multirag
@Author：龙
@file： guard_library_service.py
@date：2025/01/11 16:20
@desc: AI安全护栏词库管理服务
"""
import logging
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from sqlalchemy import and_, func
from datetime import datetime, UTC

from api.db.db_models import GuardLibrary
from api.db.services.common_service import CommonService
from api.utils import get_uuid


class GuardLibraryService(CommonService):
    """AI安全护栏词库管理服务"""
    model = GuardLibrary

    @classmethod
    def create_library(cls, db: Session, library_type: str, name: str,
                      description: str = None, tenant_id: str = None,
                      created_by: str = None, **kwargs) -> Optional[str]:
        """
        创建护栏词库
        
        Args:
            db: 数据库会话
            library_type: 词库类型
            name: 词库名称
            description: 词库描述
            tenant_id: 租户ID
            created_by: 创建者ID
            **kwargs: 其他参数
            
        Returns:
            创建成功返回词库ID，失败返回None
        """
        try:
            library_data = {
                "id": get_uuid(),
                "library_type": library_type,
                "name": name,
                "description": description,
                "tenant_id": tenant_id,
                "created_by": created_by,
                "category": kwargs.get("category"),
                "tags": kwargs.get("tags", []),
                "config": kwargs.get("config", {}),
                "item_count": 0,
                "hit_count": 0,
                "version": 1,
                "status": kwargs.get("status", "1")
            }
            
            library = cls.save(db, **library_data)
            return library.id
            
        except Exception as e:
            logging.error(f"创建词库失败: {e}")
            return None

    @classmethod
    def get_library_by_name(cls, db: Session, name: str, library_type: str,
                           tenant_id: str) -> Optional[GuardLibrary]:
        """
        根据名称和类型获取词库
        
        Args:
            db: 数据库会话
            name: 词库名称
            library_type: 词库类型
            tenant_id: 租户ID
            
        Returns:
            词库对象或None
        """
        try:
            return db.query(cls.model).filter(
                and_(
                    cls.model.name == name,
                    cls.model.library_type == library_type,
                    cls.model.tenant_id == tenant_id,
                    cls.model.status == "1"
                )
            ).first()
        except Exception as e:
            logging.error(f"获取词库失败: {e}")
            return None

    @classmethod
    def get_libraries_by_type(cls, db: Session, library_type: str,
                             tenant_id: str) -> List[GuardLibrary]:
        """
        根据类型获取词库列表
        
        Args:
            db: 数据库会话
            library_type: 词库类型
            tenant_id: 租户ID
            
        Returns:
            词库列表
        """
        try:
            return db.query(cls.model).filter(
                and_(
                    cls.model.library_type == library_type,
                    cls.model.tenant_id == tenant_id,
                    cls.model.status == "1"
                )
            ).order_by(cls.model.create_time.desc()).all()
        except Exception as e:
            logging.error(f"获取词库列表失败: {e}")
            return []

    @classmethod
    def get_libraries_by_tenant(cls, db: Session, tenant_id: str,
                               library_type: str = None,
                               category: str = None) -> List[GuardLibrary]:
        """
        获取租户的词库列表
        
        Args:
            db: 数据库会话
            tenant_id: 租户ID
            library_type: 词库类型（可选）
            category: 词库分类（可选）
            
        Returns:
            词库列表
        """
        try:
            query = db.query(cls.model).filter(
                and_(
                    cls.model.tenant_id == tenant_id,
                    cls.model.status == "1"
                )
            )
            
            if library_type:
                query = query.filter(cls.model.library_type == library_type)
            
            if category:
                query = query.filter(cls.model.category == category)
                
            return query.order_by(cls.model.create_time.desc()).all()
        except Exception as e:
            logging.error(f"获取租户词库失败: {e}")
            return []

    @classmethod
    def get_libraries_by_ids(cls, db: Session, library_ids: List[str],
                            tenant_id: str) -> List[GuardLibrary]:
        """
        根据ID列表获取词库
        
        Args:
            db: 数据库会话
            library_ids: 词库ID列表
            tenant_id: 租户ID
            
        Returns:
            词库列表
        """
        try:
            return db.query(cls.model).filter(
                and_(
                    cls.model.id.in_(library_ids),
                    cls.model.tenant_id == tenant_id,
                    cls.model.status == "1"
                )
            ).all()
        except Exception as e:
            logging.error(f"批量获取词库失败: {e}")
            return []

    @classmethod
    def update_library(cls, db: Session, library_id: str, 
                      update_data: Dict[str, Any]) -> int:
        """
        更新词库信息
        
        Args:
            db: 数据库会话
            library_id: 词库ID
            update_data: 更新数据
            
        Returns:
            更新成功返回True，失败返回False
        """
        try:
            return cls.update_by_id(db, library_id, update_data)
        except Exception as e:
            logging.error(f"更新词库失败: {e}")
            return False

    @classmethod
    def delete_library(cls, db: Session, library_id: str) -> int:
        """
        删除词库（物理删除）
        
        Args:
            db: 数据库会话
            library_id: 词库ID
            
        Returns:
            删除成功返回True，失败返回False
        """
        try:
            return cls.delete_by_id(db, library_id) > 0
        except Exception as e:
            logging.error(f"删除词库失败: {e}")
            return False

    @classmethod
    def increment_item_count(cls, db: Session, library_id: str, 
                           count: int = 1) -> int:
        """
        增加词库项数量
        
        Args:
            db: 数据库会话
            library_id: 词库ID
            count: 增加数量
            
        Returns:
            更新成功返回True，失败返回False
        """
        try:
            library = cls.get_by_id(db, library_id)
            if not library:
                return False
                
            new_count = library.item_count + count
            return cls.update_by_id(db, library_id, {"item_count": new_count})
        except Exception as e:
            logging.error(f"更新词库项数量失败: {e}")
            return False

    @classmethod
    def increment_hit_count(cls, db: Session, library_id: str, 
                           count: int = 1) -> int:
        """
        增加词库命中次数
        
        Args:
            db: 数据库会话
            library_id: 词库ID
            count: 增加数量
            
        Returns:
            更新成功返回True，失败返回False
        """
        try:
            library = cls.get_by_id(db, library_id)
            if not library:
                return False
                
            new_count = library.hit_count + count
            update_data = {
                "hit_count": new_count,
                "last_hit_time": datetime.now(UTC)
            }
            return cls.update_by_id(db, library_id, update_data)
        except Exception as e:
            logging.error(f"更新词库命中数失败: {e}")
            return False

    @classmethod
    def get_library_stats(cls, db: Session, tenant_id: str) -> Dict[str, Any]:
        """
        获取词库统计信息
        
        Args:
            db: 数据库会话
            tenant_id: 租户ID
            
        Returns:
            统计信息字典
        """
        try:
            libraries = cls.get_libraries_by_tenant(db, tenant_id)
            
            # 按类型统计
            type_stats = {}
            total_items = 0
            total_hits = 0
            
            for library in libraries:
                lib_type = library.library_type
                if lib_type not in type_stats:
                    type_stats[lib_type] = {
                        "count": 0,
                        "total_items": 0,
                        "total_hits": 0
                    }
                
                type_stats[lib_type]["count"] += 1
                type_stats[lib_type]["total_items"] += library.item_count
                type_stats[lib_type]["total_hits"] += library.hit_count
                
                total_items += library.item_count
                total_hits += library.hit_count
            
            return {
                "total_libraries": len(libraries),
                "total_items": total_items,
                "total_hits": total_hits,
                "type_stats": type_stats,
                "library_types": list(type_stats.keys())
            }
        except Exception as e:
            logging.error(f"获取词库统计失败: {e}")
            return {}

    @classmethod
    def search_libraries(cls, db: Session, tenant_id: str,
                        keyword: str = None, library_type: str = None,
                        category: str = None, tags: List[str] = None) -> List[GuardLibrary]:
        """
        搜索词库
        
        Args:
            db: 数据库会话
            tenant_id: 租户ID
            keyword: 关键词
            library_type: 词库类型
            category: 词库分类
            tags: 标签列表
            
        Returns:
            匹配的词库列表
        """
        try:
            query = db.query(cls.model).filter(
                and_(
                    cls.model.tenant_id == tenant_id,
                    cls.model.status == "1"
                )
            )
            
            if keyword:
                query = query.filter(
                    cls.model.name.contains(keyword) | 
                    cls.model.description.contains(keyword)
                )
            
            if library_type:
                query = query.filter(cls.model.library_type == library_type)
            
            if category:
                query = query.filter(cls.model.category == category)
            
            if tags:
                # 搜索包含指定标签的词库
                for tag in tags:
                    query = query.filter(
                        func.jsonb_exists(cls.model.tags, tag)
                    )
                    
            return query.order_by(cls.model.create_time.desc()).all()
        except Exception as e:
            logging.error(f"搜索词库失败: {e}")
            return []

    @classmethod
    def init_default_libraries(cls, db: Session, tenant_id: str, 
                              created_by: str) -> List[str]:
        """
        初始化默认词库
        
        Args:
            db: 数据库会话
            tenant_id: 租户ID
            created_by: 创建者ID
            
        Returns:
            创建的词库ID列表
        """
        default_libraries = [
            {
                "library_type": "blacklist",
                "name": "政治敏感词库",
                "description": "包含政治相关敏感词汇",
                "category": "内容合规",
                "tags": ["政治", "敏感"],
                "config": {
                    "match_mode": "exact",
                    "case_sensitive": False
                }
            },
            {
                "library_type": "blacklist",
                "name": "色情内容词库",
                "description": "包含色情相关词汇",
                "category": "内容合规",
                "tags": ["色情", "成人"],
                "config": {
                    "match_mode": "partial",
                    "case_sensitive": False
                }
            },
            {
                "library_type": "blacklist",
                "name": "暴力词库",
                "description": "包含暴力、极端主义词汇",
                "category": "内容合规",
                "tags": ["暴力", "极端"],
                "config": {
                    "match_mode": "exact",
                    "case_sensitive": False
                }
            },
            {
                "library_type": "whitelist",
                "name": "通用白名单",
                "description": "常用的正常词汇白名单",
                "category": "基础配置",
                "tags": ["白名单", "基础"],
                "config": {
                    "match_mode": "exact",
                    "case_sensitive": False
                }
            },
            {
                "library_type": "reply",
                "name": "内容合规代答库",
                "description": "内容合规检测的标准回复",
                "category": "代答回复",
                "tags": ["代答", "合规"],
                "config": {
                    "support_variables": True,
                    "default_reply": "抱歉，您的内容涉及敏感信息，请修改后重试。"
                }
            },
            {
                "library_type": "reply",
                "name": "敏感信息代答库",
                "description": "敏感信息检测的标准回复",
                "category": "代答回复",
                "tags": ["代答", "隐私"],
                "config": {
                    "support_variables": True,
                    "default_reply": "检测到您的内容包含敏感信息，已进行处理。"
                }
            }
        ]
        
        created_ids = []
        for library_data in default_libraries:
            library_id = cls.create_library(
                db=db,
                tenant_id=tenant_id,
                created_by=created_by,
                **library_data
            )
            if library_id:
                created_ids.append(library_id)
                
        return created_ids