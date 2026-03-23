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
import sqlalchemy as sa
from sqlalchemy import create_engine, String, DateTime, BigInteger, event, Integer, Float, Boolean, Text, text, JSON, select
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker, Session, object_session, DeclarativeBase, Mapped, mapped_column
from sqlalchemy.exc import OperationalError, DisconnectionError, SQLAlchemyError
from sqlalchemy.inspection import inspect as sa_inspect
from sqlalchemy.orm.attributes import get_history
from sqlalchemy.dialects.postgresql import JSONB
from typing import Any
import uuid
from datetime import datetime, timezone
import time
import functools
import hashlib
from alembic.config import Config
from alembic import command
from alembic.script import ScriptDirectory
from alembic.runtime.migration import MigrationContext

# from common.time_utils import current_timestamp, timestamp_to_date, date_string_to_timestamp
from common.config_utils import decrypt_database_config
from common.constants import ParserType

DATABASE_TYPE = os.getenv("DB_TYPE", 'postgresql')
DATABASE = decrypt_database_config(name=DATABASE_TYPE)
database_config = DATABASE

def build_database_url(db_config: dict[str, Any]) -> str:
    """
    使用 SQLAlchemy URL.create 构建连接串，避免手动拼接导致特殊字符解析错误。
    """
    drivername = str(db_config.get("name", "postgresql")).strip()
    drivername_lower = drivername.lower()

    if drivername_lower.startswith("sqlite"):
        sqlite_db = db_config.get("dbname") or db_config.get("database") or ":memory:"
        return URL.create(drivername="sqlite+pysqlite", database=str(sqlite_db)).render_as_string(
            hide_password=False
        )

    # OceanBase is MySQL-compatible; map to mysql+pymysql driver
    if drivername_lower == "oceanbase":
        drivername = "mysql+pymysql"
        drivername_lower = "mysql+pymysql"

    database = db_config.get("dbname") or db_config.get("database")
    if not database and drivername_lower.startswith("postgresql"):
        database = "postgres"

    port = db_config.get("port")
    if isinstance(port, str):
        port = port.strip() or None
    return URL.create(
        drivername=drivername,
        username=db_config.get("user"),
        password=db_config.get("password"),
        host=db_config.get("host"),
        port=int(port) if port is not None else None,
        database=database,
    ).render_as_string(hide_password=False)


DATABASE_URL = build_database_url(database_config)


def get_engine_config(db_config: dict) -> dict:
    """
    将配置文件中的数据库配置映射到 SQLAlchemy 引擎配置

    优化的连接池配置：
    - pool_pre_ping: 使用前自动检测连接是否存活（最关键）
    - pool_recycle: 定期回收连接，防止超过数据库超时时间
    - connect_args: 数据库特定的连接参数

    Args:
        db_config: 数据库配置字典

    Returns:
        SQLAlchemy 引擎配置字典
    """
    max_connections = db_config.get('max_connections', 100)
    stale_timeout = db_config.get('stale_timeout', 30)

    # 基础连接池配置
    engine_config = {
        # 连接池大小配置
        'pool_size': max_connections,              # 常驻连接数
        'max_overflow': max_connections // 2,      # 最大溢出连接数
        'pool_timeout': stale_timeout,             # 获取连接的超时时间（秒）

        # 连接健康检查（关键配置）
        'pool_pre_ping': True,                     # 使用前自动ping检测连接

        # 连接回收配置
        'pool_recycle': min(stale_timeout * 60, 1800),  # 30分钟或配置值，防止连接超时

        # 日志配置
        'echo': False,                             # 生产环境关闭SQL日志
        'echo_pool': False,                        # 关闭连接池日志
    }

    # 根据数据库类型添加特定连接参数
    db_type = db_config.get('name', 'postgresql').lower()

    if db_type in ('mysql', 'oceanbase'):
        # MySQL/OceanBase 特定配置（OceanBase 兼容 MySQL 协议）
        engine_config['connect_args'] = {
            'connect_timeout': 10,      # 连接超时（秒）
            'read_timeout': 30,         # 读取超时（秒）
            'write_timeout': 30,        # 写入超时（秒）
            'charset': 'utf8mb4',
        }
    elif db_type == 'postgresql':
        # PostgreSQL 特定配置
        engine_config['connect_args'] = {
            'connect_timeout': 10,      # 连接超时（秒）
            'options': '-c statement_timeout=30000',  # 30秒语句超时
        }

    return engine_config


engine_config = get_engine_config(database_config)
engine_kwargs = dict(engine_config)
if str(database_config.get("name", "")).lower().startswith("postgresql"):
    engine_kwargs["client_encoding"] = "utf8"

engine = create_engine(DATABASE_URL, **engine_kwargs)


# ==================== 连接池事件监听器 ====================
# 用于监控和记录连接池状态

@event.listens_for(engine, "connect")
def receive_connect(dbapi_conn, connection_record):
    """
    连接建立时触发

    用于：
    - 记录新连接的创建
    - 设置连接级别的参数
    """
    connection_record.info['pid'] = os.getpid()
    logging.debug(f"[连接池] 新数据库连接已建立 | 进程PID: {os.getpid()}")


@event.listens_for(engine, "checkout")
def receive_checkout(dbapi_conn, connection_record, connection_proxy):
    """
    从连接池取出连接时触发

    用于：
    - 验证连接的有效性（pool_pre_ping 会在这之前自动检查）
    - 记录连接使用情况
    """
    pid = connection_record.info.get('pid')
    if pid != os.getpid():
        # 检测连接是否在不同进程中使用（多进程场景）
        logging.warning(
            f"[连接池] 连接跨进程使用 | 创建进程: {pid}, 当前进程: {os.getpid()}"
        )
        # ⚠️ 关键：跨进程复用连接会导致连接异常/莫名断开。
        # 使用 SQLAlchemy 官方推荐的 invalidate() 方法使连接失效，
        # 连接池会自动丢弃该连接并新建连接（自愈机制）
        connection_record.invalidate()
        raise DisconnectionError(
            f"DB connection belongs to PID {pid}, cannot be used in PID {os.getpid()}"
        )


@event.listens_for(engine, "checkin")
def receive_checkin(dbapi_conn, connection_record):
    """
    连接归还到池中时触发

    用于：
    - 清理连接状态
    - 确保连接处于干净状态
    """
    # 确保连接归还时没有未提交的事务
    try:
        if dbapi_conn.in_transaction:
            dbapi_conn.rollback()
            logging.warning("[连接池] 连接归还时存在未提交事务，已自动回滚")
    except Exception as e:
        logging.debug(f"[连接池] 连接状态检查: {e}")


@event.listens_for(engine, "close")
def receive_close(dbapi_conn, connection_record):
    """
    连接关闭时触发

    用于：
    - 记录连接关闭事件
    - 清理资源
    """
    logging.debug(f"[连接池] 数据库连接已关闭 | 进程PID: {os.getpid()}")


SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    expire_on_commit=False,
    bind=engine
)

