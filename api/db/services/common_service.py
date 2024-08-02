# common_services.py
import uuid
from datetime import datetime, timezone
from typing import Type, List, Any, Optional, Dict

from sqlalchemy import Row, desc, asc
from sqlalchemy.orm import Session
from fastapi import HTTPException
from sqlalchemy.exc import NoResultFound, IntegrityError

from api.db import db_models, schemas


class CommonService:
    model: Type[db_models.BaseModel]

    def __init__(self, model: Type[db_models.BaseModel]):
        self.model = model

    @staticmethod
    def current_timestamp():
        return int(datetime.now(timezone.utc).timestamp() * 1000)

    @staticmethod
    def current_datetime():
        return datetime.now(timezone.utc)

    @classmethod
    def query(cls, db: Session, cols: List[str] = None, reverse: Optional[bool] = None, order_by: Optional[str] = None,
              **kwargs) -> list[Row[tuple[Type[db_models.BaseModel]]]]:
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
        # if cols:
        #     query = query.with_entities(*[getattr(cls.model, col) for col in cols])
        # if reverse is not None:
        #     if not order_by or not hasattr(cls.model, order_by):
        #         order_by = "create_time"
        #     order_column = getattr(cls.model, order_by)
        #     if reverse:
        #         query = query.order_by(order_column.desc())
        #     else:
        #         query = query.order_by(order_column.asc())

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
    def get_all(cls, db: Session, cols: List[str] = None, reverse: Optional[bool] = None, order_by: str = None) -> list[
        Row[tuple[Type[db_models.BaseModel]]]]:
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
    def get(cls, db: Session, **kwargs) -> Type[db_models.BaseModel]:
        try:
            return db.query(cls.model).filter_by(**kwargs).one()
        except NoResultFound:
            raise HTTPException(status_code=404, detail="Item not found")

    @classmethod
    def get_or_none(cls, db: Session, **kwargs) -> Optional[db_models.BaseModel]:
        return db.query(cls.model).filter_by(**kwargs).one_or_none()

    @classmethod
    def save(cls, db: Session, **kwargs) -> db_models.BaseModel:
        # db_item = cls.model(**kwargs)
        # db.add(db_item)
        # db.commit()
        # db.refresh(db_item)
        # return db_item

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
    def insert(cls, db: Session, **kwargs) -> db_models.BaseModel:
        if "id" not in kwargs:
            kwargs["id"] = str(uuid.uuid4())
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
    def insert_many(cls, db: Session, data_list: List[Dict[str, Any]], batch_size: int = 100):
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
    def update_many_by_id(cls, db: Session, data_list: List[Dict[str, Any]]):
        now = cls.current_timestamp()
        now_datetime = cls.current_datetime()
        with db.begin():
            for data in data_list:
                data["update_time"] = now
                data["update_date"] = now_datetime
                db.query(cls.model).filter_by(id=data["id"]).update(data)
            db.commit()

    @classmethod
    def update_by_id(cls, db: Session, pid: str, data: Dict[str, Any]) -> int:
        now = cls.current_timestamp()
        now_datetime = cls.current_datetime()
        data["update_time"] = now
        data["update_date"] = now_datetime
        num = db.query(cls.model).filter_by(id=pid).update(data)
        db.commit()
        return num

    @classmethod
    def get_by_id(cls, db: Session, pid: Any) -> Type[db_models.BaseModel]:
        try:
            return db.query(cls.model).filter(cls.model.id == pid).one()
        except NoResultFound:
            raise HTTPException(status_code=404, detail="Item not found")

    @classmethod
    def get_by_ids(cls, db: Session, pids: List[Any], cols: List[str] = None) -> list[
        Row[tuple[Type[db_models.BaseModel]]]]:
        query = db.query(cls.model).filter(cls.model.id.in_(pids))
        if cols:
            query = query.with_entities(*[getattr(cls.model, col) for col in cols])
        return query.all()

    @classmethod
    def delete_by_id(cls, db: Session, pid: Any) -> int:
        return db.query(cls.model).filter(cls.model.id == pid).delete(synchronize_session=False)

    @classmethod
    def filter_update(cls, db: Session, filters: List[Any], update_data: Dict[str, Any]):
        now = cls.current_timestamp()
        now_datetime = cls.current_datetime()
        update_data["update_time"] = now
        update_data["update_date"] = now_datetime
        if not db.in_transaction():
            with db.begin():
                db.query(cls.model).filter(*filters).update(update_data, synchronize_session=False)
                db.commit()
        else:
            db.query(cls.model).filter(*filters).update(update_data, synchronize_session=False)

    @classmethod
    def filter_delete(cls, db: Session, filters: List[Any]) -> int:
        # with db.begin():
        #     num = db.query(cls.model).filter(*filters).delete(synchronize_session=False)
        #     db.commit()
        #     return num
        num = db.query(cls.model).filter(*filters).delete(synchronize_session=False)
        db.commit()
        return num

    @classmethod
    def cut_list(cls, tar_list: List[Any], n: int) -> List[tuple]:
        return [tuple(tar_list[i:i + n]) for i in range(0, len(tar_list), n)]

    @classmethod
    def filter_scope_list(cls, db: Session, in_key: str, in_filters_list: List[Any], filters: List = None,
                          cols: List[str] = None) -> list[Row[tuple[Type[db_models.BaseModel]]]]:
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
