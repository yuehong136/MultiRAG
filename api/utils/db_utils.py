"""
@project: multirag
@Author：龙
@file： db_utils.py
@date：2024/7/22 15:22
@desc:
"""
import logging
import operator
from functools import reduce

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

# from api.db.database import BaseModel as DataBaseModel
from api.db.db_models import BaseModel as DataBaseModel
from common.time_utils import current_timestamp, timestamp_to_date


def bulk_insert_into_db(db: Session, model, data_source, replace_on_conflict=False):
    """
    批量插入数据到数据库（SQLAlchemy 2.0 Core 风格）。

    Args:
        db: 数据库会话
        model: ORM 模型类
        data_source: 要插入的数据字典列表
        replace_on_conflict: 是否在冲突时更新（基于主键 id）
    """
    if not data_source:
        return

    for i, data in enumerate(data_source):
        current_time = current_timestamp() + i
        current_date = timestamp_to_date(current_time)
        if 'create_time' not in data:
            data['create_time'] = current_time
        data['create_date'] = current_date
        data['update_time'] = current_time
        data['update_date'] = current_date

    batch_size = 1000
    for i in range(0, len(data_source), batch_size):
        batch = data_source[i:i + batch_size]
        try:
            # SQLAlchemy 2.0 Core 风格：统一使用 insert().values()
            stmt = insert(model).values(batch)
            if replace_on_conflict:
                update_cols = {col: getattr(stmt.excluded, col) for col in batch[0].keys() if col not in {'create_time', 'create_date'}}
                stmt = stmt.on_conflict_do_update(
                    index_elements=[model.id],
                    set_=update_cols
                )
            db.execute(stmt)
            db.commit()
        except SQLAlchemyError as e:
            db.rollback()
            logging.error(f"Error bulk inserting into DB: {e}")
            raise


def get_dynamic_db_model(base, job_id):
    class DynamicModel(base):
        __tablename__ = f"{base.__tablename__}_{get_dynamic_tracking_table_index(job_id)}"
    return DynamicModel


def get_dynamic_tracking_table_index(job_id):
    return job_id[:8]


def fill_db_model_object(model_object, human_model_dict):
    for k, v in human_model_dict.items():
        attr_name = f'f_{k}'
        if hasattr(model_object.__class__, attr_name):
            setattr(model_object, attr_name, v)
    return model_object


# https://docs.sqlalchemy.org/en/14/core/operators.html
supported_operators = {
    '==': operator.eq,
    '<': operator.lt,
    '<=': operator.le,
    '>': operator.gt,
    '>=': operator.ge,
    '!=': operator.ne,
    '<<': operator.lshift,
    '>>': operator.rshift,
    '%': operator.mod,
    '**': operator.pow,
    '^': operator.xor,
    '~': operator.inv,
}


def query_dict2expression(model: type[DataBaseModel], query: dict[str, bool | int | str | list | tuple]):
    expression = []
    for field, value in query.items():
        if not isinstance(value, (list, tuple)):
            value = ('==', value)
        op, *val = value
        field = getattr(model, field)
        value = supported_operators[op](field, val[0]) if op in supported_operators else getattr(field, op)(*val)
        expression.append(value)
    return reduce(operator.iand, expression)


def query_db(db: Session, model: type[DataBaseModel], limit: int = 0, offset: int = 0,
             query: dict = None, order_by: str | list | tuple | None = None):
    """
    通用数据库查询函数（SQLAlchemy 2.0 Core 风格）。

    Args:
        db: 数据库会话
        model: ORM 模型类
        limit: 返回结果数量限制，0 表示不限制
        offset: 结果偏移量
        query: 查询条件字典，格式为 {field: value} 或 {field: (operator, value)}
        order_by: 排序字段，可以是字符串或 (field, 'asc'/'desc') 元组

    Returns:
        (data_list, total_count) 元组
    """
    # 构建基础 select 语句
    stmt = select(model)

    # 添加过滤条件
    if query:
        stmt = stmt.where(query_dict2expression(model, query))

    # 统计总数（SQLAlchemy 2.0 风格）
    count_stmt = select(func.count()).select_from(stmt.subquery())
    count = db.execute(count_stmt).scalar_one()

    # 处理排序
    if not order_by:
        order_by = 'create_time'
    if not isinstance(order_by, (list, tuple)):
        order_by = (order_by, 'asc')
    order_field, order_direction = order_by
    order_column = getattr(model, order_field)
    order_column = getattr(order_column, order_direction)()
    stmt = stmt.order_by(order_column)

    # 分页
    if limit > 0:
        stmt = stmt.limit(limit)
    if offset > 0:
        stmt = stmt.offset(offset)

    # 执行查询（SQLAlchemy 2.0 风格）
    data = db.scalars(stmt).all()
    return list(data), count