class Base(DeclarativeBase):
    """SQLAlchemy 2.0 风格的声明式基类"""
    pass

from contextlib import contextmanager


@contextmanager
def db_connection():
    """
    提供数据库连接的上下文管理器（用于手动管理事务）

    特点：
    - 不自动提交，需要手动调用 db.commit()
    - 异常时自动回滚
    - 保证连接关闭

    用法:
        with db_connection() as db:
            user = User(name="test")
            db.add(user)
            db.commit()  # 手动提交
    """
    db = SessionLocal()
    try:
        yield db
    except Exception as e:
        db.rollback()
        logging.error(f"[数据库] 事务执行失败，已回滚: {e}")
        raise
    finally:
        db.close()


def get_db():
    """
    FastAPI 依赖注入：提供数据库会话

    特点：
    - 不自动提交，由 Service 层手动控制事务
    - 异常时自动回滚
    - 保证连接关闭
    - 配合 pool_pre_ping 和 tenacity 重试实现高可用

    用法:
        @router.post('/create')
        def create_user(
            request: CreateUserRequest,
            db: Session = Depends(get_db)
        ):
            # Service 层方法内部会调用 db.commit()
            user = UserService.save(db, **request.dict())
            return {"code": 0, "data": user}
    """
    db = SessionLocal()
    try:
        yield db
    except Exception as e:
        db.rollback()
        logging.debug(f"[数据库] 请求异常，事务已回滚: {type(e).__name__}")
        raise
    finally:
        db.close()


# ==================== 连接池状态监控工具 ====================

def get_pool_status() -> dict:
    """
    获取数据库连接池的状态信息

    Returns:
        dict: 包含连接池状态的字典
            - pool_size: 连接池大小（配置的常驻连接数）
            - checked_out: 当前被取出使用的连接数
            - overflow: 溢出连接数（超出 pool_size 的连接）
            - checked_in: 池中可用的空闲连接数
            - total_connections: 总连接数
            - usage_rate: 连接池使用率（百分比）

    用法:
        # 在健康检查接口中使用
        @router.get('/health/db')
        def db_health():
            status = get_pool_status()
            if status['usage_rate'] > 80:
                return {"status": "warning", "data": status}
            return {"status": "healthy", "data": status}
    """
    pool = engine.pool

    pool_size = pool.size()
    checked_out = pool.checkedout()
    overflow = pool.overflow()
    checked_in = pool.checkedin()
    total = checked_out + checked_in

    # 计算使用率
    max_connections = pool_size + (pool._max_overflow if hasattr(pool, '_max_overflow') else 0)
    usage_rate = (checked_out / max_connections * 100) if max_connections > 0 else 0

    return {
        'pool_size': pool_size,           # 连接池大小
        'checked_out': checked_out,       # 已取出连接数
        'overflow': overflow,             # 溢出连接数
        'checked_in': checked_in,         # 池中空闲连接数
        'total_connections': total,       # 总连接数
        'usage_rate': round(usage_rate, 2),  # 使用率（%）
        'status': 'warning' if usage_rate > 80 else 'healthy'
    }


def check_db_connection() -> bool:
    """
    检查数据库连接是否正常

    Returns:
        bool: True 表示连接正常，False 表示连接异常

    用法:
        if not check_db_connection():
            logging.error("数据库连接异常！")
    """
    try:
        with db_connection() as db:
            db.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logging.error(f"[数据库] 连接检查失败: {e}")
        return False


def close_stale_connections(age: int = 30):
    """
    关闭池中超过指定时间的空闲连接

    Args:
        age: 空闲时间阈值（秒）

    注意：
        SQLAlchemy 的 pool_recycle 参数已经自动处理过期连接
        这个函数主要用于手动触发清理

    用法:
        # 可以在定时任务中调用
        close_stale_connections(age=60)  # 关闭60秒以上的空闲连接
    """
    try:
        # 清理连接池中的过期连接
        engine.dispose()
        logging.info(f"[连接池] 已清理超过 {age} 秒的空闲连接")
    except Exception as e:
        logging.error(f"[连接池] 清理空闲连接失败: {e}")


# 保留原代码中的常量和辅助函数
CONTINUOUS_FIELD_TYPE = {int, float, datetime}
AUTO_DATE_TIMESTAMP_FIELD_PREFIX = {
    "create",
    "start",
    "end",
    "update",
    "read_access",
    "write_access"}


def is_continuous_field(cls: type[Any]) -> bool:
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
    """获取所有时间戳字段名称集合，如 {'create_time', 'update_time', ...}"""
    return {f"{f}_time" for f in AUTO_DATE_TIMESTAMP_FIELD_PREFIX}


def auto_date_timestamp_date_field():
    """获取所有日期字段名称集合，如 {'create_date', 'update_date', ...}"""
    return {f"{f}_date" for f in AUTO_DATE_TIMESTAMP_FIELD_PREFIX}


def timestamp_to_datetime(ts: int) -> datetime:
    """将毫秒时间戳转换为 UTC datetime 对象"""
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)


def get_utc_now():
    """获取当前UTC时间"""
    return datetime.now(timezone.utc)


def get_timestamp_ms():
    """获取当前UTC时间的毫秒时间戳"""
    return int(get_utc_now().timestamp() * 1000)


# ==================== 批量更新数据规范化 ====================
# 类似 Peewee BaseModel._normalize_data 的功能
# 提供统一的数据规范化函数，供 CommonService 使用（SQLAlchemy 2.0 风格）


def normalize_update_data(model, values: dict) -> dict:
    """
    规范化批量更新的数据，自动注入时间戳字段。

    模拟 Peewee BaseModel._normalize_data 的行为：
    1. 自动设置 update_time 为当前时间戳
    2. 自动同步 *_time 和 *_date 字段（如果 *_time 被设置，自动设置对应的 *_date）

    参数:
        model: SQLAlchemy 模型类
        values: 更新值字典

    返回:
        规范化后的更新值字典（新字典，不修改原 values）

    使用示例（SQLAlchemy 2.0 风格）:
        from sqlalchemy import update

        values = normalize_update_data(Knowledgebase, {
            Knowledgebase.chunk_num: Knowledgebase.chunk_num + 10
        })
        stmt = update(Knowledgebase).where(Knowledgebase.id == kb_id).values(values)
        db.execute(stmt)
    """
    if not values:
        return values

    # 创建新字典，避免修改原 values
    result = dict(values)

    now = get_utc_now()
    now_ts = get_timestamp_ms()

    # 1. 自动设置 update_time（如果未显式设置）
    if hasattr(model, 'update_time'):
        if 'update_time' not in result and model.update_time not in result:
            result[model.update_time] = now_ts

    # 2. 自动同步 *_time 和 *_date 字段
    for prefix in AUTO_DATE_TIMESTAMP_FIELD_PREFIX:
        time_field = f"{prefix}_time"
        date_field = f"{prefix}_date"

        # 检查模型是否同时具有 *_time 和 *_date 字段
        if hasattr(model, time_field) and hasattr(model, date_field):
            time_attr = getattr(model, time_field)
            date_attr = getattr(model, date_field)

            # 获取 *_time 的值（可能以字符串或属性对象形式存在于 result 中）
            time_value = result.get(time_field) or result.get(time_attr)

            if time_value is not None:
                # 如果 *_date 未设置，自动从 *_time 同步
                if date_field not in result and date_attr not in result:
                    if isinstance(time_value, int):
                        result[date_attr] = timestamp_to_datetime(time_value)
                    elif isinstance(time_value, datetime):
                        result[date_attr] = time_value

    return result


