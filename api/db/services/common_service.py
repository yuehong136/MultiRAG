import functools
import logging
import time
from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from fastapi import HTTPException
from sqlalchemy import Row, asc, delete, desc, exc, insert, select, update
from sqlalchemy.exc import IntegrityError, NoResultFound
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import InstrumentedAttribute
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from api.db import db_models
from common.misc_utils import get_uuid

# from common.time_utils import current_timestamp, datetime_format

# 配置日志
logger = logging.getLogger(__name__)

RETRYABLE_DB_EXCEPTIONS = (
    exc.OperationalError,
    exc.DisconnectionError,
    exc.InterfaceError,
    exc.TimeoutError,
    exc.PendingRollbackError,
)


def _get_session_from_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Session | None:
    """从函数参数中提取 Session，用于失败时自动回滚。"""
    db = kwargs.get("db")
    if isinstance(db, Session):
        return db

    for arg in args:
        if isinstance(arg, Session):
            return arg

    for value in kwargs.values():
        if isinstance(value, Session):
            return value

    return None


def _safe_rollback(db: Session, func_name: str) -> None:
    try:
        db.rollback()
    except Exception as rollback_error:
        logger.warning(f"[DB Retry] {func_name} 回滚失败: {rollback_error}")


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
        def _before_sleep(retry_state):
            logger.warning(
                f"[DB Retry] {func.__name__} 执行失败，"
                f"{retry_state.next_action.sleep:.1f}秒后重试 "
                f"(第 {retry_state.attempt_number}/{max_attempts} 次重试)"
            )

        @retry(
            stop=stop_after_attempt(max_attempts),
            wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
            retry=retry_if_exception_type(RETRYABLE_DB_EXCEPTIONS),
            before_sleep=_before_sleep,
            reraise=True,  # 重试失败后重新抛出异常
        )
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            db = _get_session_from_call(args, kwargs)
            try:
                return func(*args, **kwargs)
            except RETRYABLE_DB_EXCEPTIONS:
                if db is not None:
                    _safe_rollback(db, func.__name__)
                raise
        return wrapper
    return decorator


TRANSIENT_TX_CONFLICT_CODES = frozenset({"40P01", "40001", "1213", "1205"})


def _extract_dbapi_error_code(error: exc.DBAPIError) -> str | None:
    """Return SQLSTATE / driver error code for portable transient-tx checks."""
    orig = getattr(error, "orig", None)
    if orig is not None:
        for attr in ("sqlstate", "pgcode", "code"):
            code = getattr(orig, attr, None)
            if code:
                return str(code)

        orig_args = getattr(orig, "args", ())
        if orig_args:
            first_arg = orig_args[0]
            if first_arg is not None:
                return str(first_arg)

    error_args = getattr(error, "args", ())
    if error_args:
        first_arg = error_args[0]
        if first_arg is not None:
            return str(first_arg)

    return None


def _is_transient_tx_conflict(error: exc.DBAPIError) -> bool:
    code = _extract_dbapi_error_code(error)
    return code in TRANSIENT_TX_CONFLICT_CODES


