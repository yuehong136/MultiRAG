# coding=utf-8
"""
@project: multirag
@Author：龙
@file： db_models.py
@date：2024/8/7 17:00
@desc:
"""
import logging
import os
import sys
import inspect
from sqlalchemy.exc import OperationalError
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy import Column, String, Integer, Float, DateTime, Boolean, Text, BigInteger, text
from sqlalchemy.dialects.postgresql import JSONB
import typing
import uuid
from datetime import datetime, timezone
import time
import functools
import hashlib

from sqlalchemy import String, DateTime, BigInteger, event
from sqlalchemy import create_engine, Column
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from sqlalchemy.exc import SQLAlchemyError

from api.utils import decrypt_database_config

from alembic.config import Config
from alembic import command
from alembic.script import ScriptDirectory
from alembic.runtime.migration import MigrationContext

DATABASE_TYPE = os.getenv("DB_TYPE", 'postgresql')
DATABASE = decrypt_database_config(name=DATABASE_TYPE)
database_config = DATABASE

# DATABASE_URL = "postgresql://postgres:123456@127.0.0.1:5432/postgres"
DATABASE_URL = (
    f"{database_config['name']}://"
    f"{database_config['user']}:{database_config['password']}@"
    f"{database_config['host']}:{database_config['port']}/"
    f"{database_config.get('dbname', 'postgres')}"
)


def get_engine_config(db_config: dict) -> dict:
    """将配置文件中的数据库配置映射到 SQLAlchemy 引擎配置"""
    max_connections = db_config.get('max_connections', 100)
    stale_timeout = db_config.get('stale_timeout', 30)

    return {
        'pool_size': max_connections,
        'max_overflow': max_connections // 2,  # 或者你可以设置为固定值
        'pool_timeout': stale_timeout,
        'pool_recycle': stale_timeout * 100,  # 转换为秒，设置连接回收时间
        'echo': False
    }


engine_config = get_engine_config(database_config)
engine = create_engine(
    DATABASE_URL,
    client_encoding='utf8',
    **engine_config
)
# engine = create_engine(
#     DATABASE_URL,
#     client_encoding='utf8',
#     pool_size=database_config.get('pool_size', 20),
#     max_overflow=database_config.get('max_overflow', 20),
#     pool_timeout=database_config.get('pool_timeout', 30),
#     pool_recycle=database_config.get('pool_recycle', 1800),
#     echo=False
# )
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


class User(BaseModel):
    __tablename__ = "t_ai_users"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    access_token = Column(String(255), index=True, nullable=True)
    nickname = Column(String(100), index=True, nullable=False)
    password = Column(String(255), index=True, nullable=True)
    email = Column(String(255), unique=True, index=True, nullable=False)
    avatar = Column(Text, index=False, nullable=True, doc="avatar base64 string")
    language = Column(String(32), index=True, nullable=True, default="English")
    color_schema = Column(String(32), index=True, nullable=True, default="Bright")
    timezone = Column(String(64), index=True, nullable=True, default="UTC+8\tAsia/Shanghai")
    last_login_time = Column(DateTime, index=True, nullable=True)
    is_authenticated = Column(Boolean, index=True, nullable=False, default=True)
    is_active = Column(Boolean, index=True, nullable=False, default=True)
    is_anonymous = Column(Boolean, index=True, nullable=False, default=False)
    login_channel = Column(String, index=True, nullable=True, default=None)
    status = Column(String(1), index=True, nullable=True, default="1")
    is_superuser = Column(Boolean, index=True, nullable=True, default=False)

    def to_dict(self):
        return {
            "id": self.id,
            "access_token": self.access_token,
            "avatar": self.avatar,
            "email": self.email,
            "is_active": self.is_active,
            "is_anonymous": self.is_anonymous,
            "language": self.language,
            "nickname": self.nickname,
            "password": self.password,
            "status": self.status,
            "timezone": self.timezone,
            "last_login_time": self.last_login_time,
            "is_superuser": self.is_superuser
        }


class Tenant(BaseModel):
    __tablename__ = "t_ai_tenants"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    name = Column(String(100), index=True, nullable=True, doc="Tenant name")
    public_key = Column(String(255), index=True, nullable=True)
    llm_id = Column(String(128), index=True, nullable=False, doc="default llm ID")
    embd_id = Column(String(128), index=True, nullable=False, doc="default embedding model ID")
    asr_id = Column(String(128), index=True, nullable=False, doc="default ASR model ID")
    img2txt_id = Column(String(128), index=True, nullable=False, doc="default image to text model ID")
    rerank_id = Column(String(128), index=True, nullable=True, doc="default rerank model ID")
    tts_id = Column(String(256), index=True, nullable=True, doc="default tts model ID")
    parser_ids = Column(String(256), index=True, nullable=False, doc="document processors")
    credit = Column(Integer, index=True, nullable=False, default=512)
    status = Column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")

    def to_dict(self):
        return {
            "tenant_id": self.id,
            "name": self.name,
            "llm_id": self.llm_id,
            "embd_id": self.embd_id,
            "rerank_id": self.rerank_id,
            "asr_id": self.asr_id,
            "img2txt_id": self.img2txt_id,
            "parser_ids": self.parser_ids
        }


class UserTenant(BaseModel):
    __tablename__ = "t_ai_user_tenants"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(128), primary_key=True, index=False, nullable=False)
    user_id = Column(String(128), index=True, nullable=False)
    tenant_id = Column(String(128), index=True, nullable=False)
    role = Column(String(128), index=True, nullable=False, doc="UserTenantRole")
    invited_by = Column(String(128), index=True, nullable=False)
    status = Column(String(1), index=True, nullable=True, default="1")

    def to_dict(self):
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "role": self.role
        }


class LLMFactories(BaseModel):
    __tablename__ = "t_ai_llm_factories"
    __table_args__ = {"schema": "usr_ai"}

    name = Column(String(128), primary_key=True, index=False, nullable=False, doc="LLM factory name")
    logo = Column(Text, index=False, nullable=True)
    tags = Column(String(255), index=True, nullable=False, doc="LLM, Text Embedding, Image2Text, ASR")
    status = Column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")


class LLM(BaseModel):
    __tablename__ = "t_ai_llms"
    __table_args__ = {"schema": "usr_ai"}

    llm_name = Column(String(128), primary_key=True, index=True, nullable=False)
    mdl_type = Column(String(128), index=True, nullable=False, doc="LLM, Text Embedding, Image2Text, ASR")
    fid = Column(String(128), index=True, nullable=False, doc="LLM factory id")
    max_tokens = Column(BigInteger, index=False, nullable=False, default=0)
    tags = Column(String(255), index=True, nullable=False, doc="LLM, Text Embedding, Image2Text, Chat, 32k...")
    is_tools = Column(Boolean, index=True, nullable=False, default=False, doc="support tools")
    status = Column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")


class TenantLLM(BaseModel):
    __tablename__ = "t_ai_tenant_llms"
    __table_args__ = {"schema": "usr_ai"}

    tenant_id = Column(String(32), primary_key=True, index=True, nullable=False)
    llm_factory = Column(String(128), primary_key=True, index=True, nullable=False, doc="LLM factory name")
    mdl_type = Column(String(128), index=True, nullable=True, doc="LLM, Text Embedding, Image2Text, ASR")
    llm_name = Column(String(128), primary_key=True, index=True, nullable=True)
    api_key = Column(String(2048), index=True, nullable=True)
    api_base = Column(String(255), index=False, nullable=True)
    max_tokens = Column(Integer, index=True, nullable=False, default=8192)
    used_tokens = Column(Integer, index=True, nullable=False, default=0)


class TenantLangfuse(BaseModel):
    __tablename__ = "t_ai_tenant_langfuse"
    __table_args__ = {"schema": "usr_ai"}

    tenant_id = Column(String(32), primary_key=True, index=True, nullable=False)
    secret_key = Column(String(2048), index=True, nullable=False, doc="SECRET KEY")
    public_key = Column(String(2048), index=True, nullable=False, doc="PUBLIC KEY")
    host = Column(String(128), index=True, nullable=False, doc="HOST")

    def __str__(self):
        # Mimicking the original __str__ method, but f-string is more Pythonic
        return f"Langfuse tenant_id: {self.tenant_id}, host: {self.host}"

    def to_dict(self):
        # You can customize this if you want a different representation
        # than the default BaseModel.to_dict() or if you want to exclude sensitive fields.
        # For example, to exclude secret_key from general dictionary conversions:
        return {
            "tenant_id": self.tenant_id,
            "public_key": self.public_key,
            "host": self.host
        }


