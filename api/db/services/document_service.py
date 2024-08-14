# coding=utf-8
"""
@project: multirag
@Author：龙
@file： document_service.py
@date：2024/8/14 11:00
@desc:
"""
import random
from datetime import datetime
from typing import Optional, List, Dict

from pymilvus import MilvusException
from sqlalchemy.orm import Session, aliased
from sqlalchemy import func

from api.db import FileType, TaskStatus, StatusEnum, ParserType
from api.db.db_models import Document, Knowledgebase, Tenant, Task
from api.db.services.common_service import CommonService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.settings import stat_logger, RetCode
from api.utils import current_timestamp, get_format_time, get_uuid
from api.utils.api_utils import construct_json_result
from api.utils.db_utils import bulk_insert_into_db
from core.nlp import search
from core.settings import SVR_QUEUE_NAME
from core.utils.milvus_conn import MILVUS_CONNECTION
from core.utils.redis_conn import REDIS_CONN


class DocumentService(CommonService):
    model = Document

    def __init__(self):
        super().__init__(Document)

    @classmethod
    def get_by_kb_id(cls, db: Session, kb_id: str, page_number: int, items_per_page: int,
                     orderby: str, desc: bool, keywords: Optional[str] = None) -> (List[Dict], int):
        query = db.query(cls.model).filter_by(kb_id=kb_id)
        if keywords:
            query = query.filter(func.lower(cls.model.name).contains(keywords.lower()))

        count = query.count()

        if desc:
            query = query.order_by(getattr(cls.model, orderby).desc())
        else:
            query = query.order_by(getattr(cls.model, orderby).asc())

        docs = query.offset((page_number - 1) * items_per_page).limit(items_per_page).all()
        return [doc.to_dict() for doc in docs], count

    @classmethod
    def get_by_doc_id(cls, db: Session, doc_id: str) -> Optional[Dict]:
        """
        通过文档ID获取文档信息。

        参数:
        - db: 数据库会话对象，用于执行数据库查询和更新操作。
        - doc_id: 需要查询的文档的ID。

        返回:
        - 返回包含文档信息的字典，如果未找到文档，则返回None。
        """
        doc = db.query(cls.model).filter_by(id=doc_id).first()
        return doc.to_dict() if doc else None

    @classmethod
    def list_documents_in_dataset(cls, db: Session, dataset_id: str, offset: int, count: int,
                                  order_by: str, descend: bool, keywords: Optional[str] = None) -> (List[Dict], int):
        query = db.query(cls.model).filter_by(kb_id=dataset_id)
        if keywords:
            query = query.filter(func.lower(cls.model.name).contains(keywords.lower()))

        total = query.count()

        if descend:
            query = query.order_by(getattr(cls.model, order_by).desc())
        else:
            query = query.order_by(getattr(cls.model, order_by).asc())

        docs = query.all()

        if offset < 0 or offset > len(docs):
            raise IndexError("Offset is out of the valid range.")

        if count == -1:
            return [doc.to_dict() for doc in docs[offset:]], total

        return [doc.to_dict() for doc in docs[offset:offset + count]], total

    @classmethod
    def insert(cls, db: Session, doc: dict):
        new_doc = cls.save(db, **doc)
        kb = KnowledgebaseService.get_by_id(db, doc["kb_id"])
        KnowledgebaseService.update_by_id(db, kb.id, {"doc_num": kb.doc_num + 1})
        return new_doc

    @classmethod
    def remove_document(cls, db: Session, doc: Document, tenant_id: str):
        # ELASTICSEARCH.delete_by_query(
        #     Q("match", doc_id=doc.id), index=search.index_name(tenant_id))
        document = DocumentService.get_by_doc_id(db, doc.id)
        kb = KnowledgebaseService.get_by_id(db, document["kb_id"])
        # 构建 Milvus 集合名称
        collection_name = search.index_name_one(tenant_id, kb.name)
        # 检查集合是否存在并删除 Milvus 中的数据
        try:
            if MILVUS_CONNECTION.has_collection(collection_name):
                MILVUS_CONNECTION.delete(
                    collection_name=collection_name,
                    filter=f"doc_id == '{doc.id}'"
                )
        except MilvusException as e:
            return e
        cls.clear_chunk_num(db, doc.id)
        return cls.delete_by_id(db, doc.id)

    @classmethod
    def get_newly_uploaded(cls, db: Session):
        query = db.query(
            cls.model.id, cls.model.kb_id, cls.model.parser_id, cls.model.parser_config, cls.model.name,
            cls.model.type, cls.model.location, cls.model.size, Knowledgebase.tenant_id, Tenant.embd_id,
            Tenant.img2txt_id, Tenant.asr_id, cls.model.update_time
        ).join(Knowledgebase, cls.model.kb_id == Knowledgebase.id
               ).join(Tenant, Knowledgebase.tenant_id == Tenant.id
                      ).filter(
            cls.model.status == StatusEnum.VALID.value,
            cls.model.type != FileType.VIRTUAL.value,
            cls.model.progress == 0,
            cls.model.update_time >= current_timestamp() - 1000 * 600,
            cls.model.run == TaskStatus.RUNNING.value
        ).order_by(cls.model.update_time.asc())
        return query.all()

    @classmethod
    def get_unfinished_docs(cls, db: Session):
        query = db.query(
            cls.model.id, cls.model.process_begin_at, cls.model.parser_config, cls.model.progress_msg, cls.model.run
        ).filter(
            cls.model.status == StatusEnum.VALID.value,
            cls.model.type != FileType.VIRTUAL.value,
            cls.model.progress < 1,
            cls.model.progress > 0
        )
        return query.all()

    @classmethod
    def increment_chunk_num(cls, db: Session, doc_id: str, kb_id: str, token_num: int, chunk_num: int, duration: int):
        """
        更新文档和知识库的片段数量、令牌数量和处理时长。

        本方法通过查询指定ID的文档和知识库，在数据库中更新它们的令牌数量、片段数量和处理时长。
        如果文档未找到，则抛出LookupError异常。

        参数:
        - db: 数据库会话对象，用于执行数据库查询和更新操作。
        - doc_id: 需要更新的文档的ID。
        - kb_id: 需要更新的知识库的ID。
        - token_num: 需要增加的令牌数量。
        - chunk_num: 需要增加的片段数量。
        - duration: 需要增加的处理时长。

        返回:
        - kb_update: 知识库更新的影响行数。
        """
        # 更新文档的令牌数量、片段数量和处理时长
        doc_update = db.query(cls.model).filter_by(id=doc_id).update({
            cls.model.token_num: cls.model.token_num + token_num,
            cls.model.chunk_num: cls.model.chunk_num + chunk_num,
            cls.model.process_duration: cls.model.process_duration + duration
        })

        # 如果文档更新影响行数为0，表示未找到文档，抛出异常
        if doc_update == 0:
            raise LookupError("Document not found which is supposed to be there")

        # 更新知识库的令牌数量和片段数量
        kb_update = db.query(Knowledgebase).filter_by(id=kb_id).update({
            Knowledgebase.token_num: Knowledgebase.token_num + token_num,
            Knowledgebase.chunk_num: Knowledgebase.chunk_num + chunk_num
        })
        return kb_update

    @classmethod
    def decrement_chunk_num(cls, db: Session, doc_id: str, kb_id: str, token_num: int, chunk_num: int, duration: int):
        """
        减少文档和知识库的片段数量、令牌数量和处理时长。

        本方法通过查询指定ID的文档和知识库，在数据库中更新它们的令牌数量、片段数量和处理时长。
        如果文档未找到，则抛出LookupError异常。

        参数:
        - db: 数据库会话对象，用于执行数据库查询和更新操作。
        - doc_id: 需要更新的文档的ID。
        - kb_id: 需要更新的知识库的ID。
        - token_num: 需要减少的令牌数量。
        - chunk_num: 需要减少的片段数量。
        - duration: 需要增加的处理时长。

        返回:
        - kb_update: 知识库更新的影响行数。
        """
        # 更新文档的令牌数量、片段数量和处理时长
        doc_update = db.query(cls.model).filter_by(id=doc_id).update({
            cls.model.token_num: cls.model.token_num - token_num,
            cls.model.chunk_num: cls.model.chunk_num - chunk_num,
            cls.model.process_duration: cls.model.process_duration + duration
        })

        # 如果文档更新影响行数为0，表示未找到文档，抛出异常
        if doc_update == 0:
            raise LookupError("Document not found which is supposed to be there")

        # 更新知识库的令牌数量和片段数量
        kb_update = db.query(Knowledgebase).filter_by(id=kb_id).update({
            Knowledgebase.token_num: Knowledgebase.token_num - token_num,
            Knowledgebase.chunk_num: Knowledgebase.chunk_num - chunk_num
        })
        return kb_update

    @classmethod
    def clear_chunk_num(cls, db: Session, doc_id: str):
        doc = cls.get_by_id(db, doc_id)
        if not doc:
            raise LookupError("Can't find document in database.")

        kb_update = db.query(Knowledgebase).filter_by(id=doc.kb_id).update({
            Knowledgebase.token_num: Knowledgebase.token_num - doc.token_num,
            Knowledgebase.chunk_num: Knowledgebase.chunk_num - doc.chunk_num,
            Knowledgebase.doc_num: Knowledgebase.doc_num - 1
        })
        return kb_update

    @classmethod
    def get_tenant_id(cls, db: Session, doc_id: str):
        # 使用 aliased 创建表别名
        KnowledgebaseAlias = aliased(Knowledgebase)
        DocumentAlias = aliased(Document)
        query = db.query(KnowledgebaseAlias.tenant_id) \
            .select_from(DocumentAlias) \
            .join(KnowledgebaseAlias, DocumentAlias.kb_id == KnowledgebaseAlias.id) \
            .filter(DocumentAlias.id == doc_id, KnowledgebaseAlias.status == StatusEnum.VALID.value) \
            .first()
        # query = db.query(Knowledgebase.tenant_id).join(Knowledgebase, cls.model.kb_id == Knowledgebase.id
        #                                                ).filter(
        #     cls.model.id == doc_id,
        #     Knowledgebase.status == StatusEnum.VALID.value
        # ).first()
        return query.tenant_id if query else None

    @classmethod
    def get_tenant_id_by_name(cls, db: Session, name: str):
        query = db.query(Knowledgebase.tenant_id).join(Knowledgebase, cls.model.kb_id == Knowledgebase.id
                                                       ).filter(
            cls.model.name == name,
            Knowledgebase.status == StatusEnum.VALID.value
        ).first()
        return query.tenant_id if query else None

    @classmethod
    def get_embd_id(cls, db: Session, doc_id: str):
        query = db.query(Knowledgebase.embd_id).join(Knowledgebase, cls.model.kb_id == Knowledgebase.id
                                                     ).filter(
            cls.model.id == doc_id,
            Knowledgebase.status == StatusEnum.VALID.value
        ).first()
        return query.embd_id if query else None

    @classmethod
    def get_doc_id_by_doc_name(cls, db: Session, doc_name: str):
        query = db.query(cls.model.id).filter_by(name=doc_name).first()
        return query.id if query else None

    @classmethod
    def get_thumbnails(cls, db: Session, doc_ids: List[str]):
        query = db.query(cls.model.id, cls.model.thumbnail).filter(cls.model.id.in_(doc_ids))
        return query.all()

    @classmethod
    def update_parser_config(cls, db: Session, id: str, config: dict):
        doc = cls.get_by_id(db, id)
        if not doc:
            raise LookupError(f"Document({id}) not found.")

        def dfs_update(old, new):
            for k, v in new.items():
                if k not in old:
                    old[k] = v
                    continue
                if isinstance(v, dict):
                    assert isinstance(old[k], dict)
                    dfs_update(old[k], v)
                else:
                    old[k] = v

        dfs_update(doc.parser_config, config)
        cls.update_by_id(db, id, {"parser_config": doc.parser_config})

    @classmethod
    def get_doc_count(cls, db: Session, tenant_id: str):
        query = db.query(cls.model.id).join(Knowledgebase, Knowledgebase.id == cls.model.kb_id
                                            ).filter(Knowledgebase.tenant_id == tenant_id)
        return query.count()

    @classmethod
    def begin2parse(cls, db: Session, doc_id: str):
        cls.update_by_id(db, doc_id, {
            "progress": random.random() * 1 / 100.,
            "progress_msg": "Task dispatched...",
            "process_begin_at": get_format_time()
        })

    @classmethod
    def update_progress(cls, db: Session):
        docs = cls.get_unfinished_docs(db)
        for d in docs:
            try:
                # tsks = db.query(Task).filter_by(doc_id=d["id"]).order_by(Task.create_time).all()
                tsks = db.query(Task).filter_by(doc_id=d.id).order_by(Task.create_time).all()
                if not tsks:
                    continue
                msg = []
                prg = 0
                finished = True
                bad = 0

                # doc = DocumentService.get_by_id(d["id"])
                doc = DocumentService.get_by_id(d.id)
                status = doc.run  # TaskStatus.RUNNING.value

                # status = TaskStatus.RUNNING.value
                for t in tsks:
                    if 0 <= t.progress < 1:
                        finished = False
                    prg += t.progress if t.progress >= 0 else 0

                    if t.progress_msg not in msg:
                        msg.append(t.progress_msg)

                    # msg.append(t.progress_msg)
                    if t.progress == -1:
                        bad += 1
                prg /= len(tsks)
                if finished and bad:
                    prg = -1
                    status = TaskStatus.FAIL.value
                elif finished:
                    # if d["parser_config"].get("raptor", {}).get("use_raptor") and d["progress_msg"].lower().find(
                    if d.parser_config.get("raptor", {}).get("use_raptor") and d.progress_msg.lower().find(
                            " raptor") < 0:
                        queue_raptor_tasks(d)
                        prg *= 0.98
                        msg.append("------ RAPTOR -------")
                    else:
                        status = TaskStatus.DONE.value

                msg = "\n".join(msg)
                info = {
                    # "process_duration": datetime.timestamp(datetime.now()) - d["process_begin_at"].timestamp(),
                    "process_duration": datetime.timestamp(datetime.now()) - d.process_begin_at.timestamp(),
                    "run": status
                }
                if prg != 0:
                    info["progress"] = prg
                if msg:
                    info["progress_msg"] = msg
                # cls.update_by_id(db, d["id"], info)
                cls.update_by_id(db, d.id, info)
            except Exception as e:
                stat_logger.error("fetch task exception:" + str(e))

    @classmethod
    def get_kb_doc_count(cls, db: Session, kb_id: str):
        query = db.query(cls.model.id).filter_by(kb_id=kb_id)
        return query.count()

    @classmethod
    def do_cancel(cls, db: Session, doc_id):
        try:
            _, doc = DocumentService.get_by_id(db, doc_id)
            return doc.run == TaskStatus.CANCEL.value or doc.progress < 0
        except Exception as e:
            pass
        return False


def queue_raptor_tasks(doc):
    def new_task():
        return {
            "id": get_uuid(),
            "doc_id": doc["id"],
            "from_page": 0,
            "to_page": -1,
            "progress_msg": "Start to do RAPTOR (Recursive Abstractive Processing For Tree-Organized Retrieval)."
        }

    task = new_task()
    bulk_insert_into_db(Task, [task], True)
    task["type"] = "raptor"
    assert REDIS_CONN.queue_product(SVR_QUEUE_NAME, message=task), "Can't access Redis. Please check the Redis' status."
