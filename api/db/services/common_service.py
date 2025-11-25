# common_services.py
import uuid
import logging
from datetime import datetime, timezone
from typing import Any, Type, Generic, TypeVar

from sqlalchemy import Row, desc, asc, text, exc
from sqlalchemy.orm import Session
from fastapi import HTTPException
from sqlalchemy.exc import NoResultFound, IntegrityError
from sqlalchemy.orm.attributes import InstrumentedAttribute
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from api.db import db_models
from common.misc_utils import get_uuid
# from common.time_utils import current_timestamp, datetime_format

# 配置日志
logger = logging.getLogger(__name__)


def retry_db_operation(max_attempts=3, min_wait=1, max_wait=5):
    """
    数据库操作重试装饰器（使用 tenacity）

    自动重试以下数据库错误：
    - OperationalError: 连接丢失、服务器断开等
    - DisconnectionError: 连接断开
    - InterfaceError: 数据库接口错误
    - TimeoutError: 数据库操作超时

    Args:
        max_attempts: 最大重试次数（默认3次）
        min_wait: 最小等待时间（秒，默认1秒）
        max_wait: 最大等待时间（秒，默认5秒）

    Example:
        @retry_db_operation(max_attempts=5, max_wait=10)
        def critical_operation(db, data):
            # 关键业务操作
            pass
    """
    def decorator(func):
        @retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
            retry=retry_if_exception_type((
                exc.OperationalError,      # MySQL/PostgreSQL 操作错误（连接断开等）
                exc.DisconnectionError,    # 连接断开错误
                exc.InterfaceError,        # 数据库接口错误
                exc.TimeoutError,          # 超时错误
            )),
            before_sleep=lambda retry_state: logger.warning(
                f"[DB Retry] {func.__name__} 执行失败，"
                f"{retry_state.next_action.sleep:.1f}秒后重试 "
                f"(第 {retry_state.attempt_number}/{max_attempts} 次重试)"
            ),
            reraise=True,  # 重试失败后重新抛出异常
        )
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper
    return decorator


# Define a TypeVar bound to db_models.BaseModel for better type hinting
ModelType = TypeVar("ModelType", bound=db_models.BaseModel)