# ===== AI Guard domain models =====


class GuardService(BaseModel):
    __tablename__ = "t_guard_services"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    code = Column(String(128), index=True, nullable=False, unique=True)
    name = Column(String(128), index=True, nullable=False)
    description = Column(Text, index=False, nullable=True)
    service_type = Column(String(32), index=True, nullable=False)
    enabled_dimensions = Column(JSONB, index=False, nullable=False, default=list)
    enabled_labels = Column(JSONB, index=False, nullable=False, default=list)
    policy_config = Column(JSONB, index=False, nullable=False, default=dict)
    cache_enabled = Column(Boolean, index=True, nullable=False, default=True)
    timeout_ms = Column(Integer, index=True, nullable=False, default=1000)
    total_requests = Column(Integer, index=True, nullable=False, default=0)
    blocked_requests = Column(Integer, index=True, nullable=False, default=0)
    tenant_id = Column(String(32), index=True, nullable=False)
    created_by = Column(String(32), index=True, nullable=False)
    status = Column(String(1), index=True, nullable=True, default="1")


class GuardServiceLibrary(BaseModel):
    __tablename__ = "t_guard_service_libraries"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    service_id = Column(String(32), index=True, nullable=False)
    library_id = Column(String(32), index=True, nullable=False)
    enabled = Column(Boolean, index=True, nullable=False, default=True)
    priority = Column(Integer, index=True, nullable=False, default=0)
    library_type = Column(String(32), index=True, nullable=True)
    apply_to_dimensions = Column(JSONB, index=False, nullable=False, default=list)
    apply_to_labels = Column(JSONB, index=False, nullable=False, default=list)
    config = Column(JSONB, index=False, nullable=False, default=dict)
    tenant_id = Column(String(32), index=True, nullable=False)
    created_by = Column(String(32), index=True, nullable=False)
    status = Column(String(1), index=True, nullable=True, default="1")


class GuardRule(BaseModel):
    __tablename__ = "t_guard_rules"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    label_id = Column(String(32), index=True, nullable=False)
    rule_type = Column(String(32), index=True, nullable=False)
    content = Column(Text, index=True, nullable=False)
    content_hash = Column(String(64), index=True, nullable=False)
    match_mode = Column(String(16), index=True, nullable=False)
    case_sensitive = Column(Boolean, index=True, nullable=False, default=False)
    config = Column(JSONB, index=False, nullable=False, default=dict)
    weight = Column(Float, index=True, nullable=False, default=0.0)
    priority = Column(Integer, index=True, nullable=False, default=0)
    source = Column(String(64), index=True, nullable=True)
    description = Column(Text, index=False, nullable=True)
    tenant_id = Column(String(32), index=True, nullable=False)
    created_by = Column(String(32), index=True, nullable=False)
    status = Column(String(1), index=True, nullable=True, default="1")


class GuardLog(BaseModel):
    __tablename__ = "t_guard_logs"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    service_id = Column(String(32), index=True, nullable=False)
    service_code = Column(String(128), index=True, nullable=True)
    request_id = Column(String(64), index=True, nullable=True)
    chat_id = Column(String(64), index=True, nullable=True)
    user_id = Column(String(32), index=True, nullable=True)
    tenant_id = Column(String(32), index=True, nullable=False)
    content = Column(Text, index=False, nullable=True)
    content_hash = Column(String(64), index=True, nullable=False)
    content_length = Column(Integer, index=True, nullable=False, default=0)
    content_preview = Column(String(500), index=False, nullable=True)
    is_blocked = Column(Boolean, index=True, nullable=False, default=False)
    risk_score = Column(Float, index=True, nullable=False, default=0.0)
    content_risk_level = Column(String(16), index=True, nullable=True)
    content_results = Column(JSONB, index=False, nullable=False, default=dict)
    sensitive_level = Column(String(8), index=True, nullable=True)
    sensitive_results = Column(JSONB, index=False, nullable=False, default=dict)
    attack_level = Column(String(16), index=True, nullable=True)
    attack_results = Column(JSONB, index=False, nullable=False, default=dict)
    customized_hits = Column(JSONB, index=False, nullable=False, default=list)
    risk_words = Column(JSONB, index=False, nullable=False, default=list)
    sensitive_data = Column(JSONB, index=False, nullable=False, default=list)
    action_taken = Column(String(32), index=True, nullable=True)
    action_detail = Column(JSONB, index=False, nullable=False, default=dict)
    source_type = Column(String(64), index=True, nullable=True)
    source_id = Column(String(255), index=True, nullable=True)
    client_ip = Column(String(64), index=True, nullable=True)
    user_agent = Column(Text, index=False, nullable=True)
    process_time_ms = Column(Integer, index=True, nullable=True)
    cloud_service_used = Column(Boolean, index=True, nullable=False, default=False)


class GuardLibraryItem(BaseModel):
    __tablename__ = "t_guard_library_items"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    library_id = Column(String(32), index=True, nullable=False)
    content = Column(Text, index=True, nullable=False)
    content_hash = Column(String(64), index=True, nullable=False)
    content_type = Column(String(32), index=True, nullable=False)
    item_metadata = Column(JSONB, index=False, nullable=False, default=dict)
    hit_count = Column(Integer, index=True, nullable=False, default=0)
    last_hit_time = Column(DateTime, index=True, nullable=True)
    sort_order = Column(Integer, index=True, nullable=False, default=0)
    tenant_id = Column(String(32), index=True, nullable=False)
    status = Column(String(1), index=True, nullable=True, default="1")


class GuardLibrary(BaseModel):
    __tablename__ = "t_guard_libraries"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    library_type = Column(String(32), index=True, nullable=False)
    name = Column(String(128), index=True, nullable=False)
    description = Column(Text, index=False, nullable=True)
    category = Column(String(64), index=True, nullable=True)
    tags = Column(JSONB, index=False, nullable=False, default=list)
    config = Column(JSONB, index=False, nullable=False, default=dict)
    item_count = Column(Integer, index=True, nullable=False, default=0)
    hit_count = Column(Integer, index=True, nullable=False, default=0)
    last_hit_time = Column(DateTime, index=True, nullable=True)
    version = Column(Integer, index=True, nullable=False, default=1)
    tenant_id = Column(String(32), index=True, nullable=False)
    created_by = Column(String(32), index=True, nullable=False)
    status = Column(String(1), index=True, nullable=True, default="1")


class GuardLabel(BaseModel):
    __tablename__ = "t_guard_labels"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    dimension_id = Column(String(32), index=True, nullable=False)
    code = Column(String(64), index=True, nullable=False, unique=True)
    name = Column(String(128), index=True, nullable=False)
    description = Column(Text, index=False, nullable=True)
    cloud_label = Column(String(64), index=True, nullable=True)
    cloud_label_type = Column(String(16), index=True, nullable=False)
    detection_ranges = Column(JSONB, index=False, nullable=False, default=list)
    enabled = Column(Boolean, index=True, nullable=False, default=True)
    risk_score = Column(Float, index=True, nullable=False, default=0.0)
    risk_level = Column(Integer, index=True, nullable=False, default=0)
    sensitive_level = Column(String(8), index=True, nullable=True)
    action = Column(String(32), index=True, nullable=False)
    action_config = Column(JSONB, index=False, nullable=False, default=dict)
    sort_order = Column(Integer, index=True, nullable=False, default=0)
    tenant_id = Column(String(32), index=True, nullable=False)
    created_by = Column(String(32), index=True, nullable=False)
    status = Column(String(1), index=True, nullable=True, default="1")