def retry_transient_tx_conflict(max_attempts: int = 3, base_delay: float = 0.1, max_delay: float = 1.0):
    """Retry a full transaction when the database aborts due to a transient conflict.

    This helper is intentionally narrow: it only retries known deadlock /
    serialization conflict codes that are safe to replay at the transaction
    boundary.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            db = _get_session_from_call(args, kwargs)

            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exc.DBAPIError as error:
                    if not _is_transient_tx_conflict(error) or attempt >= max_attempts:
                        raise

                    if db is not None:
                        _safe_rollback(db, func.__name__)

                    code = _extract_dbapi_error_code(error) or "unknown"
                    delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
                    logger.warning(
                        "[DB Tx Retry] %s hit transient tx conflict %s; retrying in %.2fs (%s/%s)",
                        func.__qualname__,
                        code,
                        delay,
                        attempt,
                        max_attempts,
                    )
                    time.sleep(delay)

        return wrapper

    return decorator


# Define a TypeVar bound to db_models.BaseModel for better type hinting
ModelType = TypeVar("ModelType", bound=db_models.BaseModel)

class CommonService(Generic[ModelType]):
    model: type[ModelType]

    def __init__(self, model: type[ModelType]):
        self.model = model

    @staticmethod
    def current_timestamp():
        return int(datetime.now(UTC).timestamp() * 1000)

    @staticmethod
    def current_datetime():
        return datetime.now(UTC)

    @classmethod
    def query(cls, db: Session, cols: list[str] | None = None, reverse: bool | None = None,
              order_by: str | InstrumentedAttribute | None = None, **kwargs) -> list[ModelType] | list[Row]:
        """
        根据条件查询数据库中的记录（SQLAlchemy 2.0 Core 风格）。

        :param db: 数据库会话对象。
        :param cols: 需要查询的列，可选。
        :param reverse: 是否逆序排序，可选。
        :param order_by: 按哪个字段排序，可选。
        :param kwargs: 其他过滤条件。
        :return: 查询结果列表。
        """
        # 构建 select 语句
        if cols:
            stmt = select(*[getattr(cls.model, col) for col in cols])
        else:
            stmt = select(cls.model)

        # 根据过滤条件构造 where 子句
        if kwargs:
            conditions = [getattr(cls.model, k) == v for k, v in kwargs.items() if hasattr(cls.model, k)]
            stmt = stmt.where(*conditions)

        # 处理排序
        if order_by:
            if not isinstance(order_by, str):
                order_by = str(order_by)  # 确保 order_by 是字符串类型
            if not hasattr(cls.model, order_by):
                raise ValueError(f"'{order_by}' is not a valid attribute of '{cls.model.__name__}'")
            order_column = getattr(cls.model, order_by)
            if reverse:
                stmt = stmt.order_by(desc(order_column))
            else:
                stmt = stmt.order_by(asc(order_column))
        else:
            order_column = getattr(cls.model, "create_time", None)
            if order_column is not None:
                if reverse:
                    stmt = stmt.order_by(desc(order_column))
                else:
                    stmt = stmt.order_by(asc(order_column))

        # 执行查询并返回结果
        if cols:
            return db.execute(stmt).all()
        else:
            return db.scalars(stmt).all()

    @classmethod
    def get_all(cls, db: Session, cols: list[str] | None = None, reverse: bool | None = None, order_by: str | None = None) -> list[ModelType] | list[Row]:
        """
        获取所有记录（SQLAlchemy 2.0 Core 风格）。
        """
        # 构建 select 语句
        if cols:
            stmt = select(*[getattr(cls.model, col) for col in cols])
        else:
            stmt = select(cls.model)

        # 处理排序
        if reverse is not None:
            if not order_by or not hasattr(cls.model, order_by):
                order_by = "create_time"
            order_column = getattr(cls.model, order_by)
            if reverse:
                stmt = stmt.order_by(order_column.desc())
            else:
                stmt = stmt.order_by(order_column.asc())

        # 执行查询并返回结果
        if cols:
            return db.execute(stmt).all()
        else:
            return db.scalars(stmt).all()

    @classmethod
    def get(cls, db: Session, **kwargs) -> ModelType:
        """
        根据条件获取单个记录（SQLAlchemy 2.0 Core 风格）。
        如果未找到记录，抛出 HTTPException 404。
        """
        try:
            stmt = select(cls.model)
            if kwargs:
                conditions = [getattr(cls.model, k) == v for k, v in kwargs.items() if hasattr(cls.model, k)]
                stmt = stmt.where(*conditions)
            return db.scalars(stmt).one()
        except NoResultFound:
            raise HTTPException(status_code=404, detail="Item not found")

    @classmethod
    def get_or_none(cls, db: Session, **kwargs) -> ModelType | None:
        """
        根据条件获取单个记录，如果未找到则返回 None（SQLAlchemy 2.0 Core 风格）。
        """
        stmt = select(cls.model)
        if kwargs:
            conditions = [getattr(cls.model, k) == v for k, v in kwargs.items() if hasattr(cls.model, k)]
            stmt = stmt.where(*conditions)
        return db.scalars(stmt).one_or_none()

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
        timestamp = cls.current_timestamp()
        cur_datetime = cls.current_datetime()
        kwargs["create_time"] = timestamp
        kwargs["create_date"] = cur_datetime
        kwargs["update_time"] = timestamp
        kwargs["update_date"] = cur_datetime
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
        """
        批量插入记录（SQLAlchemy 2.0 Core 风格）。

        Args:
            db: 数据库会话
            data_list: 要插入的数据字典列表
            batch_size: 每批次插入的记录数量
        """
        if not data_list:
            return

        now = cls.current_timestamp()
        now_datetime = cls.current_datetime()
        for data in data_list:
            data["create_time"] = now
            data["create_date"] = now_datetime
            data["update_time"] = now
            data["update_date"] = now_datetime

        # SQLAlchemy 2.0 Core 风格：使用 insert().values() 批量插入
        for i in range(0, len(data_list), batch_size):
            batch = data_list[i:i + batch_size]
            stmt = insert(cls.model).values(batch)
            db.execute(stmt)
            db.commit()

    @classmethod
    def update_many_by_id(cls, db: Session, data_list: list[dict[str, Any]]):
        """
        批量通过 ID 更新记录（SQLAlchemy 2.0 Core 风格）。

        Args:
            db: 数据库会话
            data_list: 包含更新数据的字典列表，每个字典必须包含 'id' 字段
        """
        timestamp = cls.current_timestamp()
        cur_datetime = cls.current_datetime()
        for data in data_list:
            data["update_time"] = timestamp
            data["update_date"] = cur_datetime
            record_id = data["id"]
            stmt = update(cls.model).where(cls.model.id == record_id).values(data)
            db.execute(stmt)
        db.commit()

    @classmethod
    @retry_db_operation(max_attempts=3)  # 更新操作重试3次
    def update_by_id(cls, db: Session, pid: str, data: dict[str, Any]) -> int:
        """
        通过 ID 更新记录（SQLAlchemy 2.0 Core 风格），带自动重试机制

        Args:
            db: 数据库会话
            pid: 记录 ID
            data: 要更新的数据字典

        Returns:
            更新的记录数量
        """
        data["update_time"] = cls.current_timestamp()
        data["update_date"] = cls.current_datetime()
        stmt = update(cls.model).where(cls.model.id == pid).values(data)
        result = db.execute(stmt)
        db.commit()
        return result.rowcount

    @classmethod
    @retry_db_operation(max_attempts=3)
    def get_by_id(cls, db: Session, pid: Any) -> ModelType | None:
        """
        通过主键查询单个记录（SQLAlchemy 2.0 推荐的 session.get() 方式，带自动重试）。

        使用 session.get() 的优势：
        - 会先检查 Session 的 identity map，如果对象已加载则直接返回
        - 代码更简洁，语义更清晰

        重试机制：
        - 自动重试 OperationalError、DisconnectionError 等连接类错误
        - 最多重试 3 次，使用指数退避（1-5秒）

        Returns:
            找到时返回对象，找不到时返回 None
        """
        return db.get(cls.model, pid)

    @classmethod
    def get_by_ids(cls, db: Session, pids: list[Any], cols: list[str] | None = None) -> list[ModelType] | list[Row]:
        """
        通过 ID 列表批量查询记录（SQLAlchemy 2.0 Core 风格）。
        """
        if cols:
            stmt = select(*[getattr(cls.model, col) for col in cols]).where(cls.model.id.in_(pids))
            return db.execute(stmt).all()
        else:
            stmt = select(cls.model).where(cls.model.id.in_(pids))
            return db.scalars(stmt).all()

    @classmethod
    def delete_by_id(cls, db: Session, pid: Any) -> int:
        """
        通过 ID 删除记录（SQLAlchemy 2.0 Core 风格）。
        """
        try:
            stmt = delete(cls.model).where(cls.model.id == pid)
            result = db.execute(stmt)
            db.commit()
            return result.rowcount
        except Exception as e:
            db.rollback()
            logger.error(f"Error occurred in delete_by_id: {e}")
            return 0

    @classmethod
    def delete_by_ids(cls, db: Session, pids: list[Any]) -> int:
        """
        通过 ID 列表批量删除记录（SQLAlchemy 2.0 Core 风格）。

        Args:
            db: Database session
            pids: List of record IDs

        Returns:
            Number of records deleted
        """
        try:
            stmt = delete(cls.model).where(cls.model.id.in_(pids))
            result = db.execute(stmt)
            db.commit()
            return result.rowcount
        except Exception as e:
            db.rollback()
            logger.error(f"Error occurred in delete_by_ids: {e}")
            return 0

    @classmethod
    @retry_db_operation(max_attempts=3)
    def filter_update(cls, db: Session, filters: list[Any], update_data: dict[str, Any]):
        """
        批量更新方法（SQLAlchemy 2.0 Core 风格）。

        Args:
            db: 数据库会话
            filters: 过滤条件列表
            update_data: 更新数据字典

        Returns:
            bool: 是否有记录被更新
        """
        try:
            update_data["update_time"] = cls.current_timestamp()
            update_data["update_date"] = cls.current_datetime()
            stmt = update(cls.model).where(*filters).values(update_data)
            result = db.execute(stmt)
            db.commit()
            return result.rowcount > 0
        except Exception:
            db.rollback()
            raise

    @classmethod
    @retry_db_operation(max_attempts=3)
    def filter_delete(cls, db: Session, filters: list[Any]) -> int:
        """
        根据条件批量删除记录（SQLAlchemy 2.0 Core 风格）。
        """
        try:
            stmt = delete(cls.model).where(*filters)
            result = db.execute(stmt)
            db.commit()
            return result.rowcount
        except Exception:
            db.rollback()
            raise

    @classmethod
    def cut_list(cls, tar_list: list[Any], n: int) -> list[tuple]:
        return [tuple(tar_list[i:i + n]) for i in range(0, len(tar_list), n)]

    @classmethod
    def filter_scope_list(cls, db: Session, in_key: str, in_filters_list: list[Any], filters: list | None = None,
                          cols: list[str] | None = None) -> list[Row] | list[ModelType]:
        """
        根据 IN 条件和其他过滤条件批量查询记录（SQLAlchemy 2.0 Core 风格）。
        """
        in_filters_tuple_list = cls.cut_list(in_filters_list, 20)
        if not filters:
            filters = []
        res_list = []
        for filters_tuple in in_filters_tuple_list:
            # 构建 select 语句
            if cols:
                stmt = select(*[getattr(cls.model, col) for col in cols])
            else:
                stmt = select(cls.model)

            # 添加 where 条件
            stmt = stmt.where(getattr(cls.model, in_key).in_(filters_tuple), *filters)

            # 执行查询
            if cols:
                res_list.extend(db.execute(stmt).all())
            else:
                res_list.extend(db.scalars(stmt).all())
        return res_list