class CommonService(Generic[ModelType]):
    model: Type[ModelType]

    def __init__(self, model: Type[ModelType]):
        self.model = model

    @staticmethod
    def current_timestamp():
        return int(datetime.now(timezone.utc).timestamp() * 1000)

    @staticmethod
    def current_datetime():
        return datetime.now(timezone.utc)

    @classmethod
    def query(cls, db: Session, cols: list[str] | None = None, reverse: bool | None = None,
              order_by: str | InstrumentedAttribute | None = None, **kwargs) -> list[ModelType] | list[Row]:
        """
       根据条件查询数据库中的记录。

       :param db: 数据库会话对象。
       :param cols: 需要查询的列，可选。
       :param reverse: 是否逆序排序，可选。
       :param order_by: 按哪个字段排序，可选。
       :param kwargs: 其他过滤条件。
       :return: 查询结果列表。
       """
        # 根据过滤条件构造查询表达式
        query = db.query(cls.model).filter_by(**kwargs)

        if order_by:
            if not isinstance(order_by, str):
                order_by = str(order_by)  # 确保 order_by 是字符串类型
            if not hasattr(cls.model, order_by):
                raise ValueError(f"'{order_by}' is not a valid attribute of '{cls.model.__name__}'")
            order_column = getattr(cls.model, order_by)
            if reverse:
                query = query.order_by(desc(order_column))
            else:
                query = query.order_by(asc(order_column))
        else:
            order_column = getattr(cls.model, "create_time", None)
            if order_column is not None:
                if reverse:
                    query = query.order_by(desc(order_column))
                else:
                    query = query.order_by(asc(order_column))

        if cols:
            query = query.with_entities(*[getattr(cls.model, col) for col in cols])
        return query.all()

    @classmethod
    def get_all(cls, db: Session, cols: list[str] | None = None, reverse: bool | None = None, order_by: str | None = None) ->  list[ModelType] | list[Row]:
        query = db.query(cls.model)
        if cols:
            query = query.with_entities(*[getattr(cls.model, col) for col in cols])
        if reverse is not None:
            if not order_by or not hasattr(cls.model, order_by):
                order_by = "create_time"
            order_column = getattr(cls.model, order_by)
            if reverse:
                query = query.order_by(order_column.desc())
            else:
                query = query.order_by(order_column.asc())
        return query.all()

    @classmethod
    def get(cls, db: Session, **kwargs) -> ModelType:
        try:
            return db.query(cls.model).filter_by(**kwargs).one()
        except NoResultFound:
            raise HTTPException(status_code=404, detail="Item not found")

    @classmethod
    def get_or_none(cls, db: Session, **kwargs) -> ModelType | None:
        return db.query(cls.model).filter_by(**kwargs).one_or_none()

    @classmethod
    @retry_db_operation(max_attempts=3)  # 保存操作重试3次
    def save(cls, db: Session, **kwargs) -> ModelType:
        db_item = cls.model(**kwargs)
        db.add(db_item)
        try:
            db.commit()
        except IntegrityError as e:
            db.rollback()
            raise e
        db.refresh(db_item)
        return db_item

    @classmethod
    @retry_db_operation(max_attempts=3)  # 插入操作重试3次
    def insert(cls, db: Session, **kwargs) -> ModelType:
        if "id" not in kwargs:
            kwargs["id"] = get_uuid()
        now = cls.current_timestamp()
        kwargs["create_time"] = now
        kwargs["create_date"] = cls.current_datetime()
        kwargs["update_time"] = now
        kwargs["update_date"] = cls.current_datetime()
        db_item = cls.model(**kwargs)
        db.add(db_item)
        db.commit()
        db.refresh(db_item)
        return db_item

    # @classmethod
    # def insert_many(cls, db: Session, data_list: List[Dict[str, Any]], batch_size: int = 100):
    #     now = cls.current_timestamp()
    #     now_datetime = cls.current_datetime()
    #     for data in data_list:
    #         data["create_time"] = now
    #         data["create_date"] = now_datetime
    #     db.bulk_insert_mappings(cls.model, data_list)
    #     db.commit()

    @classmethod
    @retry_db_operation(max_attempts=5, max_wait=10)  # 批量插入重试5次，关键操作
    def insert_many(cls, db: Session, data_list: list[dict[str, Any]], batch_size: int = 100):
        now = cls.current_timestamp()
        now_datetime = cls.current_datetime()
        for data in data_list:
            data["create_time"] = now
            data["create_date"] = now_datetime

        # Perform batch insertion in chunks
        for i in range(0, len(data_list), batch_size):
            db.bulk_insert_mappings(cls.model, data_list[i:i + batch_size])
            db.commit()

    @classmethod
    def update_many_by_id(cls, db: Session, data_list: list[dict[str, Any]]):
        now = cls.current_timestamp()
        now_datetime = cls.current_datetime()
        for data in data_list:
            data["update_time"] = now
            data["update_date"] = now_datetime
            db.query(cls.model).filter_by(id=data["id"]).update(data)
        db.commit()

    @classmethod
    @retry_db_operation(max_attempts=3)  # 更新操作重试3次
    def update_by_id(cls, db: Session, pid: str, data: dict[str, Any]) -> int:
        """
        通过 ID 更新记录，带自动重试机制

        Args:
            db: 数据库会话
            pid: 记录 ID
            data: 要更新的数据字典

        Returns:
            更新的记录数量
        """
        now = cls.current_timestamp()
        now_datetime = cls.current_datetime()
        data["update_time"] = now
        data["update_date"] = now_datetime
        num = db.query(cls.model).filter(cls.model.id == pid).update(data)
        db.commit()
        return num

    @classmethod
    def get_by_id(cls, db: Session, pid: Any) -> ModelType | None:
        """
        通过主键或 ID 字段查询单个记录。
        找不到时返回 None；其他异常（如数据库连接错误）会正常抛出。
        """
        # one_or_none() 在没有结果时返回 None，找到多条时会抛出 MultipleResultsFound
        return (
            db.query(cls.model)
            .filter(cls.model.id == pid)
            .one_or_none()
        )

    @classmethod
    def get_by_ids(cls, db: Session, pids: list[Any], cols: list[str] | None = None) -> list[ModelType]:
        query = db.query(cls.model).filter(cls.model.id.in_(pids))
        if cols:
            query = query.with_entities(*[getattr(cls.model, col) for col in cols])
        return query.all()

    @classmethod
    def delete_by_id(cls, db: Session, pid: Any) -> int:
        try:
            deleted_count = db.query(cls.model).filter(cls.model.id == pid).delete(synchronize_session=False)
            db.commit()  # 确保提交事务
            return deleted_count
        except Exception as e:
            db.rollback()  # 回滚事务
            print(f"Error occurred: {e}")
            return 0

    @classmethod
    def delete_by_ids(cls, db: Session, pids: list[Any]) -> int:
        """
        Delete multiple records by their IDs

        Args:
            db: Database session
            pids: List of record IDs

        Returns:
            Number of records deleted
        """
        try:
            deleted_count = db.query(cls.model).filter(cls.model.id.in_(pids)).delete(synchronize_session=False)
            db.commit()  # 确保提交事务
            return deleted_count
        except Exception as e:
            db.rollback()  # 回滚事务
            print(f"Error occurred: {e}")
            return 0

    @classmethod
    def filter_update(cls, db: Session, filters: list[Any], update_data: dict[str, Any]):
        now = cls.current_timestamp()
        now_datetime = cls.current_datetime()
        update_data["update_time"] = now
        update_data["update_date"] = now_datetime
        # with db.begin():
        updated_rows= db.query(cls.model).filter(*filters).update(update_data, synchronize_session=False)
        db.commit()
        return updated_rows > 0  # Return True if any rows were updated

    @classmethod
    def filter_delete(cls, db: Session, filters: list[Any]) -> int:
        num = db.query(cls.model).filter(*filters).delete(synchronize_session=False)
        db.commit()
        return num

    @classmethod
    def cut_list(cls, tar_list: list[Any], n: int) -> list[tuple]:
        return [tuple(tar_list[i:i + n]) for i in range(0, len(tar_list), n)]

    @classmethod
    def filter_scope_list(cls, db: Session, in_key: str, in_filters_list: list[Any], filters: list | None = None,
                          cols: list[str] | None = None) -> list[Row[tuple[Type[db_models.BaseModel]]]]:
        in_filters_tuple_list = cls.cut_list(in_filters_list, 20)
        if not filters:
            filters = []
        res_list = []
        for filters_tuple in in_filters_tuple_list:
            query = db.query(cls.model)
            if cols:
                query = query.with_entities(*[getattr(cls.model, col) for col in cols])
            query = query.filter(getattr(cls.model, in_key).in_(filters_tuple), *filters)
            res_list.extend(query.all())
        return res_list