class GuardLabelLibrary(BaseModel):
    __tablename__ = "t_guard_label_libraries"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    label_id = Column(String(32), index=True, nullable=False)
    library_id = Column(String(32), index=True, nullable=False)
    enabled = Column(Boolean, index=True, nullable=False, default=True)
    priority = Column(Integer, index=True, nullable=False, default=0)
    config = Column(JSONB, index=False, nullable=False, default=dict)
    conditions = Column(JSONB, index=False, nullable=False, default=dict)
    tenant_id = Column(String(32), index=True, nullable=False)
    created_by = Column(String(32), index=True, nullable=False)
    status = Column(String(1), index=True, nullable=True, default="1")


class GuardDimension(BaseModel):
    __tablename__ = "t_guard_dimensions"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    code = Column(String(64), index=True, nullable=False, unique=True)
    name = Column(String(128), index=True, nullable=False)
    description = Column(Text, index=False, nullable=True)
    enabled = Column(Boolean, index=True, nullable=False, default=True)
    config = Column(JSONB, index=False, nullable=False, default=dict)
    sort_order = Column(Integer, index=True, nullable=False, default=0)
    tenant_id = Column(String(32), index=True, nullable=False)
    created_by = Column(String(32), index=True, nullable=False)
    status = Column(String(1), index=True, nullable=True, default="1")


class Knowledgebase(BaseModel):
    __tablename__ = "t_ai_knowledgebases"
    __table_args__ = {"schema": "usr_ai"}
    id = Column(String(32), primary_key=True, index=False, nullable=False)
    avatar = Column(Text, index=False, nullable=True, doc="avatar base64 string")
    tenant_id = Column(String(32), index=True, nullable=False)
    name = Column(String(128), index=True, nullable=False, doc="KB name")
    language = Column(String(32), index=True, nullable=True, default="English", doc="English|Chinese")
    description = Column(Text, index=False, nullable=True, doc="KB description")
    embd_id = Column(String(128), index=True, nullable=False, doc="default embedding model ID")
    permission = Column(String(16), index=True, nullable=False, default="me", doc="me|team")
    created_by = Column(String(32), index=True, nullable=False)
    doc_num = Column(Integer, index=True, nullable=False, default=0)
    token_num = Column(Integer, index=True, nullable=False, default=0)
    chunk_num = Column(Integer, index=True, nullable=False, default=0)
    similarity_threshold = Column(Float, index=True, nullable=False, default=0.2)
    vector_similarity_weight = Column(Float, index=True, nullable=False, default=0.3)
    parser_id = Column(String(32), index=True, nullable=False, doc="default parser ID")
    parser_config = Column(JSONB, index=False, nullable=False, default={"pages": [[1, 1000000]]})
    pagerank = Column(Integer, index=False, nullable=False, default=0)
    status = Column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")


class Document(BaseModel):
    __tablename__ = "t_ai_documents"
    __table_args__ = {"schema": "usr_ai"}
    id = Column(String(32), primary_key=True, index=False, nullable=False)
    thumbnail = Column(Text, index=False, nullable=True, doc="thumbnail base64 string")
    kb_id = Column(String(256), index=True, nullable=False)
    parser_id = Column(String(32), index=True, nullable=False, doc="default parser ID")
    parser_config = Column(JSONB, index=False, nullable=False, default={"pages": [[1, 1000000]]})
    source_type = Column(String(128), index=True, nullable=False, default="local",
                         doc="where dose this document come from")
    type = Column(String(32), index=True, nullable=False, doc="file extension")
    created_by = Column(String, index=True, nullable=False, doc="who created it")
    name = Column(String(255), index=True, nullable=True, doc="file name")
    location = Column(String(255), index=True, nullable=True, doc="where dose it store")
    size = Column(Integer, index=True, nullable=False, default=0)
    auth = Column(Text, index=False, nullable=True, doc="attribution of data rights and responsibilities")
    token_num = Column(Integer, index=True, nullable=False, default=0)
    chunk_num = Column(Integer, index=True, nullable=False, default=0)
    progress = Column(Float, index=True, nullable=False, default=0)
    progress_msg = Column(Text, index=False, nullable=True, default="", doc="process message")
    process_begin_at = Column(DateTime, index=True, nullable=True)
    process_duration = Column(Float, index=False, nullable=False, default=0)
    meta_fields = Column(JSONB, index=False, nullable=False, default={})
    suffix = Column(String(32), index=True, nullable=False, default="", doc="The real file extension suffix")
    run = Column(String(1), index=True, nullable=True, default="0", doc="start to run processing or cancel.(1: run it; 2: cancel)")
    status = Column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")


class File(BaseModel):
    __tablename__ = "t_ai_files"
    __table_args__ = {"schema": "usr_ai"}
    id = Column(String(32), primary_key=True, index=False, nullable=False)
    parent_id = Column(String(32), index=True, nullable=False, doc="parent folder id")
    tenant_id = Column(String(32), index=True, nullable=False, doc="tenant id")
    created_by = Column(String(32), index=True, nullable=False, doc="who created it")
    name = Column(String(255), index=True, nullable=False, doc="file name or folder name")
    location = Column(String(255), index=True, nullable=True, doc="where dose it store")
    size = Column(Integer, index=True, nullable=False, default=0)
    type = Column(String(32), index=True, nullable=False)
    source_type = Column(String(128), index=True, nullable=False, default="", doc="where dose this document come from")

    def to_dict(self):
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "tenant_id": self.tenant_id,
            "created_by": self.created_by,
            "name": self.name,
            "location": self.location,
            "size": self.size,
            "type": self.type,
            "source_type": self.source_type
        }


class File2Document(BaseModel):
    __tablename__ = "t_ai_file2documents"
    __table_args__ = {"schema": "usr_ai"}
    id = Column(String(32), primary_key=True, index=False, nullable=False)
    file_id = Column(String(32), index=True, nullable=True, doc="file id")
    document_id = Column(String(32), index=True, nullable=True, doc="document id")


class Task(BaseModel):
    __tablename__ = "t_ai_tasks"
    __table_args__ = {"schema": "usr_ai"}
    id = Column(String(32), primary_key=True, index=False, nullable=False)
    doc_id = Column(String(32), index=True, nullable=False)
    from_page = Column(Integer, index=False, nullable=False, default=0)
    to_page = Column(Integer, index=False, nullable=False, default=100000000)
    task_type = Column(String(32), index=False, nullable=False, default="")
    begin_at = Column(DateTime, index=True, nullable=True)
    process_duration = Column(Float, index=False, nullable=False, default=0)
    progress = Column(Float, index=True, nullable=False, default=0)
    progress_msg = Column(Text, index=False, nullable=True, default="", doc="process message")
    retry_count = Column(Integer, index=False, nullable=True, default=0)
    digest = Column(Text, index=False, nullable=True, default="", doc="task digest")
    chunk_ids = Column(Text, index=False, nullable=True, default="", doc="chunk ids")
    priority = Column(Integer, index=False, nullable=False, default=0)


class Dialog(BaseModel):
    __tablename__ = "t_ai_dialogs"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    tenant_id = Column(String(32), index=True, nullable=False)
    name = Column(String(255), index=True, nullable=True, doc="dialog application name")
    description = Column(Text, index=False, nullable=True, doc="Dialog description")
    icon = Column(Text, index=False, nullable=True, doc="icon base64 string")
    language = Column(String(32), index=True, nullable=True, default="English", doc="English|Chinese")
    llm_id = Column(String(128), index=False, nullable=False, doc="default llm ID")
    llm_setting = Column(JSONB, index=False, nullable=False,
                         default={"temperature": 0.1, "top_p": 0.3, "frequency_penalty": 0.7, "presence_penalty": 0.4,
                                  "max_tokens": 512})
    prompt_type = Column(String(16), index=True, nullable=False, default="simple", doc="simple|advanced")
    prompt_config = Column(JSONB, index=False, nullable=False,
                           default={"system": "", "prologue": "Hi! I'm your assistant. What can I do for you?",
                                    "parameters": [],
                                    "empty_response": "Sorry! No relevant content was found in the knowledge base!"})
    meta_data_filter = Column(JSONB,index=False, nullable=True, default={})
    similarity_threshold = Column(Float, index=False, nullable=False, default=0.2)
    vector_similarity_weight = Column(Float, index=False, nullable=False, default=0.3)
    top_n = Column(Integer, index=False, nullable=False, default=6)
    top_k = Column(Integer, index=False, nullable=False, default=1024)
    do_refer = Column(String(1), index=False, nullable=False, default="1",
                      doc="it needs to insert reference index into answer or not")
    rerank_id = Column(String(128), index=False, nullable=True, doc="default rerank model ID")
    kb_ids = Column(JSONB, index=False, nullable=False, default=[])
    search_mode = Column(JSONB, index=False, nullable=True,
                          doc="search mode configuration: hybrid, sparse, dense, or fusion")
    status = Column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")


