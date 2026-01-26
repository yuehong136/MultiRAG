# coding=utf-8
"""
@project: multirag
@Author：龙
@file： file2document_service.py
@date：2024/7/9 9:00
@desc:
"""
import logging
from datetime import datetime
from sqlalchemy.orm import Session
from fastapi import HTTPException

from common.constants import FileSource
from api.db.db_models import File2Document, File
from api.db.services.common_service import CommonService
from api.db.services.document_service import DocumentService
from common.time_utils import current_timestamp, datetime_format


class File2DocumentService(CommonService):
    model = File2Document

    def __init__(self):
        super().__init__(File2Document)

    @classmethod
    def get_by_file_id(cls, db: Session, file_id: str):
        return db.query(cls.model).filter_by(file_id=file_id).all()

    @classmethod
    def get_by_document_id(cls, db: Session, document_id: str):
        return db.query(cls.model).filter_by(document_id=document_id).all()

    @classmethod
    def get_by_document_ids(cls, db: Session, document_ids: list[str]) -> list[dict]:
        """根据文档ID列表批量查询文件-文档关联记录"""
        try:
            objs = db.query(cls.model).filter(
                cls.model.document_id.in_(document_ids)
            ).all()

            return [
                {
                    "id": obj.id,
                    "file_id": obj.file_id,
                    "document_id": obj.document_id,
                    "create_time": obj.create_time,
                    "create_date": obj.create_date,
                    "update_time": obj.update_time,
                    "update_date": obj.update_date
                }
                for obj in objs
            ]
        except Exception:
            logging.exception("Failed to get file2document records by document_ids")
            return []

    @classmethod
    def insert(cls, db: Session, obj: dict):
        if not cls.save(db, **obj):
            raise RuntimeError("Database error (File)!")
        return File2Document(**obj)

    @classmethod
    def delete_by_file_id(cls, db: Session, file_id: str):
        try:
            deleted_count = db.query(cls.model).filter(cls.model.file_id == file_id).delete(synchronize_session=False)
            db.commit()  # 确保提交事务
            return deleted_count
        except Exception:
            db.rollback()  # 回滚事务
            logging.exception(f"[delete_by_file_id] Error occurred")
            return 0

    @classmethod
    def delete_by_document_ids_or_file_ids(cls, db: Session, document_ids: list[str] | None = None, file_ids: list[str] | None = None) -> int:
        """根据文档ID列表或文件ID列表删除文件-文档关联记录"""
        try:
            from sqlalchemy import or_

            # 构造过滤条件
            if not document_ids and not file_ids:
                return 0

            if not document_ids:
                # 只有 file_ids
                result = db.query(cls.model).filter(
                    cls.model.file_id.in_(file_ids)
                ).delete(synchronize_session=False)
            elif not file_ids:
                # 只有 document_ids
                result = db.query(cls.model).filter(
                    cls.model.document_id.in_(document_ids)
                ).delete(synchronize_session=False)
            else:
                # 两者都有，使用 OR 条件
                result = db.query(cls.model).filter(
                    or_(
                        cls.model.document_id.in_(document_ids),
                        cls.model.file_id.in_(file_ids)
                    )
                ).delete(synchronize_session=False)

            db.commit()
            return result
        except Exception as e:
            db.rollback()
            logging.exception("Failed to delete file2document records")
            raise e

    @classmethod
    def delete_by_document_id(cls, db: Session, doc_id: str):
        try:
            deleted_count = db.query(cls.model).filter(cls.model.document_id == doc_id).delete(synchronize_session=False)
            db.commit()  # 确保提交事务
            return deleted_count
        except Exception:
            db.rollback()  # 回滚事务
            logging.exception(f"[delete_by_file_id] Error occurred")
            return 0

    @classmethod
    def update_by_file_id(cls, db: Session, file_id: str, obj: dict):
        current_ts = current_timestamp()
        current_date = datetime_format(datetime.now())
        obj["update_time"] = current_ts
        obj["update_date"] = current_date
        db.query(cls.model).filter_by(id=file_id).update(obj)
        db.commit()
        updated_obj = db.query(cls.model).filter_by(id=file_id).one()
        return updated_obj

    @classmethod
    def get_storage_address(cls, db: Session, doc_id: str = None, file_id: str = None):
        if doc_id:
            f2d = cls.get_by_document_id(db, doc_id)
        else:
            f2d = cls.get_by_file_id(db, file_id)

        if f2d:
            file = db.query(File).filter_by(id=f2d[0].file_id).one()
            if not file.source_type or file.source_type == FileSource.LOCAL:
                return file.parent_id, file.location
            doc_id = f2d[0].document_id

        if not doc_id:
            raise HTTPException(status_code=400, detail="Please specify doc_id")

        doc = DocumentService.get_by_id(db, doc_id)
        return doc.kb_id, doc.location
