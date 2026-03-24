# coding=utf-8
"""
@project: multirag
@Author：龙
@file： file_service.py
@date：2024/7/15 15:00
@desc:
"""
import asyncio
import base64
import logging
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import xxhash
from sqlalchemy.orm import Session
from sqlalchemy import func, select

from api.db import KNOWLEDGEBASE_FOLDER_NAME, FileType
from common.constants import FileSource, ParserType, TaskStatus
from api.db.db_models import Document, File, File2Document, Knowledgebase, Task
from api.db.services import duplicate_name
from api.db.services.common_service import CommonService
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from common.misc_utils import get_uuid
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.task_service import TaskService
from api.utils.file_utils import filename_type, read_potential_broken_pdf, thumbnail_img, sanitize_path
from core.llm.cv_model.models.gptv4 import GptV4
from common import settings


class FileService(CommonService):
    model = File

    def __init__(self):
        super().__init__(File)

    @classmethod
    def get_by_pf_id(cls, db: Session, tenant_id: str, pf_id: str, page_number: int, items_per_page: int,
                     orderby: str, desc: bool, keywords: str | None = None) -> tuple[list[dict], int]:
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
                file["kbs_info"] = []
                # 检查该文件夹是否有子文件夹
                children = db.query(cls.model).filter(
                    cls.model.tenant_id == tenant_id,
                    cls.model.parent_id == file["id"],
                    cls.model.id != file["id"]
                ).all()

                file["has_child_folder"] = any(child.to_dict()["type"] == FileType.FOLDER.value for child in children)
            else:
                file["kbs_info"] = cls.get_kb_id_by_file_id(db, file["id"])

        return res_files, count

    @classmethod
    def get_kb_id_by_file_id(cls, db: Session, file_id: str) -> list[dict]:
        """
        根据文件ID获取关联的知识库信息

        Args:
            db: 数据库会话
            file_id: 文件ID

        Returns:
            知识库信息列表，包含 kb_id, kb_name, document_id
        """
        stmt = (
            select(Knowledgebase.id, Knowledgebase.name, File2Document.document_id)
            .select_from(cls.model)
            .join(File2Document, File2Document.file_id == cls.model.id)
            .join(Document, File2Document.document_id == Document.id)
            .join(Knowledgebase, Knowledgebase.id == Document.kb_id)
            .where(cls.model.id == file_id)
        )
        result = db.execute(stmt).all()
        if not result:
            return []
        return [{"kb_id": row.id, "kb_name": row.name, "document_id": row.document_id} for row in result]

    @classmethod
    def get_by_pf_id_name(cls, db: Session, id: str, name: str) -> File | None:
        file = db.query(cls.model).filter_by(parent_id=id, name=name).first()
        if file:
            return file
        return None

    @classmethod
    def get_id_list_by_id(cls, db: Session, id: str, name: list[str], count: int, res: list[str]) -> list[str]:
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
    def get_all_innermost_file_ids(cls, db: Session, folder_id: str, result_ids: list[str]) -> list[str]:
        subfolders = db.query(cls.model).filter_by(parent_id=folder_id).all()
        if subfolders:
            for subfolder in subfolders:
                cls.get_all_innermost_file_ids(db, subfolder.id, result_ids)
        else:
            result_ids.append(folder_id)
        return result_ids

    @classmethod
    def get_all_file_ids_by_tenant_id(cls, db: Session, tenant_id: str) -> list[dict]:
        """根据tenant_id批量查询所有文件ID，使用分页避免内存溢出"""
        stmt = (
            select(cls.model.id)
            .where(cls.model.tenant_id == tenant_id)
            .order_by(cls.model.create_time.asc())
        )

        offset, limit = 0, 100
        res = []

        while True:
            try:
                file_batch = db.execute(
                    stmt.offset(offset).limit(limit)
                ).scalars().all()

                if not file_batch:
                    break

                res.extend([{"id": file_id} for file_id in file_batch])
                offset += limit
            except Exception:
                logging.exception("Failed to get file IDs for tenant_id=%s at offset %d", tenant_id, offset)
                break

        return res

    @classmethod
    def create_folder(cls, db: Session, file: File, parent_id: str, name: list[str], count: int) -> File:
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
    def get_root_folder(cls, db: Session, tenant_id: str) -> dict:
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

    # @classmethod
    # def get_kb_folder(cls, db: Session, tenant_id: str) -> dict:
    #     root = db.query(cls.model).filter_by(tenant_id=tenant_id, parent_id=cls.model.id).first()
    #     if root:
    #         folder = db.query(cls.model).filter_by(tenant_id=tenant_id, parent_id=root.id,
    #                                                name=KNOWLEDGEBASE_FOLDER_NAME).first()
    #         if folder:
    #             return folder.to_dict()
    #     raise RuntimeError("Can't find the KB folder. Database init error.")

    @classmethod
    def get_kb_folder(cls, db: Session, tenant_id: str) -> dict:
        root_folder = cls.get_root_folder(db, tenant_id)
        root_id = root_folder["id"]

        kb_folder = db.query(cls.model).filter_by(
            tenant_id=tenant_id,
            parent_id=root_id,
            name=KNOWLEDGEBASE_FOLDER_NAME
        ).first()

        if not kb_folder:
            kb_folder = cls.new_a_file_from_kb(db, tenant_id, KNOWLEDGEBASE_FOLDER_NAME, root_id)
            return kb_folder

        return kb_folder.to_dict()

    @classmethod
    def new_a_file_from_kb(cls, db: Session, tenant_id: str, name: str, parent_id: str, ty=FileType.FOLDER.value,
                           size=0, location="") -> dict:
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
    def get_all_parent_folders(cls, db: Session, start_id: str) -> list[File]:
        parent_folders = []
        current_id = start_id
        while current_id:
            file = cls.get_by_id(db, current_id)
            if file and file.parent_id != file.id:
                parent_folders.append(file)
                current_id = file.parent_id
            else:
                if file:
                    parent_folders.append(file)
                break
        return parent_folders

    @classmethod
    def insert(cls, db: Session, file_data: dict) -> File:
        if not cls.save(db, **file_data):
            raise RuntimeError("Database error (File)!")
        return File(**file_data)

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
        except Exception:
            logging.exception("delete_folder_by_pf_id")
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
    def add_file_from_kb(cls, db: Session, doc: dict, kb_folder_id: str, tenant_id: str):
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
    def move_file(cls, db: Session, file_ids: list[str], folder_id: str):
        try:
            cls.filter_update(db, [cls.model.id.in_(file_ids)], {'parent_id': folder_id})
        except Exception:
            logging.exception("move_file")
            raise RuntimeError("Database error (File move)!")

    @classmethod
    def upload_document(cls, db: Session, kb: Knowledgebase, file_objs: list, user_id, labels: list[str] | None = None, src: str="local", parent_path: str | None = None) -> tuple[list[str], list[tuple[dict, bytes]]]:
        # 初始化根文件夹和知识库文件夹
        root_folder = cls.get_root_folder(db, user_id)
        pf_id = root_folder["id"]
        cls.init_knowledgebase_docs(db, pf_id, user_id)
        kb_root_folder = cls.get_kb_folder(db, user_id)
        kb_folder = cls.new_a_file_from_kb(db, kb.tenant_id, kb.name, kb_root_folder["id"])

        safe_parent_path = sanitize_path(parent_path)

        err, files_info = [], []
        for file_obj in file_objs:
            # 支持 FileObj 对象（带 id/read()）和原始 (blob, filename) 元组两种格式
            if isinstance(file_obj, tuple):
                file_blob, filename = file_obj
                file_id = None
            else:
                file_blob = file_obj.read()
                filename = file_obj.filename
                file_id = file_obj.id if hasattr(file_obj, "id") else None

            # 如果文档 id 已存在，直接更新存储内容而非新建
            if file_id is not None:
                existing_doc = DocumentService.get_by_id(db, file_id)
                if existing_doc is not None:
                    try:
                        new_hash = xxhash.xxh128(file_blob).hexdigest()
                        old_hash = existing_doc.content_hash or ""
                        settings.STORAGE_IMPL.put(kb.id, existing_doc.location, file_blob)
                        existing_doc.size = len(file_blob)
                        existing_doc.content_hash = new_hash
                        doc_dict = existing_doc.to_dict()
                        DocumentService.update_by_id(db, file_id, doc_dict)
                        if new_hash != old_hash:
                            files_info.append((doc_dict, file_blob))
                    except Exception as exc:
                        logging.exception(f"Failed to update document {file_id}: {exc}")
                        err.append(filename + ": " + str(exc))
                    continue

            try:
                DocumentService.check_doc_health(db, kb.tenant_id, filename)
                filename = duplicate_name(
                    lambda *args, **kwargs: DocumentService.query(db, *args, **kwargs),
                    name=filename,
                    kb_id=kb.id)

                filetype = filename_type(filename)
                if filetype == FileType.OTHER.value:
                    raise RuntimeError("This type of file has not been supported yet!")

                location = filename if not safe_parent_path else f"{safe_parent_path}/{filename}"
                while settings.STORAGE_IMPL.obj_exist(kb.id, location):
                    location += "_"

                # 如果是PDF文件，尝试修复可能损坏的PDF
                if filetype == FileType.PDF.value:
                    file_blob = read_potential_broken_pdf(file_blob)

                settings.STORAGE_IMPL.put(kb.id, location, file_blob)

                if file_id is None:
                    file_id = get_uuid()

                img = thumbnail_img(filename, file_blob)
                thumbnail_location = ""
                if img is not None:
                    thumbnail_location = f"thumbnail_{file_id}.png"
                    settings.STORAGE_IMPL.put(kb.id, thumbnail_location, img)

                doc = {
                    "id": file_id,
                    "kb_id": kb.id,
                    "parser_id": cls.get_parser(filetype, filename, kb.parser_id),
                    "pipeline_id": kb.pipeline_id,
                    "parser_config": kb.parser_config,
                    "created_by": user_id,
                    "type": filetype,
                    "name": filename,
                    "source_type": src,
                    "suffix": Path(filename).suffix.lstrip("."),
                    "location": location,
                    "size": len(file_blob),
                    "thumbnail": thumbnail_location,
                    "content_hash": xxhash.xxh128(file_blob).hexdigest(),
                    "auth": json.dumps(labels) if labels else None  # 将 labels 转换为 JSON 字符串
                }
                DocumentService.insert(db, doc)

                cls.add_file_from_kb(db, doc, kb_folder["id"], kb.tenant_id)
                files_info.append((doc, file_blob))  # 返回文档信息和二进制数据
            except Exception as e:
                err.append(f"{filename}: {str(e)}")

        return err, files_info

    @classmethod
    def list_all_files_by_parent_id(cls, db: Session, parent_id: str) -> list[File]:
        """
        根据父文件夹ID查询所有子文件和子文件夹
        
        Args:
            db: 数据库会话
            parent_id: 父文件夹ID
            
        Returns:
            文件列表
        """
        try:
            stmt = (
                select(cls.model)
                .where(
                    cls.model.parent_id == parent_id,
                    cls.model.id != parent_id
                )
            )
            files = db.execute(stmt).scalars().all()
            return list(files)
        except Exception:
            logging.exception("list_by_parent_id failed")
            raise RuntimeError("Database error (list_by_parent_id)!")

    @staticmethod
    def parse_docs(file_data, user_id):
        exe = ThreadPoolExecutor(max_workers=12)
        threads = []

        for blob, filename in file_data:
            threads.append(exe.submit(FileService.parse, filename, blob, False, user_id))

        res = []
        for th in threads:
            res.append(th.result())

        return "\n\n".join(res)

    @staticmethod
    def parse(filename, blob, img_base64=True, tenant_id=None, layout_recognize=None):
        from core.app import audio, email, naive, picture, presentation

        def dummy(prog=None, msg=""):
            pass

        FACTORY = {ParserType.PRESENTATION.value: presentation, ParserType.PICTURE.value: picture, ParserType.AUDIO.value: audio, ParserType.EMAIL.value: email}
        parser_config = {"chunk_token_num": 16096, "delimiter": "\n!?;。；！？", "layout_recognize": layout_recognize or "Plain Text"}
        kwargs = {"lang": "Chinese", "callback": dummy, "parser_config": parser_config, "from_page": 0, "to_page": 100000, "tenant_id": tenant_id}
        file_type = filename_type(filename)
        if img_base64 and file_type == FileType.VISUAL.value:
            return GptV4.image2base64(blob)
        cks = FACTORY.get(FileService.get_parser(file_type, filename, ""), naive).chunk(filename, blob, **kwargs)
        return f"\n -----------------\nFile: {filename}\nContent as following: \n" + "\n".join([ck["content_with_weight"] for ck in cks])

    @staticmethod
    def get_parser(doc_type, filename, default):
        if doc_type == FileType.VISUAL:
            return ParserType.PICTURE.value
        if doc_type == FileType.AURAL:
            return ParserType.AUDIO.value
        if re.search(r"\.(ppt|pptx|pages)$", filename):
            return ParserType.PRESENTATION.value
        if re.search(r"\.(msg|eml)$", filename):
            return ParserType.EMAIL.value
        return default

    @staticmethod
    def get_blob(user_id, location):
        bname = f"{user_id}-downloads"
        return  settings.STORAGE_IMPL.get(bname, location)

    @staticmethod
    def put_blob(user_id, location, blob):
        bname = f"{user_id}-downloads"
        return  settings.STORAGE_IMPL.put(bname, location, blob)

    @classmethod
    def delete_docs(cls, db: Session, doc_ids, tenant_id):
        root_folder = FileService.get_root_folder(db, tenant_id)
        pf_id = root_folder["id"]
        FileService.init_knowledgebase_docs(db, pf_id, tenant_id)
        errors = ""
        kb_table_num_map = {}
        for doc_id in doc_ids:
            try:
                doc = DocumentService.get_by_id(db, doc_id)
                if not doc:
                    raise Exception("Document not found!")
                tenant_id = DocumentService.get_tenant_id(db, doc_id)
                if not tenant_id:
                    raise Exception("Tenant not found!")

                # 在删除文档前保存需要的属性，避免访问已删除对象
                doc_parser = doc.parser_id
                kb_id = doc.kb_id

                b, n = File2DocumentService.get_storage_address(db, doc_id=doc_id)

                TaskService.filter_delete(db, [Task.doc_id == doc_id])
                if not DocumentService.remove_document(db, doc, tenant_id):
                    raise Exception("Database error (Document removal)!")

                f2d = File2DocumentService.get_by_document_id(db, doc_id)
                deleted_file_count = 0
                if f2d:
                    deleted_file_count = FileService.filter_delete(
                        db, [File.source_type == FileSource.KNOWLEDGEBASE, File.id == f2d[0].file_id])
                File2DocumentService.delete_by_document_id(db, doc_id)
                if deleted_file_count > 0:
                    settings.STORAGE_IMPL.rm(b, n)

                if doc_parser == ParserType.TABLE:
                    if kb_id not in kb_table_num_map:
                        counts = DocumentService.count_by_kb_id(db, kb_id=kb_id, keywords="", run_status=[TaskStatus.DONE], types=[])
                        kb_table_num_map[kb_id] = counts
                    kb_table_num_map[kb_id] -= 1
                    if kb_table_num_map[kb_id] <= 0:
                        KnowledgebaseService.delete_field_map(db, kb_id)
            except Exception as e:
                errors += str(e)

        return errors

    @staticmethod
    async def upload_info(db: Session, user_id, file, url: str | None = None):
        """
        上传文件或从URL下载内容
        
        Args:
            db: 数据库会话（用于健康检查）
            user_id: 用户ID
            file: 文件对象（可以是 FastAPI UploadFile 或普通文件对象，也可以是 None）
            url: URL地址（可选），用于爬取网页内容
        """
        def structured(filename, filetype, blob, content_type):
            nonlocal user_id
            if filetype == FileType.PDF.value:
                blob = read_potential_broken_pdf(blob)

            location = get_uuid()
            FileService.put_blob(user_id, location, blob)

            return {
                "id": location,
                "name": filename,
                "size": sys.getsizeof(blob),
                "extension": filename.split(".")[-1].lower(),
                "mime_type": content_type,
                "created_by": user_id,
                "created_at": time.time(),
                "preview_url": None
            }

        if url:
            from crawl4ai import (
                AsyncWebCrawler,
                BrowserConfig,
                CrawlerRunConfig,
                DefaultMarkdownGenerator,
                PruningContentFilter,
                CrawlResult
            )
            filename = re.sub(r"\?.*", "", url.split("/")[-1])

            browser_config = BrowserConfig(
                headless=True,
                verbose=False,
            )
            async with AsyncWebCrawler(config=browser_config) as crawler:
                crawler_config = CrawlerRunConfig(
                    markdown_generator=DefaultMarkdownGenerator(
                        content_filter=PruningContentFilter()
                    ),
                    pdf=True,
                    screenshot=False
                )
                page: CrawlResult = await crawler.arun(
                    url=url,
                    config=crawler_config
                )
            
            if page.pdf:
                if filename.split(".")[-1].lower() != "pdf":
                    filename += ".pdf"
                return structured(filename, "pdf", page.pdf, page.response_headers.get("content-type", "application/pdf"))

            return structured(filename, "html", str(page.markdown).encode("utf-8"), page.response_headers.get("content-type", "text/html"))

        # 处理文件上传
        if hasattr(file, 'read'):
            # 支持异步和同步读取
            if asyncio.iscoroutinefunction(file.read):
                file_content = await file.read()
            else:
                file_content = file.read()
        else:
            raise ValueError("Invalid file object")
            
        DocumentService.check_doc_health(db, user_id, file.filename)
        return structured(file.filename, filename_type(file.filename), file_content, file.content_type)

    @staticmethod
    def get_files(files: list[dict] | None, raw: bool = False, layout_recognize: str = None) -> list[str] | tuple[list[str], list[bytes]]:
        if not files:
            return ([], []) if raw else []

        def image_to_base64(file):
            return "data:{};base64,{}".format(
                file["mime_type"],
                base64.b64encode(FileService.get_blob(file["created_by"], file["id"])).decode("utf-8")
            )

        exe = ThreadPoolExecutor(max_workers=5)
        threads = []
        imgs = []
        for file in files:
            if file["mime_type"].find("image") >= 0:
                if raw:
                    imgs.append(FileService.get_blob(file["created_by"], file["id"]))
                else:
                    threads.append(exe.submit(image_to_base64, file))
                continue
            threads.append(exe.submit(
                FileService.parse,
                file["name"],
                FileService.get_blob(file["created_by"], file["id"]),
                True,
                file["created_by"],
                layout_recognize
            ))

        if raw:
            return [th.result() for th in threads], imgs
        else:
            return [th.result() for th in threads]