class Conversation(BaseModel):
    __tablename__ = "t_ai_conversations"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    dialog_id = Column(String(32), index=True, nullable=False)
    name = Column(String(255), index=True, nullable=True, doc="converastion name")
    message = Column(JSONB, index=False, nullable=True)
    reference = Column(JSONB, index=False, nullable=True, default=[])
    user_id = Column(String(32), index=True, nullable=True, doc="user_id")


class APIToken(BaseModel):
    __tablename__ = "t_ai_api_tokens"
    __table_args__ = {"schema": "usr_ai"}

    tenant_id = Column(String(32), primary_key=True, index=True, nullable=False)
    name = Column(String(20), nullable=False, doc="Token名称")
    description = Column(Text, nullable=True, doc="Token描述")  # 使用 Text 类型
    token = Column(String(255), primary_key=True, index=True, nullable=False)
    dialog_id = Column(String(32), index=True, nullable=True)
    source = Column(String(16), index=True, nullable=True, doc="none|agent|dialog")
    beta = Column(String(255), index=True, nullable=True)


class API4Conversation(BaseModel):
    __tablename__ = "t_ai_api4conversations"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    dialog_id = Column(String(32), index=True, nullable=False)
    user_id = Column(String(255), index=True, nullable=False, doc="user_id")
    message = Column(JSONB, index=False, nullable=True)
    reference = Column(JSONB, index=False, nullable=True, default=[])
    tokens = Column(Integer, index=False, nullable=False, default=0)
    source = Column(String(16), index=True, nullable=True, doc="none|agent|dialog")
    dsl = Column(JSONB, index=False, nullable=True, default={})
    duration = Column(Float, index=True, nullable=False, default=0)
    round = Column(Integer, index=True, nullable=False, default=0)
    thumb_up = Column(Integer, index=True, nullable=False, default=0)
    errors = Column(Text, index=False, nullable=True, default=None, doc="errors")


class UserCanvas(BaseModel):
    __tablename__ = "t_ai_user_canvases"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    avatar = Column(Text, index=False, nullable=True, doc="avatar base64 string")
    user_id = Column(String(255), index=True, nullable=False, doc="user_id")
    title = Column(String(255), index=False, nullable=True, doc="Canvas title")
    permission = Column(String(16), index=True, nullable=False, default="me", doc="me|team")
    description = Column(Text, index=False, nullable=True, doc="Canvas description")
    canvas_type = Column(String(32), index=True, nullable=True, doc="Canvas type")
    dsl = Column(JSONB, index=False, nullable=True, default={})


class CanvasTemplate(BaseModel):
    __tablename__ = "t_ai_canvas_templates"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    avatar = Column(Text, index=False, nullable=True, doc="avatar base64 string")
    title = Column(JSONB, index=False, nullable=True, default=dict, doc="Canvas title")
    description = Column(JSONB, index=False, nullable=True, default=dict, doc="Canvas description")
    canvas_type = Column(String(32), index=True, nullable=True, doc="Canvas type")
    dsl = Column(JSONB, index=False, nullable=True, default={})


class WritingProject(BaseModel):
    __tablename__ = "t_ai_writing_projects"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    user_input = Column(Text, index=False, nullable=False, doc="用户输入的需求")
    content_type = Column(String(255), index=True, nullable=False, doc="文案类型")
    language_style = Column(String(255), index=True, nullable=False, doc="语言风格")
    word_count = Column(Integer, index=False, nullable=False, doc="文章篇幅/预期字数")
    reference = Column(Text, index=False, nullable=True, doc="用户提供的参考信息")
    model = Column(String(128), index=True, nullable=False, default="gpt-4o", doc="使用的模型")
    title = Column(String(255), index=True, nullable=True, doc="文章标题")
    user_id = Column(String(32), index=True, nullable=False, doc="所属用户ID")
    status = Column(String(1), index=True, nullable=True, default="1", doc="状态(0:已删除,1:有效)")

    def to_dict(self):
        return {
            "id": self.id,
            "user_input": self.user_input,
            "content_type": self.content_type,
            "language_style": self.language_style,
            "word_count": self.word_count,
            "model": self.model,
            "title": self.title,
            "reference": self.reference,
            "user_id": self.user_id
        }


class WritingChapter(BaseModel):
    __tablename__ = "t_ai_writing_chapters"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    project_id = Column(String(32), index=True, nullable=False, doc="所属项目ID")
    title = Column(String(255), index=True, nullable=False, doc="章节标题")
    summary = Column(Text, index=False, nullable=True, doc="章节摘要")
    level = Column(Integer, index=False, nullable=False, default=1, doc="章节层级(1:主章节,2:子章节)")
    parent_id = Column(String(32), index=True, nullable=True, doc="父章节ID")
    order_index = Column(Integer, index=False, nullable=False, default=0, doc="排序索引")
    status = Column(String(1), index=True, nullable=True, default="1", doc="状态(0:已删除,1:有效)")

    def to_dict(self):
        return {
            "id": self.id,
            "project_id": self.project_id,
            "title": self.title,
            "summary": self.summary,
            "level": self.level,
            "parent_id": self.parent_id,
            "order_index": self.order_index
        }


class WritingReferenceMaterial(BaseModel):
    __tablename__ = "t_ai_writing_references"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    chapter_id = Column(String(32), index=True, nullable=False, doc="所属章节ID")
    title = Column(String(255), index=True, nullable=False, doc="参考资料标题")
    content = Column(Text, index=False, nullable=False, doc="参考资料内容")
    source = Column(String(255), index=True, nullable=True, doc="来源(URL或文件名)")
    type = Column(String(32), index=True, nullable=False, default="text", doc="类型(text,url,file)")
    order_index = Column(Integer, index=False, nullable=False, default=0, doc="排序索引")
    status = Column(String(1), index=True, nullable=True, default="1", doc="状态(0:已删除,1:有效)")

    def to_dict(self):
        return {
            "id": self.id,
            "chapter_id": self.chapter_id,
            "title": self.title,
            "content": self.content,
            "source": self.source,
            "type": self.type,
            "order_index": self.order_index
        }


class WritingChapterContent(BaseModel):
    __tablename__ = "t_ai_writing_contents"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    chapter_id = Column(String(32), index=True, nullable=False, doc="所属章节ID", unique=True)
    content = Column(Text, index=False, nullable=True, doc="章节内容")
    status = Column(String(1), index=True, nullable=True, default="1", doc="状态(0:已删除,1:有效)")

    def to_dict(self):
        return {
            "id": self.id,
            "chapter_id": self.chapter_id,
            "content": self.content
        }


class AskDataHistory(BaseModel):
    __tablename__ = "t_ai_ask_data_history"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    conversation_id = Column(String(32), index=True, nullable=False, doc="conversation_id")
    ask_id = Column(String(32), index=True, nullable=False, doc="ask_id")
    user_id = Column(String(32), index=True, nullable=True, doc="user_id")
    data = Column(Text, index=False, nullable=False, doc="data") # 前端用于展示的数据
    status = Column(String(1), index=True, nullable=True, default="1", doc="状态(0:已删除,1:有效)")
    user_question = Column(Text, index=False, nullable=True, doc="用户问题")
    round_id = Column(String(32), index=True, nullable=False, doc="用于标识对话轮次的ID")
    processed_semantic_layer = Column(Text, index=False, nullable=True, doc="该问题构建的语义层")
    sql_info = Column(Text, index=False, nullable=True, doc="生成的SQL及执行SQL的结果还有其他信息")

    def to_dict(self):
        """序列化方法"""
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "ask_id": self.ask_id,
            "user_id": self.user_id,
            "data": self.data,
            "status": self.status,
            "create_time": self.create_time,
            "update_time": self.update_time,
            "user_question": self.user_question,
            "round_id": self.round_id,
            "processed_semantic_layer": self.processed_semantic_layer,
            "sql_info": self.sql_info
        }


