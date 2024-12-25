# coding=utf-8
"""
@project: multirag
@Author：龙
@file： task_service.py
@date：2024/7/22 15:10
@desc:
"""
import bisect
import os
import random
from datetime import datetime

import xxhash
from sqlalchemy import asc, desc, select, update, text
from sqlalchemy.orm import Session

from api.utils.db_utils import bulk_insert_into_db
from deepdoc.parser import PdfParser
from api.db.db_models import Task, Document, Knowledgebase, Tenant, File2Document, File
from api.db import StatusEnum, FileType, TaskStatus
from api.db.services.common_service import CommonService
from api.db.services.document_service import DocumentService
from api.utils import current_timestamp, get_uuid
from deepdoc.parser.excel_parser import RAGFlowExcelParser
from core.settings import SVR_QUEUE_NAME
from core.utils.storage_factory import STORAGE_IMPL
from core.utils.redis_conn import REDIS_CONN
from api import settings
from core.nlp import search

def trim_header_by_lines(text: str, max_length) -> str:
    len_text = len(text)
    if len_text <= max_length:
        return text
    for i in range(len_text):
        if text[i] == '\n' and len_text - i <= max_length:
            return text[i+1:]
    return text

class TaskService(CommonService):
    model = Task

    @classmethod
    def get_task(cls, db: Session, task_id):
        query = db.query(
            cls.model.id,
            cls.model.doc_id,
            cls.model.from_page,
            cls.model.to_page,
            cls.model.retry_count,
            Document.kb_id,
            Document.parser_id,
            Document.parser_config,
            Document.name,
            Document.type,
            Document.location,
            Document.size,
            Document.auth,
            Knowledgebase.tenant_id,
            Knowledgebase.language,
            Knowledgebase.embd_id,
            # Knowledgebase.pagerank,
            Tenant.img2txt_id,
            Tenant.asr_id,
            Tenant.llm_id,
            cls.model.update_time,
            cls.model.progress_msg
        ).join(Document, cls.model.doc_id == Document.id
               ).join(Knowledgebase, Document.kb_id == Knowledgebase.id
                      ).join(Tenant, Knowledgebase.tenant_id == Tenant.id
                             ).filter(cls.model.id == task_id)

        docs = query.all()
        if not docs:
            return None

        # 将结果转换为字典
        task = {col["name"]: value for col, value in zip(query.column_descriptions, docs[0])}

        msg = f"\n{datetime.now().strftime('%H:%M:%S')} Task has been received."
        prog = random.random() / 10.0
        if task["retry_count"] >= 3:
            msg = "\nERROR: Task is abandoned after 3 times attempts."
            prog = -1

        # 更新进度消息和进度
        task["progress_msg"] = task["progress_msg"] + "\n" + msg
        task["progress"] = prog

        # 将更新写入数据库
        db.query(cls.model).filter(cls.model.id == task["id"]).update({
            "progress_msg": task["progress_msg"],
            "progress": task["progress"]
        })

        db.commit()

        if task["retry_count"] >= 3:
            return None

        return task

    @classmethod
    def get_tasks(cls, db: Session, doc_id: str):
        fields = [
            cls.model.id,
            cls.model.from_page,
            cls.model.progress,
            cls.model.digest,
            cls.model.chunk_ids,
        ]
        stmt = (
            select(*fields)
            .where(cls.model.doc_id == doc_id)
            .order_by(asc(cls.model.from_page), desc(cls.model.create_time))
        )
        tasks = db.execute(stmt).mappings().all()
        if not tasks:
            return None
        return [dict(task) for task in tasks]

    @classmethod
    def update_chunk_ids(cls, db: Session, id: str, chunk_ids: str):
        stmt = (
            update(cls.model)
            .where(cls.model.id == id)
            .values(chunk_ids=chunk_ids)
        )
        db.execute(stmt)
        db.commit()

    @classmethod
    def get_ongoing_doc_name(cls, db: Session):
        with db.begin():
            docs = db.query(
                Document.id,
                Document.kb_id,
                Document.location,
                File.parent_id
            ).join(File2Document, File2Document.document_id == Document.id, isouter=True
                   ).join(File, File2Document.file_id == File.id, isouter=True
                          ).filter(
                Document.status == StatusEnum.VALID.value,
                Document.run == TaskStatus.RUNNING.value,
                Document.type != FileType.VIRTUAL.value,
                cls.model.progress < 1,
                cls.model.create_time >= current_timestamp() - 1000 * 600,
            ).all()

            if not docs:
                return []

            return list(
                set(
                    [
                        (
                            d.parent_id if d.parent_id else d.kb_id,
                            d.location,
                        )
                        for d in docs
                    ]
                )
            )

    @classmethod
    def do_cancel(cls, db: Session, task_id):
        # 使用 get_by_id 方法获取任务
        task = cls.get_by_id(db, task_id)
        # 获取与任务关联的文档
        doc = DocumentService.get_by_id(db, task.doc_id)
        # 判断文档是否满足取消条件
        return doc.run == TaskStatus.CANCEL.value or doc.progress < 0

    # @classmethod
    # def update_progress(cls, db: Session, task_id, info):
    #     task = db.query(cls.model).get(task_id)
    #     if not task:
    #         return
    #     if "progress_msg" in info:
    #         task.progress_msg += "\n" + info["progress_msg"]
    #     if "progress" in info:
    #         task.progress = info["progress"]
    #     db.commit()
    @classmethod
    def update_progress(cls, db: Session, id: str, info: dict):
        """
        更新任务的 progress 和 progress_msg，并使用数据库锁。
        """
        if os.environ.get("MACOS"):
            # 直接更新逻辑
            if "progress_msg" in info and info["progress_msg"]:
                task = db.query(cls.model).get(id)
                if task:
                    progress_msg = trim_header_by_lines(
                        (task.progress_msg or "") + "\n" + info["progress_msg"], 1000
                    )
                    db.execute(
                        update(cls.model)
                        .where(cls.model.id == id)
                        .values(progress_msg=progress_msg)
                    )

            if "progress" in info:
                db.execute(
                    update(cls.model)
                    .where(cls.model.id == id)
                    .values(progress=info["progress"])
                )
            db.commit()
            return
        # 动态生成锁名
        lock_name = f"update_progress_{id}"
        lock_query = text(f"SELECT pg_advisory_lock(hashtext('{lock_name}'))")
        unlock_query = text(f"SELECT pg_advisory_unlock(hashtext('{lock_name}'))")

        try:
            # 获取锁
            db.execute(lock_query)
            db.commit()

            # 更新逻辑
            if "progress_msg" in info and info["progress_msg"]:
                task = db.query(cls.model).get(id)
                if task:
                    progress_msg = trim_header_by_lines(
                        (task.progress_msg or "") + "\n" + info["progress_msg"], 1000
                    )
                    db.execute(
                        update(cls.model)
                        .where(cls.model.id == id)
                        .values(progress_msg=progress_msg)
                    )

            if "progress" in info:
                db.execute(
                    update(cls.model)
                    .where(cls.model.id == id)
                    .values(progress=info["progress"])
                )

            db.commit()

        finally:
            # 释放锁
            db.execute(unlock_query)
            db.commit()

