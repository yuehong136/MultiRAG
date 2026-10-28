"""
@project: multirag
@Author：龙
@file： file2document_service.py
@date：2024/7/9 9:00
@desc:
"""

import copy
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from api.db import FileType
from api.db.db_models import Document, File, File2Document, Knowledgebase, Tenant, User
from api.db.services.common_service import CommonService, retry_db_operation
from api.db.services.document_service import DocumentService
from common.constants import FileSource, StatusEnum
from common.misc_utils import get_uuid
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
            objs = db.query(cls.model).filter(cls.model.document_id.in_(document_ids)).all()

            return [
                {
                    "id": obj.id,
                    "file_id": obj.file_id,
                    "document_id": obj.document_id,
                    "create_time": obj.create_time,
                    "create_date": obj.create_date,
                    "update_time": obj.update_time,
                    "update_date": obj.update_date,
                }
                for obj in objs
            ]
        except Exception:
            logging.exception("Failed to get file2document records by document_ids")
            return []

    _doc_fields = ("kb_id", "created_by", "location", "type", "source_type", "name", "size", "content_hash")
    _file_fields = ("id", "tenant_id", "created_by", "parent_id", "name", "location", "size", "type", "source_type")
    _kb_fields = ("tenant_id", "status", "permission")

    @staticmethod
    def _snapshot(db: Session, model: type[Any], identifier: str, fields: tuple[str, ...]) -> tuple[Any, ...] | None:
        row = db.execute(select(*(getattr(model, field) for field in fields)).where(model.id == identifier)).one_or_none()
        return tuple(row) if row is not None else None

    @classmethod
    def _lock_document(cls, db: Session, document_id: str, expected: tuple[Any, ...] | None) -> Document:
        doc = db.scalar(select(Document).where(Document.id == document_id).with_for_update().execution_options(populate_existing=True))
        if doc is None or expected is None:
            raise LookupError("Document is unavailable.")
        if tuple(getattr(doc, field) for field in cls._doc_fields) != expected:
            raise PermissionError("Document changed during attachment.")
        return doc

    @classmethod
    def _lock_kb(cls, db: Session, kb_id: str, expected: tuple[Any, ...] | None) -> Knowledgebase:
        kb = db.scalar(select(Knowledgebase).where(Knowledgebase.id == kb_id).order_by(Knowledgebase.id).with_for_update().execution_options(populate_existing=True))
        if kb is None or expected is None:
            raise LookupError("Dataset is unavailable.")
        if tuple(getattr(kb, field) for field in cls._kb_fields) != expected:
            raise PermissionError("Dataset changed during attachment.")
        return kb

    @classmethod
    def _lock_file(cls, db: Session, file_id: str, expected: tuple[Any, ...] | None) -> File:
        file = db.scalar(select(File).where(File.id == file_id).order_by(File.id).with_for_update().execution_options(populate_existing=True))
        if file is None or expected is None:
            raise LookupError("Source file is unavailable.")
        if tuple(getattr(file, field) for field in cls._file_fields) != expected:
            raise PermissionError("Source file changed during attachment.")
        return file

    @staticmethod
    def _authorize_actor(db: Session, kb: Knowledgebase, *, actor_id: str, file: File | None = None) -> None:
        """Check the current actor/dataset scope even for an attachment no-op."""
        from api.common.check_team_permission import check_file_team_permission, check_kb_team_permission

        tenants = {actor_id, kb.tenant_id}
        if file is not None:
            tenants.add(file.tenant_id)
        active_tenants = set(db.scalars(select(Tenant.id).where(Tenant.id.in_(tenants), Tenant.status == StatusEnum.VALID.value)))
        actor = db.scalar(select(User.id).where(User.id == actor_id, User.status == StatusEnum.VALID.value, User.is_active.is_(True)))
        if not actor_id or active_tenants != tenants or actor is None or not check_kb_team_permission(db, kb, actor_id) or (file is not None and not check_file_team_permission(db, file, actor_id)):
            raise PermissionError("No authorization for the source file or dataset.")

    @classmethod
    def _attach_locked(cls, db: Session, doc: Document, kb: Knowledgebase, file: File, *, actor_id: str, identifier: str) -> File2Document:
        """Validate current permissions and stage a link; the caller owns locks/commit."""
        if doc.kb_id != kb.id:
            raise PermissionError("No authorization for the source file or dataset.")
        cls._authorize_actor(db, kb, actor_id=actor_id, file=file)
        row = cls.model(id=identifier, file_id=file.id, document_id=doc.id)
        db.add(row)
        return row

    @classmethod
    def _attach_created_file(cls, db: Session, doc: Document, kb: Knowledgebase, file: File, *, actor_id: str, identifier: str) -> File2Document:
        """Stage only the fresh KB File created from this locked document.

        The new File has no existing share link yet. Its content and ownership
        come from the authorized document/KB, rather than a preexisting File.
        """
        if doc.kb_id != kb.id or file.source_type != FileSource.KNOWLEDGEBASE or file.tenant_id != kb.tenant_id or file.created_by != kb.tenant_id:
            raise PermissionError("No authorization for the source file or dataset.")
        if (file.name, file.type, file.size, file.location) != (doc.name, doc.type, doc.size, doc.location):
            raise PermissionError("Document or source file changed during attachment.")
        cls._authorize_actor(db, kb, actor_id=actor_id)
        row = cls.model(id=identifier, file_id=file.id, document_id=doc.id)
        db.add(row)
        return row

    @classmethod
    @retry_db_operation(max_attempts=3)
    def save(cls, db: Session, *, actor_id: str, **kwargs: Any) -> File2Document:
        """Own the entire retry and Document -> KB -> File lock/commit sequence."""
        try:
            with db.no_autoflush:
                document_id, file_id = kwargs.get("document_id"), kwargs.get("file_id")
                if not actor_id or not isinstance(document_id, str) or not document_id or not isinstance(file_id, str) or not file_id:
                    raise ValueError("A source file, document and authorized actor are required.")
                doc_before = cls._snapshot(db, Document, document_id, cls._doc_fields)
                file_before = cls._snapshot(db, File, file_id, cls._file_fields)
                if doc_before is None or file_before is None:
                    raise LookupError("Document or source file is unavailable.")
                kb_before = cls._snapshot(db, Knowledgebase, doc_before[0], cls._kb_fields)
                doc = cls._lock_document(db, document_id, doc_before)
                kb = cls._lock_kb(db, doc.kb_id, kb_before)
                file = cls._lock_file(db, file_id, file_before)
                row = cls._attach_locked(db, doc, kb, file, actor_id=actor_id, identifier=kwargs["id"])
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(row)
        return row

    @classmethod
    def insert(cls, db: Session, obj: dict[str, Any], *, actor_id: str) -> File2Document:
        return cls.save(db, actor_id=actor_id, **obj)

    @classmethod
    @retry_db_operation(max_attempts=3)
    def create_file_for_document(cls, db: Session, document_id: str, kb_folder_id: str, *, actor_id: str) -> File2Document | None:
        """Commit a new knowledgebase File and its mapping together."""
        try:
            with db.no_autoflush:
                doc_before = cls._snapshot(db, Document, document_id, cls._doc_fields)
                if doc_before is None:
                    raise LookupError("Document is unavailable.")
                kb_before = cls._snapshot(db, Knowledgebase, doc_before[0], cls._kb_fields)
                doc = cls._lock_document(db, document_id, doc_before)
                kb = cls._lock_kb(db, doc.kb_id, kb_before)
                cls._authorize_actor(db, kb, actor_id=actor_id)
                if db.scalar(select(File2Document.id).where(File2Document.document_id == doc.id).limit(1)) is not None:
                    db.commit()
                    return None
                file = File(
                    id=get_uuid(),
                    parent_id=kb_folder_id,
                    tenant_id=kb.tenant_id,
                    created_by=kb.tenant_id,
                    name=doc.name,
                    type=doc.type,
                    size=doc.size,
                    location=doc.location,
                    source_type=FileSource.KNOWLEDGEBASE,
                )
                # This unpublished row is exclusively owned by this transaction.
                # Existing resources were locked in Document -> KB order first.
                db.add(file)
                db.flush([file])
                row = cls._attach_created_file(db, doc, kb, file, actor_id=actor_id, identifier=get_uuid())
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(row)
        return row

    @classmethod
    @retry_db_operation(max_attempts=3)
    def create_document_for_file(
        cls,
        db: Session,
        file_id: str,
        kb_id: str,
        *,
        actor_id: str,
        expected_file: dict[str, Any],
        document_id: str,
    ) -> Document | None:
        """Commit a new document, KB count and source mapping as one transaction."""
        from api.db.services import duplicate_name
        from api.db.services.file_service import FileService

        try:
            with db.no_autoflush:
                if not actor_id or expected_file.get("id") != file_id or any(field not in expected_file for field in cls._file_fields):
                    raise ValueError("A complete source file snapshot and authorized actor are required.")
                kb_before = cls._snapshot(db, Knowledgebase, kb_id, cls._kb_fields)
                # The fresh UUID row is unpublished and exclusively ours. Flush
                # it before KB/File acquisition so every path keeps Doc -> KB -> File.
                doc = Document(id=document_id, kb_id=kb_id, created_by=actor_id, parser_id="", parser_config={}, type="")
                db.add(doc)
                db.flush([doc])
                kb = cls._lock_kb(db, kb_id, kb_before)
                file = cls._lock_file(db, file_id, tuple(expected_file[field] for field in cls._file_fields))
                cls._authorize_actor(db, kb, actor_id=actor_id, file=file)
                existing_pair = db.scalar(select(Document.id).join(File2Document, File2Document.document_id == Document.id).where(File2Document.file_id == file.id, Document.kb_id == kb.id).limit(1))
                if existing_pair is not None:
                    # Another conversion may have committed while we waited.
                    # Only our unpublished placeholder is removed; never lock
                    # an existing Document after acquiring its KB/File locks.
                    db.delete(doc)
                    db.commit()
                    return None

                def existing_name(**filters: Any) -> Any:
                    return DocumentService.query(db, **filters)

                # No source-dependent name/parser/content address is consumed
                # until the locked File matches the caller's snapshot.
                filename = duplicate_name(existing_name, name=file.name, kb_id=kb.id)
                doc.parser_id = FileService.get_parser(file.type, filename, kb.parser_id)
                doc.pipeline_id = kb.pipeline_id
                doc.parser_config = copy.deepcopy(kb.parser_config)
                doc.type, doc.name, doc.location, doc.size = file.type, filename, file.location, file.size
                doc.suffix = Path(filename).suffix.lstrip(".")
                cls._attach_locked(db, doc, kb, file, actor_id=actor_id, identifier=get_uuid())
                result = db.execute(
                    update(Knowledgebase)
                    .where(Knowledgebase.id == kb.id)
                    .values(
                        doc_num=Knowledgebase.doc_num + 1,
                        update_time=current_timestamp(),
                        update_date=datetime_format(datetime.now()),
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    raise RuntimeError("Dataset document count could not be updated.")
            db.commit()
        except Exception:
            db.rollback()
            raise
        db.refresh(doc)
        return doc

    @classmethod
    def delete_by_file_id(cls, db: Session, file_id: str):
        try:
            deleted_count = db.query(cls.model).filter(cls.model.file_id == file_id).delete(synchronize_session=False)
            db.commit()  # 确保提交事务
            return deleted_count
        except Exception:
            db.rollback()  # 回滚事务
            logging.exception("[delete_by_file_id] Error occurred")
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
                result = db.query(cls.model).filter(cls.model.file_id.in_(file_ids)).delete(synchronize_session=False)
            elif not file_ids:
                # 只有 document_ids
                result = db.query(cls.model).filter(cls.model.document_id.in_(document_ids)).delete(synchronize_session=False)
            else:
                # 两者都有，使用 OR 条件
                result = db.query(cls.model).filter(or_(cls.model.document_id.in_(document_ids), cls.model.file_id.in_(file_ids))).delete(synchronize_session=False)

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
            logging.exception("[delete_by_file_id] Error occurred")
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
    def get_authorized_document_storage_address(cls, db: Session, doc_id: str, tenant_id: str) -> tuple[str, str]:
        """Resolve a document's registered object only inside an owned dataset.

        File-to-document links intentionally share source bytes with datasets.
        For local source bytes the namespace must be a folder of the source
        file's tenant; a forged parent must never redirect into another tenant.
        """
        denied = "You do not have access to this document."
        tenant = db.get(Tenant, tenant_id)
        doc = db.get(Document, doc_id)
        kb = db.get(Knowledgebase, doc.kb_id) if doc else None
        if not tenant or tenant.status != StatusEnum.VALID.value or not doc or doc.status != StatusEnum.VALID.value or not kb or kb.status != StatusEnum.VALID.value or kb.tenant_id != tenant_id:
            raise PermissionError(denied)

        links = cls.get_by_document_id(db, doc_id)
        if len(links) > 1:
            raise PermissionError(denied)
        bucket, key = kb.id, doc.location
        if links:
            file = db.get(File, links[0].file_id) if links[0].file_id else None
            if file is None:
                raise PermissionError(denied)
            if not file.source_type or file.source_type == FileSource.LOCAL:
                folder = db.get(File, file.parent_id)
                if not file.tenant_id or not folder or folder.tenant_id != file.tenant_id or folder.type != FileType.FOLDER.value:
                    raise PermissionError(denied)
                bucket, key = folder.id, file.location
        # Storage adapters can join namespaces/keys to a filesystem or a shared
        # object prefix. Reject malformed addresses instead of normalizing them.
        if not bucket or bucket in {".", ".."} or any(c in bucket for c in "/\\") or any(ord(c) < 32 or ord(c) == 127 for c in bucket):
            raise PermissionError(denied)
        if not key or key.startswith("/") or "\\" in key or any(part in {".", ".."} for part in key.split("/")) or any(ord(c) < 32 or ord(c) == 127 for c in key):
            raise PermissionError(denied)
        return bucket, key

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