# ===== API环境管理相关表 =====

class ApiEnvironment(BaseModel):
    """API环境表"""
    __tablename__ = "t_ai_api_environments"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    tenant_id = Column(String(32), index=True, nullable=False, doc="租户ID")
    name = Column(String(100), index=True, nullable=False, doc="环境名称")
    description = Column(Text, index=False, nullable=True, doc="环境描述")
    base_url = Column(String(500), index=True, nullable=False, doc="前置URL/基础URL")
    is_default = Column(Boolean, index=True, nullable=False, default=False, doc="是否默认环境")
    is_global = Column(Boolean, index=True, nullable=False, default=False, doc="是否全局环境")
    status = Column(String(1), index=True, nullable=False, default="1", doc="状态(0:禁用,1:启用)")

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "base_url": self.base_url,
            "is_default": self.is_default,
            "is_global": self.is_global,
            "status": self.status,
            "create_time": self.create_time,
            "update_time": self.update_time,
            "create_date": self.create_date,
            "update_date": self.update_date
        }


class ApiEnvironmentVariable(BaseModel):
    """API环境变量表"""
    __tablename__ = "t_ai_api_environment_variables"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    environment_id = Column(String(32), index=True, nullable=False, doc="环境ID")
    key_name = Column(String(100), index=True, nullable=False, doc="变量名")
    key_value = Column(Text, index=False, nullable=False, doc="变量值")
    description = Column(Text, index=False, nullable=True, doc="变量描述")
    is_secret = Column(Boolean, index=True, nullable=False, default=False, doc="是否敏感信息")
    variable_type = Column(String(20), index=True, nullable=False, default="string", doc="变量类型(string,number,boolean)")
    status = Column(String(1), index=True, nullable=False, default="1", doc="状态(0:禁用,1:启用)")

    def to_dict(self):
        return {
            "id": self.id,
            "environment_id": self.environment_id,
            "key_name": self.key_name,
            "key_value": self.key_value,
            "description": self.description,
            "is_secret": self.is_secret,
            "variable_type": self.variable_type,
            "status": self.status,
            "create_time": self.create_time,
            "update_time": self.update_time,
            "create_date": self.create_date,
            "update_date": self.update_date
        }


class GlobalApiEnvironment(BaseModel):
    """全局预设API环境表"""
    __tablename__ = "t_ai_global_api_environments"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    name = Column(String(100), index=True, nullable=False, doc="环境名称")
    description = Column(Text, index=False, nullable=True, doc="环境描述")
    server_url = Column(String(500), index=True, nullable=True, doc="服务器URL")
    variables = Column(JSONB, index=False, nullable=False, default=dict, doc="预设变量")
    is_active = Column(Boolean, index=True, nullable=False, default=True, doc="是否启用")
    status = Column(String(1), index=True, nullable=False, default="1", doc="状态(0:禁用,1:启用)")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "server_url": self.server_url,
            "variables": self.variables,
            "is_active": self.is_active,
            "status": self.status,
            "create_time": self.create_time,
            "update_time": self.update_time,
            "create_date": self.create_date,
            "update_date": self.update_date
        }


# class SensitiveWordCategory(BaseModel):
#     """敏感词分类表"""
#     __tablename__ = "t_ai_sensitive_categories"
#     __table_args__ = {"schema": "usr_ai"}
#
#     id = Column(String(32), primary_key=True, index=False, nullable=False)
#     name = Column(String(128), index=True, nullable=False, doc="分类名称")
#     description = Column(Text, index=False, nullable=True, doc="分类描述")
#     tenant_id = Column(String(32), index=True, nullable=False, doc="租户ID")
#     created_by = Column(String(32), index=True, nullable=False, doc="创建用户ID")
#     status = Column(String(1), index=True, nullable=True, default="1", doc="状态(0:禁用,1:启用)")
#
#     def to_dict(self):
#         return {
#             "id": self.id,
#             "name": self.name,
#             "description": self.description,
#             "tenant_id": self.tenant_id,
#             "created_by": self.created_by,
#             "status": self.status,
#             "create_time": self.create_time,
#             "update_time": self.update_time
#         }
#
#
# class SensitiveWordLevel(BaseModel):
#     """敏感词等级表"""
#     __tablename__ = "t_ai_sensitive_levels"
#     __table_args__ = {"schema": "usr_ai"}
#
#     id = Column(String(32), primary_key=True, index=False, nullable=False)
#     name = Column(String(64), index=True, nullable=False, doc="等级名称")
#     level = Column(Integer, index=True, nullable=False, doc="等级数值(1-5)")
#     description = Column(Text, index=False, nullable=True, doc="等级描述")
#     action = Column(String(32), index=True, nullable=False, default="block", doc="处理动作: block/replace/warn")
#     replacement = Column(String(128), index=False, nullable=True, doc="替换文本")
#     tenant_id = Column(String(32), index=True, nullable=False, doc="租户ID")
#     status = Column(String(1), index=True, nullable=True, default="1", doc="状态(0:禁用,1:启用)")
#
#     def to_dict(self):
#         return {
#             "id": self.id,
#             "name": self.name,
#             "level": self.level,
#             "description": self.description,
#             "action": self.action,
#             "replacement": self.replacement,
#             "tenant_id": self.tenant_id,
#             "status": self.status,
#             "create_time": self.create_time,
#             "update_time": self.update_time
#         }
#
#
# class SensitiveWord(BaseModel):
#     """敏感词表"""
#     __tablename__ = "t_ai_sensitive_words"
#     __table_args__ = {"schema": "usr_ai"}
#
#     id = Column(String(32), primary_key=True, index=False, nullable=False)
#     word = Column(String(255), index=True, nullable=False, doc="敏感词内容")
#     word_hash = Column(String(64), index=True, nullable=False, doc="敏感词MD5哈希")
#     category_id = Column(String(32), index=True, nullable=False, doc="分类ID")
#     level_id = Column(String(32), index=True, nullable=False, doc="等级ID")
#     match_type = Column(String(16), index=True, nullable=False, default="exact", doc="匹配类型: exact/partial/regex")
#     description = Column(Text, index=False, nullable=True, doc="词汇描述")
#     source = Column(String(64), index=True, nullable=True, doc="来源")
#     tenant_id = Column(String(32), index=True, nullable=False, doc="租户ID")
#     created_by = Column(String(32), index=True, nullable=False, doc="创建用户ID")
#     status = Column(String(1), index=True, nullable=True, default="1", doc="状态(0:禁用,1:启用)")
#
#     def to_dict(self):
#         return {
#             "id": self.id,
#             "word": self.word,
#             "category_id": self.category_id,
#             "level_id": self.level_id,
#             "match_type": self.match_type,
#             "description": self.description,
#             "source": self.source,
#             "tenant_id": self.tenant_id,
#             "created_by": self.created_by,
#             "status": self.status,
#             "create_time": self.create_time,
#             "update_time": self.update_time
#         }
#
#
# class SensitiveWordWhitelist(BaseModel):
#     """敏感词白名单表"""
#     __tablename__ = "t_ai_sensitive_whitelists"
#     __table_args__ = {"schema": "usr_ai"}
#
#     id = Column(String(32), primary_key=True, index=False, nullable=False)
#     word = Column(String(255), index=True, nullable=False, doc="白名单词汇")
#     word_hash = Column(String(64), index=True, nullable=False, doc="白名单词汇MD5哈希")
#     reason = Column(Text, index=False, nullable=True, doc="白名单原因")
#     tenant_id = Column(String(32), index=True, nullable=False, doc="租户ID")
#     created_by = Column(String(32), index=True, nullable=False, doc="创建用户ID")
#     status = Column(String(1), index=True, nullable=True, default="1", doc="状态(0:禁用,1:启用)")
#
#     def to_dict(self):
#         return {
#             "id": self.id,
#             "word": self.word,
#             "reason": self.reason,
#             "tenant_id": self.tenant_id,
#             "created_by": self.created_by,
#             "status": self.status,
#             "create_time": self.create_time,
#             "update_time": self.update_time
#         }
#
#
# class SensitiveFilterLog(BaseModel):
#     """敏感词过滤日志表"""
#     __tablename__ = "t_ai_sensitive_filter_logs"
#     __table_args__ = {"schema": "usr_ai"}
#
#     id = Column(String(32), primary_key=True, index=False, nullable=False)
#     user_id = Column(String(32), index=True, nullable=True, doc="用户ID")
#     tenant_id = Column(String(32), index=True, nullable=False, doc="租户ID")
#     content_hash = Column(String(64), index=True, nullable=False, doc="内容MD5哈希")
#     matched_words = Column(JSONB, index=False, nullable=False, default=[], doc="匹配的敏感词")
#     filter_action = Column(String(32), index=True, nullable=False, doc="过滤动作")
#     source_type = Column(String(64), index=True, nullable=True, doc="来源类型")
#     source_id = Column(String(255), index=True, nullable=True, doc="来源ID")
#     ip_address = Column(String(64), index=True, nullable=True, doc="IP地址")
#     user_agent = Column(Text, index=False, nullable=True, doc="用户代理")
#
#     def to_dict(self):
#         return {
#             "id": self.id,
#             "user_id": self.user_id,
#             "tenant_id": self.tenant_id,
#             "content_hash": self.content_hash,
#             "matched_words": self.matched_words,
#             "filter_action": self.filter_action,
#             "source_type": self.source_type,
#             "source_id": self.source_id,
#             "ip_address": self.ip_address,
#             "user_agent": self.user_agent,
#             "create_time": self.create_time,
#             "update_time": self.update_time
#         }
#
#
# class SensitiveFilterStats(BaseModel):
#     """敏感词过滤统计表"""
#     __tablename__ = "t_ai_sensitive_filter_stats"
#     __table_args__ = {"schema": "usr_ai"}
#
#     id = Column(String(32), primary_key=True, index=False, nullable=False)
#     tenant_id = Column(String(32), index=True, nullable=False, doc="租户ID")
#     stat_date = Column(DateTime, index=True, nullable=False, doc="统计日期")
#     total_requests = Column(Integer, index=False, nullable=False, default=0, doc="总请求数")
#     filtered_requests = Column(Integer, index=False, nullable=False, default=0, doc="被过滤请求数")
#     top_matched_words = Column(JSONB, index=False, nullable=False, default=[], doc="热门敏感词")
#     filter_rate = Column(Float, index=False, nullable=False, default=0.0, doc="过滤率")
#
#     def to_dict(self):
#         return {
#             "id": self.id,
#             "tenant_id": self.tenant_id,
#             "stat_date": self.stat_date,
#             "total_requests": self.total_requests,
#             "filtered_requests": self.filtered_requests,
#             "top_matched_words": self.top_matched_words,
#             "filter_rate": self.filter_rate,
#             "create_time": self.create_time,
#             "update_time": self.update_time
#         }


