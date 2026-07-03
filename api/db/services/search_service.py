"""
@project: multirag
@Author：龙
@file： search_service.py
@date：2024/7/15 16:15
@desc: 搜索服务
"""

from sqlalchemy import and_, asc, desc, func, or_
from sqlalchemy.orm import Session

from api.db.db_models import Search, User
from api.db.services.common_service import CommonService
from common.constants import StatusEnum

# from common.time_utils import current_timestamp, datetime_format


class SearchService(CommonService):
    model = Search

    @classmethod
    def save(cls, db: Session, **kwargs):
        """创建搜索配置"""
        return cls.insert(db, **kwargs)

    @classmethod
    def accessible4deletion(cls, db: Session, search_id: str, user_id: str) -> bool:
        """检查用户是否有权限删除搜索配置"""
        search = (
            db.query(cls.model.id)
            .filter(
                and_(
                    cls.model.id == search_id,
                    cls.model.created_by == user_id,
                    cls.model.status == StatusEnum.VALID.value,
                )
            )
            .first()
        )
        return search is not None

    @classmethod
    def get_detail(cls, db: Session, search_id: str):
        """获取搜索配置详情"""
        query = (
            db.query(
                cls.model.id,
                cls.model.avatar,
                cls.model.tenant_id,
                cls.model.name,
                cls.model.description,
                cls.model.created_by,
                cls.model.search_config,
                cls.model.update_time,
                User.nickname,
                User.avatar.label("tenant_avatar"),
            )
            .join(User, and_(User.id == cls.model.tenant_id, User.status == StatusEnum.VALID.value))
            .filter(and_(cls.model.id == search_id, cls.model.status == StatusEnum.VALID.value))
        )

        result = query.first()
        if result:
            return {
                "id": result.id,
                "avatar": result.avatar,
                "tenant_id": result.tenant_id,
                "name": result.name,
                "description": result.description,
                "created_by": result.created_by,
                "search_config": result.search_config,
                "update_time": result.update_time,
                "nickname": result.nickname,
                "tenant_avatar": result.tenant_avatar,
            }
        return {}

    @classmethod
    def get_by_tenant_ids(
        cls, db: Session, joined_tenant_ids: list, user_id: str, page_number: int = None, items_per_page: int = None, orderby: str = "update_time", desc_order: bool = True, keywords: str = None
    ):
        """根据租户ID获取搜索配置列表"""
        query = (
            db.query(
                cls.model.id,
                cls.model.avatar,
                cls.model.tenant_id,
                cls.model.name,
                cls.model.description,
                cls.model.created_by,
                cls.model.status,
                cls.model.update_time,
                cls.model.create_time,
                User.nickname,
                User.avatar.label("tenant_avatar"),
            )
            .join(User, cls.model.tenant_id == User.id)
            .filter(and_(or_(cls.model.tenant_id.in_(joined_tenant_ids), cls.model.tenant_id == user_id), cls.model.status == StatusEnum.VALID.value))
        )

        # 关键词搜索
        if keywords:
            query = query.filter(func.lower(cls.model.name).contains(keywords.lower()))

        # 排序
        if not hasattr(cls.model, orderby):
            orderby = "update_time"

        order_column = getattr(cls.model, orderby)
        if desc_order:
            query = query.order_by(desc(order_column))
        else:
            query = query.order_by(asc(order_column))

        # 获取总数
        count = query.count()

        # 分页
        if page_number and items_per_page:
            offset = (page_number - 1) * items_per_page
            query = query.offset(offset).limit(items_per_page)

        # 执行查询并转换为字典
        results = query.all()
        search_list = []
        for result in results:
            search_dict = {
                "id": result.id,
                "avatar": result.avatar,
                "tenant_id": result.tenant_id,
                "name": result.name,
                "description": result.description,
                "created_by": result.created_by,
                "status": result.status,
                "update_time": result.update_time,
                "create_time": result.create_time,
                "nickname": result.nickname,
                "tenant_avatar": result.tenant_avatar,
            }
            search_list.append(search_dict)

        return search_list, count

    @classmethod
    def delete_by_tenant_id(cls, db: Session, tenant_id: str) -> int:
        """根据tenant_id删除所有相关的搜索配置记录"""
        try:
            result = db.query(cls.model).filter(cls.model.tenant_id == tenant_id).delete(synchronize_session=False)
            db.commit()
            return result
        except Exception as e:
            db.rollback()
            raise e

    @classmethod
    def get_by_user_id(cls, db: Session, user_id: str):
        """根据用户ID获取搜索配置列表"""
        query = db.query(cls.model).filter(and_(cls.model.created_by == user_id, cls.model.status == StatusEnum.VALID.value)).order_by(desc(cls.model.update_time))

        return query.all()

    @classmethod
    def update_search_config(cls, db: Session, search_id: str, search_config: dict):
        """更新搜索配置"""
        return cls.update_by_id(db, search_id, {"search_config": search_config})

    @classmethod
    def delete_by_id(cls, db: Session, search_id: str):
        """软删除搜索配置"""
        return cls.update_by_id(db, search_id, {"status": StatusEnum.INVALID.value})
