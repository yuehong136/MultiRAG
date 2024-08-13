# coding=utf-8
"""
@project: multirag
@Author：龙
@file： file_service.py
@date：2024/7/15 15:00
@desc:
"""
import re
import os
from sqlalchemy.orm import Session, aliased
from sqlalchemy import func
from typing import List, Optional, Dict

from api.db import FileType, KNOWLEDGEBASE_FOLDER_NAME, FileSource, ParserType
from api.db.db_models import File, Document, Knowledgebase, File2Document
from api.db.services import duplicate_name
from api.db.services.common_service import CommonService
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.utils import get_uuid
from api.utils.file_utils import filename_type, thumbnail
from core.utils.minio_conn import MINIO


class FileService(CommonService):
    model = File

    def __init__(self):
        super().__init__(File)

    @classmethod
    def get_by_pf_id(cls, db: Session, tenant_id: str, pf_id: str, page_number: int, items_per_page: int,
                     orderby: str, desc: bool, keywords: Optional[str] = None) -> (List[Dict], int):
        query = db.query(cls.model).filter(
            cls.model.tenant_id == tenant_id,
            cls.model.parent_id == pf_id,
            cls.model.id != pf_id
        )

        if keywords:
            query = query.filter(func.lower(cls.model.name).contains(keywords.lower()))

        count = query.count()

        if desc:
            query = query.order_by(getattr(cls.model, orderby).desc())
        else:
            query = query.order_by(getattr(cls.model, orderby).asc())

        files = query.offset((page_number - 1) * items_per_page).limit(items_per_page).all()

        res_files = [file.to_dict() for file in files]
        for file in res_files:
            if file["type"] == FileType.FOLDER.value:
                file["size"] = cls.get_folder_size(db, file["id"])
                file['kbs_info'] = []
                # 检查该文件夹是否有子文件夹
                children = db.query(cls.model).filter(
                    cls.model.tenant_id == tenant_id,
                    cls.model.parent_id == file["id"],
                    cls.model.id != file["id"]
                ).all()

                file["has_child_folder"] = any(child.to_dict()["type"] == FileType.FOLDER.value for child in children)
            else:
                file['kbs_info'] = cls.get_kb_id_by_file_id(db, file['id'])

        return res_files, count

    @classmethod
    def get_kb_id_by_file_id(cls, db: Session, file_id: str) -> List[Dict]:
        # 使用 aliased 创建表别名
        KnowledgebaseAlias = aliased(Knowledgebase)
        DocumentAlias = aliased(Document)
        FileAlias = aliased(cls.model)
        kbs = db.query(Knowledgebase.id, Knowledgebase.name) \
            .select_from(FileAlias) \
            .join(File2Document, File2Document.file_id == FileAlias.id) \
            .join(DocumentAlias, File2Document.document_id == DocumentAlias.id) \
            .join(KnowledgebaseAlias, KnowledgebaseAlias.id == DocumentAlias.kb_id) \
            .filter(FileAlias.id == file_id).all()
        return [{"kb_id": kb.id, "kb_name": kb.name} for kb in kbs]

    @classmethod
    def get_by_pf_id_name(cls, db: Session, id: str, name: str) -> Optional[File]:
        file = db.query(cls.model).filter_by(parent_id=id, name=name).first()
        if file:
            return file
        return None

    @classmethod
    def get_id_list_by_id(cls, db: Session, id: str, name: List[str], count: int, res: List[str]) -> List[str]:
        if count < len(name):
            file = cls.get_by_pf_id_name(db, id, name[count])
            if file:
                res.append(file.id)
                return cls.get_id_list_by_id(db, file.id, name, count + 1, res)
            else:
                return res
        else:
            return res

    @classmethod
    def get_all_innermost_file_ids(cls, db: Session, folder_id: str, result_ids: List[str]) -> List[str]:
        subfolders = db.query(cls.model).filter_by(parent_id=folder_id).all()
        if subfolders:
            for subfolder in subfolders:
                cls.get_all_innermost_file_ids(db, subfolder.id, result_ids)
        else:
            result_ids.append(folder_id)
        return result_ids

    @classmethod
    def create_folder(cls, db: Session, file: File, parent_id: str, name: List[str], count: int) -> File:
        if count > len(name) - 2:
            return file
        else:
            new_file = cls.insert(db, {
                "id": get_uuid(),
                "parent_id": parent_id,
                "tenant_id": file.tenant_id,
                "created_by": file.created_by,
                "name": name[count],
                "location": "",
                "size": 0,
                "type": FileType.FOLDER.value
            })
            return cls.create_folder(db, new_file, new_file.id, name, count + 1)

    @classmethod
    def is_parent_folder_exist(cls, db: Session, parent_id: str) -> bool:
        parent_files = db.query(cls.model).filter_by(id=parent_id).count()
        if parent_files:
            return True
        cls.delete_folder_by_pf_id(db, parent_id)
        return False

    @classmethod
    def get_root_folder(cls, db: Session, tenant_id: str) -> Dict:
        root_folder = db.query(cls.model).filter_by(tenant_id=tenant_id, parent_id=cls.model.id).first()
        if root_folder:
            return root_folder.to_dict()

        file_id = get_uuid()
        file_data = {
            "id": file_id,
            "parent_id": file_id,
            "tenant_id": tenant_id,
            "created_by": tenant_id,
            "name": "/",
            "type": FileType.FOLDER.value,
            "size": 0,
            "location": "",
        }
        root_folder = cls.insert(db, file_data)
        return root_folder.to_dict()

    @classmethod
    def get_kb_folder(cls, db: Session, tenant_id: str) -> Dict:
        root = db.query(cls.model).filter_by(tenant_id=tenant_id, parent_id=cls.model.id).first()
        if root:
            folder = db.query(cls.model).filter_by(tenant_id=tenant_id, parent_id=root.id,
                                                   name=KNOWLEDGEBASE_FOLDER_NAME).first()
            if folder:
                return folder.to_dict()
        raise RuntimeError("Can't find the KB folder. Database init error.")

    @classmethod
    def new_a_file_from_kb(cls, db: Session, tenant_id: str, name: str, parent_id: str, ty=FileType.FOLDER.value,
                           size=0, location="") -> Dict:
        existing_files = cls.query(db, tenant_id=tenant_id, parent_id=parent_id, name=name)
        if existing_files:
            # 处理查询返回的列表
            existing_file = existing_files[0]  # 获取第一个匹配的文件
            return existing_file.to_dict()
        file_data = {
            "id": get_uuid(),
            "parent_id": parent_id,
            "tenant_id": tenant_id,
            "created_by": tenant_id,
            "name": name,
            "type": ty,
            "size": size,
            "location": location,
            "source_type": FileSource.KNOWLEDGEBASE
        }
        new_file = cls.insert(db, file_data)
        return new_file.to_dict()

    @classmethod
    def init_knowledgebase_docs(cls, db: Session, root_id: str, tenant_id: str):
        kb_folder_exists = db.query(cls.model).filter_by(name=KNOWLEDGEBASE_FOLDER_NAME, parent_id=root_id).first()
        if kb_folder_exists:
            return

        folder = cls.new_a_file_from_kb(db, tenant_id, KNOWLEDGEBASE_FOLDER_NAME, root_id)

        for kb in db.query(Knowledgebase).filter_by(tenant_id=tenant_id).all():
            kb_folder = cls.new_a_file_from_kb(db, tenant_id, kb.name, folder["id"])
            for doc in DocumentService.query(db, kb_id=kb.id):
                cls.add_file_from_kb(db, doc.to_dict(), kb_folder["id"], tenant_id)

    @classmethod
    def get_parent_folder(cls, db: Session, file_id: str) -> File:
        file = db.query(cls.model).filter_by(id=file_id).first()
        if file:
            parent_file = cls.get_by_id(db, file.parent_id)
            if parent_file:
                return parent_file
            raise RuntimeError("Database error (File retrieval)!")
        raise RuntimeError("Database error (File doesn't exist)!")

    @classmethod
    def get_all_parent_folders(cls, db: Session, start_id: str) -> List[File]:
        parent_folders = []
        current_id = start_id
        while current_id:
            file = cls.get_by_id(db, current_id)
            if file.parent_id != file.id:
                parent_folders.append(file)
                current_id = file.parent_id
            else:
                parent_folders.append(file)
                break
        return parent_folders

    @classmethod
    def insert(cls, db: Session, file_data: Dict) -> File:
        file = cls.save(db, **file_data)
        return file

    @classmethod
    def delete(cls, db: Session, file: File) -> int:
        return cls.delete_by_id(db, file.id)

    @classmethod
    def delete_by_pf_id(cls, db: Session, folder_id: str) -> int:
        return db.query(cls.model).filter_by(parent_id=folder_id).delete(synchronize_session=False)

    @classmethod
    def delete_folder_by_pf_id(cls, db: Session, user_id: str, folder_id: str) -> int:
        try:
            files = db.query(cls.model).filter_by(tenant_id=user_id, parent_id=folder_id).all()
            for file in files:
                cls.delete_folder_by_pf_id(db, user_id, file.id)
            return db.query(cls.model).filter_by(tenant_id=user_id, id=folder_id).delete(synchronize_session=False)
        except Exception as e:
            print(e)
            raise RuntimeError("Database error (File retrieval)!")

    @classmethod
    def get_file_count(cls, db: Session, tenant_id: str) -> int:
        return db.query(cls.model).filter_by(tenant_id=tenant_id).count()

    @classmethod
    def get_folder_size(cls, db: Session, folder_id: str) -> int:
        size = 0

        def dfs(parent_id):
            nonlocal size
            for f in db.query(cls.model).filter_by(parent_id=parent_id).all():
                size += f.size
                if f.type == FileType.FOLDER.value:
                    dfs(f.id)

        dfs(folder_id)
        return size

    @classmethod
    def add_file_from_kb(cls, db: Session, doc: Dict, kb_folder_id: str, tenant_id: str):
        if File2DocumentService.get_by_document_id(db, doc["id"]):
            return
        file_data = {
            "id": get_uuid(),
            "parent_id": kb_folder_id,
            "tenant_id": tenant_id,
            "created_by": tenant_id,
            "name": doc["name"],
            "type": doc["type"],
            "size": doc["size"],
            "location": doc["location"],
            "source_type": FileSource.KNOWLEDGEBASE
        }
        file = cls.save(db, **file_data)
        File2DocumentService.save(db, **{"id": get_uuid(), "file_id": file.id, "document_id": doc["id"]})

    @classmethod
    def move_file(cls, db: Session, file_ids: List[str], folder_id: str):
        try:
            cls.filter_update(db, [cls.model.id.in_(file_ids)], {'parent_id': folder_id})
        except Exception as e:
            print(e)
            raise RuntimeError("Database error (File move)!")
