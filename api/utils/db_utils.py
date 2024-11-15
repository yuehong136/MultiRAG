# coding=utf-8
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
from typing import Dict, Type, Union

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session
from sqlalchemy.exc import SQLAlchemyError

from api.utils import current_timestamp, timestamp_to_date

from api.db.database import BaseModel as DataBaseModel




def bulk_insert_into_db(db: Session, model, data_source, replace_on_conflict=False):
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
            if replace_on_conflict:
                stmt = insert(model).values(batch)
                update_cols = {col: getattr(stmt.excluded, col) for col in batch[0].keys() if col not in {'create_time', 'create_date'}}
                stmt = stmt.on_conflict_do_update(
                    index_elements=[model.id],
                    set_=update_cols
                )
                db.execute(stmt)
            else:
                db.bulk_insert_mappings(model, batch)
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


def query_dict2expression(model: Type[DataBaseModel], query: Dict[str, Union[bool, int, str, list, tuple]]):
    expression = []
    for field, value in query.items():
        if not isinstance(value, (list, tuple)):
            value = ('==', value)
        op, *val = value
        field = getattr(model, field)
        value = supported_operators[op](field, val[0]) if op in supported_operators else getattr(field, op)(*val)
        expression.append(value)
    return reduce(operator.iand, expression)


def query_db(db: Session, model: Type[DataBaseModel], limit: int = 0, offset: int = 0,
             query: dict = None, order_by: Union[str, list, tuple] = None):
    data = db.query(model)
    if query:
        data = data.filter(query_dict2expression(model, query))
    count = data.count()

    if not order_by:
        order_by = 'create_time'
    if not isinstance(order_by, (list, tuple)):
        order_by = (order_by, 'asc')
    order_by, order = order_by
    order_by = getattr(model, order_by)
    order_by = getattr(order_by, order)()
    data = data.order_by(order_by)

    if limit > 0:
        data = data.limit(limit)
    if offset > 0:
        data = data.offset(offset)

    return list(data), count