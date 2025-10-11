import json
import logging
from typing import Optional

from sqlalchemy.orm import Session
from sqlalchemy import asc

from api.db.db_models import AskDataHistory
from api.db.services.common_service import CommonService
from api.utils import get_uuid


class AskDataHistoryService(CommonService):
    model = AskDataHistory

    def __init__(self):
        super().__init__(AskDataHistory)

    @classmethod
    def insert_record(cls, db: Session, conversation_id: str, ask_id: str, data: str,
                      user_id: str = None, status: str = "1", round_id: str = None, user_question: str = "",
                      processed_semantic_layer: str = None, sql_info: str = None) -> AskDataHistory:
        """
        插入一条新的询问数据历史记录

        参数:
        - db: 数据库会话对象
        - conversation_id: 对话ID
        - ask_id: 询问ID
        - data: 数据内容
        - user_id: 用户ID（可选）
        - status: 状态，默认为"1"（有效）

        返回:
        - AskDataHistory: 插入的记录对象
        """
        try:
            record_data = {
                "id": get_uuid(),
                "conversation_id": conversation_id,
                "ask_id": ask_id,
                "user_id": user_id,
                "data": data,
                "status": status,
                "round_id": round_id,
                "user_question": user_question,
                "processed_semantic_layer": processed_semantic_layer,
                "sql_info": sql_info
            }

            # 使用CommonService的insert方法，它会自动处理时间字段
            return cls.insert(db, **record_data)

        except Exception as e:
            logging.error(f"插入AskDataHistory记录失败: {e}")
            raise e

    @classmethod
    def get_by_conversation_id(cls, db: Session, conversation_id: str,
                               user_id: str = None, status: str = "1") -> list[AskDataHistory]:
        """
        根据conversation_id和user_id获取记录列表，按插入时间升序排列

        参数:
        - db: 数据库会话对象
        - conversation_id: 对话ID
        - user_id: 用户ID（可选）
        - status: 记录状态，默认为"1"（有效）

        返回:
        - list[AskDataHistory]: 记录列表
        """
        try:
            query = db.query(cls.model).filter(
                cls.model.conversation_id == conversation_id,
                cls.model.status == status
            )

            # 如果提供了user_id，添加user_id过滤条件
            if user_id is not None:
                query = query.filter(cls.model.user_id == user_id)

            query = query.order_by(asc(cls.model.create_time))

            return query.all()  # type: ignore

        except Exception as e:
            logging.error(f"根据conversation_id查询AskDataHistory记录失败: {e}")
            raise e

    @classmethod
    def get_by_conversation_id_as_dict(cls, db: Session, conversation_id: str,
                                       user_id: str = None, status: str = "1") -> list[dict]:
        """
        根据conversation_id和user_id获取记录列表（字典格式），按插入时间升序排列

        参数:
        - db: 数据库会话对象
        - conversation_id: 对话ID
        - user_id: 用户ID（可选）
        - status: 记录状态，默认为"1"（有效）

        返回:
        - list[dict]: 记录字典列表，第一条是最早的，最后一条是最新的
        """
        try:
            records = cls.get_by_conversation_id(db, conversation_id, user_id, status)
            return [record.to_dict() for record in records]

        except Exception as e:
            logging.error(f"根据conversation_id查询AskDataHistory记录失败: {e}")
            raise e

    # 新增的插入方法
    @classmethod
    def add_history(cls, db: Session, conversation_id: str, ask_id: str, data: str, user_id: str, round_id: str = None,
                    user_origin_question: str = "", rewritten_question: Optional[str] = "",
                    processed_semantic_layer: str = "") -> AskDataHistory:
        """
        新增一条历史记录

        参数:
        - db: 数据库会话
        - conversation_id: 对话ID
        - ask_id: 询问ID
        - data: 记录内容
        - user_id: 用户ID

        返回:
        - AskDataHistory: 创建的记录对象
        """
        logging.info(f"Adding history for conversation_id: {conversation_id}, ask_id: {ask_id}")
        json_data = json.loads(data, )
        user_question = {
            "user_original_question": user_origin_question,
            "rewritten_question": rewritten_question,
        }
        sql_info = {
            "sql": json_data.get("row").get("data").get("sql"),
            "pagination_sql": json_data.get("row").get("data").get("pagination_sql"),
            "sql_result_sample": json_data.get("row").get("data").get("result").get("data")[0:3],
            "sql_components": json_data.get("row").get("data").get("sql_components"),
            "model_table_alias_mapping_list": json_data.get("row").get("data").get("model_table_alias_mapping_list"),
        }

        try:
            history_record = cls.insert_record(
                db=db,
                conversation_id=conversation_id,
                ask_id=ask_id,
                data=data,
                user_id=user_id,
                round_id=round_id,
                user_question=json.dumps(user_question, ensure_ascii=False),
                processed_semantic_layer=processed_semantic_layer,
                sql_info=json.dumps(sql_info, ensure_ascii=False)
            )
            logging.info(f"Successfully added history record with id: {history_record.id}")
            return history_record
        except Exception as e:
            logging.error(f"Failed to add history record: {e}")
            raise

    # 新增的查询方法
    @classmethod
    def get_history_by_conversation_id(cls, db: Session, conversation_id: str, user_id: str) -> list[dict]:
        """
        根据对话ID和用户ID获取历史记录

        参数:
        - db: 数据库会话
        - conversation_id: 对话ID
        - user_id: 用户ID

        返回:
        - list[dict]: 历史记录字典列表
        """
        logging.info(f"Fetching history for conversation_id: {conversation_id}, user_id: {user_id}")
        try:
            history_records = cls.get_by_conversation_id_as_dict(db, conversation_id, user_id)
            logging.info(
                f"Found {len(history_records)} records for conversation_id: {conversation_id}, user_id: {user_id}")
            return history_records
        except Exception as e:
            logging.error(f"Failed to fetch history for conversation_id {conversation_id}, user_id {user_id}: {e}")
            raise

    @classmethod
    def delete_by_conversation_id(cls, db: Session, conversation_id: str, user_id: str) -> int:
        """
        根据conversation_id和user_id软删除记录（将status设置为0）

        参数:
        - db: 数据库会话
        - conversation_id: 对话ID
        - user_id: 用户ID

        返回:
        - int: 被删除的记录数量
        """
        logging.info(f"Soft deleting records for conversation_id: {conversation_id}, user_id: {user_id}")
        try:
            updated_count = (
                db.query(cls.model)
                .filter(
                    cls.model.conversation_id == conversation_id,
                    cls.model.user_id == user_id,
                    cls.model.status == "1"
                )
                .update({"status": "0"}, synchronize_session=False)
            )
            db.commit()
            logging.info(
                f"Successfully soft deleted {updated_count} records for conversation_id: {conversation_id}, user_id: {user_id}")
            return updated_count
        except Exception as e:
            db.rollback()
            logging.error(
                f"Failed to soft delete records for conversation_id {conversation_id}, user_id {user_id}: {e}")
            raise

    @classmethod
    def delete_by_ask_id(cls, db: Session, ask_id: str, user_id: str) -> int:
        """
        根据ask_id和user_id软删除记录（将status设置为0）

        参数:
        - db: 数据库会话
        - ask_id: 询问ID
        - user_id: 用户ID

        返回:
        - int: 被删除的记录数量
        """
        logging.info(f"Soft deleting records for ask_id: {ask_id}, user_id: {user_id}")
        try:
            updated_count = (
                db.query(cls.model)
                .filter(
                    cls.model.ask_id == ask_id,
                    cls.model.user_id == user_id,
                    cls.model.status == "1"
                )
                .update({"status": "0"}, synchronize_session=False)
            )
            db.commit()
            logging.info(f"Successfully soft deleted {updated_count} records for ask_id: {ask_id}, user_id: {user_id}")
            return updated_count
        except Exception as e:
            db.rollback()
            logging.error(f"Failed to soft delete records for ask_id {ask_id}, user_id {user_id}: {e}")
            raise

    @classmethod
    def get_by_round_id(cls, db: Session, round_id: str, status: str = "1") -> list[dict]:
        """
        根据round_id获取历史记录并按时间升序排列，最早的记录在前。

        参数:
        - db: 数据库会话
        - round_id: 对话轮次ID
        - status: 记录状态，默认为"1"

        返回:
        - list[dict]: 包含user_question、round_id、processed_semantic_layer、sql_info的字典列表
        """
        logging.info(f"Fetching history for round_id: {round_id}")
        try:
            records = (
                db.query(cls.model)
                .filter(
                    cls.model.round_id == round_id,
                    cls.model.status == status
                )
                .order_by(asc(cls.model.create_time))
                .all()
            )

            result = []
            for record in records:
                user_original_question = None
                rewritten_question = None

                if record.user_question:
                    try:
                        question_payload = json.loads(record.user_question)
                        user_original_question = question_payload.get("user_original_question")
                        rewritten_question = question_payload.get("rewritten_question")
                    except (TypeError, ValueError) as parse_error:
                        logging.warning(
                            "Failed to parse user_question JSON for record %s: %s",
                            record.id,
                            parse_error
                        )

                result.append({
                    "user_original_question": user_original_question,
                    "rewritten_question": rewritten_question,
                    "round_id": record.round_id,
                    "processed_semantic_layer": record.processed_semantic_layer,
                    "sql_info": record.sql_info
                })

            return result
        except Exception as e:
            logging.error(f"Failed to fetch history for round_id {round_id}: {e}")
            raise