class BaseModel(Base):
    __abstract__ = True

    id: Mapped[str] = mapped_column(String, primary_key=False, nullable=False, index=True, default=lambda: str(uuid.uuid4()))

    # 日期时间对象
    create_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True, default=get_utc_now)
    # 注意：不使用 onupdate，时间戳更新由 before_update 事件监听器控制
    # 这样可以只在有真正变更时才更新时间戳
    update_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True, default=get_utc_now)

    # 毫秒时间戳
    create_time: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True, default=get_timestamp_ms)
    # 注意：不使用 onupdate，时间戳更新由 before_update 事件监听器控制
    update_time: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True, default=get_timestamp_ms)

    def to_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}

    @classmethod
    def query(cls, reverse=None, order_by=None, **kwargs):
        """SQLAlchemy 2.0 风格的查询方法"""
        session = SessionLocal()
        try:
            stmt = select(cls)
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
                stmt = stmt.where(*filters)

            if order_by:
                order_column = getattr(cls, order_by, None)
                if order_column is not None:
                    if reverse:
                        stmt = stmt.order_by(order_column.desc())
                    else:
                        stmt = stmt.order_by(order_column.asc())
                else:
                    stmt = stmt.order_by(cls.create_time.desc() if reverse else cls.create_time.asc())

            return session.scalars(stmt).all()
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
    """在更新前同步更新时间字段

    关键改进：只有在对象真正有变更（除了时间字段外的其他字段）时才更新时间戳。
    这避免了以下问题：
    1. JSONB 字段被访问时可能导致对象被误判为 dirty
    2. 同一 session 中的批量操作导致所有加载的对象时间戳被同时更新

    注意：JSONB/JSON 字段使用深度比较来判断是否真正变更，因为 SQLAlchemy
    对可变类型的变更检测不可靠。
    """
    session = object_session(target)
    if session is None:
        return

    # 检查是否有真正的变更（排除时间戳字段本身）
    time_fields = {'update_time', 'update_date', 'create_time', 'create_date'}
    has_real_changes = False

    # 获取 mapper 中定义的所有列属性
    for attr in mapper.column_attrs:
        attr_name = attr.key
        # 跳过时间戳字段
        if attr_name in time_fields:
            continue

        # 获取列类型
        column = attr.columns[0]
        column_type = type(column.type)

        # 对于 JSONB/JSON 字段，使用深度比较而不是 get_history
        # 因为 SQLAlchemy 对可变类型的变更检测不可靠
        if column_type in (JSONB, JSON):
            history = get_history(target, attr_name)
            if history.has_changes():
                # 进行深度比较：比较旧值和新值是否真的不同
                old_value = history.deleted[0] if history.deleted else None
                new_value = getattr(target, attr_name)
                # 只有当值真的不同时才认为是变更
                if old_value != new_value:
                    has_real_changes = True
                    break
        else:
            # 非 JSONB 字段，使用标准的 get_history
            history = get_history(target, attr_name)
            if history.has_changes():
                has_real_changes = True
                break

    # 只有在有真正变更时才更新时间戳
    if has_real_changes:
        now = get_utc_now()
        timestamp = int(now.timestamp() * 1000)
        target.update_date = now
        target.update_time = timestamp


class User(BaseModel):
    __tablename__ = "t_ai_users"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    access_token: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    nickname: Mapped[str] = mapped_column(String(100), index=True, nullable=False)
    password: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    avatar: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="avatar base64 string")
    language: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, default="English")
    color_schema: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, default="Bright")
    timezone: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True, default="UTC+8\tAsia/Shanghai")
    last_login_time: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    is_authenticated: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=True)
    is_anonymous: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=False)
    login_channel: Mapped[str | None] = mapped_column(String, index=True, nullable=True, default=None)
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1")
    is_superuser: Mapped[bool | None] = mapped_column(Boolean, index=True, nullable=True, default=False)

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

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    name: Mapped[str | None] = mapped_column(String(100), index=True, nullable=True, doc="Tenant name")
    public_key: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    llm_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, doc="default llm ID")
    embd_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, doc="default embedding model ID")
    asr_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, doc="default ASR model ID")
    img2txt_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, doc="default image to text model ID")
    rerank_id: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True, doc="default rerank model ID")
    tts_id: Mapped[str | None] = mapped_column(String(256), index=True, nullable=True, doc="default tts model ID")
    parser_ids: Mapped[str] = mapped_column(String(256), index=True, nullable=False, doc="document processors")
    credit: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=512)
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")

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

    id: Mapped[str] = mapped_column(String(128), primary_key=True, index=False, nullable=False)
    user_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    role: Mapped[str] = mapped_column(String(128), index=True, nullable=False, doc="UserTenantRole")
    invited_by: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1")

    def to_dict(self):
        return {
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "role": self.role
        }


class LLMFactories(BaseModel):
    __tablename__ = "t_ai_llm_factories"
    __table_args__ = {"schema": "usr_ai"}

    name: Mapped[str] = mapped_column(String(128), primary_key=True, index=False, nullable=False, doc="LLM factory name")
    logo: Mapped[str | None] = mapped_column(Text, index=False, nullable=True)
    tags: Mapped[str] = mapped_column(String(255), index=True, nullable=False, doc="LLM, Text Embedding, Image2Text, ASR")
    rank: Mapped[int | None] = mapped_column(Integer, index=False, default=0)
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")


class LLM(BaseModel):
    __tablename__ = "t_ai_llms"
    __table_args__ = {"schema": "usr_ai"}

    fid: Mapped[str] = mapped_column(String(128), primary_key=True, index=True, nullable=False, doc="LLM factory id")
    llm_name: Mapped[str] = mapped_column(String(128), primary_key=True, index=True, nullable=False)
    mdl_type: Mapped[str] = mapped_column(String(128), index=True, nullable=False, doc="LLM, Text Embedding, Image2Text, ASR")
    max_tokens: Mapped[int] = mapped_column(BigInteger, index=False, nullable=False, default=0)
    tags: Mapped[str] = mapped_column(String(255), index=True, nullable=False, doc="LLM, Text Embedding, Image2Text, Chat, 32k...")
    is_tools: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=False, doc="support tools")
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")


