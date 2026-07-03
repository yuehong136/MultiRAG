"""
@project: multirag
@Author：龙
@file： guard_library_item_service.py
@date：2025/01/11 16:30
@desc: AI安全护栏词库项管理服务
"""
import hashlib
import logging
from datetime import datetime
from typing import Any

from sqlalchemy import and_, text
from sqlalchemy.orm import Session

from api.db.db_models import GuardLibraryItem
from api.db.services.common_service import CommonService
from api.db.services.guard_library_service import GuardLibraryService
from common.misc_utils import get_uuid


class GuardLibraryItemService(CommonService):
    """AI安全护栏词库项管理服务"""
    model = GuardLibraryItem

    @classmethod
    def create_item(cls, db: Session, library_id: str, content: str,
                   content_type: str = "text", item_metadata: dict[str, Any] = None,
                   tenant_id: str = None, **kwargs) -> str | None:
        """
        创建词库项

        Args:
            db: 数据库会话
            library_id: 词库ID
            content: 内容
            content_type: 内容类型
            item_metadata: 元数据
            tenant_id: 租户ID
            **kwargs: 其他参数

        Returns:
            创建成功返回项ID，失败返回None
        """
        try:
            # 生成内容哈希
            content_hash = hashlib.md5(content.encode('utf-8')).hexdigest()

            # 检查是否已存在
            existing = cls.get_item_by_hash(db, library_id, content_hash)
            if existing:
                logging.warning(f"词库项内容已存在，库ID: {library_id}")
                return None

            item_data = {
                "id": get_uuid(),
                "library_id": library_id,
                "content": content,
                "content_hash": content_hash,
                "content_type": content_type,
                "item_metadata": item_metadata or {},
                "tenant_id": tenant_id,
                "hit_count": 0,
                "sort_order": kwargs.get("sort_order", 0),
                "status": kwargs.get("status", "1")
            }

            item = cls.save(db, **item_data)

            # 更新词库项数量
            GuardLibraryService.increment_item_count(db, library_id, 1)

            return item.id

        except Exception as e:
            logging.error(f"创建词库项失败: {e}")
            return None

    @classmethod
    def get_item_by_hash(cls, db: Session, library_id: str,
                        content_hash: str) -> GuardLibraryItem | None:
        """
        根据内容哈希获取词库项

        Args:
            db: 数据库会话
            library_id: 词库ID
            content_hash: 内容哈希

        Returns:
            词库项对象或None
        """
        try:
            # 移除status过滤，避免重复添加已禁用的词库项
            # 用户应该通过更新status来启用已禁用的词，而不是添加新的
            return db.query(cls.model).filter(
                and_(
                    cls.model.library_id == library_id,
                    cls.model.content_hash == content_hash
                )
            ).first()
        except Exception as e:
            logging.error(f"获取词库项失败: {e}")
            return None

    @classmethod
    def get_items_by_library(cls, db: Session, library_id: str,
                           page: int = 1, page_size: int = 50) -> dict[str, Any]:
        """
        获取词库的项列表（分页）

        Args:
            db: 数据库会话
            library_id: 词库ID
            page: 页码
            page_size: 每页数量

        Returns:
            包含项列表和分页信息的字典
        """
        try:
            # 移除status过滤，让前端能展示所有词库项（包括禁用的）
            query = db.query(cls.model).filter(
                cls.model.library_id == library_id
            ).order_by(cls.model.sort_order.asc(), cls.model.create_time.desc())

            total = query.count()
            items = query.offset((page - 1) * page_size).limit(page_size).all()

            return {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size
            }
        except Exception as e:
            logging.error(f"获取词库项列表失败: {e}")
            return {"items": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0}

    @classmethod
    def get_all_items_by_library(cls, db: Session, library_id: str) -> list[GuardLibraryItem]:
        """
        获取词库的所有项

        Args:
            db: 数据库会话
            library_id: 词库ID

        Returns:
            词库项列表
        """
        try:
            # 移除status过滤，让前端能导出所有词库项（包括禁用的）
            return db.query(cls.model).filter(
                cls.model.library_id == library_id
            ).order_by(cls.model.sort_order.asc()).all()
        except Exception as e:
            logging.error(f"获取词库所有项失败: {e}")
            return []

    @classmethod
    def get_items_by_ids(cls, db: Session, item_ids: list[str],
                        tenant_id: str = None) -> list[GuardLibraryItem]:
        """
        根据词库项ID列表批量获取词库项

        Args:
            db: 数据库会话
            item_ids: 词库项ID列表
            tenant_id: 租户ID（可选，用于安全验证）

        Returns:
            词库项列表
        """
        try:
            # 移除status过滤，让前端能批量获取所有词库项（包括禁用的）
            query = db.query(cls.model).filter(
                cls.model.id.in_(item_ids)
            )

            # 如果提供了tenant_id，添加租户过滤
            if tenant_id:
                query = query.filter(cls.model.tenant_id == tenant_id)

            return query.order_by(cls.model.sort_order.asc()).all()
        except Exception as e:
            logging.error(f"批量获取词库项失败: {e}")
            return []

    @classmethod
    def search_items(cls, db: Session, library_id: str, keyword: str,
                    page: int = 1, page_size: int = 50) -> dict[str, Any]:
        """
        搜索词库项

        Args:
            db: 数据库会话
            library_id: 词库ID
            keyword: 搜索关键词
            page: 页码
            page_size: 每页数量

        Returns:
            包含搜索结果和分页信息的字典
        """
        try:
            # 移除status过滤，让前端能搜索所有词库项（包括禁用的）
            query = db.query(cls.model).filter(
                and_(
                    cls.model.library_id == library_id,
                    cls.model.content.contains(keyword)
                )
            ).order_by(cls.model.create_time.desc())

            total = query.count()
            items = query.offset((page - 1) * page_size).limit(page_size).all()

            return {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "total_pages": (total + page_size - 1) // page_size,
                "keyword": keyword
            }
        except Exception as e:
            logging.error(f"搜索词库项失败: {e}")
            return {"items": [], "total": 0, "page": page, "page_size": page_size, "total_pages": 0}

    @classmethod
    def batch_create_items(cls, db: Session, library_id: str, contents: list[str],
                          content_type: str = "text", tenant_id: str = None,
                          item_metadata: dict[str, Any] = None) -> dict[str, Any]:
        """
        批量创建词库项

        Args:
            db: 数据库会话
            library_id: 词库ID
            contents: 内容列表
            content_type: 内容类型
            tenant_id: 租户ID
            item_metadata: 元数据

        Returns:
            创建结果统计
        """
        success_count = 0
        failed_count = 0
        failed_contents = []

        for content in contents:
            content = content.strip()
            if not content:
                continue

            item_id = cls.create_item(
                db=db,
                library_id=library_id,
                content=content,
                content_type=content_type,
                item_metadata=item_metadata,
                tenant_id=tenant_id
            )

            if item_id:
                success_count += 1
            else:
                failed_count += 1
                failed_contents.append(content)

        return {
            "success_count": success_count,
            "failed_count": failed_count,
            "failed_contents": failed_contents
        }



    @classmethod
    def delete_items(cls, db: Session, item_ids: list[str], tenant_id: str = None) -> dict[str, Any]:
        """
        删除词库项（硬删除，支持批量）

        Args:
            db: 数据库会话
            item_ids: 项ID列表
            tenant_id: 租户ID（用于安全验证）

        Returns:
            删除结果统计
        """
        try:
            success_count = 0
            failed_count = 0
            library_item_counts = {}  # 记录每个词库需要减少的项数

            for item_id in item_ids:
                item = cls.get_by_id(db, item_id)
                if not item:
                    failed_count += 1
                    continue

                # 租户验证
                if tenant_id and item.tenant_id != tenant_id:
                    failed_count += 1
                    continue

                # 记录词库项数量变化
                if item.library_id not in library_item_counts:
                    library_item_counts[item.library_id] = 0
                library_item_counts[item.library_id] += 1

                # 硬删除
                db.delete(item)
                success_count += 1

            # 提交删除操作
            db.commit()

            # 更新词库项数量
            for library_id, count in library_item_counts.items():
                GuardLibraryService.increment_item_count(db, library_id, -count)

            return {
                "success_count": success_count,
                "failed_count": failed_count,
                "total": len(item_ids)
            }
        except Exception as e:
            db.rollback()
            logging.error(f"批量删除词库项失败: {e}")
            return {
                "success_count": 0,
                "failed_count": len(item_ids),
                "total": len(item_ids)
            }

    @classmethod
    def update_item_by_hash(cls, db: Session, library_id: str, content_hash: str,
                           update_data: dict[str, Any]) -> int:
        """
        根据词库ID和内容哈希更新词库项

        Args:
            db: 数据库会话
            library_id: 词库ID
            content_hash: 内容哈希
            update_data: 更新数据

        Returns:
            更新的行数，失败返回0
        """
        try:
            # 获取词库项
            item = cls.get_item_by_hash(db, library_id, content_hash)
            if not item:
                logging.warning(f"词库项不存在: library_id={library_id}, content_hash={content_hash}")
                return 0

            # 如果更新了内容，需要重新计算哈希
            if 'content' in update_data:
                new_content_hash = hashlib.md5(update_data['content'].encode('utf-8')).hexdigest()
                update_data['content_hash'] = new_content_hash

            return cls.update_by_id(db, item.id, update_data)
        except Exception as e:
            logging.error(f"根据哈希更新词库项失败: {e}")
            return 0

    @classmethod
    def delete_item_by_hash(cls, db: Session, library_id: str, content_hash: str) -> bool:
        """
        根据词库ID和内容哈希删除词库项（硬删除）

        Args:
            db: 数据库会话
            library_id: 词库ID
            content_hash: 内容哈希

        Returns:
            删除成功返回True，失败返回False
        """
        try:
            # 获取词库项
            item = cls.get_item_by_hash(db, library_id, content_hash)
            if not item:
                logging.warning(f"词库项不存在: library_id={library_id}, content_hash={content_hash}")
                return False

            # 硬删除
            db.delete(item)
            db.commit()

            # 更新词库项数量
            GuardLibraryService.increment_item_count(db, library_id, -1)

            return True
        except Exception as e:
            db.rollback()
            logging.error(f"根据哈希删除词库项失败: {e}")
            return False

    @classmethod
    def update_items_status(cls, db: Session, item_ids: list[str], status: str,
                           tenant_id: str = None) -> dict[str, Any]:
        """
        批量更新词库项状态（启用/禁用）

        Args:
            db: 数据库会话
            item_ids: 项ID列表
            status: 新状态值（"1":启用, "0":禁用）
            tenant_id: 租户ID（用于安全验证）

        Returns:
            更新结果统计
        """
        try:
            success_count = 0
            failed_count = 0

            for item_id in item_ids:
                item = cls.get_by_id(db, item_id)
                if not item:
                    failed_count += 1
                    continue

                # 租户验证
                if tenant_id and item.tenant_id != tenant_id:
                    failed_count += 1
                    continue

                # 更新状态
                if cls.update_by_id(db, item_id, {"status": status}):
                    success_count += 1
                else:
                    failed_count += 1

            return {
                "success_count": success_count,
                "failed_count": failed_count,
                "total": len(item_ids)
            }
        except Exception as e:
            logging.error(f"批量更新词库项状态失败: {e}")
            return {
                "success_count": 0,
                "failed_count": len(item_ids),
                "total": len(item_ids)
            }

    @classmethod
    def update_item_by_id(cls, db: Session, item_id: str, update_data: dict[str, Any],
                         tenant_id: str = None) -> int:
        """
        根据词库项ID更新词库项

        Args:
            db: 数据库会话
            item_id: 词库项ID
            update_data: 更新数据
            tenant_id: 租户ID（用于安全验证）

        Returns:
            更新的行数，失败返回0
        """
        try:
            # 获取并验证词库项
            item = cls.get_by_id(db, item_id)
            if not item:
                logging.warning(f"词库项不存在: item_id={item_id}")
                return 0

            # 租户验证
            if tenant_id and item.tenant_id != tenant_id:
                logging.warning(f"无权限访问词库项: item_id={item_id}")
                return 0

            # 如果更新了内容，需要重新计算哈希
            if 'content' in update_data:
                new_content_hash = hashlib.md5(update_data['content'].encode('utf-8')).hexdigest()
                update_data['content_hash'] = new_content_hash

            return cls.update_by_id(db, item_id, update_data)
        except Exception as e:
            logging.error(f"根据ID更新词库项失败: {e}")
            return 0



    @classmethod
    def increment_hit_count(cls, db: Session, item_id: str,
                           count: int = 1) -> int:
        """
        增加词库项命中次数

        Args:
            db: 数据库会话
            item_id: 项ID
            count: 增加数量

        Returns:
            更新的行数，失败返回0
        """
        try:
            now_timestamp = cls.current_timestamp()
            now_datetime = cls.current_datetime()
            result = db.execute(
                text("""
                    UPDATE usr_ai.t_guard_library_items
                    SET hit_count = hit_count + :count,
                        last_hit_time = :last_hit_time,
                        update_time = :update_time,
                        update_date = :update_date
                    WHERE id = :item_id
                    RETURNING library_id
                """),
                {
                    "count": count,
                    "last_hit_time": datetime.utcnow(),
                    "update_time": now_timestamp,
                    "update_date": now_datetime,
                    "item_id": item_id
                }
            )
            row = result.fetchone()
            if row is None:
                db.rollback()
                logging.warning(f"命中统计更新失败，未找到词库项: {item_id}")
                return 0

            library_id = row[0]
            db.commit()

            # 同时更新词库的命中次数
            GuardLibraryService.increment_hit_count(db, library_id, count)
            return 1
        except Exception as e:
            db.rollback()
            logging.error(f"更新项命中数失败: {e}")
            return 0

    @classmethod
    def get_item_stats(cls, db: Session, library_id: str) -> dict[str, Any]:
        """
        获取词库项统计信息

        Args:
            db: 数据库会话
            library_id: 词库ID

        Returns:
            统计信息字典
        """
        try:
            items = cls.get_all_items_by_library(db, library_id)

            total_hits = sum(item.hit_count for item in items)
            content_types = {}

            for item in items:
                content_type = item.content_type
                if content_type not in content_types:
                    content_types[content_type] = 0
                content_types[content_type] += 1

            return {
                "total_items": len(items),
                "total_hits": total_hits,
                "content_types": content_types,
                "average_hits": total_hits / len(items) if items else 0
            }
        except Exception as e:
            logging.error(f"获取项统计失败: {e}")
            return {}

    @classmethod
    def export_items(cls, db: Session, library_id: str,
                    format_type: str = "text") -> list[str]:
        """
        导出词库项

        Args:
            db: 数据库会话
            library_id: 词库ID
            format_type: 格式类型

        Returns:
            导出的内容列表
        """
        try:
            items = cls.get_all_items_by_library(db, library_id)

            if format_type == "text":
                return [item.content for item in items]
            elif format_type == "json":
                return [item.to_dict() for item in items]
            else:
                return [item.content for item in items]

        except Exception as e:
            logging.error(f"导出词库项失败: {e}")
            return []