class UserCanvasVersion(BaseModel):
    __tablename__ = "t_ai_user_canvas_version"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    user_canvas_id = Column(String(255), index=True, nullable=False, doc="user_canvas_id")

    title = Column(String(255), index=False, nullable=True, doc="Canvas title")
    description = Column(Text, index=False, nullable=True, doc="Canvas description")
    dsl = Column(JSONB, index=False, nullable=True, default={})


class MCPServer(BaseModel):
    __tablename__ = "t_ai_mcp_server"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    name = Column(String(255), index=True, nullable=False, doc="MCP Server name")
    tenant_id = Column(String(32), index=True, nullable=False)
    url = Column(String(2048), index=False, nullable=False, doc="MCP Server URL")
    server_type = Column(String(32), index=True, nullable=False, doc="MCP Server type")
    description = Column(Text, index=False, nullable=True, doc="MCP Server description")
    variables = Column(JSONB, index=False, nullable=True, default=dict, doc="MCP Server variables")
    headers = Column(JSONB, index=False, nullable=True, default=dict, doc="MCP Server additional request headers")


class ToolsData(BaseModel):
    __tablename__ = "tools_data"
    __table_args__ = {"schema": "usr_ai"}

    flow_id = Column(String(255), primary_key=True, index=True, nullable=False)
    user_id = Column(String(255), primary_key=True, index=True, nullable=False)
    meta_data = Column(Text, nullable=True)
    final_data = Column(Text, nullable=True)


class Search(BaseModel):
    """
    搜索配置表
    """
    __tablename__ = "t_ai_search"
    __table_args__ = {"schema": "usr_ai"}

    id = Column(String(32), primary_key=True, index=False, nullable=False)
    avatar = Column(Text, index=False, nullable=True, doc="avatar base64 string")
    tenant_id = Column(String(32), index=True, nullable=False, doc="租户ID")
    name = Column(String(128), index=True, nullable=False, doc="Search name")
    description = Column(Text, index=False, nullable=True, doc="Search description")
    created_by = Column(String(32), index=True, nullable=False, doc="创建人")
    search_config = Column(JSONB, index=False, nullable=False, default=lambda: {
        "kb_ids": [],
        "doc_ids": [],
        "similarity_threshold": 0.2,
        "vector_similarity_weight": 0.3,
        "use_kg": False,
        # rerank settings
        "rerank_id": "",
        "top_k": 1024,
        # chat settings
        "summary": False,
        "chat_id": "",
        # Leave it here for reference, don't need to set default values
        "llm_setting": {
            # "temperature": 0.1,
            # "top_p": 0.3,
            # "frequency_penalty": 0.7,
            # "presence_penalty": 0.4,
        },
        "chat_settingcross_languages": [],
        "highlight": False,
        "keyword": False,
        "web_search": False,
        "related_search": False,
        "query_mindmap": False,
    }, doc="搜索配置")
    status = Column(String(1), index=True, nullable=True, default="1",
                    doc="是否有效(0: 已删除, 1: 有效)")

    def to_dict(self):
        return {
            "id": self.id,
            "avatar": self.avatar,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "created_by": self.created_by,
            "search_config": self.search_config,
            "status": self.status,
            "create_time": self.create_time,
            "update_time": self.update_time
        }

    def __str__(self):
        return self.name


'''
拥有权限，采用这种方式
'''
# def init_database_tables():
#     # 需要创建的 schema 名称
#     schema_name = 'usr_ai'
#
#     # # 检查并创建 schema
#     # with engine.connect() as connection:
#     #     connection.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema_name}"))
#     #
#     #     connection.execute(text("COMMIT"))  # 提交创建schema的事务
#
#     # 检查 schema 是否存在
#     schema_exists = False
#     try:
#         with engine.connect() as connection:
#             result = connection.execute(text(
#                 "SELECT schema_name FROM information_schema.schemata WHERE schema_name = :schema_name"
#             ), {"schema_name": schema_name})
#             schema_exists = result.fetchone() is not None
#
#         # 如果 schema 不存在，则创建 schema
#         if not schema_exists:
#             logging.info(f"Schema {schema_name} does not exist. Creating schema...")
#             with engine.connect() as connection:
#                 connection.execute(text(f"CREATE SCHEMA {schema_name}"))
#                 connection.execute(text("COMMIT"))
#             logging.info(f"Schema {schema_name} created successfully.")
#         else:
#             logging.info(f"Schema {schema_name} already exists. Skipping schema creation.")
#
#     except OperationalError as e:
#         logging.exception(f"OperationalError while checking or creating schema: {e}")
#         return f"OperationalError: {str(e)}"
#
#     # # 构建相对路径到 alembic.ini 和迁移脚本目录
#     # current_dir = os.path.dirname(__file__)
#     # alembic_ini_path = os.path.join(current_dir, '..', '..', 'configs', 'alembic.ini')
#     # migrations_path = os.path.join(current_dir, '..', '..', 'configs', 'alembic')
#     #
#     # # 执行 Alembic 迁移
#     # alembic_cfg = Config(r"E:\Project\python\study\RAG\configs\alembic.ini")
#     # print("Generated SQLAlchemy URL:", str(engine.url))
#     # alembic_cfg.set_main_option("sqlalchemy.url", str(engine.url))
#     # alembic_cfg.set_main_option("script_location", r"E:\Project\python\study\RAG\configs\alembic")
#     #
#     # try:
#     #     LOGGER.info("Starting Alembic migration...")
#     #     command.upgrade(alembic_cfg, "head")
#     #     LOGGER.info("Alembic migration completed successfully.")
#     # except UnicodeDecodeError as e:
#     #     LOGGER.error(f"UnicodeDecodeError: {e}")
#     #     raise
#     # except Exception as e:
#     #     LOGGER.exception(f"Alembic migration failed: {e}")
#     #     raise
#
#     # 获取现有表列表
#     inspector = sa_inspect(engine)
#     existing_tables = inspector.get_table_names(schema=schema_name)
#     members = inspect.getmembers(sys.modules[__name__], inspect.isclass)
#     table_objs = []
#     create_failed_list = []
#
#     for name, obj in members:
#         if obj != BaseModel and issubclass(obj, BaseModel):
#             table_objs.append(obj)
#             logging.info(f"Start creating table {obj.__name__} in schema {schema_name}")
#             try:
#                 # 检查表是否存在并创建表
#                 if obj.__tablename__ not in existing_tables:
#                     obj.__table__.create(bind=engine, checkfirst=True)
#                     logging.info(f"Successfully created table: {obj.__name__}")
#             except OperationalError as e:
#                 logging.exception(f"Error creating table {obj.__name__}: {e}")
#                 create_failed_list.append(obj.__name__)
#
#     if create_failed_list:
#         logging.error(f"Failed to create tables: {create_failed_list}")
#         raise Exception(f"Failed to create tables: {create_failed_list}")

