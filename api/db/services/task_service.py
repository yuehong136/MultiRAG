# coding=utf-8
"""
@project: multirag
@Author：龙
@file： task_service.py
@date：2024/7/22 15:10
@desc:
"""
import logging
import os
import random
from datetime import datetime

import xxhash
from sqlalchemy import asc, desc, select, update, or_
from sqlalchemy.orm import Session

from api.utils.db_utils import bulk_insert_into_db
from deepdoc.parser import PdfParser
from api.db.db_models import Task, Document, Knowledgebase, Tenant, File2Document, File, DatabaseLock
from api.db import StatusEnum, FileType, TaskStatus
from api.db.services.common_service import CommonService
from api.db.services.document_service import DocumentService
from common.misc_utils import get_uuid
from common.time_utils import current_timestamp
from deepdoc.parser.excel_parser import RAGFlowExcelParser
from core.settings import get_svr_queue_name
from core.utils.storage_factory import STORAGE_IMPL
from core.utils.redis_conn import REDIS_CONN
from api import settings
from core.nlp import search

CANVAS_DEBUG_DOC_ID = "dataflow_x"
GRAPH_RAPTOR_FAKE_DOC_ID = "graph_raptor_x"

def trim_header_by_lines(text: str, max_length) -> str:
    len_text = len(text)
    if len_text <= max_length:
        return text
    for i in range(len_text):
        if text[i] == '\n' and len_text - i <= max_length:
            return text[i+1:]
    return text