class TenantLLM(BaseModel):
    __tablename__ = "t_ai_tenant_llms"
    __table_args__ = (
        sa.UniqueConstraint("tenant_id", "llm_factory", "llm_name", name="idx_tenant_llm_unique"),
        {"schema": "usr_ai"},
    )

    # The database migrates this table to an integer identity primary key.
    # Keep the ORM aligned so inserts don't send UUID strings into BIGINT columns.
    id: Mapped[int | None] = mapped_column(BigInteger, primary_key=True, autoincrement=True, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=False, index=True, nullable=False)
    llm_factory: Mapped[str] = mapped_column(String(128), primary_key=False, index=True, nullable=False, doc="LLM factory name")
    mdl_type: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True, doc="LLM, Text Embedding, Image2Text, ASR")
    llm_name: Mapped[str | None] = mapped_column(String(128), primary_key=False, index=True, nullable=True)
    api_key: Mapped[str | None] = mapped_column(Text, nullable=True, doc="API KEY")
    api_base: Mapped[str | None] = mapped_column(String(255), index=False, nullable=True)
    max_tokens: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=8192)
    used_tokens: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(1), index=True, nullable=False, default="1", doc="is it validate(0: wasted, 1: validate)")


class TenantLangfuse(BaseModel):
    __tablename__ = "t_ai_tenant_langfuse"
    __table_args__ = {"schema": "usr_ai"}

    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True, index=True, nullable=False)
    secret_key: Mapped[str] = mapped_column(String(2048), index=True, nullable=False, doc="SECRET KEY")
    public_key: Mapped[str] = mapped_column(String(2048), index=True, nullable=False, doc="PUBLIC KEY")
    host: Mapped[str] = mapped_column(String(128), index=True, nullable=False, doc="HOST")

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

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    code: Mapped[str] = mapped_column(String(128), index=True, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True)
    service_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    enabled_dimensions: Mapped[list] = mapped_column(JSONB, index=False, nullable=False, default=list)
    enabled_labels: Mapped[list] = mapped_column(JSONB, index=False, nullable=False, default=list)
    policy_config: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=dict)
    cache_enabled: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=True)
    timeout_ms: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=1000)
    total_requests: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    blocked_requests: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1")


class GuardServiceLibrary(BaseModel):
    __tablename__ = "t_guard_service_libraries"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    service_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    library_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    library_type: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    apply_to_dimensions: Mapped[list] = mapped_column(JSONB, index=False, nullable=False, default=list)
    apply_to_labels: Mapped[list] = mapped_column(JSONB, index=False, nullable=False, default=list)
    config: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=dict)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1")


class GuardRule(BaseModel):
    __tablename__ = "t_guard_rules"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    label_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    rule_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    content: Mapped[str] = mapped_column(Text, index=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    match_mode: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    case_sensitive: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=False)
    config: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=dict)
    weight: Mapped[float] = mapped_column(Float, index=True, nullable=False, default=0.0)
    priority: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    source: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1")


class GuardLog(BaseModel):
    __tablename__ = "t_guard_logs"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    service_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    service_code: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    content: Mapped[str | None] = mapped_column(Text, index=False, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    content_length: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    content_preview: Mapped[str | None] = mapped_column(String(500), index=False, nullable=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=False)
    risk_score: Mapped[float] = mapped_column(Float, index=True, nullable=False, default=0.0)
    content_risk_level: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True)
    content_results: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=dict)
    sensitive_level: Mapped[str | None] = mapped_column(String(8), index=True, nullable=True)
    sensitive_results: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=dict)
    attack_level: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True)
    attack_results: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=dict)
    customized_hits: Mapped[list] = mapped_column(JSONB, index=False, nullable=False, default=list)
    risk_words: Mapped[list] = mapped_column(JSONB, index=False, nullable=False, default=list)
    sensitive_data: Mapped[list] = mapped_column(JSONB, index=False, nullable=False, default=list)
    action_taken: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    action_detail: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=dict)
    source_type: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    client_ip: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text, index=False, nullable=True)
    process_time_ms: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    cloud_service_used: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=False)


class GuardLibraryItem(BaseModel):
    __tablename__ = "t_guard_library_items"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    library_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    content: Mapped[str] = mapped_column(Text, index=True, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    content_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    item_metadata: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=dict)
    hit_count: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    last_hit_time: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1")


class GuardLibrary(BaseModel):
    __tablename__ = "t_guard_libraries"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    library_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    tags: Mapped[list] = mapped_column(JSONB, index=False, nullable=False, default=list)
    config: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=dict)
    item_count: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    hit_count: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    last_hit_time: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    version: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=1)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1")


class GuardLabel(BaseModel):
    __tablename__ = "t_guard_labels"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    dimension_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    code: Mapped[str] = mapped_column(String(64), index=True, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True)
    cloud_label: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    cloud_label_type: Mapped[str] = mapped_column(String(16), index=True, nullable=False)
    detection_ranges: Mapped[list] = mapped_column(JSONB, index=False, nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=True)
    risk_score: Mapped[float] = mapped_column(Float, index=True, nullable=False, default=0.0)
    risk_level: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    sensitive_level: Mapped[str | None] = mapped_column(String(8), index=True, nullable=True)
    action: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    action_config: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=dict)
    sort_order: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1")


class GuardLabelLibrary(BaseModel):
    __tablename__ = "t_guard_label_libraries"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    label_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    library_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    config: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=dict)
    conditions: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=dict)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1")


class GuardDimension(BaseModel):
    __tablename__ = "t_guard_dimensions"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    code: Mapped[str] = mapped_column(String(64), index=True, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=True)
    config: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=dict)
    sort_order: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    created_by: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1")


class Knowledgebase(BaseModel):
    __tablename__ = "t_ai_knowledgebases"
    __table_args__ = {"schema": "usr_ai"}
    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    avatar: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="avatar base64 string")
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), index=True, nullable=False, doc="KB name")
    language: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, default="English", doc="English|Chinese")
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="KB description")
    embd_id: Mapped[str] = mapped_column(String(128), index=True, nullable=False, doc="default embedding model ID")
    permission: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="me", doc="me|team")
    created_by: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    doc_num: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    token_num: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    chunk_num: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    similarity_threshold: Mapped[float] = mapped_column(Float, index=True, nullable=False, default=0.2)
    vector_similarity_weight: Mapped[float] = mapped_column(Float, index=True, nullable=False, default=0.3)
    parser_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default=ParserType.NAIVE.value, doc="default parser ID")
    pipeline_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, doc="Pipeline ID")
    parser_config: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default={"pages": [[1, 1000000]], "table_context_size": 0, "image_context_size": 0})
    pagerank: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=0)

    graphrag_task_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, doc="Graph RAG task ID")
    graphrag_task_finish_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    raptor_task_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, doc="RAPTOR task ID")
    raptor_task_finish_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    mindmap_task_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, doc="Mindmap task ID")
    mindmap_task_finish_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)

    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")