'''
没有权限，采用这种方式
'''
def init_database_tables():
    # 需要检查的 schema 名称
    schema_name = 'usr_ai'

    # 检查 schema 是否存在
    schema_exists = False
    try:
        with engine.connect() as connection:
            result = connection.execute(text(
                "SELECT schema_name FROM information_schema.schemata WHERE schema_name = :schema_name"
            ), {"schema_name": schema_name})
            schema_exists = result.fetchone() is not None

        # 如果 schema 不存在，则返回报错提示
        if not schema_exists:
            error_msg = f"Schema {schema_name} does not exist. Please ensure the schema is created before proceeding."
            logging.error(error_msg)
            return error_msg
        else:
            logging.info(f"Schema {schema_name} already exists. Continuing with table creation...")

    except OperationalError as e:
        logging.exception(f"OperationalError while checking schema existence: {e}")
        return f"OperationalError: {str(e)}"

    # 获取现有表列表
    inspector = sa_inspect(engine)
    existing_tables = set(inspector.get_table_names(schema=schema_name))  # 使用 set 提高查找效率
    members = inspect.getmembers(sys.modules[__name__], inspect.isclass)
    table_objs = []
    create_failed_list = []

    for name, obj in members:
        if obj != BaseModel and issubclass(obj, BaseModel):
            table_objs.append(obj)
            table_name = obj.__tablename__

            # 检查表是否存在
            if table_name not in existing_tables:
                logging.info(f"Table {table_name} does not exist, creating...")
                try:
                    # 使用更安全的方式创建表
                    # checkfirst=True 确保 SQLAlchemy 再次检查表是否存在
                    obj.__table__.create(bind=engine, checkfirst=True)
                    logging.info(f"Successfully created table: {table_name}")

                    # 更新已存在的表列表，避免后续重复检查
                    existing_tables.add(table_name)

                except OperationalError as e:
                    logging.exception(f"Error creating table {table_name}: {e}")
                    create_failed_list.append(table_name)
                except Exception as e:
                    # 捕获其他可能的异常
                    logging.exception(f"Unexpected error creating table {table_name}: {e}")
                    create_failed_list.append(table_name)
            else:
                logging.debug(f"Table {table_name} already exists, skipping creation")

    if create_failed_list:
        error_msg = f"Failed to create tables: {create_failed_list}"
        logging.error(error_msg)
        raise Exception(error_msg)

    logging.info("Database table initialization completed successfully")
    return "Success"

def upgrade_database_tables():
    logging.info("开始执行数据库结构升级...")

    # 获取项目根目录
    # 注意：此处假设db_models.py位于api/db目录下，根据实际项目结构调整
    current_dir = os.path.dirname(os.path.abspath(__file__))
    base_dir = os.path.abspath(os.path.join(current_dir, "..", ".."))

    # Alembic配置文件路径
    alembic_ini_path = os.path.join(base_dir, "alembic.ini")
    alembic_path = os.path.join(base_dir, "configs", "alembic")

    if not os.path.exists(alembic_ini_path):
        error_msg = f"Alembic配置文件不存在: {alembic_ini_path}"
        logging.error(error_msg)
        return error_msg

    try:
        # 创建Alembic配置对象
        alembic_cfg = Config(alembic_ini_path)

        # 更新SQLAlchemy URL
        alembic_cfg.set_main_option("sqlalchemy.url", DATABASE_URL)
        alembic_cfg.set_main_option("script_location", alembic_path)
        # 获取数据库连接
        with engine.connect() as connection:
            # 检查schema是否存在
            schema_name = 'usr_ai'

            # 获取当前数据库版本
            context = MigrationContext.configure(connection, opts={"version_table_schema": schema_name})
            current_rev = context.get_current_revision()

            # 获取最新迁移版本
            script_directory = ScriptDirectory.from_config(alembic_cfg)
            head_rev = script_directory.get_current_head()

            if current_rev == head_rev:
                logging.info(f"数据库已经是最新版本: {current_rev}")
                return "数据库已经是最新版本"

            logging.info(f"当前数据库版本: {current_rev or '无版本'}")
            logging.info(f"目标数据库版本: {head_rev or '无版本'}")

            # 执行迁移升级到最新版本
            logging.info("开始执行数据库迁移...")
            command.upgrade(alembic_cfg, "head")

            logging.info("数据库迁移成功完成")
            return "数据库迁移成功完成"

    except Exception as e:
        error_msg = f"数据库迁移过程中发生错误: {str(e)}"
        logging.error(error_msg)
        import traceback
        logging.error(traceback.format_exc())
        return error_msg


def with_retry(max_retries=3, retry_delay=1.0):
    """为数据库操作添加重试机制

    Args:
        max_retries (int): 最大重试次数
        retry_delay (float): 初始重试延迟(秒)，将指数增长

    Returns:
        装饰后的函数
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for retry in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    # 获取self和方法名用于日志记录
                    self_obj = args[0] if args else None
                    func_name = func.__name__
                    lock_name = getattr(self_obj, 'lock_name', 'unknown') if self_obj else 'unknown'

                    if retry < max_retries - 1:
                        current_delay = retry_delay * (2 ** retry)
                        logging.warning(f"{func_name} {lock_name} 失败: {str(e)}, 重试中 ({retry + 1}/{max_retries})")
                        time.sleep(current_delay)
                    else:
                        logging.error(f"{func_name} {lock_name} 在所有尝试后失败: {str(e)}")

            if last_exception:
                raise last_exception
            return False

        return wrapper

    return decorator


class PostgreSQLDatabaseLock:
    """PostgreSQL 数据库锁实现"""

    def __init__(self, session: Session, lock_name: str, timeout: int = 10):
        """初始化 PostgreSQL 锁

        Args:
            session: SQLAlchemy 会话对象
            lock_name: 锁名称
            timeout: 获取锁的超时时间(秒)
        """
        self.session = session
        self.lock_name = lock_name
        self.lock_id = int(hashlib.md5(lock_name.encode()).hexdigest(), 16) % (2 ** 31 - 1)
        self.timeout = int(timeout)

    @with_retry(max_retries=3, retry_delay=1.0)
    def lock(self):
        """获取锁

        Returns:
            bool: 成功时返回 True

        Raises:
            Exception: 获取锁失败时抛出异常
        """
        result = self.session.execute(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": self.lock_id}
        )
        row = result.fetchone()

        if not row or row[0] == 0:
            raise Exception(f"获取 PostgreSQL 锁 {self.lock_name} 超时")
        elif row[0] == 1:
            return True
        else:
            raise Exception(f"获取锁 {self.lock_name} 失败")

    @with_retry(max_retries=3, retry_delay=1.0)
    def unlock(self):
        """释放锁

        Returns:
            bool: 成功时返回 True

        Raises:
            Exception: 释放锁失败时抛出异常
        """
        result = self.session.execute(
            text("SELECT pg_advisory_unlock(:lock_id)"),
            {"lock_id": self.lock_id}
        )
        row = result.fetchone()

        if not row or row[0] == 0:
            raise Exception(f"PostgreSQL 锁 {self.lock_name} 不是由当前会话持有")
        elif row[0] == 1:
            return True
        else:
            raise Exception(f"PostgreSQL 锁 {self.lock_name} 不存在")

    def __enter__(self):
        """上下文管理器入口"""
        self.lock()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器退出"""
        self.unlock()

    def __call__(self, func):
        """使类实例可作为装饰器使用"""

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with self:
                return func(*args, **kwargs)

        return wrapper