class TaskService(CommonService):
    """Service class for managing document processing tasks.

    This class extends CommonService to provide specialized functionality for document
    processing task management, including task creation, progress tracking, and chunk
    management. It handles various document types (PDF, Excel, etc.) and manages their
    processing lifecycle.

    The class implements a robust task queue system with retry mechanisms and progress
    tracking, supporting both synchronous and asynchronous task execution.

    Attributes:
        model: The Task model class for database operations.
    """
    model = Task

    @classmethod
    def get_task(cls, db: Session, task_id, doc_ids=[]):
        # 先获取任务的doc_id，判断是否需要使用doc_ids[0]
        task_record = db.query(cls.model.doc_id).filter(cls.model.id == task_id).first()
        if not task_record:
            return None
        
        # 根据任务的doc_id决定用哪个ID去JOIN Document表
        task_doc_id = task_record[0]
        use_first_doc = task_doc_id in [CANVAS_DEBUG_DOC_ID, GRAPH_RAPTOR_FAKE_DOC_ID] and doc_ids
        
        if use_first_doc:
            # 对于fake_doc_id任务，使用doc_ids[0]去获取Document配置
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
                Knowledgebase.pagerank,
                Knowledgebase.parser_config.label("kb_parser_config"),
                Tenant.img2txt_id,
                Tenant.asr_id,
                Tenant.llm_id,
                cls.model.update_time,
            ).select_from(cls.model
                         ).join(Document, Document.id == doc_ids[0]
                                ).join(Knowledgebase, Document.kb_id == Knowledgebase.id
                                       ).join(Tenant, Knowledgebase.tenant_id == Tenant.id
                                              ).filter(cls.model.id == task_id)
        else:
            # 普通任务，使用Task.doc_id去JOIN
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
                Knowledgebase.pagerank,
                Knowledgebase.parser_config.label("kb_parser_config"),
                Tenant.img2txt_id,
                Tenant.asr_id,
                Tenant.llm_id,
                cls.model.update_time,
            ).select_from(cls.model
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
        # task["progress_msg"] = task["progress_msg"] + "\n" + msg
        # task["progress"] = prog

        # 将更新写入数据库
        db.query(cls.model).filter(cls.model.id == task["id"]).update({
            "progress_msg": cls.model.progress_msg + msg,
            "progress": prog
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

    # # ------------------ 私有拼 values ------------------
    # @classmethod
    # def _do_update(cls, session, id_: str, info: dict):
    #     values = {}
    #     if info.get("progress_msg"):
    #         task = session.get(cls.model, id_)
    #         values["progress_msg"] = trim_header_by_lines(
    #             (task.progress_msg or "") + "\n" + info["progress_msg"], 3000
    #         )
    #     if "progress" in info:
    #         values["progress"] = info["progress"]
    #
    #     if values:
    #         session.execute(
    #             update(cls.model)
    #             .where(cls.model.id == id_)
    #             .values(**values)
    #         )
    #
    # # ------------------ 同步核心（终版） -----------------
    # @classmethod
    # def _update_progress_sync(cls, id_: str, info: dict) -> None:
    #     """
    #     使用独立 AUTOCOMMIT 连接做 advisory-lock，
    #     再开启普通 Session 执行 UPDATE，避免事务状态冲突。
    #     """
    #     lock_key = f"update_progress_{id_}"
    #
    #     # ① 先拿一条 AUTOCOMMIT 连接来加/解锁
    #     with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as lock_conn:
    #         lock_conn.execute(
    #             text("SELECT pg_advisory_lock(hashtext(:k))"), {"k": lock_key}
    #         )
    #         try:
    #             # ② 再用常规 Session 完成数据更新
    #             with db_connection() as db:
    #                 cls._do_update(db, id_, info)
    #                 db.commit()
    #         finally:
    #             # ③ 立即释放锁
    #             lock_conn.execute(
    #                 text("SELECT pg_advisory_unlock(hashtext(:k))"), {"k": lock_key}
    #             )
    #
    # # ------------------ Trio 线程池壳 -------------------
    # @classmethod
    # async def update_progress(cls, id_: str, info: dict) -> None:
    #     await trio.to_thread.run_sync(
    #         cls._update_progress_sync, id_, info, cancellable=True
    #     )

    @classmethod
    def update_progress(cls, db: Session, id: str, info: dict):
        """Update the progress information for a task.

        This method updates both the progress message and completion percentage of a task.
        It handles platform-specific behavior (macOS vs others) and uses database locking
        when necessary to ensure thread safety.

        Update Rules:
            - progress_msg: Always appends the new message to the existing one, and trims the result to max 3000 lines.
            - progress: Only updates if the current progress is not -1 AND
                        (the new progress is -1 OR greater than the existing progress),
                        to avoid overwriting valid progress with invalid or regressive values.

        Args:
            id (str): The unique identifier of the task to update.
            info (dict): Dictionary containing progress information with keys:
                        - progress_msg (str, optional): Progress message to append
                        - progress (float, optional): Progress percentage (0.0 to 1.0)
        """
        task = cls.get_by_id(db, id)
        if not task:
            logging.warning("Update_progress error: task not found")
            return

        if os.environ.get("MACOS"):
            # 直接更新逻辑
            if info.get("progress_msg"):
                progress_msg = trim_header_by_lines(task.progress_msg + "\n" + info["progress_msg"], 3000)
                db.execute(
                    update(cls.model)
                    .where(cls.model.id == id)
                    .values(progress_msg=progress_msg)
                )

            if "progress" in info:
                prog = info["progress"]
                db.execute(
                    update(cls.model)
                    .where(
                        (cls.model.id == id) &
                        (cls.model.progress != -1) &
                        or_(prog == -1, prog > cls.model.progress)
                    )
                    .values(progress=prog)
                )
        else:
            with DatabaseLock.create(db, f"update_progress_{id}"):
                if info.get("progress_msg"):
                    progress_msg = trim_header_by_lines(task.progress_msg + "\n" + info["progress_msg"], 3000)
                    db.execute(
                        update(cls.model)
                        .where(cls.model.id == id)
                        .values(progress_msg=progress_msg)
                    )

                if "progress" in info:
                    prog = info["progress"]
                    db.execute(
                        update(cls.model)
                        .where(
                            (cls.model.id == id) &
                            (cls.model.progress != -1) &
                            or_(prog == -1, prog > cls.model.progress)
                        )
                        .values(progress=prog)
                    )

        # Update process_duration after progress updates
        if task.begin_at:
            process_duration = (datetime.now() - task.begin_at).total_seconds()
            db.execute(
                update(cls.model)
                .where(cls.model.id == id)
                .values(process_duration=process_duration)
            )

        # Note: 不在此处 commit，由调用者控制事务边界

    @classmethod
    def delete_by_doc_ids(cls, db: Session, doc_ids: list[str]) -> int:
        """根据文档ID列表删除相关的任务记录"""
        try:
            result = db.query(cls.model).filter(
                cls.model.doc_id.in_(doc_ids)
            ).delete(synchronize_session=False)
            db.commit()
            return result
        except Exception as e:
            db.rollback()
            logging.exception(f"Failed to delete tasks for doc_ids={doc_ids}")
            raise e


def queue_tasks(db: Session, doc: dict, bucket: str, name: str, priority: int):
    """Create and queue document processing tasks.

    This function creates processing tasks for a document based on its type and configuration.
    It handles different document types (PDF, Excel, etc.) differently and manages task
    chunking and configuration. It also implements task reuse optimization by checking
    for previously completed tasks.

    Args:
        db: SQLAlchemy Session.
        doc (dict): Document dictionary containing metadata and configuration.
        bucket (str): Storage bucket name where the document is stored.
        name (str): File name of the document.
        priority (int, optional): Priority level for task queueing (default is 0).

    Note:
        - For PDF documents, tasks are created per page range based on configuration
        - For Excel documents, tasks are created per row range
        - Task digests are calculated for optimization and reuse
        - Previous task chunks may be reused if available
    """
    def new_task():
        return {
            "id": get_uuid(),
            "doc_id": doc["id"],
            "progress": 0.0,
            "from_page": 0,
            "to_page": 100000000,
            "begin_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }

    parse_task_array = []

    if doc["type"] == FileType.PDF.value:
        file_bin = STORAGE_IMPL.get(bucket, name)
        do_layout = doc["parser_config"].get("layout_recognize", "DeepDOC")
        pages = PdfParser.total_page_number(doc["name"], file_bin)
        if pages is None:
            pages = 0
        page_size = doc["parser_config"].get("task_page_size") or 12
        if doc["parser_id"] == "paper":
            page_size = doc["parser_config"].get("task_page_size") or 22
        if doc["parser_id"] in ["one", "knowledge_graph"] or do_layout != "DeepDOC" or doc["parser_config"].get("toc_extraction", False):
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
            if field == "parser_config":
                for k in ["raptor", "graphrag"]:
                    if k in chunking_config[field]:
                        del chunking_config[field][k]
            hasher.update(str(chunking_config[field]).encode("utf-8"))
        for field in ["doc_id", "from_page", "to_page"]:
            hasher.update(str(task.get(field, "")).encode("utf-8"))
        task_digest = hasher.hexdigest()
        task["digest"] = task_digest
        task["progress"] = 0.0
        task["priority"] = priority

    prev_tasks = TaskService.get_tasks(db, doc["id"])
    ck_num = 0
    if prev_tasks:
        for task in parse_task_array:
            ck_num += reuse_prev_task_chunks(task, prev_tasks, chunking_config)
        TaskService.filter_delete(db, [Task.doc_id == doc["id"]])
        pre_chunk_ids = []
        for pre_task in prev_tasks:
            if pre_task["chunk_ids"]:
                pre_chunk_ids.extend(pre_task["chunk_ids"].split())
        if pre_chunk_ids:
            settings.docStoreConn.delete({"id": pre_chunk_ids}, search.index_name_one(chunking_config["tenant_id"], chunking_config["name"]),
                                         chunking_config["kb_id"])
    DocumentService.update_by_id(db, doc["id"], {"chunk_num": ck_num})

    bulk_insert_into_db(db, Task, parse_task_array, True)
    DocumentService.begin2parse(db, doc["id"])

    unfinished_task_array = [task for task in parse_task_array if task["progress"] < 1.0]
    for unfinished_task in unfinished_task_array:
        assert REDIS_CONN.queue_product(
            get_svr_queue_name(priority), message=unfinished_task
        ), "Can't access Redis. Please check the Redis' status."

def reuse_prev_task_chunks(task: dict, prev_tasks: list[dict], chunking_config: dict):
    idx = 0
    while idx < len(prev_tasks):
        prev_task = prev_tasks[idx]
        if prev_task.get("from_page", 0) == task.get("from_page", 0) \
                and prev_task.get("digest", 0) == task.get("digest", ""):
            break
        idx += 1

    if idx >= len(prev_tasks):
        return 0
    prev_task = prev_tasks[idx]
    if prev_task["progress"] < 1.0 or not prev_task["chunk_ids"]:
        return 0
    task["chunk_ids"] = prev_task["chunk_ids"]
    task["progress"] = 1.0
    if "from_page" in task and "to_page" in task and int(task['to_page']) - int(task['from_page']) >= 10 ** 6:
        task["progress_msg"] = f"Page({task['from_page']}~{task['to_page']}): "
    else:
        task["progress_msg"] = ""
    task["progress_msg"] = " ".join(
        [datetime.now().strftime("%H:%M:%S"), task["progress_msg"], "Reused previous task's chunks."])
    prev_task["chunk_ids"] = ""

    return len(task["chunk_ids"].split())


def cancel_all_task_of(db, doc_id):
        for t in TaskService.query(db, doc_id=doc_id):
            try:
                REDIS_CONN.set(f"{t.id}-cancel", "x")
            except Exception as e:
                logging.exception(e)


def has_canceled(task_id):
    try:
        if REDIS_CONN.get(f"{task_id}-cancel"):
            return True
    except Exception as e:
        logging.exception(e)
    return False


def queue_dataflow(db: Session, tenant_id: str, flow_id: str, task_id: str, doc_id: str = CANVAS_DEBUG_DOC_ID, file: dict = None, priority: int = 0, rerun: bool = False) -> tuple[bool, str]:
    """
    Returns a tuple (success: bool, error_message: str).
    """

    task = dict(
        id=task_id,
        doc_id=doc_id,
        from_page=0,
        to_page=100000000,
        task_type="dataflow" if not rerun else "dataflow_rerun",
        priority=priority,
        begin_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )

    if doc_id not in [CANVAS_DEBUG_DOC_ID, GRAPH_RAPTOR_FAKE_DOC_ID]:
        TaskService.filter_delete(db, [Task.doc_id == doc_id])
        DocumentService.begin2parse(db, doc_id)
    bulk_insert_into_db(db, model=Task, data_source=[task], replace_on_conflict=True)

    task["kb_id"] = DocumentService.get_knowledgebase_id(db, doc_id)
    task["tenant_id"] = tenant_id
    task["dataflow_id"] = flow_id
    task["file"] = file

    if not REDIS_CONN.queue_product(
        get_svr_queue_name(priority), message=task
    ):
        return False, "Can't access Redis. Please check the Redis' status."

    return True, ""