class Document(BaseModel):
    __tablename__ = "t_ai_documents"
    __table_args__ = {"schema": "usr_ai"}
    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    thumbnail: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="thumbnail base64 string")
    kb_id: Mapped[str] = mapped_column(String(256), index=True, nullable=False)
    parser_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="default parser ID")
    pipeline_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, doc="Pipeline ID")
    parser_config: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default={"pages": [[1, 1000000]], "table_context_size": 0, "image_context_size": 0})
    source_type: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="local",
                         doc="where dose this document come from")
    type: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="file extension")
    created_by: Mapped[str] = mapped_column(String, index=True, nullable=False, doc="who created it")
    name: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True, doc="file name")
    location: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True, doc="where dose it store")
    size: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    auth: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="attribution of data rights and responsibilities")
    token_num: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    chunk_num: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    progress: Mapped[float] = mapped_column(Float, index=True, nullable=False, default=0)
    progress_msg: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, default="", doc="process message")
    process_begin_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    process_duration: Mapped[float] = mapped_column(Float, index=False, nullable=False, default=0)
    meta_fields: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default={})
    suffix: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="", doc="The real file extension suffix")
    run: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="0", doc="start to run processing or cancel.(1: run it; 2: cancel)")
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")


class DocumentMetadata(BaseModel):
    """Independent metadata table — decouples metadata from Document.meta_fields.

    For Milvus backend: metadata truth lives here instead of Document.meta_fields.
    For ES/Infinity backends: metadata lives in docStoreConn sidecar index, but this
    table is still the canonical model definition.
    """
    __tablename__ = "t_ai_document_metadata"
    __table_args__ = (
        sa.Index("ix_doc_meta_tenant_kb", "tenant_id", "kb_id"),
        sa.Index("ix_doc_meta_kb", "kb_id"),
        {"schema": "usr_ai"},
    )
    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False,
                                    doc="same as document id")
    tenant_id: Mapped[str] = mapped_column(String(32), nullable=False)
    kb_id: Mapped[str] = mapped_column(String(32), nullable=False)
    meta_fields: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default={})


class File(BaseModel):
    __tablename__ = "t_ai_files"
    __table_args__ = {"schema": "usr_ai"}
    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    parent_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="parent folder id")
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="tenant id")
    created_by: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="who created it")
    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False, doc="file name or folder name")
    location: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True, doc="where dose it store")
    size: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    type: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    source_type: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="", doc="where dose this document come from")

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
    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    file_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, doc="file id")
    document_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, doc="document id")


class Task(BaseModel):
    __tablename__ = "t_ai_tasks"
    __table_args__ = {"schema": "usr_ai"}
    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    doc_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    from_page: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=0)
    to_page: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=100000000)
    task_type: Mapped[str] = mapped_column(String(32), index=False, nullable=False, default="")
    begin_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    process_duration: Mapped[float] = mapped_column(Float, index=False, nullable=False, default=0)
    progress: Mapped[float] = mapped_column(Float, index=True, nullable=False, default=0)
    progress_msg: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, default="", doc="process message")
    retry_count: Mapped[int | None] = mapped_column(Integer, index=False, nullable=True, default=0)
    digest: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, default="", doc="task digest")
    chunk_ids: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, default="", doc="chunk ids")
    priority: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=0)


class Dialog(BaseModel):
    __tablename__ = "t_ai_dialogs"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True, doc="dialog application name")
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="Dialog description")
    icon: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="icon base64 string")
    language: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, default="English", doc="English|Chinese")
    llm_id: Mapped[str] = mapped_column(String(128), index=False, nullable=False, doc="default llm ID")
    llm_setting: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False,
                         default={"temperature": 0.1, "top_p": 0.3, "frequency_penalty": 0.7, "presence_penalty": 0.4,
                                  "max_tokens": 512})
    prompt_type: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="simple", doc="simple|advanced")
    prompt_config: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False,
                           default={"system": "", "prologue": "Hi! I'm your assistant. What can I do for you?",
                                    "parameters": [],
                                    "empty_response": "Sorry! No relevant content was found in the knowledge base!"})
    meta_data_filter: Mapped[dict | None] = mapped_column(JSONB, index=False, nullable=True, default={})
    similarity_threshold: Mapped[float] = mapped_column(Float, index=False, nullable=False, default=0.2)
    vector_similarity_weight: Mapped[float] = mapped_column(Float, index=False, nullable=False, default=0.3)
    top_n: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=6)
    top_k: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=1024)
    do_refer: Mapped[str] = mapped_column(String(1), index=False, nullable=False, default="1",
                      doc="it needs to insert reference index into answer or not")
    rerank_id: Mapped[str | None] = mapped_column(String(128), index=False, nullable=True, doc="default rerank model ID")
    kb_ids: Mapped[list] = mapped_column(JSONB, index=False, nullable=False, default=[])
    search_mode: Mapped[dict | None] = mapped_column(JSONB, index=False, nullable=True,
                          doc="search mode configuration: hybrid, sparse, dense, or fusion")
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted，1: validate)")


class Conversation(BaseModel):
    __tablename__ = "t_ai_conversations"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    dialog_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True, doc="conversation name")
    message: Mapped[list | None] = mapped_column(JSONB, index=False, nullable=True)
    reference: Mapped[list | None] = mapped_column(JSONB, index=False, nullable=True, default=[])
    user_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, doc="user_id")


class APIToken(BaseModel):
    __tablename__ = "t_ai_api_tokens"
    __table_args__ = {"schema": "usr_ai"}

    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(20), nullable=False, doc="Token名称")
    description: Mapped[str | None] = mapped_column(Text, nullable=True, doc="Token描述")
    token: Mapped[str] = mapped_column(String(255), primary_key=True, index=True, nullable=False)
    dialog_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    source: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True, doc="none|agent|dialog")
    beta: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)


class API4Conversation(BaseModel):
    __tablename__ = "t_ai_api4conversations"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    name: Mapped[str | None] = mapped_column(String(255), index=False, nullable=True, doc="conversation name")
    dialog_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False, doc="user_id")
    exp_user_id: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True, doc="exp_user_id")
    message: Mapped[list | None] = mapped_column(JSONB, index=False, nullable=True)
    reference: Mapped[list | None] = mapped_column(JSONB, index=False, nullable=True, default=[])
    tokens: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=0)
    source: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True, doc="none|agent|dialog")
    dsl: Mapped[dict | None] = mapped_column(JSONB, index=False, nullable=True, default={})
    duration: Mapped[float] = mapped_column(Float, index=True, nullable=False, default=0)
    round: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    thumb_up: Mapped[int] = mapped_column(Integer, index=True, nullable=False, default=0)
    errors: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, default=None, doc="errors")


class UserCanvas(BaseModel):
    __tablename__ = "t_ai_user_canvases"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    avatar: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="avatar base64 string")
    user_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False, doc="user_id")
    title: Mapped[str | None] = mapped_column(String(255), index=False, nullable=True, doc="Canvas title")
    permission: Mapped[str] = mapped_column(String(16), index=True, nullable=False, default="me", doc="me|team")
    release: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=False, doc="is released")
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="Canvas description")
    canvas_type: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, doc="Canvas type")
    canvas_category: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="agent_canvas", doc="Canvas category: agent_canvas|dataflow_canvas")
    dsl: Mapped[dict | None] = mapped_column(JSONB, index=False, nullable=True, default={})


