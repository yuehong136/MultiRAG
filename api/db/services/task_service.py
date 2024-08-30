# coding=utf-8
"""
@project: multirag
@Author：龙
@file： task_service.py
@date：2024/7/22 15:10
@desc:
"""
import os
import random
from sqlalchemy.orm import Session, joinedload

from api.utils.db_utils import bulk_insert_into_db
from deepdoc.parser import PdfParser
from api.db.db_models import Task, Document, Knowledgebase, Tenant, File2Document, File
from api.db import StatusEnum, FileType, TaskStatus
from api.db.services.common_service import CommonService
from api.db.services.document_service import DocumentService
from api.utils import current_timestamp, get_uuid
from deepdoc.parser.excel_parser import RAGFlowExcelParser
from core.settings import SVR_QUEUE_NAME
from core.utils.minio_conn import MINIO
from core.utils.redis_conn import REDIS_CONN


class TaskService(CommonService):
    model = Task

    @classmethod
    def get_tasks(cls, db: Session, task_id):
        query = db.query(
            cls.model.id,
            cls.model.doc_id,
            cls.model.from_page,
            cls.model.to_page,
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
            return []

        # task = docs[0]
        # task.progress_msg = task.progress_msg + "\n" + "Task has been received."
        # task.progress = random.random() / 10.

        # 将查询结果转换为字典
        task = docs[0]._asdict()  # 转换为字典

        msg = "\nTask has been received."
        prog = random.random() / 10.
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
            return []

        return docs

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
                cls.model.create_time >= current_timestamp() - 1000 * 600
            ).all()

            if not docs:
                return []

            return list(set([(d.parent_id if d.parent_id else d.kb_id, d.location) for d in docs]))

    @classmethod
    def do_cancel(cls, db: Session, task_id):
        try:
            task = db.query(cls.model).get(task_id)
            doc = db.query(Document).get(task.doc_id)
            return doc.run == TaskStatus.CANCEL.value or doc.progress < 0
        except Exception:
            return False

    @classmethod
    def update_progress(cls, db: Session, task_id, info):
        task = db.query(cls.model).get(task_id)
        if not task:
            return
        if "progress_msg" in info:
            task.progress_msg += "\n" + info["progress_msg"]
        if "progress" in info:
            task.progress = info["progress"]
        db.commit()


def queue_tasks(db: Session, doc, bucket, name):
    def new_task():
        return {
            "id": get_uuid(),
            "doc_id": doc["id"]
        }

    tsks = []

    if doc["type"] == FileType.PDF.value:
        file_bin = MINIO.get(bucket, name)
        do_layout = doc["parser_config"].get("layout_recognize", True)
        pages = PdfParser.total_page_number(doc["name"], file_bin)
        page_size = doc["parser_config"].get("task_page_size", 12)
        if doc["parser_id"] == "paper":
            page_size = doc["parser_config"].get("task_page_size", 22)
        if doc["parser_id"] == "one":
            page_size = 1000000000
        if not do_layout:
            page_size = 1000000000
        page_ranges = doc["parser_config"].get("pages")
        if not page_ranges:
            page_ranges = [(1, 100000)]
        for s, e in page_ranges:
            s -= 1
            s = max(0, s)
            e = min(e - 1, pages)
            for p in range(s, e, page_size):
                task = new_task()
                task["from_page"] = p
                task["to_page"] = min(p + page_size, e)
                tsks.append(task)

    elif doc["parser_id"] == "table":
        file_bin = MINIO.get(bucket, name)
        rn = RAGFlowExcelParser.row_number(doc["name"], file_bin)
        for i in range(0, rn, 3000):
            task = new_task()
            task["from_page"] = i
            task["to_page"] = min(i + 3000, rn)
            tsks.append(task)
    else:
        tsks.append(new_task())

    bulk_insert_into_db(db, Task, tsks, True)
    DocumentService.begin2parse(db, doc["id"])

    for t in tsks:
        assert REDIS_CONN.queue_product(SVR_QUEUE_NAME,
                                        message=t), "Can't access Redis. Please check the Redis' status."