# coding=utf-8
"""
@project: multirag
@Author：龙
@file： file2document_service.py
@date：2024/7/9 9:00
@desc:
"""
from datetime import datetime
from sqlalchemy.orm import Session
from fastapi import HTTPException

from api.db import FileSource
from api.db.db_models import File2Document, File, Document
from api.db.services.common_service import CommonService
from api.db.services.document_service import DocumentService
from api.utils import current_timestamp, datetime_format, get_uuid


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
    def insert(cls, db: Session, obj: dict):
        file2document = cls.save(db, **obj)
        return file2document

    @classmethod
    def delete_by_file_id(cls, db: Session, file_id: str):
        # return db.query(cls.model).filter_by(file_id=file_id).delete(synchronize_session=False)
        try:
            deleted_count = db.query(cls.model).filter(cls.model.file_id == file_id).delete(synchronize_session=False)
            db.commit()  # 确保提交事务
            return deleted_count
        except Exception as e:
            db.rollback()  # 回滚事务
            print(f"Error occurred: {e}")
            return 0

    @classmethod
    def delete_by_document_id(cls, db: Session, doc_id: str):
        # return db.query(cls.model).filter_by(document_id=doc_id).delete(synchronize_session=False)
        try:
            deleted_count = db.query(cls.model).filter(cls.model.document_id == doc_id).delete(synchronize_session=False)
            db.commit()  # 确保提交事务
            return deleted_count
        except Exception as e:
            db.rollback()  # 回滚事务
            print(f"Error occurred: {e}")
            return 0

    @classmethod
    def update_by_file_id(cls, db: Session, file_id: str, obj: dict):
        obj["update_time"] = current_timestamp()
        obj["update_date"] = datetime_format(datetime.now())
        db.query(cls.model).filter_by(id=file_id).update(obj)
        db.commit()
        updated_obj = db.query(cls.model).filter_by(id=file_id).one()
        return updated_obj

    @classmethod
    def get_minio_address(cls, db: Session, doc_id: str = None, file_id: str = None):
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