class CanvasTemplate(BaseModel):
    __tablename__ = "t_ai_canvas_templates"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    avatar: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="avatar base64 string")
    title: Mapped[dict | None] = mapped_column(JSONB, index=False, nullable=True, default=dict, doc="Canvas title")
    description: Mapped[dict | None] = mapped_column(JSONB, index=False, nullable=True, default=dict, doc="Canvas description")
    canvas_type: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, doc="Canvas type")
    canvas_category: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="agent_canvas", doc="Canvas category: agent_canvas|dataflow_canvas")
    dsl: Mapped[dict | None] = mapped_column(JSONB, index=False, nullable=True, default={})


class WritingProject(BaseModel):
    __tablename__ = "t_ai_writing_projects"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    user_input: Mapped[str] = mapped_column(Text, index=False, nullable=False, doc="用户输入的需求")
    content_type: Mapped[str] = mapped_column(String(255), index=True, nullable=False, doc="文案类型")
    language_style: Mapped[str] = mapped_column(String(255), index=True, nullable=False, doc="语言风格")
    word_count: Mapped[int] = mapped_column(Integer, index=False, nullable=False, doc="文章篇幅/预期字数")
    reference: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="用户提供的参考信息")
    model: Mapped[str] = mapped_column(String(128), index=True, nullable=False, default="gpt-4o", doc="使用的模型")
    title: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True, doc="文章标题")
    user_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="所属用户ID")
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1", doc="状态(0:已删除,1:有效)")

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

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    project_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="所属项目ID")
    title: Mapped[str] = mapped_column(String(255), index=True, nullable=False, doc="章节标题")
    summary: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="章节摘要")
    level: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=1, doc="章节层级(1:主章节,2:子章节)")
    parent_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, doc="父章节ID")
    order_index: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=0, doc="排序索引")
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1", doc="状态(0:已删除,1:有效)")

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

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    chapter_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="所属章节ID")
    title: Mapped[str] = mapped_column(String(255), index=True, nullable=False, doc="参考资料标题")
    content: Mapped[str] = mapped_column(Text, index=False, nullable=False, doc="参考资料内容")
    source: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True, doc="来源(URL或文件名)")
    type: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="text", doc="类型(text,url,file)")
    order_index: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=0, doc="排序索引")
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1", doc="状态(0:已删除,1:有效)")

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

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    chapter_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="所属章节ID", unique=True)
    content: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="章节内容")
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1", doc="状态(0:已删除,1:有效)")

    def to_dict(self):
        return {
            "id": self.id,
            "chapter_id": self.chapter_id,
            "content": self.content
        }


class AskDataHistory(BaseModel):
    __tablename__ = "t_ai_ask_data_history"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    conversation_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="conversation_id")
    ask_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="ask_id")
    user_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, doc="user_id")
    data: Mapped[str] = mapped_column(Text, index=False, nullable=False, doc="data")  # 前端用于展示的数据
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1", doc="状态(0:已删除,1:有效)")
    user_question: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="用户问题")
    round_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="用于标识对话轮次的ID")
    processed_semantic_layer: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="该问题构建的语义层")
    sql_info: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="生成的SQL及执行SQL的结果还有其他信息")

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

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="租户ID")
    name: Mapped[str] = mapped_column(String(100), index=True, nullable=False, doc="环境名称")
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="环境描述")
    base_url: Mapped[str] = mapped_column(String(500), index=True, nullable=False, doc="前置URL/基础URL")
    is_default: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=False, doc="是否默认环境")
    is_global: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=False, doc="是否全局环境")
    status: Mapped[str] = mapped_column(String(1), index=True, nullable=False, default="1", doc="状态(0:禁用,1:启用)")

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

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    environment_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="环境ID")
    key_name: Mapped[str] = mapped_column(String(100), index=True, nullable=False, doc="变量名")
    key_value: Mapped[str] = mapped_column(Text, index=False, nullable=False, doc="变量值")
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="变量描述")
    is_secret: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=False, doc="是否敏感信息")
    variable_type: Mapped[str] = mapped_column(String(20), index=True, nullable=False, default="string", doc="变量类型(string,number,boolean)")
    status: Mapped[str] = mapped_column(String(1), index=True, nullable=False, default="1", doc="状态(0:禁用,1:启用)")

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

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    name: Mapped[str] = mapped_column(String(100), index=True, nullable=False, doc="环境名称")
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="环境描述")
    server_url: Mapped[str | None] = mapped_column(String(500), index=True, nullable=True, doc="服务器URL")
    variables: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=dict, doc="预设变量")
    is_active: Mapped[bool] = mapped_column(Boolean, index=True, nullable=False, default=True, doc="是否启用")
    status: Mapped[str] = mapped_column(String(1), index=True, nullable=False, default="1", doc="状态(0:禁用,1:启用)")

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


class UserCanvasVersion(BaseModel):
    __tablename__ = "t_ai_user_canvas_version"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    user_canvas_id: Mapped[str] = mapped_column(String(255), index=True, nullable=False, doc="user_canvas_id")

    title: Mapped[str | None] = mapped_column(String(255), index=False, nullable=True, doc="Canvas title")
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="Canvas description")
    dsl: Mapped[dict | None] = mapped_column(JSONB, index=False, nullable=True, default={})


class MCPServer(BaseModel):
    __tablename__ = "t_ai_mcp_server"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False, doc="MCP Server name")
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    url: Mapped[str] = mapped_column(String(2048), index=False, nullable=False, doc="MCP Server URL")
    server_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="MCP Server type")
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="MCP Server description")
    variables: Mapped[dict | None] = mapped_column(JSONB, index=False, nullable=True, default=dict, doc="MCP Server variables")
    headers: Mapped[dict | None] = mapped_column(JSONB, index=False, nullable=True, default=dict, doc="MCP Server additional request headers")


class ToolsData(BaseModel):
    __tablename__ = "tools_data"
    __table_args__ = {"schema": "usr_ai"}

    flow_id: Mapped[str] = mapped_column(String(255), primary_key=True, index=True, nullable=False)
    user_id: Mapped[str] = mapped_column(String(255), primary_key=True, index=True, nullable=False)
    meta_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_data: Mapped[str | None] = mapped_column(Text, nullable=True)


