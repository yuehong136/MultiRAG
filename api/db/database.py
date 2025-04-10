# -*- coding: utf-8 -*-
import operator
import os
import sys
import typing
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, DateTime, BigInteger, event
from sqlalchemy import create_engine, Column
from sqlalchemy.orm import sessionmaker, declarative_base
from api.settings import DATABASE

database_config = DATABASE

# DATABASE_URL = "postgresql://postgres:123456@127.0.0.1:5432/postgres"
DATABASE_URL = (
    f"{database_config['name']}://"
    f"{database_config['user']}:{database_config['password']}@"
    f"{database_config['host']}:{database_config['port']}/"
    f"{database_config.get('dbname', 'postgres')}"
)

engine = create_engine(
    DATABASE_URL,
    client_encoding='utf8',
    pool_size=database_config.get('pool_size', 20),
    max_overflow=database_config.get('max_overflow', 20),
    pool_timeout=database_config.get('pool_timeout', 30),
    pool_recycle=database_config.get('pool_recycle', 1800),
    echo=False
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()

from contextlib import contextmanager


@contextmanager
def db_connection():
    """提供数据库连接的上下文管理器。

    用法:
    with db_connection() as db:
        # 使用db进行操作
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# 保留原代码中的常量和辅助函数
CONTINUOUS_FIELD_TYPE = {int, float, datetime}
AUTO_DATE_TIMESTAMP_FIELD_PREFIX = {
    "create",
    "start",
    "end",
    "update",
    "read_access",
    "write_access"}


def is_continuous_field(cls: typing.Type) -> bool:
    """检查类型是否是连续字段类型（例如数值或日期类型）。"""
    if cls in CONTINUOUS_FIELD_TYPE:
        return True
    for p in cls.__bases__:
        if p in CONTINUOUS_FIELD_TYPE:
            return True
        elif p is not object:
            if is_continuous_field(p):
                return True
    return False


def auto_date_timestamp_field():
    return {f"{f}_time" for f in AUTO_DATE_TIMESTAMP_FIELD_PREFIX}


def get_utc_now():
    """获取当前UTC时间"""
    return datetime.now(timezone.utc)


def get_timestamp_ms():
    """获取当前UTC时间的毫秒时间戳"""
    return int(get_utc_now().timestamp() * 1000)


class BaseModel(Base):
    __abstract__ = True

    id = Column(String, primary_key=False, nullable=False, index=True,
                default=lambda: str(uuid.uuid4()))

    # 日期时间对象
    create_date = Column(DateTime, nullable=True, index=True,
                         default=get_utc_now)
    update_date = Column(DateTime, nullable=True, index=True,
                         default=get_utc_now, onupdate=get_utc_now)

    # 毫秒时间戳
    create_time = Column(BigInteger, nullable=True, index=True,
                         default=get_timestamp_ms)
    update_time = Column(BigInteger, nullable=True, index=True,
                         default=get_timestamp_ms, onupdate=get_timestamp_ms)

    def to_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}

    @classmethod
    def query(cls, reverse=None, order_by=None, **kwargs):
        session = SessionLocal()
        try:
            query = session.query(cls)
            filters = []
            for f_n, f_v in kwargs.items():
                attr_name = f"{f_n}"
                if not hasattr(cls, attr_name) or f_v is None:
                    continue
                column_attr = getattr(cls, attr_name)

                if isinstance(f_v, (list, set)):
                    f_v = list(f_v)
                    if is_continuous_field(type(column_attr)):
                        if len(f_v) == 2:
                            lt_value, gt_value = f_v
                            if lt_value is not None:
                                filters.append(column_attr >= lt_value)
                            if gt_value is not None:
                                filters.append(column_attr <= gt_value)
                    else:
                        filters.append(column_attr.in_(f_v))
                else:
                    filters.append(column_attr == f_v)

            if filters:
                query = query.filter(*filters)

            if order_by:
                order_column = getattr(cls, order_by, None)
                if order_column is not None:
                    if reverse:
                        query = query.order_by(order_column.desc())
                    else:
                        query = query.order_by(order_column.asc())
                else:
                    query = query.order_by(cls.create_time.desc() if reverse else cls.create_time.asc())

            return query.all()
        finally:
            session.close()


# 使用SQLAlchemy事件来确保时间戳和日期时间的一致性
@event.listens_for(BaseModel, 'before_insert', propagate=True)
def before_insert(mapper, connection, target):
    """在插入前同步时间字段"""
    now = get_utc_now()
    timestamp = int(now.timestamp() * 1000)

    target.create_date = now
    target.update_date = now
    target.create_time = timestamp
    target.update_time = timestamp


@event.listens_for(BaseModel, 'before_update', propagate=True)
def before_update(mapper, connection, target):
    """在更新前同步更新时间字段"""
    now = get_utc_now()
    timestamp = int(now.timestamp() * 1000)

    target.update_date = now
    target.update_time = timestamp