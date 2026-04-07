# coding=utf-8
"""
@project: multirag
@Author：龙
@file： tools_data_service.py
@date：2025/9/23 10:00
@desc: MCP工具数据服务
"""
from sqlalchemy import select
from sqlalchemy.orm import Session

from api.db.db_models import ToolsData
from api.db.services.common_service import CommonService


class ToolsDataService(CommonService):
    model = ToolsData

    def __init__(self):
        super().__init__(ToolsData)

    @classmethod
    def get_by_flow_id_user_id(cls, db: Session, flow_id: str, user_id: str | None) -> ToolsData | None:
        """根据 flow_id 与 user_id 获取唯一的工具数据记录。"""
        stmt = select(cls.model).where(
            cls.model.flow_id == flow_id,
            cls.model.user_id == user_id,
        )
        return db.execute(stmt).scalar_one_or_none()

    @classmethod
    def create_or_update(
        cls,
        db: Session,
        *,
        flow_id: str,
        user_id: str,
        meta_data: str | None = None,
        final_data: str | None = None,
    ) -> ToolsData:
        """创建或更新工具数据，存在则更新，不存在则创建。"""
        record = cls.get_by_flow_id_user_id(db, flow_id, user_id)
        if record:
            record.meta_data = meta_data
            record.final_data = final_data
        else:
            record = cls.model(
                flow_id=flow_id,
                user_id=user_id,
                meta_data=meta_data,
                final_data=final_data,
            )
            db.add(record)

        db.commit()
        db.refresh(record)
        return record

    @classmethod
    def delete_by_flow_id_user_id(cls, db: Session, flow_id: str, user_id: str) -> bool:
        """根据主键删除工具数据。返回是否删除成功。"""
        record = cls.get_by_flow_id_user_id(db, flow_id, user_id)
        if not record:
            return False

        db.delete(record)
        db.commit()
        return True