class Search(BaseModel):
    """
    搜索配置表
    """
    __tablename__ = "t_ai_search"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    avatar: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="avatar base64 string")
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="租户ID")
    name: Mapped[str] = mapped_column(String(128), index=True, nullable=False, doc="Search name")
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="Search description")
    created_by: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="创建人")
    search_config: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=lambda: {
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
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1",
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


class PipelineOperationLog(BaseModel):
    __tablename__ = "t_pipeline_operation_log"
    __table_args__ = {"schema": "usr_ai"}
    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    document_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    kb_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    pipeline_id: Mapped[str | None] = mapped_column(String(32), index=True, nullable=True, doc="Pipeline ID")
    pipeline_title: Mapped[str | None] = mapped_column(String(128), index=True, nullable=True, doc="Pipeline title")
    parser_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="Parser ID")
    document_name: Mapped[str] = mapped_column(String(255), index=True, nullable=False, doc="File name")
    document_suffix: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="File suffix")
    document_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="Document type")
    source_from: Mapped[str] = mapped_column(String(128), index=True, nullable=False, doc="Source")
    progress: Mapped[float] = mapped_column(Float, index=True, nullable=False, default=0)
    progress_msg: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, default="", doc="process message")
    process_begin_at: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    process_duration: Mapped[float] = mapped_column(Float, index=False, nullable=False, default=0)
    dsl: Mapped[dict | None] = mapped_column(JSONB, index=False, nullable=True, default=dict)
    task_type: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="")
    operation_status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="Operation status")
    avatar: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="avatar base64 string")
    status: Mapped[str | None] = mapped_column(String(1), index=True, nullable=True, default="1", doc="is it validate(0: wasted, 1: validate)")


class Connector(BaseModel):
    """数据源连接器"""
    __tablename__ = "t_ai_connectors"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="Tenant ID")
    name: Mapped[str] = mapped_column(String(128), index=False, nullable=False, doc="Search name")
    source: Mapped[str] = mapped_column(String(128), index=True, nullable=False, doc="Data source")
    input_type: Mapped[str] = mapped_column(String(128), index=True, nullable=False, doc="poll/event/..")
    config: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, default=dict)
    refresh_freq: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=0)
    prune_freq: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=0)
    timeout_secs: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=3600)
    indexing_start: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), index=True, nullable=True, default="schedule", doc="schedule")

    def __str__(self):
        return self.name

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "source": self.source,
            "input_type": self.input_type,
            "config": self.config,
            "refresh_freq": self.refresh_freq,
            "prune_freq": self.prune_freq,
            "timeout_secs": self.timeout_secs,
            "indexing_start": self.indexing_start,
            "status": self.status,
            "create_time": self.create_time,
            "update_time": self.update_time
        }


class Connector2Kb(BaseModel):
    """连接器与知识库关联表"""
    __tablename__ = "t_ai_connector2kb"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    connector_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="Connector ID")
    kb_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="Knowledgebase ID")
    auto_parse: Mapped[str] = mapped_column(String(1), nullable=False, default="1", doc="Auto parse (0: disabled, 1: enabled)")

    def to_dict(self):
        return {
            "id": self.id,
            "connector_id": self.connector_id,
            "kb_id": self.kb_id,
            "auto_parse": self.auto_parse
        }


# ==================== RAG Evaluation Tables ====================

class EvaluationDataset(BaseModel):
    """Ground truth dataset for RAG evaluation"""
    __tablename__ = "t_ai_evaluation_datasets"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="Tenant ID")
    name: Mapped[str] = mapped_column(String(255), index=True, nullable=False, doc="Dataset name")
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="Dataset description")
    kb_ids: Mapped[list] = mapped_column(JSONB, index=False, nullable=False, default=list, doc="Knowledge base IDs to evaluate against")
    created_by: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="Creator user ID")
    status: Mapped[str] = mapped_column(String(1), index=True, nullable=False, default="1", doc="1=valid, 0=invalid")

    def to_dict(self):
        return {
            "id": self.id,
            "tenant_id": self.tenant_id,
            "name": self.name,
            "description": self.description,
            "kb_ids": self.kb_ids,
            "created_by": self.created_by,
            "status": self.status,
            "create_time": self.create_time,
            "update_time": self.update_time
        }


class EvaluationCase(BaseModel):
    """Individual test case in an evaluation dataset"""
    __tablename__ = "t_ai_evaluation_cases"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="FK to evaluation_datasets")
    question: Mapped[str] = mapped_column(Text, index=False, nullable=False, doc="Test question")
    reference_answer: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="Optional ground truth answer")
    relevant_doc_ids: Mapped[list | None] = mapped_column(JSONB, index=False, nullable=True, doc="Expected relevant document IDs")
    relevant_chunk_ids: Mapped[list | None] = mapped_column(JSONB, index=False, nullable=True, doc="Expected relevant chunk IDs")
    case_metadata: Mapped[dict | None] = mapped_column(JSONB, index=False, nullable=True, doc="Additional context/tags")

    def to_dict(self):
        return {
            "id": self.id,
            "dataset_id": self.dataset_id,
            "question": self.question,
            "reference_answer": self.reference_answer,
            "relevant_doc_ids": self.relevant_doc_ids,
            "relevant_chunk_ids": self.relevant_chunk_ids,
            "case_metadata": self.case_metadata,
            "create_time": self.create_time
        }


class EvaluationRun(BaseModel):
    """A single evaluation run"""
    __tablename__ = "t_ai_evaluation_runs"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="FK to evaluation_datasets")
    dialog_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="Dialog configuration being evaluated")
    name: Mapped[str] = mapped_column(String(255), index=False, nullable=False, doc="Run name")
    config_snapshot: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, doc="Dialog config at time of evaluation")
    metrics_summary: Mapped[dict | None] = mapped_column(JSONB, index=False, nullable=True, doc="Aggregated metrics")
    run_status: Mapped[str] = mapped_column(String(32), index=True, nullable=False, default="PENDING", doc="PENDING/RUNNING/COMPLETED/FAILED")
    created_by: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="User who started the run")
    complete_time: Mapped[int | None] = mapped_column(BigInteger, index=True, nullable=True, doc="Completion timestamp")

    def to_dict(self):
        return {
            "id": self.id,
            "dataset_id": self.dataset_id,
            "dialog_id": self.dialog_id,
            "name": self.name,
            "config_snapshot": self.config_snapshot,
            "metrics_summary": self.metrics_summary,
            "run_status": self.run_status,
            "created_by": self.created_by,
            "create_time": self.create_time,
            "complete_time": self.complete_time
        }


class EvaluationResult(BaseModel):
    """Result for a single test case in an evaluation run"""
    __tablename__ = "t_ai_evaluation_results"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    run_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="FK to evaluation_runs")
    case_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="FK to evaluation_cases")
    generated_answer: Mapped[str] = mapped_column(Text, index=False, nullable=False, doc="Generated answer")
    retrieved_chunks: Mapped[list] = mapped_column(JSONB, index=False, nullable=False, doc="Chunks that were retrieved")
    metrics: Mapped[dict] = mapped_column(JSONB, index=False, nullable=False, doc="All computed metrics")
    execution_time: Mapped[float] = mapped_column(Float, index=False, nullable=False, doc="Response time in seconds")
    token_usage: Mapped[dict | None] = mapped_column(JSONB, index=False, nullable=True, doc="Prompt/completion tokens")

    def to_dict(self):
        return {
            "id": self.id,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "generated_answer": self.generated_answer,
            "retrieved_chunks": self.retrieved_chunks,
            "metrics": self.metrics,
            "execution_time": self.execution_time,
            "token_usage": self.token_usage,
            "create_time": self.create_time
        }


