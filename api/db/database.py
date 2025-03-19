# -*- coding: utf-8 -*-
import operator
import os
import sys
import typing
import uuid
from datetime import datetime, timezone

from sqlalchemy import String, DateTime, BigInteger
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
SessionLocal = sessionmaker(autocommit=true, autoflush=False, bind=engine)

Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


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


class BaseModel(Base):
    __abstract__ = True

    id = Column(String, primary_key=False, nullable=False, index=True, default=lambda: str(uuid.uuid4()))
    create_time = Column(BigInteger, nullable=True, index=True,
                         default=lambda: int(datetime.now(timezone.utc).timestamp() * 1000))
    create_date = Column(DateTime, nullable=True, index=True, default=datetime.now(timezone.utc))
    update_time = Column(BigInteger, nullable=True, index=True,
                         default=lambda: int(datetime.now(timezone.utc).timestamp() * 1000),
                         onupdate=lambda: int(datetime.now(timezone.utc).timestamp() * 1000))
    update_date = Column(DateTime, nullable=True, index=True, default=datetime.now(timezone.utc),
                         onupdate=datetime.now(timezone.utc))

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