def queue_tasks(db: Session, doc: dict, bucket: str, name: str):
    def new_task():
        return {"id": get_uuid(), "doc_id": doc["id"], "progress": 0.0, "from_page": 0, "to_page": 100000000}

    parse_task_array = []

    if doc["type"] == FileType.PDF.value:
        file_bin = STORAGE_IMPL.get(bucket, name)
        do_layout = doc["parser_config"].get("layout_recognize", True)
        pages = PdfParser.total_page_number(doc["name"], file_bin)
        page_size = doc["parser_config"].get("task_page_size", 12)
        if doc["parser_id"] == "paper":
            page_size = doc["parser_config"].get("task_page_size", 22)
        if doc["parser_id"] in ["one", "knowledge_graph"] or not do_layout:
            page_size = 10**9
        page_ranges = doc["parser_config"].get("pages") or [(1, 10**5)]
        for s, e in page_ranges:
            s -= 1
            s = max(0, s)
            e = min(e - 1, pages)
            for p in range(s, e, page_size):
                task = new_task()
                task["from_page"] = p
                task["to_page"] = min(p + page_size, e)
                parse_task_array.append(task)

    elif doc["parser_id"] == "table":
        file_bin = STORAGE_IMPL.get(bucket, name)
        rn = RAGFlowExcelParser.row_number(doc["name"], file_bin)
        for i in range(0, rn, 3000):
            task = new_task()
            task["from_page"] = i
            task["to_page"] = min(i + 3000, rn)
            parse_task_array.append(task)
    else:
        parse_task_array.append(new_task())

    chunking_config = DocumentService.get_chunking_config(db, doc["id"])
    for task in parse_task_array:
        hasher = xxhash.xxh64()
        for field in sorted(chunking_config.keys()):
            hasher.update(str(chunking_config[field]).encode("utf-8"))
        for field in ["doc_id", "from_page", "to_page"]:
            hasher.update(str(task.get(field, "")).encode("utf-8"))
        task_digest = hasher.hexdigest()
        task["digest"] = task_digest
        task["progress"] = 0.0

    prev_tasks = TaskService.get_tasks(db, doc["id"])
    ck_num = 0
    if prev_tasks:
        for task in parse_task_array:
            ck_num += reuse_prev_task_chunks(task, prev_tasks, chunking_config)
        TaskService.filter_delete(db, [Task.doc_id == doc["id"]])
        chunk_ids = []
        for task in prev_tasks:
            if task["chunk_ids"]:
                chunk_ids.extend(task["chunk_ids"].split())
        if chunk_ids:
            settings.docStoreConn.delete({"id": chunk_ids}, search.index_name_one(chunking_config["tenant_id"], chunking_config["name"]),
                                         chunking_config["kb_id"])
    DocumentService.update_by_id(db, doc["id"], {"chunk_num": ck_num})

    bulk_insert_into_db(db, Task, parse_task_array, True)
    DocumentService.begin2parse(db, doc["id"])

    unfinished_task_array = [task for task in parse_task_array if task["progress"] < 1.0]
    for unfinished_task in unfinished_task_array:
        assert REDIS_CONN.queue_product(
            SVR_QUEUE_NAME, message=unfinished_task
        ), "Can't access Redis. Please check the Redis' status."

def reuse_prev_task_chunks(task: dict, prev_tasks: list[dict], chunking_config: dict):
    idx = bisect.bisect_left(prev_tasks, (task.get("from_page", 0), task.get("digest", "")),
                             key=lambda x: (x.get("from_page", 0), x.get("digest", "")))
    if idx >= len(prev_tasks):
        return 0
    prev_task = prev_tasks[idx]
    if prev_task["progress"] < 1.0 or prev_task["digest"] != task["digest"] or not prev_task["chunk_ids"]:
        return 0
    task["chunk_ids"] = prev_task["chunk_ids"]
    task["progress"] = 1.0
    if "from_page" in task and "to_page" in task:
        task["progress_msg"] = f"Page({task['from_page']}~{task['to_page']}): "
    else:
        task["progress_msg"] = ""
    task["progress_msg"] += "reused previous task's chunks."
    prev_task["chunk_ids"] = ""

    return len(task["chunk_ids"].split())