class SyncLogs(BaseModel):
    """同步日志表"""
    __tablename__ = "t_ai_sync_logs"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    connector_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="Connector ID")
    status: Mapped[str] = mapped_column(String(128), index=True, nullable=False, doc="Processing status")
    from_beginning: Mapped[str | None] = mapped_column(String(1), index=False, nullable=True, default="0")
    new_docs_indexed: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=0)
    total_docs_indexed: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=0)
    docs_removed_from_index: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=0)
    error_msg: Mapped[str] = mapped_column(Text, index=False, nullable=False, default="", doc="process message")
    error_count: Mapped[int] = mapped_column(Integer, index=False, nullable=False, default=0)
    full_exception_trace: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, default="", doc="process message")
    time_started: Mapped[datetime | None] = mapped_column(DateTime, index=True, nullable=True)
    poll_range_start: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True, doc="ISO datetime with timezone")
    poll_range_end: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True, doc="ISO datetime with timezone")
    kb_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="Knowledgebase ID")

    def to_dict(self):
        return {
            "id": self.id,
            "connector_id": self.connector_id,
            "status": self.status,
            "from_beginning": self.from_beginning,
            "new_docs_indexed": self.new_docs_indexed,
            "total_docs_indexed": self.total_docs_indexed,
            "docs_removed_from_index": self.docs_removed_from_index,
            "error_msg": self.error_msg,
            "error_count": self.error_count,
            "full_exception_trace": self.full_exception_trace,
            "time_started": self.time_started,
            "poll_range_start": self.poll_range_start,
            "poll_range_end": self.poll_range_end,
            "kb_id": self.kb_id,
            "create_time": self.create_time,
            "update_time": self.update_time
        }


class Memory(BaseModel):
    """Memory数据集模型

    用于管理和存储AI记忆数据集，支持多种记忆类型（raw/semantic/episodic/procedural）
    """
    __tablename__ = "t_ai_memories"
    __table_args__ = {"schema": "usr_ai"}

    id: Mapped[str] = mapped_column(String(32), primary_key=True, index=False, nullable=False)
    name: Mapped[str] = mapped_column(String(128), index=True, nullable=False, doc="Memory name")
    avatar: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="Avatar base64 string")
    tenant_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False, doc="Tenant ID")
    memory_type: Mapped[int] = mapped_column(
        Integer, index=True, nullable=False, default=1,
        doc="Bit flags (LSB->MSB): 1=raw, 2=semantic, 4=episodic, 8=procedural. E.g., 5 enables raw + episodic."
    )
    storage_type: Mapped[str] = mapped_column(
        String(32), index=True, nullable=False, default="table",
        doc="Storage type: table|graph"
    )
    embd_id: Mapped[str] = mapped_column(String(128), index=False, nullable=False, doc="Embedding model ID")
    llm_id: Mapped[str] = mapped_column(String(128), index=False, nullable=False, doc="Chat model ID")
    permissions: Mapped[str] = mapped_column(
        String(16), index=True, nullable=False, default="me",
        doc="Permission scope: me|team"
    )
    description: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="Memory description")
    memory_size: Mapped[int] = mapped_column(
        Integer, index=False, nullable=False, default=5242880,
        doc="Maximum memory size in bytes (default 5MB)"
    )
    forgetting_policy: Mapped[str] = mapped_column(
        String(32), index=False, nullable=False, default="FIFO",
        doc="Forgetting policy: LRU|FIFO"
    )
    temperature: Mapped[float] = mapped_column(Float, index=False, nullable=False, default=0.5, doc="LLM temperature")
    system_prompt: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="System prompt")
    user_prompt: Mapped[str | None] = mapped_column(Text, index=False, nullable=True, doc="User prompt")

    def to_dict(self):
        return {
            "id": self.id,
            "name": self.name,
            "avatar": self.avatar,
            "tenant_id": self.tenant_id,
            "memory_type": self.memory_type,
            "storage_type": self.storage_type,
            "embd_id": self.embd_id,
            "llm_id": self.llm_id,
            "permissions": self.permissions,
            "description": self.description,
            "memory_size": self.memory_size,
            "forgetting_policy": self.forgetting_policy,
            "temperature": self.temperature,
            "system_prompt": self.system_prompt,
            "user_prompt": self.user_prompt,
            "create_time": self.create_time,
            "create_date": self.create_date,
            "update_time": self.update_time,
            "update_date": self.update_date
        }


class SystemSettings(BaseModel):
    """系统设置模型

    用于存储全局系统配置项，支持按名称查询和更新
    """
    __tablename__ = "t_ai_system_settings"
    __table_args__ = {"schema": "usr_ai"}

    name: Mapped[str] = mapped_column(String(128), primary_key=True, index=False, nullable=False, doc="Setting name")
    source: Mapped[str] = mapped_column(String(32), nullable=False, index=False, doc="Setting type (e.g. config)")
    data_type: Mapped[str] = mapped_column(String(32), nullable=False, index=False, doc="Data type (e.g. bool, string, integer)")
    value: Mapped[str] = mapped_column(Text, nullable=False, doc="Configuration value (JSON, string, etc.)")


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

def upgrade_database_tables(is_fresh_install: bool = False):
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
        raise FileNotFoundError(error_msg)

    try:
        # 创建Alembic配置对象
        alembic_cfg = Config(alembic_ini_path)

        # 仅设置迁移脚本路径。数据库连接通过 attributes 传递，避免 URL 中 `%` 被 ConfigParser 插值解析。
        alembic_cfg.set_main_option("script_location", alembic_path)
        # 获取数据库连接
        # 使用 engine.begin() 确保迁移事务在成功后提交，
        # 避免连接归还时被连接池回滚钩子撤销。
        with engine.begin() as connection:
            alembic_cfg.attributes["connection"] = connection
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

            # 全新环境：表刚由 init_web_db() 按最新 db_models.py 创建，
            # 无需跑历史迁移（历史迁移只针对老环境做列/重命名补丁），
            # 直接 stamp 到 head 即可。
            if current_rev is None and is_fresh_install:
                logging.info("检测到全新环境，跳过历史迁移，直接 stamp 到最新版本...")
                command.stamp(alembic_cfg, "head")
                logging.info(f"全新环境已 stamp 到最新版本: {head_rev}")
                return "全新环境已 stamp 最新版本"

            # 存量环境（含迁移体系建立之前的老环境）：正常升级
            logging.info("开始执行数据库迁移...")
            command.upgrade(alembic_cfg, "head")

            logging.info("数据库迁移成功完成")
            return "数据库迁移成功完成"

    except Exception as e:
        error_msg = f"数据库迁移过程中发生错误: {str(e)}"
        logging.error(error_msg)
        import traceback
        logging.error(traceback.format_exc())
        raise RuntimeError(error_msg) from e


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
        elif db_type.lower() in ('mysql', 'oceanbase'):
            # OceanBase 兼容 MySQL 协议，使用相同的锁实现
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