class MySQLDatabaseLock:
    """MySQL 数据库锁实现"""

    def __init__(self, session: Session, lock_name: str, timeout: int = 10):
        """初始化 MySQL 锁

        Args:
            session: SQLAlchemy 会话对象
            lock_name: 锁名称
            timeout: 获取锁的超时时间(秒)
        """
        self.session = session
        self.lock_name = lock_name
        self.timeout = int(timeout)

    @with_retry(max_retries=3, retry_delay=1.0)
    def lock(self):
        """获取锁

        Returns:
            bool: 成功时返回 True

        Raises:
            Exception: 获取锁失败时抛出异常
        """
        result = self.session.execute(
            text("SELECT GET_LOCK(:lock_name, :timeout)"),
            {"lock_name": self.lock_name, "timeout": self.timeout}
        )
        row = result.fetchone()

        if not row or row[0] is None:
            raise Exception(f"获取 MySQL 锁 {self.lock_name} 出错")
        elif row[0] == 0:
            raise Exception(f"获取 MySQL 锁 {self.lock_name} 超时")
        elif row[0] == 1:
            return True
        else:
            raise Exception(f"获取锁 {self.lock_name} 失败")

    @with_retry(max_retries=3, retry_delay=1.0)
    def unlock(self):
        """释放锁

        Returns:
            bool: 成功时返回 True

        Raises:
            Exception: 释放锁失败时抛出异常
        """
        result = self.session.execute(
            text("SELECT RELEASE_LOCK(:lock_name)"),
            {"lock_name": self.lock_name}
        )
        row = result.fetchone()

        if not row or row[0] is None:
            raise Exception(f"释放 MySQL 锁 {self.lock_name} 出错")
        elif row[0] == 0:
            raise Exception(f"MySQL 锁 {self.lock_name} 不是由当前会话持有")
        elif row[0] == 1:
            return True
        else:
            raise Exception(f"MySQL 锁 {self.lock_name} 不存在")

    def __enter__(self):
        """上下文管理器入口"""
        self.lock()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器退出"""
        self.unlock()

    def __call__(self, func):
        """使类实例可作为装饰器使用"""

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with self:
                return func(*args, **kwargs)

        return wrapper


class SQLiteDatabaseLock:
    """SQLite 数据库锁实现 (使用表锁模拟)"""

    def __init__(self, session: Session, lock_name: str, timeout: int = 10):
        """初始化 SQLite 锁

        Args:
            session: SQLAlchemy 会话对象
            lock_name: 锁名称
            timeout: 获取锁的超时时间(秒)
        """
        self.session = session
        self.lock_name = lock_name
        self.timeout = int(timeout)
        self._ensure_lock_table()

    def _ensure_lock_table(self):
        """确保锁表存在"""
        try:
            self.session.execute(text("""
                                      CREATE TABLE IF NOT EXISTS advisory_locks
                                      (
                                          lock_name
                                          TEXT
                                          PRIMARY
                                          KEY,
                                          session_id
                                          TEXT,
                                          created_at
                                          TIMESTAMP
                                          DEFAULT
                                          CURRENT_TIMESTAMP
                                      )
                                      """))
            self.session.commit()
        except SQLAlchemyError:
            self.session.rollback()
            # 如果表已存在，忽略错误

    @with_retry(max_retries=3, retry_delay=1.0)
    def lock(self):
        """获取锁"""
        session_id = str(id(self.session))
        start_time = time.time()

        while time.time() - start_time < self.timeout:
            try:
                self.session.execute(
                    text("INSERT INTO advisory_locks (lock_name, session_id) VALUES (:lock_name, :session_id)"),
                    {"lock_name": self.lock_name, "session_id": session_id}
                )
                self.session.commit()
                return True
            except SQLAlchemyError:
                # 锁已被获取，回滚并等待重试
                self.session.rollback()
                time.sleep(0.5)

        raise Exception(f"获取 SQLite 锁 {self.lock_name} 超时")

    @with_retry(max_retries=3, retry_delay=1.0)
    def unlock(self):
        """释放锁"""
        session_id = str(id(self.session))
        try:
            result = self.session.execute(
                text("DELETE FROM advisory_locks WHERE lock_name = :lock_name AND session_id = :session_id"),
                {"lock_name": self.lock_name, "session_id": session_id}
            )
            self.session.commit()

            if result.rowcount == 0:
                # 没有删除任何行，说明锁不是由当前会话持有
                raise Exception(f"SQLite 锁 {self.lock_name} 不是由当前会话持有")

            return True
        except SQLAlchemyError as e:
            self.session.rollback()
            raise Exception(f"释放 SQLite 锁失败: {str(e)}")

    def __enter__(self):
        """上下文管理器入口"""
        self.lock()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """上下文管理器退出"""
        self.unlock()

    def __call__(self, func):
        """使类实例可作为装饰器使用"""

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            with self:
                return func(*args, **kwargs)

        return wrapper


class DatabaseLock:
    """数据库锁工厂类，根据数据库类型创建合适的锁实现"""

    @staticmethod
    def create(session: Session, lock_name: str, timeout: int = 10, db_type: str = None):
        """创建合适的数据库锁实例

        Args:
            session: SQLAlchemy 会话对象
            lock_name: 锁名称
            timeout: 获取锁超时时间(秒)
            db_type: 数据库类型 ('postgresql', 'mysql', 'sqlite' 或 None 自动检测)

        Returns:
            对应数据库类型的锁对象
        """
        if db_type is None:
            # 自动检测数据库类型
            db_type = session.bind.dialect.name.lower()

        if db_type.lower() == 'postgresql':
            return PostgreSQLDatabaseLock(session, lock_name, timeout)
        elif db_type.lower() == 'mysql':
            return MySQLDatabaseLock(session, lock_name, timeout)
        elif db_type.lower() == 'sqlite':
            return SQLiteDatabaseLock(session, lock_name, timeout)
        else:
            # 对于其他数据库类型，使用 SQLite 的模拟实现
            logging.warning(f"未知数据库类型 {db_type}，使用基于表的锁实现")
            return SQLiteDatabaseLock(session, lock_name, timeout)


# 辅助函数 - 创建任务锁名称
def task_lock_name(task_id: str, operation: str = "update") -> str:
    """为任务生成锁名称

    Args:
        task_id: 任务ID
        operation: 操作类型

    Returns:
        格式化的锁名称
    """
    return f"{operation}_task_{task_id}"


# 装饰器工厂函数 - 创建用于保护函数的锁装饰器
def with_advisory_lock(lock_name_template: str, timeout: int = 10):
    """创建一个数据库锁装饰器

    Args:
        lock_name_template: 锁名称模板，使用 {} 作为参数占位符
        timeout: 获取锁的超时时间(秒)

    Returns:
        装饰器函数

    示例:
        @with_advisory_lock("update_progress_{}")
        def update_progress(db: Session, task_id: str, info: dict):
            # 函数内容...
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(db: Session, id: str, *args, **kwargs):
            # 格式化锁名称
            lock_name = lock_name_template.format(id)

            # 创建并使用锁
            with DatabaseLock.create(db, lock_name, timeout):
                return func(db, id, *args, **kwargs)

        return wrapper

    return decorator