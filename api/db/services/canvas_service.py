# coding=utf-8
"""
@project: multirag
@Author：龙
@file： canvas_service.py
@date：2024/8/7 10:30
@desc:
"""

from sqlalchemy import asc, desc
from sqlalchemy.orm import Session

from api.db.db_models import CanvasTemplate, UserCanvas
from api.db.services.common_service import CommonService


class CanvasTemplateService(CommonService):
    model = CanvasTemplate

class UserCanvasService(CommonService):
    model = UserCanvas

    @classmethod
    def get_list(cls, db: Session, tenant_id,
                 page_number, items_per_page, orderby, desc_flag, id=None, title=None):
        # 构建基础查询
        query = db.query(cls.model)

        # 根据 ID 筛选
        if id:
            query = query.filter(cls.model.id == id)

        # 根据标题筛选
        if title:
            query = query.filter(cls.model.title == title)

        # 根据租户 ID 筛选
        query = query.filter(cls.model.user_id == tenant_id)

        # 动态排序
        if desc_flag:
            query = query.order_by(desc(getattr(cls.model, orderby)))
        else:
            query = query.order_by(asc(getattr(cls.model, orderby)))

        # 分页
        offset = (page_number - 1) * items_per_page
        query = query.offset(offset).limit(items_per_page)

        # 执行查询并返回结果
        return query.all()