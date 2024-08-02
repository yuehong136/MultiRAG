# coding=utf-8
"""
@project: multirag
@Author：龙
@file： api_service.py
@date：2024/7/9 9:00
@desc:
"""
from datetime import datetime
from sqlalchemy import func
from sqlalchemy.orm import Session
from api.db.db_models import APIToken, API4Conversation, Dialog
from api.db.services.common_service import CommonService
from api.utils import current_timestamp, datetime_format


class APITokenService(CommonService):
    model = APIToken

    @classmethod
    def used(cls, db: Session, token: str):
        try:
            db.query(cls.model).filter(cls.model.token == token).update({
                cls.model.update_time: current_timestamp(),
                cls.model.update_date: datetime_format(datetime.now())
            })
            db.commit()
        except Exception as e:
            db.rollback()
            raise e


class API4ConversationService(CommonService):
    model = API4Conversation

    @classmethod
    def append_message(cls, db: Session, id: str, conversation: str):
        cls.update_by_id(db, id, {"conversation": conversation})
        try:
            db.query(cls.model).filter(cls.model.id == id).update({
                cls.model.round: cls.model.round + 1
            })
            db.commit()
        except Exception as e:
            db.rollback()
            raise e

    @classmethod
    def stats(cls, db: Session, tenant_id: str, from_date: datetime, to_date: datetime):
        """
        统计指定时间段内特定租户的对话数据。

        :param db: 数据库会话对象
        :param tenant_id: 租户ID
        :param from_date: 开始日期
        :param to_date: 结束日期
        :return: 包含每日统计信息的列表，每个元素是一个字典
        """
        try:
            # 构建查询语句，统计每天的对话数量（pv）、独立用户数量（uv）、代币总数（tokens）、总时长（duration）、平均轮次（round）、总点赞数（thumb_up）
            result = db.query(
                func.date_trunc('day', cls.model.create_date).label('dt'),
                func.count(cls.model.id).label('pv'),
                func.count(func.distinct(cls.model.user_id)).label('uv'),
                func.sum(cls.model.tokens).label('tokens'),
                func.sum(cls.model.duration).label('duration'),
                func.avg(cls.model.round).label('round'),
                func.sum(cls.model.thumb_up).label('thumb_up')
            ).join(Dialog, (cls.model.dialog_id == Dialog.id) & (Dialog.tenant_id == tenant_id)).filter(
                cls.model.create_date >= from_date,
                cls.model.create_date <= to_date
            ).group_by(func.date_trunc('day', cls.model.create_date)).all()

            # 将查询结果转换为字典列表格式，方便后续处理和使用
            return [dict(row) for row in result]
        except Exception as e:
            # 发生异常时，回滚数据库操作，并重新抛出异常，确保数据一致性
            db.rollback()
            raise e

