# coding=utf-8
"""
@project: multirag
@Author：龙
@file： document_service.py
@date：2024/8/14 11:00
@desc:
"""
import logging
import random
import time
from datetime import datetime

from pymilvus import MilvusException
from sqlalchemy.exc import NoResultFound, OperationalError
from sqlalchemy.orm import Session, aliased
from sqlalchemy import func, asc

from api.db import FileType, TaskStatus, StatusEnum
from api.db.db_models import Document, Knowledgebase, Tenant, Task, UserTenant
from api.db.services.common_service import CommonService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils import current_timestamp, get_format_time, get_uuid
from api.utils.db_utils import bulk_insert_into_db
from core.nlp import search, rag_tokenizer
from core import settings
from core.utils.milvus_conn import MILVUS_CONNECTION
from core.utils.storage_factory import STORAGE_IMPL
from core.utils.redis_conn import REDIS_CONN


class DocumentService(CommonService):
    model = Document

    def __init__(self):
        super().__init__(Document)

    @classmethod
    def get_list(cls, db: Session, kb_id, page_number, items_per_page, orderby, desc, keywords=None, id=None, name=None):
        # 初始化查询
        query = db.query(cls.model).filter(cls.model.kb_id == kb_id)

        # 根据 id 添加过滤条件
        if id:
            query = query.filter(cls.model.id == id)

        # 根据 name 添加精确匹配过滤条件
        if name:
            query = query.filter(cls.model.name == name)

        # 根据 keywords 添加模糊匹配过滤条件
        if keywords:
            query = query.filter(func.lower(cls.model.name).contains(keywords.lower()))

        # 根据 desc 确定排序方式
        order_clause = getattr(cls.model, orderby)
        if desc:
            query = query.order_by(desc(order_clause))
        else:
            query = query.order_by(asc(order_clause))

        # 添加分页
        query = query.offset((page_number - 1) * items_per_page).limit(items_per_page)

        # 执行查询并返回结果
        results = query.all()

        # 获取记录总数
        count = query.count()

        return [item.__dict__ for item in results], count

    @classmethod
    def get_by_kb_id(cls, db: Session, kb_id: str, page_number: int, items_per_page: int,
                     orderby: str, desc: bool, keywords: str | None = None) -> (list[dict], int):
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
    def get_by_doc_id(cls, db: Session, doc_id: str) -> dict | None:
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
                                  order_by: str, descend: bool, keywords: str | None = None) -> (list[dict], int):
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
    def clear_chunk_num(cls, db: Session, doc_id: str, max_retries=3):
        doc = cls.get_by_id(db, doc_id)
        if not doc:
            raise LookupError("Can't find document in database.")
        retries = 0
        while retries < max_retries:
            try:
                # 读取数据
                kb_record = db.query(Knowledgebase).filter_by(id=doc.kb_id).first()

                # 检查数据是否存在，进行更新
                if kb_record:
                    kb_update = db.query(Knowledgebase).filter_by(id=doc.kb_id).update({
                        Knowledgebase.token_num: Knowledgebase.token_num - doc.token_num,
                        Knowledgebase.chunk_num: Knowledgebase.chunk_num - doc.chunk_num,
                        Knowledgebase.doc_num: Knowledgebase.doc_num - 1
                    })
                    db.commit()

                    return kb_update

            except OperationalError as e:
                # 如果检测到锁冲突（例如数据库锁定），可以选择重试
                db.rollback()
                retries += 1
                wait_time = 2 ** retries  # 使用指数退避策略
                time.sleep(wait_time)
                if retries >= max_retries:
                    raise e  # 达到最大重试次数后抛出异常

            except NoResultFound:
                db.rollback()
                raise LookupError("Knowledgebase entry not found.")

            except Exception as e:
                db.rollback()  # 回滚事务以防止不一致性
                raise e

        return None
        # kb_update = db.query(Knowledgebase).filter_by(id=doc.kb_id).update({
        #     Knowledgebase.token_num: Knowledgebase.token_num - doc.token_num,
        #     Knowledgebase.chunk_num: Knowledgebase.chunk_num - doc.chunk_num,
        #     Knowledgebase.doc_num: Knowledgebase.doc_num - 1
        # })
        # return kb_update

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
    def accessible(cls, db: Session, doc_id, user_id):
        # 使用 SQLAlchemy 查询文档是否可访问
        docs = db.query(cls.model.id).join(
            Knowledgebase, cls.model.kb_id == Knowledgebase.id
        ).join(
            UserTenant, UserTenant.tenant_id == Knowledgebase.tenant_id
        ).filter(
            cls.model.id == doc_id,
            UserTenant.user_id == user_id
        ).limit(1).all()

        # 如果没有找到文档则返回 False
        if not docs:
            return False
        return True

    @classmethod
    def accessible4deletion(cls, db: Session, doc_id, user_id):
        # 使用 SQLAlchemy 查询文档是否可删除
        docs = db.query(cls.model.id).join(
            Knowledgebase, cls.model.kb_id == Knowledgebase.id
        ).filter(
            cls.model.id == doc_id,
            Knowledgebase.created_by == user_id
        ).limit(1).all()

        # 如果没有找到文档则返回 False
        if not docs:
            return False
        return True

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
    def get_thumbnails(cls, db: Session, doc_ids: list[str]):
        query = db.query(cls.model.id, cls.model.kb_id, cls.model.thumbnail).filter(cls.model.id.in_(doc_ids))
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
            "progress_msg": "Task is queued...",
            "process_begin_at": get_format_time()
        })

    @classmethod
    def update_progress(cls, db: Session):
        docs = cls.get_unfinished_docs(db)
        for d in docs:
            try:
                tsks = db.query(Task).filter_by(doc_id=d.id).order_by(Task.create_time).all()
                if not tsks:
                    continue
                msg = []
                prg = 0
                finished = True
                bad = 0

                doc = DocumentService.get_by_id(db, d.id)
                status = doc.run  # TaskStatus.RUNNING.value

                for t in tsks:
                    if 0 <= t.progress < 1:
                        finished = False
                    prg += t.progress if t.progress >= 0 else 0

                    if t.progress_msg not in msg:
                        msg.append(t.progress_msg)

                    if t.progress == -1:
                        bad += 1
                prg /= len(tsks)
                if finished and bad:
                    prg = -1
                    status = TaskStatus.FAIL.value
                elif finished:
                    if d.parser_config.get("raptor", {}).get("use_raptor") and d.progress_msg.lower().find(
                            " raptor") < 0:
                        queue_raptor_tasks(db, d)
                        prg = 0.98 * len(tsks) / (len(tsks) + 1)
                        msg.append("------ RAPTOR -------")
                    else:
                        status = TaskStatus.DONE.value

                msg = "\n".join(msg)
                info = {
                    "process_duration": datetime.timestamp(datetime.now()) - d.process_begin_at.timestamp(),
                    "run": status
                }
                if prg != 0:
                    info["progress"] = prg
                if msg:
                    info["progress_msg"] = msg
                cls.update_by_id(db, d.id, info)
            except Exception as e:
                if str(e).find("'0'") < 0:
                    logging.exception("fetch task exception")

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


def queue_raptor_tasks(db: Session, doc):
    def new_task():
        return {
            "id": get_uuid(),
            "doc_id": doc["id"],
            "from_page": 0,
            "to_page": -1,
            "progress_msg": "Start to do RAPTOR (Recursive Abstractive Processing For Tree-Organized Retrieval)."
        }

    task = new_task()
    bulk_insert_into_db(db, Task, [task], True)
    task["type"] = "raptor"
    assert REDIS_CONN.queue_product(settings.SVR_QUEUE_NAME, message=task), "Can't access Redis. Please check the Redis' status."

# def doc_upload_and_parse(conversation_id, file_objs, user_id):
#     from core.app import presentation, picture, naive, audio, email
#     from api.db.services.dialog_service import ConversationService, DialogService
#     from api.db.services.file_service import FileService
#     from api.db.services.llm_service import LLMBundle
#     from api.db.services.user_service import TenantService
#     from api.db.services.api_service import API4ConversationService
#
#     e, conv = ConversationService.get_by_id(conversation_id)
#     if not e:
#         e, conv = API4ConversationService.get_by_id(conversation_id)
#     assert e, "Conversation not found!"
#
#     e, dia = DialogService.get_by_id(conv.dialog_id)
#     kb_id = dia.kb_ids[0]
#     e, kb = KnowledgebaseService.get_by_id(kb_id)
#     if not e:
#         raise LookupError("Can't find this knowledgebase!")
#
#     idxnm = search.index_name(kb.tenant_id)
#     if not ELASTICSEARCH.indexExist(idxnm):
#         ELASTICSEARCH.createIdx(idxnm, json.load(
#             open(os.path.join(get_project_base_directory(), "conf", "mapping.json"), "r")))
#
#     embd_mdl = LLMBundle(kb.tenant_id, LLMType.EMBEDDING, llm_name=kb.embd_id, lang=kb.language)
#
#     err, files = FileService.upload_document(kb, file_objs, user_id)
#     assert not err, "\n".join(err)
#
#     def dummy(prog=None, msg=""):
#         pass
#
#     FACTORY = {
#         ParserType.PRESENTATION.value: presentation,
#         ParserType.PICTURE.value: picture,
#         ParserType.AUDIO.value: audio,
#         ParserType.EMAIL.value: email
#     }
#     parser_config = {"chunk_token_num": 4096, "delimiter": "\n!?;。；！？", "layout_recognize": False}
#     exe = ThreadPoolExecutor(max_workers=12)
#     threads = []
#     for d, blob in files:
#         kwargs = {
#             "callback": dummy,
#             "parser_config": parser_config,
#             "from_page": 0,
#             "to_page": 100000,
#             "tenant_id": kb.tenant_id,
#             "lang": kb.language
#         }
#         threads.append(exe.submit(FACTORY.get(d["parser_id"], naive).chunk, d["name"], blob, **kwargs))
#
#     for (docinfo, _), th in zip(files, threads):
#         docs = []
#         doc = {
#             "doc_id": docinfo["id"],
#             "kb_id": [kb.id]
#         }
#         for ck in th.result():
#             d = deepcopy(doc)
#             d.update(ck)
#             md5 = hashlib.md5()
#             md5.update((ck["content_with_weight"] +
#                         str(d["doc_id"])).encode("utf-8"))
#             d["_id"] = md5.hexdigest()
#             d["create_time"] = str(datetime.now()).replace("T", " ")[:19]
#             d["create_timestamp_flt"] = datetime.now().timestamp()
#             if not d.get("image"):
#                 docs.append(d)
#                 continue
#
#             output_buffer = BytesIO()
#             if isinstance(d["image"], bytes):
#                 output_buffer = BytesIO(d["image"])
#             else:
#                 d["image"].save(output_buffer, format='JPEG')
#
#             MINIO.put(kb.id, d["_id"], output_buffer.getvalue())
#             d["img_id"] = "{}-{}".format(kb.id, d["_id"])
#             del d["image"]
#             docs.append(d)
#
#     parser_ids = {d["id"]: d["parser_id"] for d, _ in files}
#     docids = [d["id"] for d, _ in files]
#     chunk_counts = {id: 0 for id in docids}
#     token_counts = {id: 0 for id in docids}
#     es_bulk_size = 64
#
#     def embedding(doc_id, cnts, batch_size=16):
#         nonlocal embd_mdl, chunk_counts, token_counts
#         vects = []
#         for i in range(0, len(cnts), batch_size):
#             vts, c = embd_mdl.encode(cnts[i: i + batch_size])
#             vects.extend(vts.tolist())
#             chunk_counts[doc_id] += len(cnts[i:i + batch_size])
#             token_counts[doc_id] += c
#         return vects
#
#     _, tenant = TenantService.get_by_id(kb.tenant_id)
#     llm_bdl = LLMBundle(kb.tenant_id, LLMType.CHAT, tenant.llm_id)
#     for doc_id in docids:
#         cks = [c for c in docs if c["doc_id"] == doc_id]
#
#         if parser_ids[doc_id] != ParserType.PICTURE.value:
#             mindmap = MindMapExtractor(llm_bdl)
#             try:
#                 mind_map = json.dumps(mindmap([c["content_with_weight"] for c in docs if c["doc_id"] == doc_id]).output,
#                                       ensure_ascii=False, indent=2)
#                 if len(mind_map) < 32: raise Exception("Few content: " + mind_map)
#                 cks.append({
#                     "id": get_uuid(),
#                     "doc_id": doc_id,
#                     "kb_id": [kb.id],
#                     "content_with_weight": mind_map,
#                     "knowledge_graph_kwd": "mind_map"
#                 })
#             except Exception:
#                 logging.exception("Mind map generation error")
#
#         vects = embedding(doc_id, [c["content_with_weight"] for c in cks])
#         assert len(cks) == len(vects)
#         for i, d in enumerate(cks):
#             v = vects[i]
#             d["q_%d_vec" % len(v)] = v
#         for b in range(0, len(cks), es_bulk_size):
#             ELASTICSEARCH.bulk(cks[b:b + es_bulk_size], idxnm)
#
#         DocumentService.increment_chunk_num(
#             doc_id, kb.id, token_counts[doc_id], chunk_counts[doc_id], 0)
#
#     return [d["id"] for d,_ in files]
