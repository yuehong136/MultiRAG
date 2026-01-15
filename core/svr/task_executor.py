import asyncio
import random
import socket
import sys
import threading
import time
import concurrent.futures

import json_repair

from api.db.db_models import db_connection
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.pipeline_operation_log_service import PipelineOperationLogService
from common.connection_utils import timeout
from common.file_utils import get_project_base_directory
from core.utils.base64_image import image2id
from core.utils.raptor_utils import should_skip_raptor, get_skip_reason
from common.log_utils import init_root_logger
from common.config_utils import show_configs
from common.signal_utils import start_tracemalloc_and_snapshot, stop_tracemalloc
from graphrag.general.index import run_graphrag_for_kb
from graphrag.utils import get_llm_cache, set_llm_cache, get_tags_from_cache, set_tags_to_cache
from core.prompts.generator import keyword_extraction, question_proposal, content_tagging, run_toc_from_text
import logging
from datetime import datetime
import json
import os
import xxhash
import copy
import re
from functools import partial

from pymilvus import DataType

import numpy as np
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.orm import Session

from multiprocessing.context import TimeoutError
from timeit import default_timer as timer
import signal
import exceptiongroup
import faulthandler
from common.constants import LLMType, ParserType, PipelineTaskType, PIPELINE_SPECIAL_PROGRESS_FREEZE_TASK_TYPES
from api.db.services.document_service import DocumentService
from api.db.services.llm_service import LLMBundle
from api.db.services.task_service import TaskService, has_canceled, CANVAS_DEBUG_DOC_ID, GRAPH_RAPTOR_FAKE_DOC_ID
from api.db.services.file2document_service import File2DocumentService
from common.versions import get_multirag_version
from core.app import laws, paper, presentation, manual, qa, table, book, resume, picture, naive, one, audio, \
    email, tag
from core.nlp import search, rag_tokenizer, add_positions, concat_img
from core.raptor import RecursiveAbstractiveProcessing4TreeOrganizedRetrieval as Raptor
from common.constants import PAGERANK_FLD, TAG_FLD, SVR_CONSUMER_GROUP_NAME
from common.token_utils import num_tokens_from_string, truncate
from core.utils.redis_conn import REDIS_CONN, RedisDistributedLock
from common.exceptions import TaskCanceledException
from common import settings
from common.misc_utils import check_and_install_mineru
from graphrag.utils import chat_limiter

BATCH_SIZE = 64


def delete_chunks_by_doc_id(collection_name: str, doc_id: str, kb_id: str = "") -> int:
    """
    兼容不同数据库的删除操作辅助函数

    Args:
        collection_name: 集合/索引名称
        doc_id: 文档ID
        kb_id: 知识库ID (ES/OpenSearch 需要)

    Returns:
        删除的记录数
    """
    db_type = settings.docStoreConn.dbType()
    try:
        if db_type == "milvus":
            # Milvus 使用 filter 参数
            return settings.docStoreConn.delete(
                collection_name=collection_name,
                filter=f"doc_id == '{doc_id}'"
            )
        else:
            # ES/OpenSearch/Infinity 使用 condition 参数
            return settings.docStoreConn.delete(
                condition={"doc_id": doc_id},
                indexName=collection_name,
                knowledgebaseId=kb_id
            )
    except Exception as e:
        logging.warning(f"delete_chunks_by_doc_id failed for {db_type}: {e}")
        return 0

FACTORY = {
    "general": naive,
    ParserType.NAIVE.value: naive,
    ParserType.PAPER.value: paper,
    ParserType.BOOK.value: book,
    ParserType.PRESENTATION.value: presentation,
    ParserType.MANUAL.value: manual,
    ParserType.LAWS.value: laws,
    ParserType.QA.value: qa,
    ParserType.TABLE.value: table,
    ParserType.RESUME.value: resume,
    ParserType.PICTURE.value: picture,
    ParserType.ONE.value: one,
    ParserType.AUDIO.value: audio,
    ParserType.EMAIL.value: email,
    ParserType.KG.value: naive,
    ParserType.TAG.value: tag
}

TASK_TYPE_TO_PIPELINE_TASK_TYPE = {
    "dataflow": PipelineTaskType.PARSE,
    "raptor": PipelineTaskType.RAPTOR,
    "graphrag": PipelineTaskType.GRAPH_RAG,
    "mindmap": PipelineTaskType.MINDMAP,
    # analyze_v2 不需要记录 pipeline 操作日志（直传文件，无 Document 记录）
}

UNACKED_ITERATOR = None

CONSUMER_NO = "0" if len(sys.argv) < 2 else sys.argv[1]
CONSUMER_NAME = "task_executor_" + CONSUMER_NO
BOOT_AT = datetime.now().astimezone().isoformat(timespec="milliseconds")
PENDING_TASKS = 0
LAG_TASKS = 0
DONE_TASKS = 0
FAILED_TASKS = 0

CURRENT_TASKS = {}

MAX_CONCURRENT_TASKS = int(os.environ.get('MAX_CONCURRENT_TASKS', "5"))
MAX_CONCURRENT_CHUNK_BUILDERS = int(os.environ.get('MAX_CONCURRENT_CHUNK_BUILDERS', "3"))
MAX_CONCURRENT_MINIO = int(os.environ.get('MAX_CONCURRENT_MINIO', '10'))
task_limiter = asyncio.Semaphore(MAX_CONCURRENT_TASKS)
chunk_limiter = asyncio.Semaphore(MAX_CONCURRENT_CHUNK_BUILDERS)
embed_limiter = asyncio.Semaphore(MAX_CONCURRENT_CHUNK_BUILDERS)
minio_limiter = asyncio.Semaphore(MAX_CONCURRENT_MINIO)
kg_limiter = asyncio.Semaphore(2)
WORKER_HEARTBEAT_TIMEOUT = int(os.environ.get('WORKER_HEARTBEAT_TIMEOUT', '120'))
stop_event = threading.Event()


def signal_handler(sig, frame):
    logging.info("Received interrupt signal, shutting down...")
    stop_event.set()
    time.sleep(1)
    sys.exit(0)


def set_progress(db: Session, task_id, from_page=0, to_page=-1, prog=None, msg="Processing...", enable_sse=False):
    """
    更新任务进度（支持双写模式）

    Args:
        db: 数据库会话
        task_id: 任务ID
        from_page: 起始页
        to_page: 结束页
        prog: 进度值（0-1，-1表示失败）
        msg: 进度消息
        enable_sse: 是否同时发送 SSE 事件到 Redis Stream（默认 False，保持向后兼容）
    """
    try:
        if prog is not None and prog < 0:
            msg = "[ERROR]" + msg
        cancel = has_canceled(task_id)

        if cancel:
            msg += " [Canceled]"
            prog = -1

        if to_page > 0:
            if msg:
                if from_page < to_page:
                    msg = f"Page({from_page + 1}~{to_page + 1}): " + msg
        if msg:
            msg = datetime.now().strftime("%H:%M:%S") + " " + msg
        d = {"progress_msg": msg}
        if prog is not None:
            d["progress"] = prog

        # 1. 更新数据库（必须，供轮询查询）
        TaskService.update_progress(db, task_id, d)
        db.commit()

        # 2. 发送 SSE 事件到 Redis（可选，供实时推送）
        if enable_sse and prog is not None:
            try:
                # 确定事件类型
                if prog == 1.0:
                    event_type = "complete"
                elif prog < 0:
                    event_type = "error"
                else:
                    event_type = "progress"

                # 提取纯消息（去掉时间戳前缀）
                clean_msg = msg.split(" ", 1)[1] if " " in msg else msg

                # 构建事件数据
                event_data = {
                    "progress": prog,
                    "message": clean_msg
                }

                # 如果是完成事件，包含完整结果
                if event_type == "complete":
                    try:
                        # 从数据库读取完整结果
                        task = TaskService.get_by_id(db, task_id)
                        if task and task.chunk_ids:
                            task_data = json.loads(task.chunk_ids)
                            result = task_data.get("result")
                            if result:
                                event_data["result"] = result
                                logging.info(f"SSE complete event includes result for task {task_id}")
                    except Exception as e:
                        logging.warning(f"Failed to include result in SSE complete event: {e}")

                # 发送到 Redis Stream
                REDIS_CONN.xadd_sse_event(
                    task_id=task_id,
                    event_type=event_type,
                    data=event_data  # 使用包含 result 的完整数据
                )
            except Exception as e:
                # SSE 发送失败不影响主流程
                logging.warning(f"Failed to send SSE event: {e}")

        db.close()
        if cancel:
            raise TaskCanceledException(msg)
        logging.info(f"set_progress({task_id}), progress: {prog}, progress_msg: {msg}, enable_sse: {enable_sse}")
    except NoResultFound:
        logging.warning("set_progress(%s): 记录不存在，无法更新进度", task_id)
    except Exception:
        logging.exception(f"set_progress({task_id}), progress: {prog}, progress_msg: {msg}, got exception")


async def collect(db: Session):
    global CONSUMER_NAME, DONE_TASKS, FAILED_TASKS
    global UNACKED_ITERATOR

    svr_queue_names = settings.get_svr_queue_names()
    try:
        if not UNACKED_ITERATOR:
            UNACKED_ITERATOR = REDIS_CONN.get_unacked_iterator(svr_queue_names, SVR_CONSUMER_GROUP_NAME, CONSUMER_NAME)
        try:
            redis_msg = next(UNACKED_ITERATOR)
        except StopIteration:
            for svr_queue_name in svr_queue_names:
                redis_msg = REDIS_CONN.queue_consumer(svr_queue_name, SVR_CONSUMER_GROUP_NAME, CONSUMER_NAME)
                if redis_msg:
                    break
    except Exception:
        logging.exception("collect got exception")
        return None, None

    if not redis_msg:
        return None, None
    msg = redis_msg.get_message()
    if not msg:
        logging.error(f"collect got empty message of {redis_msg.get_msg_id()}")
        redis_msg.ack()
        return None, None

    canceled = False
    task_type = msg.get("task_type", "")

    if msg.get("doc_id", "") in [GRAPH_RAPTOR_FAKE_DOC_ID, CANVAS_DEBUG_DOC_ID]:
        # Redis消息已包含fake_doc_id和doc_ids，先使用它
        task = msg

        if task.get("task_type") in PIPELINE_SPECIAL_PROGRESS_FREEZE_TASK_TYPES:
            # 尝试从数据库获取完整配置（tenant_id, parser_config等）
            db_task = TaskService.get_task(db, msg["id"], msg.get("doc_ids", []))

            if db_task:
                # 成功获取数据库配置，合并数据
                task = db_task
                if task:
                    task["doc_id"] = msg["doc_id"]
                    task["doc_ids"] = msg.get("doc_ids", []) or []
                # Redis消息中的这两个字段需要覆盖数据库的
                task["doc_id"] = msg["doc_id"]  # 保持使用fake_doc_id
                task["doc_ids"] = msg.get("doc_ids", []) or []  # 文档列表
                logging.info(f"Task {msg['id']}: merged DB config with Redis message (doc_id={task['doc_id']}, doc_ids count={len(task['doc_ids'])})")
            else:
                # 数据库获取失败，使用Redis消息（已包含必要字段）
                logging.warning(f"Task {msg['id']}: failed to get from DB, using Redis message data")
                # 确保doc_ids字段存在
                if "doc_ids" not in task:
                    task["doc_ids"] = []
    elif task_type == "analyze_v2":
        # analyze_v2 任务特殊处理：直接从 Task 表获取，不 JOIN Document
        # 因为可能是直传文件，没有对应的 Document 记录
        from api.db.db_models import Task as TaskModel
        task_record = db.query(TaskModel).filter(TaskModel.id == msg["id"]).first()

        if task_record:
            # 解析 chunk_ids 获取配置信息
            task_data = json.loads(task_record.chunk_ids) if task_record.chunk_ids else {}
            config = task_data.get("config", {})
            kb_id = task_data.get("kb_id")
            user_id = task_data.get("user_id")

            # 构建 task 字典
            task = {
                "id": task_record.id,
                "doc_id": task_record.doc_id,
                "from_page": task_record.from_page,
                "to_page": task_record.to_page,
                "retry_count": task_record.retry_count,
                "task_type": task_type,
                "chunk_ids": task_record.chunk_ids,
            }

            # 如果有 kb_id，尝试获取相关配置
            if kb_id:
                try:
                    from api.db.db_models import Knowledgebase, Tenant
                    kb_record = db.query(Knowledgebase, Tenant).join(
                        Tenant, Knowledgebase.tenant_id == Tenant.id
                    ).filter(Knowledgebase.id == kb_id).first()

                    if kb_record:
                        kb, tenant = kb_record
                        task["kb_id"] = kb.id
                        task["tenant_id"] = tenant.id
                        task["language"] = kb.language
                        task["embd_id"] = kb.embd_id
                        task["llm_id"] = tenant.llm_id
                        task["parser_id"] = kb.parser_id
                        task["parser_config"] = kb.parser_config
                        logging.info(f"Task {msg['id']}: loaded kb config (kb_id={kb_id}, tenant_id={tenant.id})")
                except Exception as e:
                    logging.warning(f"Task {msg['id']}: failed to load kb config: {e}")

            # 如果没有 kb_id 或获取失败，使用用户的默认配置
            if "tenant_id" not in task and user_id:
                try:
                    from api.db.db_models import UserTenant, Tenant
                    # User 没有 tenant_id，需要通过 UserTenant 关联表查询
                    user_tenant = db.query(UserTenant, Tenant).join(
                        Tenant, UserTenant.tenant_id == Tenant.id
                    ).filter(
                        UserTenant.user_id == user_id,
                        UserTenant.status == "1"  # 有效状态
                    ).first()

                    if user_tenant:
                        ut, tenant = user_tenant
                        task["tenant_id"] = tenant.id
                        task["language"] = "Chinese"  # 默认中文
                        task["llm_id"] = tenant.llm_id
                        task["embd_id"] = tenant.embd_id
                        logging.info(f"Task {msg['id']}: loaded user config (user_id={user_id}, tenant_id={tenant.id})")
                except Exception as e:
                    logging.warning(f"Task {msg['id']}: failed to load user config: {e}")

            # 如果还是没有 tenant_id，任务无法执行
            if "tenant_id" not in task:
                logging.error(f"Task {msg['id']}: Missing tenant_id. Please provide kb_id or valid user_id.")
                task = None
        else:
            task = None
    else:
        # 普通任务，从数据库获取
        task = TaskService.get_task(db, msg["id"])

    if task:
        canceled = has_canceled(task["id"])
    if not task or canceled:
        state = "is unknown" if not task else "has been cancelled"
        FAILED_TASKS += 1
        logging.warning(f"collect task {msg['id']} {state}")
        redis_msg.ack()
        return None, None

    # task_type 已在前面定义
    task["task_type"] = task_type
    if task_type[:8] == "dataflow":
        task["tenant_id"] = msg["tenant_id"]
        task["dataflow_id"] = msg["dataflow_id"]
        task["kb_id"] = msg.get("kb_id", "")
    return redis_msg, task


async def get_storage_binary(bucket, name):
    return await asyncio.to_thread(settings.STORAGE_IMPL.get, bucket, name)


@timeout(60 * 80, 1)
async def build_chunks(task, progress_callback, db: Session):
    if task["size"] > settings.DOC_MAXIMUM_SIZE:
        set_progress(db, task["id"], prog=-1, msg="File size exceeds( <= %dMb )" %
                                                  (int(settings.DOC_MAXIMUM_SIZE / 1024 / 1024)))
        return []

    chunker = FACTORY[task["parser_id"].lower()]
    try:
        st = timer()
        bucket, name = File2DocumentService.get_storage_address(db, doc_id=task["doc_id"])
        binary = await get_storage_binary(bucket, name)
        logging.info("From minio({}) {}/{}".format(timer() - st, task["location"], task["name"]))
    except TimeoutError:
        progress_callback(-1, "Internal server error: Fetch file from minio timeout. Could you try it again.")
        logging.exception("Minio {}/{} got timeout: Fetch file from minio timeout.".format(task["location"], task["name"]))
        raise
    except Exception as e:
        if re.search("(No such file|not found)", str(e)):
            progress_callback(-1, "Can not find file <%s> from minio. Could you try it again?" % task["name"])
        else:
            progress_callback(-1, "Get file from minio: %s" % str(e).replace("'", ""))
        logging.exception("Chunking {}/{} got exception".format(task["location"], task["name"]))
        # traceback.print_exc()
        raise

    try:
        async with chunk_limiter:
            cks = await asyncio.to_thread(
                chunker.chunk,
                task["name"],
                binary=binary,
                from_page=task["from_page"],
                to_page=task["to_page"],
                lang=task["language"],
                callback=progress_callback,
                kb_id=task["kb_id"],
                parser_config=task["parser_config"],
                tenant_id=task["tenant_id"],
            )
        logging.info("Chunking({}) {}/{}".format(timer() - st, task["location"], task["name"]))
    except TaskCanceledException:
        raise
    except Exception as e:
        progress_callback(-1, "Internal server error while chunking: %s" % str(e).replace("'", ""))
        logging.exception("Chunking {}/{} got exception".format(task["location"], task["name"]))
        raise

    docs = []
    doc = {
        "doc_id": task["doc_id"],
        "kb_id": [str(task["kb_id"])]
    }

    if "auth" in task and task["auth"]:
        doc["auth"] = task["auth"]
    if task.get("pagerank"):
        doc[PAGERANK_FLD] = int(task["pagerank"])
    st = timer()

    @timeout(60)
    async def upload_to_minio(document, chunk):
        try:
            d = copy.deepcopy(document)
            d.update(chunk)
            d["pk"] = xxhash.xxh64(
                (chunk["content_with_weight"] + str(d["doc_id"])).encode("utf-8", "surrogatepass")).hexdigest()
            d["create_time"] = str(datetime.now()).replace("T", " ")[:19]
            d["create_timestamp_flt"] = datetime.now().timestamp()
            d["page_num_int"] = d.get("page_num_int", [])
            d["position_int"] = d.get("position_int", [])
            d["top_int"] = d.get("top_int", [])
            if not d.get("image"):
                _ = d.pop("image", None)
                d["img_id"] = ""
                docs.append(d)
                return
            await image2id(d, partial(settings.STORAGE_IMPL.put, tenant_id=task["tenant_id"]), d["pk"], task["kb_id"])
            docs.append(d)
        except Exception:
            logging.exception("Saving image of chunk {}/{}/{} got exception".format(task["location"], task["name"], d["pk"]))
            raise

    tasks = []
    for ck in cks:
        tasks.append(asyncio.create_task(upload_to_minio(doc, ck)))
    try:
        await asyncio.gather(*tasks, return_exceptions=False)
    except Exception as e:
        logging.error(f"MINIO PUT({task['name']}) got exception: {e}")
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise

    el = timer() - st
    logging.info("MINIO PUT({}) cost {:.3f} s".format(task["name"], el))

    if task["parser_config"].get("auto_keywords", 0):
        st = timer()
        progress_callback(msg="Start to generate keywords for every chunk ...")
        chat_mdl = LLMBundle(db, task["tenant_id"], LLMType.CHAT, llm_name=task["llm_id"], lang=task["language"])

        async def doc_keyword_extraction(chat_mdl, d, topn):
            cached = get_llm_cache(chat_mdl.llm_name, d["content_with_weight"], "keywords", {"topn": topn})
            if not cached:
                async with chat_limiter:
                    cached = await keyword_extraction(chat_mdl, d["content_with_weight"], topn)
                set_llm_cache(chat_mdl.llm_name, d["content_with_weight"], cached, "keywords", {"topn": topn})
            if cached:
                d["important_kwd"] = cached.split(",")
                d["important_tks"] = rag_tokenizer.tokenize(" ".join(d["important_kwd"]))
            return

        tasks = []
        for d in docs:
            tasks.append(asyncio.create_task(doc_keyword_extraction(chat_mdl, d, task["parser_config"]["auto_keywords"])))
        try:
            await asyncio.gather(*tasks, return_exceptions=False)
        except Exception as e:
            logging.error("Error in doc_keyword_extraction: {}".format(e))
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        progress_callback(msg="Keywords generation {} chunks completed in {:.2f}s".format(len(docs), timer() - st))

    if task["parser_config"].get("auto_questions", 0):
        st = timer()
        progress_callback(msg="Start to generate questions for every chunk ...")
        chat_mdl = LLMBundle(db, task["tenant_id"], LLMType.CHAT, llm_name=task["llm_id"], lang=task["language"])

        async def doc_question_proposal(chat_mdl, d, topn):
            cached = get_llm_cache(chat_mdl.llm_name, d["content_with_weight"], "question", {"topn": topn})
            if not cached:
                async with chat_limiter:
                    cached = await question_proposal(chat_mdl, d["content_with_weight"], topn)
                set_llm_cache(chat_mdl.llm_name, d["content_with_weight"], cached, "question", {"topn": topn})
            if cached:
                d["question_kwd"] = cached.split("\n")
                d["question_tks"] = rag_tokenizer.tokenize("\n".join(d["question_kwd"]))
        tasks = []
        for d in docs:
            tasks.append(asyncio.create_task(doc_question_proposal(chat_mdl, d, task["parser_config"]["auto_questions"])))
        try:
            await asyncio.gather(*tasks, return_exceptions=False)
        except Exception as e:
            logging.error("Error in doc_question_proposal", exc_info=e)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        progress_callback(msg="Question generation {} chunks completed in {:.2f}s".format(len(docs), timer() - st))

    if task["kb_parser_config"].get("tag_kb_ids", []):
        progress_callback(msg="Start to tag for every chunk ...")
        kb_ids = task["kb_parser_config"]["tag_kb_ids"]
        tenant_id = task["tenant_id"]
        topn_tags = task["kb_parser_config"].get("topn_tags", 3)
        S = 1000
        st = timer()
        examples = []
        all_tags = get_tags_from_cache(kb_ids)
        if not all_tags:
            all_tags = settings.retriever.all_tags_in_portion(tenant_id, kb_ids, S)
            set_tags_to_cache(kb_ids, all_tags)
        else:
            all_tags = json.loads(all_tags)

        chat_mdl = LLMBundle(db, task["tenant_id"], LLMType.CHAT, llm_name=task["llm_id"], lang=task["language"])

        docs_to_tag = []
        for d in docs:
            task_canceled = has_canceled(task["id"])
            if task_canceled:
                progress_callback(-1, msg="Task has been canceled.")
                return None
            if settings.retriever.tag_content(tenant_id, kb_ids, d, all_tags, topn_tags=topn_tags, S=S) and len(d[TAG_FLD]) > 0:
                examples.append({"content": d["content_with_weight"], TAG_FLD: d[TAG_FLD]})
            else:
                docs_to_tag.append(d)

        async def doc_content_tagging(chat_mdl, d, topn_tags):
            cached = get_llm_cache(chat_mdl.llm_name, d["content_with_weight"], all_tags, {"topn": topn_tags})
            if not cached:
                picked_examples = random.choices(examples, k=2) if len(examples) > 2 else examples
                if not picked_examples:
                    picked_examples.append({"content": "This is an example", TAG_FLD: {'example': 1}})
                async with chat_limiter:
                    cached = await content_tagging(
                        chat_mdl,
                        d["content_with_weight"],
                        all_tags,
                        picked_examples,
                        topn_tags,
                    )
                if cached:
                    cached = json.dumps(cached)
            if cached:
                set_llm_cache(chat_mdl.llm_name, d["content_with_weight"], cached, all_tags, {"topn": topn_tags})
                d[TAG_FLD] = json.loads(cached)
        tasks = []
        for d in docs_to_tag:
            tasks.append(asyncio.create_task(doc_content_tagging(chat_mdl, d, topn_tags)))
        try:
            await asyncio.gather(*tasks, return_exceptions=False)
        except Exception as e:
            logging.error("Error tagging docs: {}".format(e))
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        progress_callback(msg="Tagging {} chunks completed in {:.2f}s".format(len(docs), timer() - st))

    return docs


def build_TOC(task, docs, progress_callback):
    progress_callback(msg="Start to generate table of content ...")
    with db_connection() as db:
        chat_mdl = LLMBundle(db, task["tenant_id"], LLMType.CHAT, llm_name=task["llm_id"], lang=task["language"])
    docs = sorted(docs, key=lambda d: (
        d.get("page_num_int", 0)[0] if isinstance(d.get("page_num_int", 0), list) else d.get("page_num_int", 0),
        d.get("top_int", 0)[0] if isinstance(d.get("top_int", 0), list) else d.get("top_int", 0)
    ))
    toc: list[dict] = asyncio.run(run_toc_from_text([d["content_with_weight"] for d in docs], chat_mdl, progress_callback))
    logging.info("------------ T O C -------------\n" + json.dumps(toc, ensure_ascii=False, indent='  '))
    ii = 0
    while ii < len(toc):
        try:
            idx = int(toc[ii]["chunk_id"])
            del toc[ii]["chunk_id"]
            toc[ii]["ids"] = [docs[idx]["id"]]
            if ii == len(toc) - 1:
                break
            for jj in range(idx + 1, int(toc[ii + 1]["chunk_id"]) + 1):
                toc[ii]["ids"].append(docs[jj]["id"])
        except Exception as e:
            logging.exception(e)
        ii += 1

    if toc:
        d = copy.deepcopy(docs[-1])
        d["content_with_weight"] = json.dumps(toc, ensure_ascii=False)
        d["toc_kwd"] = "toc"
        d["available_int"] = 0
        d["page_num_int"] = 100000000
        d["pk"] = xxhash.xxh64((d["content_with_weight"] + str(d["doc_id"])).encode("utf-8", "surrogatepass")).hexdigest()
        return d
    return None


async def _get_embedding_dimension(row: dict) -> int:
    """获取 embedding 模型的向量维度"""
    default_dim = 768
    if not row.get("embd_id") or not row.get("tenant_id"):
        return default_dim

    try:
        with db_connection() as db:
            embedding_model = LLMBundle(
                db, row["tenant_id"], LLMType.EMBEDDING,
                llm_name=row["embd_id"], lang=row.get("language", "en")
            )
            sample_vec, _ = embedding_model.encode(["测试文本"])
            # 注意：sample_vec 是 numpy 数组，不能直接用 if sample_vec 判断
            if len(sample_vec) > 0:
                dim = len(sample_vec[0])
                logging.info(f"当前embedding模型维度: {dim}")
                return dim
    except Exception as e:
        logging.warning(f"获取嵌入模型维度失败，使用默认维度 {default_dim}: {e}")

    return default_dim


async def init_kb(row, kb_name):
    """
    初始化知识库，创建集合/索引。

    所有数据库的 createIdx / create_collection_with_mapping 内部都已实现幂等检查：
    - ES/OpenSearch: if self.indexExist(): return True
    - Infinity: ConflictType.Ignore
    - OceanBase: _try_with_lock()
    - Milvus: if self.has_collection(): return

    Args:
        row: 任务数据行
        kb_name: 知识库名称
    """
    idxnm = search.index_name_one(row["tenant_id"], kb_name)
    kb_id = row.get("kb_id", "")
    db_type = settings.docStoreConn.dbType()

    # 快速检查：集合/索引已存在则直接返回（避免不必要的 embedding 调用）
    if db_type == "milvus":
        if await asyncio.to_thread(settings.docStoreConn.has_collection, idxnm):
            return
    else:
        if await asyncio.to_thread(settings.docStoreConn.indexExist, idxnm, kb_id):
            return

    # 获取向量维度
    vector_dim = await _get_embedding_dimension(row)

    # 创建集合/索引（底层已有幂等保护）
    if db_type == "milvus":
        await _create_milvus_collection(idxnm, vector_dim)
    else:
        await asyncio.to_thread(settings.docStoreConn.createIdx, idxnm, kb_id, vector_dim)


async def _create_milvus_collection(collection_name: str, vector_dim: int):
    """创建 Milvus 集合（内部已有幂等检查）"""
    mapping_path = os.path.join(get_project_base_directory(), "configs", "mapping.json")

    def load_mapping():
        with open(mapping_path, 'r') as f:
            return json.load(f)

    mapping = await asyncio.to_thread(load_mapping)

    # 更新 mapping 中的向量维度
    for template in mapping["mappings"]["dynamic_templates"]:
        if "standard_vector_template" in template:
            template["standard_vector_template"]["mapping"]["dims"] = vector_dim

    # 构建自动维度字典
    auto_dimensions = {
        "vector": vector_dim,
        f"q_{vector_dim}_vec": vector_dim
    }

    # 创建集合（内部会检查集合是否已存在）
    await asyncio.to_thread(
        settings.docStoreConn.create_collection_with_mapping,
        collection_name, mapping, auto_dimensions
    )


def convert_data_types(data, schema):
    """
    转换数据类型以匹配向量数据库模式，确保所有必要字段都有值
    
    对于 Milvus: 根据 schema 进行严格的类型转换
    对于 ES/OpenSearch: schema 为空，直接返回原数据

    Args:
        data: 文档数据字典
        schema: 集合模式 (ES 返回 {"fields": []})

    Returns:
        转换后的数据字典
    """
    # 创建数据的副本以避免修改原始数据
    result = data.copy()

    # 处理schema中所有定义的字段
    schema_fields = {}
    for field in schema['fields']:
        schema_fields[field['name']] = field

    # 确保所有必要字段都有值
    for field_name, field_info in schema_fields.items():
        # 如果字段不在数据中，添加默认值
        if field_name not in result:
            field_type = field_info['type']

            # 根据字段类型设置默认值
            if field_type == DataType.FLOAT_VECTOR:
                result[field_name] = [0.0] * field_info['params']['dim']
            elif field_type == DataType.VARCHAR:
                if field_name == "tag_feas":
                    # 特别处理 available_int 字段，设置默认值为 1
                    result[field_name] = "{}"
                else:
                    result[field_name] = ""
            elif field_type == DataType.FLOAT:
                result[field_name] = 0.0
            elif field_type == DataType.INT64:
                if field_name == "available_int":
                    # 特别处理 available_int 字段，设置默认值为 1
                    result[field_name] = 1
                else:
                    result[field_name] = 0
            elif field_type == DataType.JSON:
                result[field_name] = "{}"
            elif field_type == DataType.ARRAY:
                result[field_name] = []
        else:
            # 转换现有数据的类型
            field_type = field_info['type']
            if field_type == DataType.FLOAT_VECTOR:
                if not isinstance(result[field_name], list):
                    result[field_name] = list(result[field_name])
            elif field_type == DataType.VARCHAR:
                if isinstance(result[field_name], list):
                    result[field_name] = ','.join(map(str, result[field_name]))
                else:
                    result[field_name] = str(result[field_name])
            elif field_type == DataType.FLOAT:
                result[field_name] = float(result[field_name])
            elif field_type == DataType.INT64:
                result[field_name] = int(result[field_name])
            elif field_type == DataType.JSON:
                if isinstance(result[field_name], list) or isinstance(result[field_name], dict):
                    result[field_name] = json.dumps(result[field_name])
                elif not isinstance(result[field_name], str):
                    result[field_name] = str(result[field_name])
            elif field_type == DataType.ARRAY:
                if not isinstance(result[field_name], list):
                    if isinstance(result[field_name], str):
                        result[field_name] = result[field_name].split(',')
                    else:
                        result[field_name] = [result[field_name]]

    # 处理动态向量字段 (q_*_vec) - 仅用于 Milvus，ES 使用动态 mapping 自动处理
    vector_fields = [k for k in result.keys() if re.match(r'q_\d+_vec', k)]
    for vector_field in vector_fields:
        if vector_field not in schema_fields:
            # 如果这是一个新的向量字段，记录一下但保留它（使用 debug 级别避免重复日志）
            logging.debug(f"发现新的向量字段 {vector_field}，保留在数据中")

    return result


async def get_schema(collection_name):
    schema = await asyncio.to_thread(settings.docStoreConn.describe_collection, collection_name)
    return schema


async def embedding(docs, mdl, parser_config=None, callback=None):
    """
    为文档生成向量嵌入，并同时存储到标准vector字段和维度特定字段中

    Args:
        docs: 文档列表
        mdl: 嵌入模型对象
        parser_config: 解析器配置
        callback: 进度回调函数

    Returns:
        token_count: 处理的token数量
    """
    if parser_config is None:
        parser_config = {}
    tts, cnts = [], []
    for d in docs:
        tts.append(d.get("docnm_kwd", "Title"))
        c = "\n".join(d.get("question_kwd", []))
        if not c:
            c = d["content_with_weight"]
        c = re.sub(r"</?(table|td|caption|tr|th)( [^<>]{0,12})?>", " ", c)
        if not c:
            c = "None"
        cnts.append(c)

    tk_count = 0
    if len(tts) == len(cnts):
        vts, c = await asyncio.to_thread(mdl.encode, tts[0:1])
        tts = np.tile(vts[0], (len(cnts), 1))
        tk_count += c

    @timeout(60)
    def batch_encode(txts):
        nonlocal mdl
        return mdl.encode([truncate(c, mdl.max_length - 10) for c in txts])

    cnts_ = np.array([])
    for i in range(0, len(cnts), settings.EMBEDDING_BATCH_SIZE):
        async with embed_limiter:
            vts, c = await asyncio.to_thread(batch_encode, cnts[i : i + settings.EMBEDDING_BATCH_SIZE])
        if len(cnts_) == 0:
            cnts_ = vts
        else:
            cnts_ = np.concatenate((cnts_, vts), axis=0)
        tk_count += c
        callback(prog=0.7 + 0.2 * (i + 1) / len(cnts), msg="")
    cnts = cnts_

    filename_embd_weight = parser_config.get("filename_embd_weight", 0.1) # due to the db support none value
    if not filename_embd_weight:
        filename_embd_weight = 0.1
    title_w = float(filename_embd_weight)
    if tts.ndim == 2 and cnts.ndim == 2 and tts.shape == cnts.shape:
        vects = title_w * tts + (1 - title_w) * cnts
    else:
        vects = cnts

    assert len(vects) == len(docs)

    # 获取向量维度
    vector_dim = len(vects[0]) if len(vects) > 0 else 0

    for i, d in enumerate(docs):
        v = vects[i].tolist()
        # 始终保存到标准vector字段
        d["vector"] = v
        # 同时保存到维度特定字段
        d[f"q_{vector_dim}_vec"] = v

    return tk_count


async def run_dataflow(db: Session, task: dict):
    from api.db.services.canvas_service import UserCanvasService
    from core.flow.pipeline import Pipeline

    task_start_ts = timer()
    dataflow_id = task["dataflow_id"]
    doc_id = task["doc_id"]
    task_id = task["id"]
    task_dataset_id = task["kb_id"]

    if task["task_type"] == "dataflow":
        cvs = UserCanvasService.get_by_id(db, dataflow_id)
        assert cvs, "User pipeline not found."
        dsl = cvs.dsl
    else:
        pipeline_log = PipelineOperationLogService.get_by_id(db, dataflow_id)
        assert pipeline_log, "Pipeline log not found."
        dsl = pipeline_log.dsl
        dataflow_id = pipeline_log.pipeline_id
    pipeline = Pipeline(dsl, tenant_id=task["tenant_id"], doc_id=doc_id, task_id=task_id, flow_id=dataflow_id)
    chunks = await pipeline.run(file=task["file"]) if task.get("file") else await pipeline.run()
    if doc_id == CANVAS_DEBUG_DOC_ID:
        return

    if not chunks:
        PipelineOperationLogService.create(db, document_id=doc_id, pipeline_id=dataflow_id, task_type=PipelineTaskType.PARSE, dsl=str(pipeline))
        return

    embedding_token_consumption = chunks.get("embedding_token_consumption", 0)
    if chunks.get("chunks"):
        chunks = copy.deepcopy(chunks["chunks"])
    elif chunks.get("json"):
        chunks = copy.deepcopy(chunks["json"])
    elif chunks.get("markdown"):
        chunks = [{"text": [chunks["markdown"]]}]
    elif chunks.get("text"):
        chunks = [{"text": [chunks["text"]]}]
    elif chunks.get("html"):
        chunks = [{"text": [chunks["html"]]}]

    keys = [k for o in chunks for k in list(o.keys())]
    if not any([re.match(r"q_[0-9]+_vec", k) for k in keys]):
        try:
            set_progress(db, task_id, prog=0.82, msg="\n-------------------------------------\nStart to embedding...")
            kb = KnowledgebaseService.get_by_id(db, task["kb_id"])
            embedding_id = kb.embd_id
            embedding_model = LLMBundle(db, task["tenant_id"], LLMType.EMBEDDING, llm_name=embedding_id)

            @timeout(60)
            def batch_encode(txts):
                nonlocal embedding_model
                return embedding_model.encode([truncate(c, embedding_model.max_length - 10) for c in txts])

            vects = np.array([])
            texts = [o.get("questions", o.get("summary", o["text"])) for o in chunks]
            delta = 0.20 / (len(texts) // settings.EMBEDDING_BATCH_SIZE + 1)
            prog = 0.8
            for i in range(0, len(texts), settings.EMBEDDING_BATCH_SIZE):
                async with embed_limiter:
                    vts, c = await asyncio.to_thread(batch_encode, texts[i : i + settings.EMBEDDING_BATCH_SIZE])
                if len(vects) == 0:
                    vects = vts
                else:
                    vects = np.concatenate((vects, vts), axis=0)
                embedding_token_consumption += c
                prog += delta
                if i % (len(texts) // settings.EMBEDDING_BATCH_SIZE / 100 + 1) == 1:
                    set_progress(db, task_id, prog=prog, msg=f"{i + 1} / {len(texts) // settings.EMBEDDING_BATCH_SIZE}")

            assert len(vects) == len(chunks)
            for i, ck in enumerate(chunks):
                v = vects[i].tolist()
                ck["q_%d_vec" % len(v)] = v
        except Exception as e:
            set_progress(db, task_id, prog=-1, msg=f"[ERROR]: {e}")
            PipelineOperationLogService.create(db, document_id=doc_id, pipeline_id=dataflow_id, task_type=PipelineTaskType.PARSE, dsl=str(pipeline))
            return


    metadata = {}
    def dict_update(meta):
        nonlocal metadata
        if not meta:
            return
        if isinstance(meta, str):
            try:
                meta = json_repair.loads(meta)
            except Exception:
                logging.error("Meta data format error.")
                return
        if not isinstance(meta, dict):
            return
        for k, v in meta.items():
            if isinstance(v, list):
                v = [vv for vv in v if isinstance(vv, str)]
                if not v:
                    continue
            if not isinstance(v, list) and not isinstance(v, str):
                continue
            if k not in metadata:
                metadata[k] = v
                continue
            if isinstance(metadata[k], list):
                if isinstance(v, list):
                    metadata[k].extend(v)
                else:
                    metadata[k].append(v)
            else:
                metadata[k] = v

    for ck in chunks:
        ck["doc_id"] = doc_id
        ck["kb_id"] = [str(task["kb_id"])]
        ck["docnm_kwd"] = task["name"]
        ck["create_time"] = str(datetime.now()).replace("T", " ")[:19]
        ck["create_timestamp_flt"] = datetime.now().timestamp()
        if not ck.get("id"):
            ck["id"] = xxhash.xxh64((ck["text"] + str(ck["doc_id"])).encode("utf-8")).hexdigest()
        if "questions" in ck:
            if "question_tks" not in ck:
                ck["question_kwd"] = ck["questions"].split("\n")
                ck["question_tks"] = rag_tokenizer.tokenize(str(ck["questions"]))
            del ck["questions"]
        if "keywords" in ck:
            if "important_tks" not in ck:
                ck["important_kwd"] = ck["keywords"].split(",")
                ck["important_tks"] = rag_tokenizer.tokenize(str(ck["keywords"]))
            del ck["keywords"]
        if "summary" in ck:
            if "content_ltks" not in ck:
                ck["content_ltks"] = rag_tokenizer.tokenize(str(ck["summary"]))
                ck["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(ck["content_ltks"])
            del ck["summary"]
        if "metadata" in ck:
            dict_update(ck["metadata"])
            del ck["metadata"]
        if "content_with_weight" not in ck:
            ck["content_with_weight"] = ck["text"]
        del ck["text"]
        if "positions" in ck:
            add_positions(ck, ck["positions"])
            del ck["positions"]

    if metadata:
        e, doc = DocumentService.get_by_id(db, doc_id)
        if e:
            if isinstance(doc.meta_fields, str):
                doc.meta_fields = json.loads(doc.meta_fields)
            dict_update(doc.meta_fields)
            DocumentService.update_by_id(db, doc_id, {"meta_fields": metadata})

    start_ts = timer()
    set_progress(task_id, prog=0.82, msg="[DOC Engine]:\nStart to index...")
    # 获取知识库名称和集合信息
    kb = KnowledgebaseService.get_by_id(db, task_dataset_id)
    kb_name = kb.name if kb else "default"
    collection_name = search.index_name_one(task["tenant_id"], kb_name)
    schema = await get_schema(collection_name)
    
    e = await insert_milvus(db, task_id, task["tenant_id"], task["kb_id"], chunks, partial(set_progress, task_id, 0, 100000000), collection_name, schema)
    if not e:
        PipelineOperationLogService.create(db, document_id=doc_id, pipeline_id=dataflow_id, task_type=PipelineTaskType.PARSE, dsl=str(pipeline))
        return

    time_cost = timer() - start_ts
    task_time_cost = timer() - task_start_ts
    set_progress(db, task_id, prog=1., msg="Indexing done ({:.2f}s). Task done ({:.2f}s)".format(time_cost, task_time_cost))
    DocumentService.increment_chunk_num(db, doc_id, task_dataset_id, embedding_token_consumption, len(chunks), task_time_cost)
    logging.info("[Done], chunks({}), token({}), elapsed:{:.2f}".format(len(chunks),  embedding_token_consumption, task_time_cost))
    PipelineOperationLogService.create(db, document_id=doc_id, pipeline_id=dataflow_id, task_type=PipelineTaskType.PARSE, dsl=str(pipeline))


@timeout(3600)
async def run_raptor_for_kb(row, kb_parser_config, chat_mdl, embd_mdl, vector_size, callback=None, doc_ids=[]):
    fake_doc_id = GRAPH_RAPTOR_FAKE_DOC_ID

    raptor_config = kb_parser_config.get("raptor", {})

    if vector_size != 768:
        vctr_nm = "q_%d_vec" % vector_size
    else:
        vctr_nm = "vector"

    res = []
    tk_count = 0
    max_errors = int(os.environ.get("RAPTOR_MAX_ERRORS", 3))

    async def generate(chunks, did):
        nonlocal tk_count, res
        raptor = Raptor(
            raptor_config.get("max_cluster", 64),
            chat_mdl,
            embd_mdl,
            raptor_config["prompt"],
            raptor_config["max_token"],
            raptor_config["threshold"],
            max_errors=max_errors,
        )
        original_length = len(chunks)
        chunks = await raptor(chunks, kb_parser_config["raptor"]["random_seed"], callback, row["id"])
        doc = {
            "doc_id": did,
            "kb_id": [str(row["kb_id"])],
            "docnm_kwd": row["name"],
            "title_tks": rag_tokenizer.tokenize(row["name"]),
            "raptor_kwd": "raptor"
        }
        if row["pagerank"]:
            doc[PAGERANK_FLD] = int(row["pagerank"])

        for content, vctr in chunks[original_length:]:
            d = copy.deepcopy(doc)
            d["pk"] = xxhash.xxh64((content + str(did)).encode("utf-8")).hexdigest()
            d["create_time"] = str(datetime.now()).replace("T", " ")[:19]
            d["create_timestamp_flt"] = datetime.now().timestamp()
            d[vctr_nm] = vctr.tolist()
            # d["vector"] = vctr.tolist() # todo 怎么合理发挥我们支持两种向量字段的特性呢？
            d["content_with_weight"] = content
            d["content_ltks"] = rag_tokenizer.tokenize(content)
            d["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(d["content_ltks"])
            res.append(d)
            tk_count += num_tokens_from_string(content)

    if raptor_config.get("scope", "file") == "file":
        for x, doc_id in enumerate(doc_ids):
            chunks = []
            for d in settings.retriever.chunk_list(doc_id, row["tenant_id"], [str(row["kb_id"])], fields=["content_with_weight", vctr_nm], sort_by_position=True):
                chunks.append((d["content_with_weight"], np.array(d[vctr_nm])))
            await generate(chunks, doc_id)
            callback(prog=(x + 1.)/len(doc_ids))
    else:
        chunks = []
        for doc_id in doc_ids:
            for d in settings.retriever.chunk_list(doc_id, row["tenant_id"], [str(row["kb_id"])], fields=["content_with_weight", vctr_nm], sort_by_position=True):
                chunks.append((d["content_with_weight"], np.array(d[vctr_nm])))

        await generate(chunks, fake_doc_id)

    return res, tk_count


async def _detect_hierarchical_structure(chunks, hierarchical_config):
    """
    检测文档是否有层次结构
    
    参考 core/flow 的设计理念：
    - 不单独实现检测逻辑，而是直接调用 hierarchical_merge
    - 通过返回的 chapters 数量判断是否有结构
    
    Args:
        chunks: chunk 列表
        hierarchical_config: 层次化配置
    
    Returns:
        bool: 是否有层次结构（chapters > 1 表示有结构）
    """
    from core.flow.utils import hierarchical_merge
    
    # 调用 hierarchical_merge 尝试识别结构
    result = await hierarchical_merge(
        chunks=chunks,
        levels=hierarchical_config.get("levels") if hierarchical_config else None,
        hierarchy=hierarchical_config.get("hierarchy", 1) if hierarchical_config else 1,
        callback=None
    )
    
    chapters = result.get("chapters", [])
    
    # 如果识别出多个章节，说明有层次结构
    has_structure = len(chapters) > 1
    
    logging.info(f"Structure detection: {len(chapters)} chapters identified, has_structure={has_structure}")
    
    return has_structure


async def _hierarchical_merge(chunks, config, tenant_id=None, db=None):
    """
    层次化合并

    使用 core/flow/utils 的 hierarchical_merge 实现，并增强：
    - 位置信息合并
    - 图片合并（仅用于展示，不进行 OCR/VLM 处理）

    Args:
        chunks: chunk 列表
        config: 层次化配置，支持以下字段：
            - levels: 标题层级正则表达式
            - hierarchy: 合并到第几层
        tenant_id: 租户ID（保留参数，未使用）
        db: 数据库session（保留参数，未使用）
    
    注意：
        图片处理应该在 Parser 阶段配置（parse_method），而不是在这里。
        章节级的图片是合并后的巨图，不适合进行 OCR/VLM 处理。
    """
    from core.flow.utils import hierarchical_merge

    # 默认配置
    if not config:
        config = {
            "levels": [
                ["^#\\s+", "^第[一二三四五六七八九十百]+章"],
                ["^##\\s+", "^\\d+\\.\\s+"]
            ],
            "hierarchy": 1
        }

    levels = config.get("levels")
    hierarchy_level = config.get("hierarchy", 1)

    # ⭐ 调用 core/flow/utils 的层次化合并
    result = await hierarchical_merge(
        chunks=chunks,
        levels=levels,
        hierarchy=hierarchy_level,
        callback=None
    )
    
    chapters = result.get("chapters", [])
    
    # ✨ 增强：为每个章节添加位置信息、图片合并
    for chapter in chapters:
        chapter_chunks = chapter.get("chunks", [])
        
        # 合并位置信息
        chapter_positions = []
        for chunk in chapter_chunks:
            if "positions" in chunk and chunk["positions"]:
                chapter_positions.extend(chunk["positions"])
        
        # 合并图片（内存中处理，不上传 MinIO）
        chapter_image = None
        for chunk in chapter_chunks:
            if "image" in chunk and chunk["image"]:
                chapter_image = concat_img(chapter_image, chunk["image"])
        
        # 计算页码范围
        page_range = None
        if chapter_positions:
            pages = [p[0] for p in chapter_positions]
            page_range = [min(pages), max(pages)]
        
        # 添加到章节信息
        chapter["positions"] = chapter_positions
        chapter["image"] = chapter_image
        chapter["page_range"] = page_range

    # ⚠️ 注意：不在章节级别处理合并后的图片
    # 
    # 原因分析：
    # 1. Parser 阶段已经对每张原始图片进行了 OCR/VLM 处理
    # 2. 图片内容已经转化为文本，存储在 chunk 的 content_with_weight 中
    # 3. 章节级的图片是合并后的巨图（可能超过 65500 像素），VLM/OCR 无法处理
    # 4. 所有其他地方（parser/picture/parser_utils）都是处理单张原始图片
    # 5. 合并后的巨图在视觉上没有连贯性（只是垂直拼接），VLM 识别效果差
    #
    # 正确的流程：
    # - 用户配置 parse_method="qwen-vl-plus" → 在 Parser 阶段对每张图片进行 VLM
    # - HierarchicalMerger 只负责合并文本（文本中已包含图片描述）
    # - 合并后的图片仅用于展示，不再进行 OCR/VLM 处理
    #
    # 参考：core/flow/parser/parser.py 第 346-375 行 - 只处理单张原始图片
    
    if config.get("image_model"):
        logging.info(
            f"Note: image_model ('{config.get('image_model')}') should be configured in parse_method, "
            f"not in hierarchical_config. Images are processed during parsing stage, "
            f"not during hierarchical merge. Each original image has already been processed individually."
        )

    # 构建结构信息
    structure = {
        "chapters": [
            {
                "title": ch["title"],
                "level": 0,
                "chunk_range": [ch["chunk_indices"][0], ch["chunk_indices"][-1]]
                if ch.get("chunk_indices") else [0, 0],
                "page_range": ch.get("page_range")  # ✨ 页码范围
            }
            for ch in chapters
        ]
    }

    logging.info(f"HierarchicalMerger: {len(chapters)} chapters identified")

    # 调试日志：打印每个章节的信息
    for idx, ch in enumerate(chapters):
        page_info = f", pages={ch['page_range']}" if ch.get('page_range') else ""
        logging.info(
            f"  Chapter {idx + 1}: title='{ch['title'][:30]}', chunks={len(ch.get('chunks', []))}, "
            f"positions={len(ch.get('positions', []))}{page_info}")

    return {
        "chapters": chapters,
        "summaries": [ch["text"] for ch in chapters],
        "structure": structure
    }


def _get_default_metadata_fields():
    """
    获取默认元数据字段配置

    当用户未配置时，默认提取：摘要 + 语义标签
    """
    return [
        {
            "field_name": "document_summary",
            "prompt": """## 角色
你是一个文档摘要专家。

## 任务
为给定的文本内容生成简洁的摘要。

## 要求
- 摘要长度控制在 150-200 字。
- 准确捕捉主题和关键要点。
- 保持内容连贯、结构清晰。
- 聚焦最重要的信息。
- 摘要必须与给定文本内容使用相同的语言。
- 只输出摘要文本，不要输出其他内容。
""",
            "source": "global_summary",
            "call_mode": "single",
            "post_process": "none",
            "temperature": 0.3,
            "max_tokens": 400
        },
        {
            "field_name": "semantic_tags",
            "prompt": """## 角色
你是一个文本分析专家。

## 任务
从给定的文本内容中提取最重要的关键词/短语。

## 要求
- 总结文本内容，提取最重要的 3-5 个关键词/短语。
- 关键词必须彼此独立，语义上不能有重叠。
- 缩写词与全称合并（优先使用全称）。
- 关键词必须与给定文本内容使用相同的语言。
- 关键词之间用英文逗号分隔。
- 只输出关键词，不要输出其他内容。
""",
            "source": "cluster_summaries",
            "call_mode": "batch",
            "post_process": "counter_top10",
            "temperature": 0.2,
            "max_tokens": 100
        }
    ]


def _get_extraction_contents(source, summaries, chunks, cluster_results=None, original_length=0):
    """
    根据 source 参数选择数据源

    ✨ 新增：支持字典格式的 summaries（包含 text、positions）

    Args:
        source: 数据源类型
        summaries: 当前处理后的摘要列表（RAPTOR 聚类摘要或原始 chunks）
                   可以是 str 列表或 dict 列表（dict 包含 text、positions、image）
        chunks: 原始 chunks
        cluster_results: RAPTOR 完整结果（可选）
        original_length: 原始 chunk 数量

    Returns:
        list[str]: 用于提取的内容列表
    """
    # ✨ 辅助函数：提取文本（兼容字符串和字典）
    def extract_text(item):
        if isinstance(item, dict):
            return item.get("text", "")
        return item if isinstance(item, str) else ""

    if source == "global_summary":
        # 全局摘要：优先用 RAPTOR 最终摘要
        if cluster_results and len(cluster_results) > original_length:
            # RAPTOR 最后一个 = 全局摘要
            return [cluster_results[-1][0]]
        else:
            # 没用 RAPTOR，智能合并 chunks
            # 如果 summaries 为空，使用原始 chunks
            if not summaries:
                summaries = [c.get("content_with_weight", "") for c in chunks if c.get("content_with_weight")]

            if not summaries:
                # 仍然为空，返回空字符串（会被后续逻辑处理）
                return [""]

            # ✨ 提取文本（兼容新格式）
            texts = [extract_text(s) for s in summaries]

            # 根据 chunk 数量决定合并多少个
            if len(texts) <= 5:
                # 少量 chunk，全部合并
                combined = "\n\n".join(texts)
            elif len(texts) <= 20:
                # 中等数量，取前 10 个
                combined = "\n\n".join(texts[:10])
            else:
                # 大量 chunk，取前 20 个
                combined = "\n\n".join(texts[:20])

            return [combined]

    elif source == "cluster_summaries":
        # 聚类摘要：使用当前的 summaries（可能是 RAPTOR 摘要或原始 chunks）
        # 如果为空，使用原始 chunks
        if not summaries:
            summaries = [c.get("content_with_weight", "") for c in chunks if c.get("content_with_weight")]
        # ✨ 提取文本（兼容新格式）
        return [extract_text(s) for s in summaries]

    elif source == "original_chunks":
        # 原始 chunks
        return [c["content_with_weight"] for c in chunks]

    else:
        # 默认：global_summary
        return _get_extraction_contents("global_summary", summaries, chunks, cluster_results, original_length)


def _post_process_result(raw_output, post_process):
    """
    后处理 LLM 输出

    参考：
    - DocumentAnalysisService 第 759 行: semantic_tags = [t.strip() for t in result.split(",")]
    - keyword_extraction: 直接返回 LLM 原始输出

    Args:
        raw_output: LLM 输出（str 或 list[str]）
        post_process: 后处理策略

    Returns:
        处理后的结果
    """
    from collections import Counter

    if post_process == "none":
        # 原样返回（参考 core/flow/extractor）
        return raw_output

    elif post_process == "split_comma":
        # 简单按逗号分割（参考 DocumentAnalysisService 第 759 行）
        if isinstance(raw_output, list):
            all_items = []
            for r in raw_output:
                items = [t.strip() for t in r.split(",") if t.strip()]
                all_items.extend(items)
            return all_items
        else:
            return [t.strip() for t in raw_output.split(",") if t.strip()]

    elif post_process == "counter_top10":
        # 频次统计 top10（参考 DocumentAnalysisService._compute_frequency_tags）
        if isinstance(raw_output, list):
            all_items = []
            for r in raw_output:
                items = [t.strip() for t in r.split(",") if t.strip()]
                all_items.extend(items)
            counter = Counter(all_items)
            return [item for item, count in counter.most_common(10)]
        else:
            items = [t.strip() for t in raw_output.split(",") if t.strip()]
            return items

    elif post_process == "concat":
        # 拼接（batch 模式）
        if isinstance(raw_output, list):
            return "\n\n".join(raw_output)
        return raw_output

    else:
        # 未知策略，原样返回
        return raw_output


@timeout(3600)
async def run_analyze_v2_task(task, chat_mdl, embd_mdl, vector_size, db, callback=None):
    """
    执行 analyze_v2 文档分析任务

    ⭐ 完全基于 core/flow 组件实现，支持灵活配置

    功能特性：
    - Parser: 文档解析（支持 PDF/Word/Excel/PPT/图片/音视频等，支持 VLM 图片理解）
    - HierarchicalMerger: 层次化章节合并
    - RAPTOR: 递归摘要聚类
    - Extractor: 元数据字段提取
    - 智能去重：smart/semantic/none

    Args:
        task: 任务信息，task["chunk_ids"] 中包含配置：
            config.processing_strategy: 处理策略 (auto/simple/hierarchical/raptor/hybrid)
            config.parse_method: 解析方法
                - "deepdoc": 深度解析（默认）
                - "plain_text": 纯文本解析
                - "mineru": MinerU 解析
                - "tcadp parser": 腾讯云 ADP 解析（支持 PDF/Excel/PPT）
                - "qwen-vl-plus": VLM 视觉理解（对每张原始图片）
                - "ocr": OCR 识别（对每张原始图片）
            config.table_result_type: TCADP 表格结果类型（默认 "1"）
            config.markdown_image_response_type: TCADP 图片响应类型（默认 "1"）
            config.parser_config: 完整解析器配置（优先级高于 parse_method）
                - pdf: {"parse_method": "deepdoc", "output_format": "json"}
                - excel: {"parse_method": "deepdoc", "output_format": "html"}
                - slides: {"parse_method": "deepdoc", "output_format": "json"}
                - image: {"parse_method": "ocr", "lang": "Chinese"}
                - word: {"output_format": "json"}
                - markdown: {"output_format": "json"}
                - email: {"output_format": "json", "fields": [...]}
                - video: {"llm_id": "..."}
            config.hierarchical_config: 层次化配置
                - levels: 标题正则表达式列表
                - hierarchy: 层级（0=章节，1=章+节）
            config.raptor_config: RAPTOR 配置
                - max_cluster: 最大聚类数
                - max_token: 摘要长度
                - threshold: 聚类阈值
            config.metadata_fields: 元数据字段列表
                - field_name: 字段名
                - prompt: LLM 提示词
                - aggregate: 聚合策略
            config.dedup_strategy: 去重策略 (smart/semantic/none)
        chat_mdl: LLM 模型（用于摘要、提取）
        embd_mdl: Embedding 模型（用于 RAPTOR 聚类）
        vector_size: 向量维度
        db: 数据库 Session
        callback: 进度回调函数

    Returns:
        dict: {
            "metadata": {字段名: 提取的值},
            "processing_info": {处理统计信息},
            "structure": {文档结构信息（如有）}
        }

    示例配置 - 使用 TCADP 解析 Excel/PPT：
        {
            "parse_method": "tcadp parser",
            "table_result_type": "1",
            "markdown_image_response_type": "1"
        }

    示例配置 - 使用 VLM 处理图片：
        {
            "parse_method": "qwen-vl-plus",
            "image_system_prompt": "描述这张图片的关键信息",
            "lang": "Chinese"
        }
    """
    # 解析任务配置
    task_data = json.loads(task.get("chunk_ids", "{}"))
    config = task_data.get("config", {})

    task_id = task["id"]
    doc_id = config.get("doc_id")
    kb_id = config.get("kb_id") or task_data.get("kb_id")
    tenant_id = task.get("tenant_id")

    # 必须有 tenant_id
    if not tenant_id:
        error_msg = "analyze_v2 task missing tenant_id configuration"
        logging.error(f"Task {task_id}: {error_msg}")
        if callback:
            callback(prog=-1, msg=error_msg)
        raise ValueError(error_msg)

    # 进度回调
    if not callback:
        callback = lambda prog, msg: None

    try:
        # ⭐ 参考 PipelineAnalysisService 的算法，在 asyncio 环境中实现

        # 1. 获取文档 chunks
        callback(prog=0.05, msg="读取文档内容...")

        chunks = []
        temp_file_path = task_data.get("temp_file_path")
        file_content_base64 = task_data.get("file_content_base64")
        filename = task_data.get("filename")

        # ⚠️ 优化：立即清理 file_content_base64（避免污染日志/数据库/Redis）
        # 提取后立即删除，不保存到任何地方
        has_base64 = file_content_base64 is not None
        file_size = task_data.get("file_size", 0)

        # 提前删除，避免后续 task_data 被打印/保存时包含大量 base64
        if has_base64:
            task_data["file_content_base64"] = f"<removed, {file_size} bytes>"  # 标记已删除

        # 情况1：直传文件，需要先解析
        if temp_file_path or file_content_base64:
            callback(prog=0.1, msg="解析文档...")

            # 读取文件内容
            if has_base64:
                import base64
                file_content = base64.b64decode(file_content_base64)
                logging.info(f"Task {task_id}: loaded file from base64 ({len(file_content)} bytes)")
                # base64 数据已在前面删除
            else:
                file_content = await get_storage_binary("multirag-temp", temp_file_path)
                if not file_content:
                    callback(prog=-1, msg="无法读取临时文件")
                    return
                logging.info(f"Task {task_id}: loaded file from MinIO ({len(file_content)} bytes)")

            # 解析文件（参考 PipelineAnalysisService._parse_uploaded_file）
            import tempfile
            if not filename:
                filename = os.path.basename(temp_file_path) if temp_file_path else "document"

            callback(prog=0.15, msg="使用 core/flow 解析器...")

            # 使用 core/flow 逻辑解析（保留位置信息）
            from core.flow.utils import parse_file, split_chunks

            # 1. 解析文件（保留结构）
            # 支持两种配置方式（参考 core/flow/parser/parser.py 的 setups 结构）
            
            parser_config_dict = config.get("parser_config")  # 完整方式（优先）
            
            # TCADP parser 特有参数（腾讯云 ADP 解析）
            tcadp_table_result_type = config.get("table_result_type", "1")
            tcadp_markdown_image_response_type = config.get("markdown_image_response_type", "1")
            
            # 媒体上下文配置（为表格/图片添加周围文本）
            table_context_size = config.get("table_context_size", 0) or 0
            image_context_size = config.get("image_context_size", 0) or 0
            
            if parser_config_dict:
                # 方式 1：使用 parser_config 字典（用户一次性配置所有文件类型）
                pdf_config = parser_config_dict.get("pdf", {"parse_method": "deepdoc", "output_format": "json"})
                image_config = parser_config_dict.get("image", {"parse_method": "ocr", "lang": "Chinese"})
                excel_config = parser_config_dict.get("excel", {"parse_method": "deepdoc", "output_format": "html"})
                word_config = parser_config_dict.get("word", {"output_format": "json"})
                email_config = parser_config_dict.get("email", {"output_format": "json", "fields": None})
                video_config = parser_config_dict.get("video", {"llm_id": config.get("video_llm_name")})
                slides_config = parser_config_dict.get("slides", {"parse_method": "deepdoc", "output_format": "json"})
                markdown_config = parser_config_dict.get("markdown", {"output_format": "json"})
                logging.info(f"Using parser_config dictionary mode")
            else:
                # 方式 2：使用简化参数（自动应用到所有文件类型）
                parse_method = config.get("parse_method", "deepdoc")
                output_format = config.get("output_format", "json")

                # PDF 支持的 parse_method: deepdoc, plain_text, mineru, tcadp parser, 或 VLM 模型名
                pdf_parse_method = parse_method
                if parse_method in ["auto", "ocr", "vlm"]:
                    pdf_parse_method = "deepdoc"
                
                pdf_config = {
                    "parse_method": pdf_parse_method,
                    "output_format": output_format,
                    "lang": config.get("lang", "Chinese"),
                    # TCADP 参数（仅当 parse_method 为 tcadp parser 时生效）
                    "table_result_type": tcadp_table_result_type,
                    "markdown_image_response_type": tcadp_markdown_image_response_type,
                    # 媒体上下文
                    "table_context_size": table_context_size,
                    "image_context_size": image_context_size,
                }
                image_config = {
                    "parse_method": parse_method,  # ✅ 直接传递，支持任何 VLM 模型名
                    "llm_name": config.get("image_llm_name"),
                    "lang": config.get("lang", "Chinese"),
                    "system_prompt": config.get("image_system_prompt")
                }
                # Excel 支持的 parse_method: deepdoc, tcadp parser
                excel_parse_method = "deepdoc"
                if parse_method.lower() == "tcadp parser":
                    excel_parse_method = "tcadp parser"
                excel_config = {
                    "parse_method": excel_parse_method,
                    "output_format": output_format if output_format in ["html", "json", "markdown"] else "html",
                    "table_result_type": tcadp_table_result_type,
                    "markdown_image_response_type": tcadp_markdown_image_response_type,
                    # 媒体上下文
                    "table_context_size": table_context_size,
                    "image_context_size": image_context_size,
                }
                word_config = {
                    "output_format": output_format,
                    "table_context_size": table_context_size,
                    "image_context_size": image_context_size,
                }
                email_config = {"output_format": output_format, "fields": config.get("email_fields")}
                video_llm_name = config.get("video_llm_name")
                if not video_llm_name:
                    candidate = config.get("parse_method")
                    if candidate and candidate not in ["deepdoc", "plain_text", "mineru", "auto", "ocr", "vlm", "tcadp parser"]:
                        video_llm_name = candidate
                video_config = {"llm_id": video_llm_name}
                # PPT/Slides 支持的 parse_method: deepdoc, tcadp parser
                slides_parse_method = "deepdoc"
                if parse_method.lower() == "tcadp parser":
                    slides_parse_method = "tcadp parser"
                slides_config = {
                    "parse_method": slides_parse_method,
                    "output_format": "json",  # PPT 目前只支持 json
                    "table_result_type": tcadp_table_result_type,
                    "markdown_image_response_type": tcadp_markdown_image_response_type,
                    # 媒体上下文
                    "table_context_size": table_context_size,
                    "image_context_size": image_context_size,
                }
                markdown_config = {
                    "output_format": output_format if output_format in ["json", "text"] else "json",
                    "table_context_size": table_context_size,
                    "image_context_size": image_context_size,
                }
                logging.info(f"Using simplified parse_method mode: {parse_method}")

            def _parser_callback(prog, msg=""):
                callback(prog=0.15 + prog * 0.05, msg=msg)

            parsed_result = await parse_file(
                filename=filename,
                binary=file_content,
                tenant_id=tenant_id,
                pdf_config=pdf_config,
                excel_config=excel_config,
                word_config=word_config,
                image_config=image_config,
                email_config=email_config,
                slides_config=slides_config,
                markdown_config=markdown_config,
                video_config=video_config,
                callback=_parser_callback
            )

            logging.info(f"Task {task_id}: parsed with flow, format={parsed_result.get('output_format')}")

            # 2. 切分（支持重叠，保留位置，child-parent chunking）
            callback(prog=0.20, msg="智能切分中...")

            splitter_config = config.get("splitter_config") or {}
            chunk_token_size = splitter_config.get("chunk_token_size", 512)
            delimiters = splitter_config.get("delimiters")
            overlapped_percent = splitter_config.get("overlapped_percent", 0.1)  # 默认 10% 重叠
            children_delimiters = splitter_config.get("children_delimiters")  # child-parent chunking

            def _splitter_callback(prog, msg):
                callback(prog=0.20 + prog * 0.05, msg=msg)

            chunked_result = await split_chunks(
                parsed_result=parsed_result,
                chunk_token_size=chunk_token_size,
                delimiters=delimiters,
                overlapped_percent=overlapped_percent,
                children_delimiters=children_delimiters,
                callback=_splitter_callback
            )

            # 3. 转换为 analyze_v2 格式
            chunks = []
            for c in chunked_result:
                chunk_dict = {
                    "content_with_weight": c.get("text", ""),
                    "embeddings": None
                }

                # 保留位置信息（如果有）
                if "positions" in c:
                    chunk_dict["positions"] = c["positions"]

                # 保留图片（如果有）
                if "image" in c and c["image"]:
                    chunk_dict["image"] = c["image"]

                # 保留 mother chunk（child-parent chunking）
                if "mom" in c and c["mom"]:
                    chunk_dict["mom"] = c["mom"]

                chunks.append(chunk_dict)

            logging.info(f"Task {task_id}: flow parsing complete, {len(chunks)} chunks (overlap={overlapped_percent})")

        # 情况2：从 Milvus 获取已有文档
        elif doc_id and kb_id:
            callback(prog=0.1, msg="从向量库获取文档...")
            if vector_size != 768:
                vctr_nm = f"q_{vector_size}_vec"
            else:
                vctr_nm = "vector"

            for d in settings.retriever.chunk_list(
                    doc_id, tenant_id, [kb_id],
                    fields=["content_with_weight", vctr_nm],
                    sort_by_position=True
            ):
                chunks.append({
                    "content_with_weight": d["content_with_weight"],
                    "embeddings": np.array(d[vctr_nm]) if vctr_nm in d else None
                })

        if not chunks:
            callback(prog=-1, msg="未找到文档内容")
            return

        callback(prog=0.2, msg=f"文档已解析，共 {len(chunks)} 个片段")

        # 2. 根据策略处理（参考 PipelineAnalysisService._process_with_strategy）
        strategy = config.get("processing_strategy", "auto")
        hierarchical_config = config.get("hierarchical_config")
        raptor_config = config.get("raptor_config") or {}

        # 2.1 自动选择策略（参考 PipelineAnalysisService._auto_select_strategy）
        if strategy == "auto":
            # 检测是否有层次结构（复用 core/flow/utils 的 hierarchical_merge）
            has_structure = await _detect_hierarchical_structure(chunks, hierarchical_config)
            is_long = len(chunks) > 50

            # ⚠️ 保护机制：如果没有检测到结构，不要使用 hierarchical 策略
            # 避免将所有 chunks 合并成一个超大章节
            if has_structure and is_long:
                strategy = "hybrid"
            elif has_structure:
                strategy = "hierarchical"
            elif len(chunks) > 10:
                strategy = "raptor"
            else:
                strategy = "simple"

            logging.info(f"Auto-selected strategy: {strategy} (has_structure={has_structure}, chunks={len(chunks)})")

        summaries = []
        cluster_count = 0
        cluster_results = None
        components_used = ["Parser"]
        structure_info = None

        # 2.2 根据策略处理
        if strategy == "simple":
            # 直接使用原始 chunks
            summaries = [c["content_with_weight"] for c in chunks]
            callback(prog=0.5, msg=f"使用简单策略，共 {len(summaries)} 个片段")

        elif strategy == "hierarchical":
            # 使用 HierarchicalMerger（参考 core/flow/hierarchical_merger）
            components_used.append("HierarchicalMerger")
            callback(prog=0.3, msg="层次化合并处理...")

            # ✨ 传递 tenant_id 和 db 以支持 VLM 图片理解
            hierarchical_result = await _hierarchical_merge(chunks, hierarchical_config, tenant_id=tenant_id, db=db)
            summaries = hierarchical_result.get("summaries", [])
            structure_info = hierarchical_result.get("structure")

            # ⚠️ 保护机制：检查是否真的识别出了结构
            if len(summaries) == 1 and len(chunks) > 10:
                logging.warning(
                    f"HierarchicalMerger only identified 1 chapter from {len(chunks)} chunks. "
                    f"This may indicate no matching title structure was found. "
                    f"Consider using 'auto' strategy or adjusting hierarchical_config."
                )

            callback(prog=0.7, msg=f"层次化合并完成，{len(summaries)} 个章节")

        elif strategy == "hybrid":
            # 混合：先 HierarchicalMerger，再 RAPTOR
            components_used.extend(["HierarchicalMerger", "RAPTOR"])
            callback(prog=0.3, msg="混合处理：层次化 + RAPTOR...")

            # 先层次化
            # ✨ 传递 tenant_id 和 db 以支持 VLM 图片理解
            hierarchical_result = await _hierarchical_merge(chunks, hierarchical_config, tenant_id=tenant_id, db=db)
            chapters = hierarchical_result.get("chapters", [])
            structure_info = hierarchical_result.get("structure")

            # ⚠️ 保护机制：检查是否真的识别出了结构
            if len(chapters) == 1 and len(chunks) > 10:
                logging.warning(
                    f"HierarchicalMerger only identified 1 chapter from {len(chunks)} chunks in hybrid strategy. "
                    f"Falling back to RAPTOR-only processing to avoid creating a single huge chapter."
                )
                # 降级为纯 RAPTOR 策略：构造一个完整的 chapter 对象
                chapters = [{
                    "chunks": chunks,
                    "title": "Full Document",
                    "text": "\n".join([c.get("content_with_weight", "") for c in chunks]),
                    "chunk_indices": list(range(len(chunks))),
                    "positions": [],
                    "image": None,
                    "page_range": None
                }]

            # 对每个章节独立运行 RAPTOR
            all_summaries = []
            for idx, chapter in enumerate(chapters):
                chapter_chunks = chapter["chunks"]
                chapter_title = chapter.get("title", "")[:30]  # 前30个字符

                logging.info(
                    f"Hybrid: processing chapter {idx + 1}/{len(chapters)}, title='{chapter_title}', chunks={len(chapter_chunks)}")

                if len(chapter_chunks) > 10:
                    # 章节较长，使用 RAPTOR
                    chapter_raptor_inputs = []
                    # ✨ 新增：保存元数据（位置和图片）以便 RAPTOR 后关联
                    chunk_metadata = []

                    for chunk in chapter_chunks:
                        text = chunk.get("content_with_weight", "")
                        embd_result, _ = await asyncio.to_thread(embd_mdl.encode, [text])
                        embd = embd_result[0] if len(embd_result) > 0 else np.array([])
                        if len(embd) > 0:
                            chapter_raptor_inputs.append((text, embd))
                            # ✨ 保存元数据
                            positions = chunk.get("positions", [])
                            chunk_metadata.append({
                                "positions": positions,
                                "image": chunk.get("image"),
                                "page_nums": sorted(set(p[0] for p in positions)) if positions else []
                            })

                    # 禁用 usage tracking
                    original_db_chat = chat_mdl.db
                    original_db_embd = embd_mdl.db
                    chat_mdl.db = None
                    embd_mdl.db = None

                    try:
                        # 修复：避免括号格式导致的SyntaxWarning
                        raptor_prompt = raptor_config.get("prompt") or "Please summarize the following content:\n{cluster_content}"
                        raptor = Raptor(
                            max_cluster=raptor_config.get("max_cluster", 64),
                            llm_model=chat_mdl,
                            embd_model=embd_mdl,
                            prompt=raptor_prompt,
                            max_token=raptor_config.get("max_token", 512),
                            threshold=raptor_config.get("threshold", 0.1),
                            max_errors=int(os.environ.get("RAPTOR_MAX_ERRORS", 3)),
                        )
                        chapter_results = await raptor(
                            chapter_raptor_inputs,
                            random_state=raptor_config.get("random_seed", 42),
                            callback=lambda msg: callback(prog=0.5, msg=f"RAPTOR ({chapter['title'][:20]}): {msg}")
                        )

                        # ✨ 提取聚类摘要（RAPTOR 生成的新节点）
                        original_len = len(chapter_raptor_inputs)
                        if len(chapter_results) > original_len:
                            # RAPTOR 生成的摘要节点（索引 >= original_len）
                            for i in range(original_len, len(chapter_results)):
                                summary_text = chapter_results[i][0]

                                # ✨ 聚类摘要继承成员的位置信息
                                # RAPTOR 返回格式: [(text, embd, parent_ids), ...]
                                # 找到这个摘要节点的所有子节点
                                child_indices = []
                                if len(chapter_results[i]) > 2:
                                    # 有 parent_ids 信息
                                    parent_ids = chapter_results[i][2] if isinstance(chapter_results[i], tuple) and len(chapter_results[i]) > 2 else []
                                    child_indices = [j for j in parent_ids if j < len(chunk_metadata)]

                                # 合并子节点的位置
                                merged_positions = []
                                merged_image = None
                                for child_idx in child_indices:
                                    if child_idx < len(chunk_metadata):
                                        meta = chunk_metadata[child_idx]
                                        if meta["positions"]:
                                            merged_positions.extend(meta["positions"])
                                        if meta["image"]:
                                            merged_image = concat_img(merged_image, meta["image"])

                                all_summaries.append({
                                    "text": summary_text,
                                    "positions": merged_positions,
                                    "image": merged_image,
                                    "is_cluster_summary": True  # 标记为聚类摘要
                                })
                    finally:
                        chat_mdl.db = original_db_chat
                        embd_mdl.db = original_db_embd
                else:
                    # ✨ 章节较短，直接使用（保留位置信息）
                    for c in chapter_chunks:
                        all_summaries.append({
                            "text": c.get("content_with_weight", ""),
                            "positions": c.get("positions", []),
                            "image": c.get("image")
                        })

            summaries = all_summaries
            cluster_count = len(summaries)
            callback(prog=0.7, msg=f"混合处理完成，{len(chapters)} 个章节，{cluster_count} 个摘要")

        elif strategy == "raptor":
            # 使用 RAPTOR
            components_used.append("RAPTOR")
            callback(prog=0.3, msg="RAPTOR 聚类处理...")

            # ⚠️ 降级保护：chunk 太少（< 3）时，RAPTOR 无法有效聚类
            if len(chunks) < 3:
                logging.info(f"RAPTOR: chunk count={len(chunks)} < 3, using simple strategy instead")
                summaries = [c["content_with_weight"] for c in chunks]
                cluster_count = 0
                cluster_results = None
                callback(prog=0.7, msg=f"文档过短（{len(chunks)} chunks），使用简单策略")
            else:
                # 正常 RAPTOR 流程
                raptor_inputs = []
                for chunk in chunks:
                    text = chunk.get("content_with_weight", "")
                    embd = chunk.get("embeddings")

                    if embd is None or (isinstance(embd, np.ndarray) and embd.size == 0):
                        embd_result, _ = await asyncio.to_thread(embd_mdl.encode, [text])
                        embd = embd_result[0] if len(embd_result) > 0 else np.array([])

                    if len(embd) > 0:
                        raptor_inputs.append((text, embd))

                # ⚠️ 关键：禁用 usage tracking（避免 asyncio 并行任务冲突）
                # 参考 DocumentAnalysisService._analyze_with_raptor 第 611-613 行
                original_db_chat = chat_mdl.db
                original_db_embd = embd_mdl.db
                chat_mdl.db = None
                embd_mdl.db = None

                try:
                    # 修复：避免括号格式导致的SyntaxWarning
                    raptor_prompt = raptor_config.get("prompt") or "Please summarize the following content:\n{cluster_content}"
                    raptor = Raptor(
                        max_cluster=raptor_config.get("max_cluster", 64),
                        llm_model=chat_mdl,
                        embd_model=embd_mdl,
                        prompt=raptor_prompt,
                        max_token=raptor_config.get("max_token", 512),
                        threshold=raptor_config.get("threshold", 0.1),
                        max_errors=int(os.environ.get("RAPTOR_MAX_ERRORS", 3)),
                    )

                    cluster_results = await raptor(
                        raptor_inputs,
                        random_state=raptor_config.get("random_seed", 42),
                        callback=lambda msg: callback(prog=0.5, msg=f"RAPTOR: {msg}")
                    )
                finally:
                    # 恢复 db session
                    chat_mdl.db = original_db_chat
                    embd_mdl.db = original_db_embd

                original_length = len(raptor_inputs)
                if len(cluster_results) > original_length:
                    # 有聚类摘要
                    summaries = [cluster_results[i][0] for i in range(original_length, len(cluster_results))]
                    cluster_count = len(summaries)
                else:
                    # 没有聚类摘要（chunk 太少），使用原始 chunks
                    summaries = [text for text, _ in cluster_results] if cluster_results else [c["content_with_weight"]
                                                                                               for c in chunks]
                    cluster_count = 0

                callback(prog=0.7, msg=f"RAPTOR 生成了 {cluster_count} 个聚类摘要，共 {len(summaries)} 个片段")
        else:
            summaries = [c["content_with_weight"] for c in chunks]
            callback(prog=0.5, msg=f"使用简单策略，共 {len(summaries)} 个片段")

        # 3. 提取元数据（参考 keyword_extraction 和 DocumentAnalysisService 的标准模式）
        callback(prog=0.75, msg="提取文档元数据...")

        metadata = {}
        metadata_fields = config.get("metadata_fields")

        # 如果没有配置，使用默认（摘要 + 标签）
        if not metadata_fields:
            metadata_fields = _get_default_metadata_fields()

        # 保存 cluster_results 用于数据源选择
        cluster_results_for_source = cluster_results if cluster_count > 0 else None
        original_chunk_length = len(chunks)

        for field_config in metadata_fields:
            field_name = field_config.get("field_name")
            source = field_config.get("source", "global_summary")
            call_mode = field_config.get("call_mode", "single")
            post_process = field_config.get("post_process", "none")

            try:
                # 1. 获取数据源
                contents = _get_extraction_contents(
                    source,
                    summaries,
                    chunks,
                    cluster_results_for_source,
                    original_chunk_length
                )

                # 2. 调用 LLM（参考 keyword_extraction 的标准模式）
                from core.prompts.generator import message_fit_in

                if call_mode == "single":
                    # 合并后一次调用
                    combined_content = "\n\n".join(contents) if isinstance(contents, list) else contents[0]

                    # 使用 message_fit_in（项目标准）
                    msg = [
                        {"role": "system", "content": field_config.get("prompt")},
                        {"role": "user", "content": combined_content}
                    ]
                    _, msg = message_fit_in(msg, chat_mdl.max_length)

                    def do_chat_single():
                        return chat_mdl.chat(msg[0]["content"], msg[1:], {
                            "temperature": field_config.get("temperature", 0.1),
                            "max_tokens": field_config.get("max_tokens", 512)
                        })

                    raw_output = await asyncio.to_thread(do_chat_single)

                    # 清理输出（参考 keyword_extraction）
                    raw_output = re.sub(r"^.*</think>", "", raw_output, flags=re.DOTALL).strip()
                    if raw_output.find("**ERROR**") >= 0:
                        raw_output = ""

                elif call_mode == "batch":
                    # 每个内容调用一次（需要频次统计的场景）
                    raw_outputs = []
                    for content in contents:
                        msg = [
                            {"role": "system", "content": field_config.get("prompt")},
                            {"role": "user", "content": content}
                        ]
                        _, msg = message_fit_in(msg, chat_mdl.max_length)

                        def do_chat_batch(m):
                            return chat_mdl.chat(m[0]["content"], m[1:], {
                                "temperature": field_config.get("temperature", 0.1),
                                "max_tokens": field_config.get("max_tokens", 512)
                            })

                        result = await asyncio.to_thread(do_chat_batch, msg)

                        result = re.sub(r"^.*</think>", "", result, flags=re.DOTALL).strip()
                        if result.find("**ERROR**") < 0:
                            raw_outputs.append(result)

                    raw_output = raw_outputs

                # 3. 后处理
                metadata[field_name] = _post_process_result(raw_output, post_process)

                logging.info(f"Extracted {field_name}: {metadata[field_name]}")

            except Exception as e:
                logging.exception(f"Failed to extract {field_name}: {e}")
                metadata[field_name] = None

        # 4. 构建结果（与 PipelineAnalysisService 保持一致）
        result = {
            "metadata": metadata,
            "processing_info": {
                "strategy_used": strategy,
                "chunk_count": len(chunks),
                "dedup_strategy": config.get("dedup_strategy", "smart"),
                "components_used": components_used
            }
        }

        # 添加结构信息（如果使用了 HierarchicalMerger）
        if structure_info:
            result["structure"] = structure_info

        # 添加聚类信息（如果使用了 RAPTOR）
        if cluster_count > 0:
            result["processing_info"]["cluster_count"] = cluster_count

        # 5. 保存结果
        callback(prog=0.95, msg="保存分析结果...")

        task_data["result"] = result

        from api.db.db_models import db_connection, Task as TaskModel
        with db_connection() as new_db:
            task_record = new_db.query(TaskModel).filter(TaskModel.id == task_id).first()
            if task_record:
                task_record.chunk_ids = json.dumps(task_data, ensure_ascii=False)
                new_db.commit()
                logging.info(f"Task {task_id}: saved result to database")

        callback(prog=1.0, msg="分析完成")

        logging.info(
            f"analyze_v2 task {task_id} completed: {len(chunks)} chunks, {len(summaries)} summaries, {len(metadata)} metadata fields")

        return result

    except Exception as e:
        logging.exception(f"analyze_v2 task {task_id} failed: {e}")
        if callback:
            callback(prog=-1, msg=f"任务失败: {str(e)}")
        raise


async def delete_image(kb_id, chunk_id):
    try:
        async with minio_limiter:
            settings.STORAGE_IMPL.delete(kb_id, chunk_id)
    except Exception:
        logging.exception(f"Deleting image of chunk {chunk_id} got exception")
        raise


async def insert_milvus(db, task_id, task_tenant_id, task_dataset_id, chunks, progress_callback, collection_name,
                        schema):
    """
    将chunks批量插入向量数据库（支持 Milvus/ES/OpenSearch/Infinity），包含类型转换和错误处理

    Args:
        db: 数据库Session
        task_id: 任务ID
        task_tenant_id: 租户ID
        task_dataset_id: 数据集ID (ES 中作为 knowledgebaseId)
        chunks: 要插入的chunk列表
        progress_callback: 进度回调函数
        collection_name: 集合/索引名称
        schema: 集合 schema，用于数据类型转换 (ES 返回空 schema)

    Returns:
        成功返回True，失败返回False
    """
    # 处理 mother chunks (用于 child-parent chunking)
    mothers = []
    mother_ids = set()
    for ck in chunks:
        mom = ck.get("mom") or ck.get("mom_with_weight") or ""
        if not mom:
            continue
        mom_id = xxhash.xxh64(mom.encode("utf-8")).hexdigest()
        ck["mom_id"] = mom_id
        if mom_id in mother_ids:
            continue
        mother_ids.add(mom_id)
        mom_ck = copy.deepcopy(ck)
        mom_ck["pk"] = mom_id  # 同时设置 pk 字段，确保 Milvus 主键正确
        mom_ck["id"] = mom_id
        mom_ck["content_with_weight"] = mom
        mom_ck["available_int"] = 0
        flds = list(mom_ck.keys())
        for fld in flds:
            # pk 是 Milvus 主键，必须保留
            if fld not in ["id", "pk", "content_with_weight", "doc_id", "docnm_kwd", "kb_id", "available_int", "position_int"]:
                del mom_ck[fld]
        mothers.append(mom_ck)

    # 先插入 mother chunks
    for b in range(0, len(mothers), settings.DOC_BULK_SIZE):
        mother_batch = mothers[b:b + settings.DOC_BULK_SIZE]
        converted_batch = [convert_data_types(m, schema) for m in mother_batch]
        try:
            db_type = settings.docStoreConn.dbType()
            if db_type == "milvus":
                await asyncio.to_thread(settings.docStoreConn.insert, rows=converted_batch, indexName=collection_name)
            else:
                es_batch = []
                for doc in converted_batch:
                    es_doc = doc.copy()
                    if "id" not in es_doc and "pk" in es_doc:
                        es_doc["id"] = es_doc["pk"]
                    es_batch.append(es_doc)
                await asyncio.to_thread(settings.docStoreConn.insert, documents=es_batch, indexName=collection_name, knowledgebaseId=task_dataset_id,)
        except Exception as e:
            logging.warning(f"Insert mother chunks error: {e}")

        task_canceled = has_canceled(task_id)
        if task_canceled:
            progress_callback(-1, msg="Task has been canceled.")
            return False

    # 用于记录成功和失败的插入信息
    successful_inserts = []
    failed_inserts = []

    # 循环分批插入
    for b in range(0, len(chunks), settings.DOC_BULK_SIZE):
        # 取出本批次要插入的chunks
        chunk_batch = chunks[b: b + settings.DOC_BULK_SIZE]

        # 将本批次内的数据先做类型转换
        converted_batch = []
        for chunk in chunk_batch:
            converted_chunk = convert_data_types(chunk, schema)
            converted_batch.append(converted_chunk)

        try:
            # 根据数据库类型调用不同的insert方法
            db_type = settings.docStoreConn.dbType()
            if db_type == "milvus":
                # Milvus 使用 collection_name 和 data 参数
                # insert 返回 list[str]：空列表表示成功，非空列表包含错误信息
                doc_store_errors = await asyncio.to_thread(settings.docStoreConn.insert, rows=converted_batch, indexName=collection_name)
                # 检查是否有错误（非空列表表示有错误）
                if doc_store_errors:
                    error_message = f"Insert failed: {doc_store_errors}"
                    progress_callback(-1, msg=error_message)
                    raise Exception(error_message)
                # 记录成功插入
                successful_inserts.append({"insert_count": len(converted_batch)})
            else:
                # ES/OpenSearch/Infinity 使用 documents, indexName, knowledgebaseId 参数
                # ES 要求文档有 "id" 字段，Milvus 使用 "pk"，需要做映射
                es_batch = []
                for doc in converted_batch:
                    es_doc = doc.copy()
                    # 如果没有 "id" 字段，使用 "pk" 作为 id
                    if "id" not in es_doc and "pk" in es_doc:
                        es_doc["id"] = es_doc["pk"]
                    es_batch.append(es_doc)
                errors = await asyncio.to_thread(settings.docStoreConn.insert, documents=es_batch, indexName=collection_name, knowledgebaseId=task_dataset_id,)
                if errors:
                    logging.warning(f"Insert errors: {errors}")
                # 记录成功插入
                successful_inserts.append({"insert_count": len(converted_batch)})

        except Exception as e:
            # 如果出现异常，记录失败并进行删除回滚
            failed_inserts.extend(chunk_batch)
            progress_callback(
                -1,
                "Insert chunk error, detail info please check log file. Please check doc store status!"
            )
            try:
                if await asyncio.to_thread(settings.docStoreConn.has_collection, collection_name):
                    # 删除本批次已经尝试插入的记录
                    for chunk in chunk_batch:
                        if "doc_id" in chunk:
                            doc_id = chunk['doc_id']
                            await asyncio.to_thread(delete_chunks_by_doc_id, collection_name, doc_id, task_dataset_id)
            except Exception as e:
                logging.exception(f"Failed to rollback inserted chunks: {e}")
            logging.exception("Insert error:")
            logging.error("Data being inserted: %s", converted_batch)
            return False  # 出错后返回False

        # 检查任务是否被取消
        task_canceled = has_canceled(task_id)
        if task_canceled:
            progress_callback(-1, msg="Task has been canceled.")
            return False

        # 每插入一定批次，做一次进度回调
        if b % 128 == 0:
            progress = 0.8 + 0.1 * (b + 1) / len(chunks)
            progress_callback(prog=progress, msg="")

        # 拼接本批次chunk_ids并更新到TaskService
        chunk_ids = [chunk["pk"] for chunk in chunk_batch]
        chunk_ids_str = " ".join(chunk_ids)
        try:
            TaskService.update_chunk_ids(db, task_id, chunk_ids_str)
        except NoResultFound:
            logging.warning(f"insert_milvus update_chunk_ids failed since task {task_id} is unknown.")
            # 如果TaskService中没有这个task，则删除已插入数据并退出
            try:
                if await asyncio.to_thread(settings.docStoreConn.has_collection, collection_name):
                    for chunk in chunk_batch:
                        if "doc_id" in chunk:
                            doc_id = chunk['doc_id']
                            await asyncio.to_thread(delete_chunks_by_doc_id, collection_name, doc_id, task_dataset_id)
            except Exception as e:
                logging.exception(f"Failed to rollback after task not found: {e}")
            async with asyncio.TaskGroup() as tg:
                for chunk_id in chunk_ids:
                    tg.create_task(delete_image(task_dataset_id, chunk_id))
            progress_callback(-1, msg=f"Chunk updates failed since task {task_id} is unknown.")
            return False

    # 统计并记录插入结果
    if successful_inserts:
        total_insert_count = sum(item.get("insert_count", 0) for item in successful_inserts)
        db_type = settings.docStoreConn.dbType()
        logging.info(
            f"Successfully inserted {total_insert_count} chunks into {db_type} index '{collection_name}'"
        )

    if failed_inserts:
        logging.warning(f"Failed to insert {len(failed_inserts)} chunks")
        logging.warning(f"Failed insert records: {failed_inserts}")

    return True


@timeout(60 * 60 * 3, 1)
async def do_handle_task(db, task):
    # 将 Row 转换为字典，确保可以修改字段
    task = task._asdict() if hasattr(task, "_asdict") else dict(task)

    # 预处理 auth 列，转换为列表，处理 None 值
    def convert_auth(auth_str):
        if auth_str is None:
            return []  # 如果 auth 为 None，转换为空列表
        try:
            return json.loads(auth_str) if isinstance(auth_str, str) else auth_str
        except json.JSONDecodeError:
            logging.exception(f"Failed to decode auth field: {auth_str}")
            return []  # 解析失败时，返回空列表

    task_type = task.get("task_type", "")

    if task_type == "dataflow" and task.get("doc_id", "") == CANVAS_DEBUG_DOC_ID:
        await run_dataflow(db, task)
        return

    # analyze_v2 特殊处理：提前处理，避免访问不存在的字段
    if task_type == "analyze_v2":
        task_id = task["id"]
        task_tenant_id = task["tenant_id"]
        task_embedding_id = task["embd_id"]
        task_language = task["language"]
        task_llm_id = task["llm_id"]

        # 解析配置
        task_data = json.loads(task.get("chunk_ids", "{}"))
        enable_sse = task_data.get("enable_sse", False)

        # 创建进度回调
        progress_callback_sse = partial(
            set_progress,
            db,
            task_id,
            task.get("from_page", 0),
            task.get("to_page", 100000000),
            enable_sse=enable_sse
        )

        try:
            # 绑定 LLM 模型
            chat_model = LLMBundle(db, task_tenant_id, LLMType.CHAT, llm_name=task_llm_id, lang=task_language)
            embedding_model = LLMBundle(db, task_tenant_id, LLMType.EMBEDDING, llm_name=task_embedding_id,
                                        lang=task_language)

            # 获取向量维度
            vts, _ = embedding_model.encode(["test"])
            vector_size = len(vts[0])

            # 执行分析任务
            await run_analyze_v2_task(
                task,
                chat_model,
                embedding_model,
                vector_size,
                db,
                callback=progress_callback_sse
            )
        except Exception as e:
            logging.exception(f"analyze_v2 task {task_id} failed: {e}")
            progress_callback_sse(prog=-1, msg=f"任务失败: {str(e)}")

        return

    # 处理 auth 列
    task['auth'] = convert_auth(task.get('auth'))

    task_id = task["id"]
    task_from_page = task["from_page"]
    task_to_page = task["to_page"]
    task_tenant_id = task["tenant_id"]
    task_embedding_id = task["embd_id"]
    task_language = task["language"]
    task_llm_id = task["llm_id"]
    task_dataset_id = task.get("kb_id")  # analyze_v2 之外的任务必须有 kb_id
    task_doc_id = task["doc_id"]
    task_document_name = task.get("name", "unknown")
    task_parser_config = task.get("parser_config", {})
    task_start_ts = timer()
    toc_thread = None
    executor = concurrent.futures.ThreadPoolExecutor()

    # prepare the progress callback function
    progress_callback = partial(set_progress, db, task_id, task_from_page, task_to_page)

    # FIXME: workaround, Infinity doesn't support table parsing method, this check is to notify user
    lower_case_doc_engine = settings.DOC_ENGINE.lower()
    if lower_case_doc_engine == 'infinity' and task['parser_id'].lower() == 'table':
        error_message = "Table parsing method is not supported by Infinity, please use other parsing methods or use Elasticsearch as the document engine."
        progress_callback(-1, msg=error_message)
        raise Exception(error_message)

    task_canceled = has_canceled(task_id)
    if task_canceled:
        progress_callback(-1, msg="Task has been canceled.")
        return None

    try:
        # bind embedding model
        embedding_model = LLMBundle(db, task_tenant_id, LLMType.EMBEDDING, llm_name=task_embedding_id,
                                    lang=task_language)
        vts, _ = embedding_model.encode(["ok"])
        vector_size = len(vts[0])
    except Exception as e:
        error_message = f'Fail to bind embedding model: {str(e)}'
        progress_callback(-1, msg=error_message)
        logging.exception(error_message)
        raise

    # init_kb(task, vector_size)
    kb_name = KnowledgebaseService.get_by_id(db, task_dataset_id).name
    await init_kb(task, kb_name)

    if task_type[:len("dataflow")] == "dataflow":
        await run_dataflow(db, task)
        return

    if task_type == "raptor":
        kb = KnowledgebaseService.get_by_id(db, task_dataset_id)
        if not kb:
            progress_callback(prog=-1.0, msg="Cannot found valid knowledgebase for RAPTOR task")
            return

        kb_parser_config = kb.parser_config
        if not kb_parser_config.get("raptor", {}).get("use_raptor", False):
            kb_parser_config.update(
                {
                    "raptor": {
                        "use_raptor": True,
                        "prompt": "Please summarize the following paragraphs. Be careful with the numbers, do not make things up. Paragraphs as following:\n      {cluster_content}\nThe above is the content you need to summarize.",
                        "max_token": 256,
                        "threshold": 0.1,
                        "max_cluster": 64,
                        "random_seed": 0,
                        "scope": "file"
                    },
                }
            )
            if not KnowledgebaseService.update_by_id(kb.id, {"parser_config": kb_parser_config}):
                progress_callback(prog=-1.0, msg="Internal error: Invalid RAPTOR configuration")
                return

        # Check if Raptor should be skipped for structured data
        file_type = task.get("type", "")
        parser_id = task.get("parser_id", "")
        raptor_config = kb_parser_config.get("raptor", {})

        if should_skip_raptor(file_type, parser_id, task_parser_config, raptor_config):
            skip_reason = get_skip_reason(file_type, parser_id, task_parser_config)
            logging.info(f"Skipping Raptor for document {task_document_name}: {skip_reason}")
            progress_callback(prog=1.0, msg=f"Raptor skipped: {skip_reason}")
            return

        # bind LLM for raptor
        chat_model = LLMBundle(db, task_tenant_id, LLMType.CHAT, llm_name=task_llm_id, lang=task_language)
        # run RAPTOR
        async with kg_limiter:
            chunks, token_count = await run_raptor_for_kb(
                row=task,
                kb_parser_config=kb_parser_config,
                chat_mdl=chat_model,
                embd_mdl=embedding_model,
                vector_size=vector_size,
                callback=progress_callback,
                doc_ids=task.get("doc_ids", []),
            )
        if fake_doc_ids := task.get("doc_ids", []):
            task_doc_id = fake_doc_ids[0] # use the first document ID to represent this task for logging purposes
    # Either using graphrag or Standard chunking methods
    elif task_type == "graphrag":
        kb = KnowledgebaseService.get_by_id(db, task_dataset_id)
        if not kb:
            progress_callback(prog=-1.0, msg="Cannot found valid knowledgebase for GraphRAG task")
            return

        kb_parser_config = kb.parser_config
        if not kb_parser_config.get("graphrag", {}).get("use_graphrag", False):
            progress_callback(prog=-1.0, msg="Internal error: Invalid GraphRAG configuration")
            return

        graphrag_conf = kb_parser_config.get("graphrag", {})
        start_ts = timer()
        chat_model = LLMBundle(db, task_tenant_id, LLMType.CHAT, llm_name=task_llm_id, lang=task_language)
        with_resolution = graphrag_conf.get("resolution", False)
        with_community = graphrag_conf.get("community", False)
        async with kg_limiter:
            # await run_graphrag(task, task_language, with_resolution, with_community, chat_model, embedding_model, progress_callback)
            result = await run_graphrag_for_kb(
                row=task,
                doc_ids=task.get("doc_ids", []),
                language=task_language,
                kb_parser_config=kb_parser_config,
                chat_model=chat_model,
                embedding_model=embedding_model,
                callback=progress_callback,
                with_resolution=with_resolution,
                with_community=with_community,
            )
            logging.info(f"GraphRAG task result for task {task}:\n{result}")
        progress_callback(prog=1.0, msg="Knowledge Graph done ({:.2f}s)".format(timer() - start_ts))
        return
    elif task_type == "mindmap":
        progress_callback(1, "place holder")
        pass
        return
    else:
        # Standard chunking methods
        start_ts = timer()
        chunks = await build_chunks(task, progress_callback, db)
        logging.info("Build document {}: {:.2f}s".format(task_document_name, timer() - start_ts))
        if not chunks:
            progress_callback(1., msg=f"No chunk built from {task_document_name}")
            return
        progress_callback(msg="Generate {} chunks".format(len(chunks)))
        start_ts = timer()
        try:
            token_count = await embedding(chunks, embedding_model, task_parser_config, progress_callback)
        except Exception as e:
            error_message = "Generate embedding error:{}".format(str(e))
            progress_callback(-1, error_message)
            logging.exception(error_message)
            token_count = 0
            raise
        progress_message = "Embedding chunks ({:.2f}s)".format(timer() - start_ts)
        logging.info(progress_message)
        progress_callback(msg=progress_message)
        if task["parser_id"].lower() == "naive" and task["parser_config"].get("toc_extraction", False):
            toc_thread = executor.submit(build_TOC, task, chunks, progress_callback)

    chunk_count = len(set([chunk["pk"] for chunk in chunks]))
    # 记录开始时间
    start_ts = timer()

    # 获取集合 schema，用于做数据类型转换
    schema = await get_schema(search.index_name_one(task_tenant_id, kb_name))
    collection_name = search.index_name_one(task_tenant_id, kb_name)
    e = await insert_milvus(db, task_id, task_tenant_id, task_dataset_id, chunks, progress_callback, collection_name,
                            schema)
    if not e:
        return
    # async def delete_image(kb_id, chunk_id):
    #     try:
    #         async with minio_limiter:
    #             settings.STORAGE_IMPL.delete(kb_id, chunk_id)
    #     except Exception:
    #         logging.exception("Deleting image of chunk {}/{}/{} got exception".format(task["location"], task["name"], chunk_id))
    #         raise
    #
    # # 循环分批插入
    # for b in range(0, chunk_count, settings.DOC_BULK_SIZE):
    #     # 取出本批次要插入的 chunks
    #     chunk_batch = chunks[b: b + settings.DOC_BULK_SIZE]
    #
    #     # 将本批次内的数据先做类型转换
    #     converted_batch = []
    #     for chunk in chunk_batch:
    #         # 选项1：对小数据保持同步
    #         converted_chunk = convert_data_types(chunk, schema)
    #         # 选项2：对大数据使用异步
    #         # converted_chunk = await convert_data_types_async(chunk, schema)
    #         converted_batch.append(converted_chunk)
    #
    #     doc_store_result = {}
    #     try:
    #         # 调用自定义的 settings.docStoreConn.insert 方法
    #         doc_store_result = await trio.to_thread.run_sync(lambda: settings.docStoreConn.insert(
    #             collection_name=collection_name,
    #             data=converted_batch
    #         ))
    #
    #         # 可选：检查 insert_count 是否与本批次长度一致
    #         # 如果你的需求是一定要完全插入成功才算成功，可以加如下校验：
    #         if doc_store_result.get("insert_count", 0) != len(converted_batch):
    #             error_message = (
    #                 f"Insert count mismatch: expected {len(converted_batch)}, "
    #                 f"got {doc_store_result.get('insert_count', 0)}."
    #             )
    #             progress_callback(-1, msg=error_message)
    #             raise Exception(error_message)
    #
    #     except Exception:
    #         # 如果出现异常，记录失败并进行删除回滚
    #         failed_inserts.extend(chunk_batch)
    #         progress_callback(
    #             -1,
    #             "Insert chunk error, detail info please check log file. Please also check Milvus status!"
    #         )
    #         try:
    #             if settings.docStoreConn.has_collection(collection_name):
    #                 # 删除本批次已经尝试插入的记录（这里按 doc_id 删除，可根据业务实际情况调整 filter 条件）
    #                 for chunk in chunk_batch:
    #                     if "doc_id" in chunk:
    #                         settings.docStoreConn.delete(
    #                             collection_name=collection_name,
    #                             filter=f"doc_id == '{chunk['doc_id']}'"
    #                         )
    #         except MilvusException as e:
    #             return e  # 可根据需要改成 raise 或其它处理
    #         logging.exception("Insert error:")
    #         logging.error("Data being inserted: %s", converted_batch)
    #         return  # 出错后直接退出
    #
    #     # 若执行到此，说明插入成功，记录插入结果
    #     successful_inserts.append(doc_store_result)
    #
    #     task_canceled = has_canceled(task_id)
    #     if task_canceled:
    #         progress_callback(-1, msg="Task has been canceled.")
    #         return
    #
    #     # 每插入 128 批，做一次进度回调（可自定义触发频率）
    #     if b % 128 == 0:
    #         progress = 0.8 + 0.1 * (b + 1) / chunk_count
    #         progress_callback(prog=progress, msg="")
    #
    #     # 拼接本批次 chunk_ids 并更新到 TaskService
    #     # （需要你确保 chunk 内有 "id" 这个字段）
    #     chunk_ids = [chunk["pk"] for chunk in chunk_batch]
    #     chunk_ids_str = " ".join(chunk_ids)
    #     try:
    #         TaskService.update_chunk_ids(db, task["id"], chunk_ids_str)
    #     except NoResultFound:
    #         logging.warning(f"do_handle_task update_chunk_ids failed since task {task['id']} is unknown.")
    #         # 如果 TaskService 中没有这个 task，则删除已插入数据并退出
    #         try:
    #             if settings.docStoreConn.has_collection(collection_name):
    #                 for chunk in chunk_batch:
    #                     if "doc_id" in chunk:
    #                         await trio.to_thread.run_sync(lambda: settings.docStoreConn.delete(
    #                             collection_name=collection_name,
    #                             filter=f"doc_id == '{chunk['doc_id']}'"
    #                         ))
    #         except MilvusException as e:
    #             return e
    #         async with trio.open_nursery() as nursery:
    #             for chunk_id in chunk_ids:
    #                 nursery.start_soon(delete_image, task_dataset_id, chunk_id)
    #         progress_callback(-1, msg=f"Chunk updates failed since task {task['id']} is unknown.")
    #         return

    logging.info("Indexing doc({}), page({}-{}), chunks({}), elapsed: {:.2f}".format(task_document_name, task_from_page,
                                                                                     task_to_page, len(chunks),
                                                                                     timer() - start_ts))

    # 如果任务被取消，则清理已插入的数据并返回
    if TaskService.do_cancel(db, task_id):
        try:
            if await asyncio.to_thread(settings.docStoreConn.has_collection, collection_name):
                await asyncio.to_thread(delete_chunks_by_doc_id, collection_name, task_doc_id, task_dataset_id)
        except Exception as e:
            return e
        return

    # 最后更新统计信息
    DocumentService.increment_chunk_num(db, task_doc_id, task_dataset_id, token_count, chunk_count, 0)

    # 做一次进度回调
    time_cost = timer() - start_ts
    progress_callback(msg="Indexing done ({:.2f}s).".format(time_cost))
    if toc_thread:
        d = toc_thread.result()
        if d:
            e = await insert_milvus(db, task_id, task_tenant_id, task_dataset_id, [d], progress_callback,
                                    collection_name, schema)
            if not e:
                return
            DocumentService.increment_chunk_num(db, task_doc_id, task_dataset_id, 0, 1, 0)

    task_time_cost = timer() - task_start_ts
    progress_callback(prog=1.0, msg="Indexing done ({:.2f}s). Task done ({:.2f}s)".format(time_cost, task_time_cost))
    logging.info(
        "Chunk doc({}), page({}-{}), chunks({}), token({}), elapsed:{:.2f}".format(task_document_name, task_from_page,
                                                                                   task_to_page, len(chunks),
                                                                                   token_count, task_time_cost))


async def handle_task():

    global DONE_TASKS, FAILED_TASKS
    with db_connection() as db:
        task_dict = None  # 确保变量初始化
        try:
            redis_msg, task = await collect(db)
            if not task:
                await asyncio.sleep(5)
                return

            task_type = task["task_type"]
            pipeline_task_type = TASK_TYPE_TO_PIPELINE_TASK_TYPE.get(task_type,
                                                                     PipelineTaskType.PARSE) or PipelineTaskType.PARSE

            try:
                # 转换为可序列化的字典
                if hasattr(task, "_asdict"):  # 检查是否为 RowProxy
                    task_dict = task._asdict()
                elif isinstance(task, dict):  # 如果已经是字典
                    task_dict = task
                else:
                    task_dict = {key: str(value) for key, value in vars(task).items()}  # 通用对象转换为字典

                # 清理 base64（避免日志膨胀）
                if "chunk_ids" in task_dict:
                    try:
                        chunk_ids_data = json.loads(task_dict["chunk_ids"])
                        if "file_content_base64" in chunk_ids_data and chunk_ids_data["file_content_base64"]:
                            if not chunk_ids_data["file_content_base64"].startswith("<removed"):
                                file_size = chunk_ids_data.get("file_size", len(chunk_ids_data["file_content_base64"]))
                                chunk_ids_data["file_content_base64"] = f"<removed, {file_size} bytes>"
                                task_dict["chunk_ids"] = json.dumps(chunk_ids_data, ensure_ascii=False)
                    except:
                        pass

                logging.info(f"handle_task begin for task {json.dumps(task_dict)}")

                # 保存到 CURRENT_TASKS 前清理 base64（避免心跳日志膨胀）
                task_for_tracking = copy.deepcopy(task)
                if "chunk_ids" in task_for_tracking:
                    try:
                        chunk_ids_data = json.loads(task_for_tracking["chunk_ids"])
                        if "file_content_base64" in chunk_ids_data:
                            file_size = chunk_ids_data.get("file_size", 0)
                            chunk_ids_data["file_content_base64"] = f"<removed, {file_size} bytes>"
                            task_for_tracking["chunk_ids"] = json.dumps(chunk_ids_data, ensure_ascii=False)
                    except:
                        pass

                CURRENT_TASKS[task["id"]] = task_for_tracking
                await do_handle_task(db, task)
                DONE_TASKS += 1
                CURRENT_TASKS.pop(task["id"], None)

                # "handle_task done" 日志也清理 base64
                task_done_dict = copy.deepcopy(task_dict)  # 复用之前清理过的 task_dict
                logging.info(f"handle_task done for task {json.dumps(task_done_dict)}")
            except Exception as e:
                FAILED_TASKS += 1
                CURRENT_TASKS.pop(task["id"], None)
                try:
                    err_msg = str(e)
                    while isinstance(e, exceptiongroup.ExceptionGroup):
                        e = e.exceptions[0]
                        err_msg += ' -- ' + str(e)
                    set_progress(db, task["id"], prog=-1, msg=f"[Exception]: {err_msg}")
                except Exception:
                    pass

                # 异常日志也清理 base64
                task_error_dict = copy.deepcopy(task_dict) if 'task_dict' in locals() else {}
                logging.exception(f"handle_task got exception for task {json.dumps(task_error_dict)}")

            finally:
                # analyze_v2 任务不记录 pipeline 操作日志（临时 doc_id，无对应 Document 记录）
                if task_type != "analyze_v2":
                    task_document_ids = []
                    if task_type in PIPELINE_SPECIAL_PROGRESS_FREEZE_TASK_TYPES:
                        task_document_ids = task["doc_ids"]
                    if not task.get("dataflow_id", ""):
                        PipelineOperationLogService.record_pipeline_operation(db, document_id=task["doc_id"],
                                                                              pipeline_id="",
                                                                              task_type=pipeline_task_type,
                                                                              fake_document_ids=task_document_ids)

            redis_msg.ack()
        except Exception:
            logging.exception(f"Error in main loop")
            db.rollback()  # 回滚事务
            raise
        else:
            db.commit()  # 提交事务


async def get_server_ip() -> str:
    # get ip by udp
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception as e:
        logging.error(str(e))
        return 'Unknown'


async def report_status():
    global CONSUMER_NAME, BOOT_AT, PENDING_TASKS, LAG_TASKS, DONE_TASKS, FAILED_TASKS
    REDIS_CONN.sadd("TASKEXE", CONSUMER_NAME)
    redis_lock = RedisDistributedLock("clean_task_executor", lock_value=CONSUMER_NAME, timeout=60)
    while True:
        try:
            now = datetime.now()
            group_info = REDIS_CONN.queue_info(settings.get_svr_queue_name(0), SVR_CONSUMER_GROUP_NAME)
            if group_info is not None:
                PENDING_TASKS = int(group_info.get("pending", 0))
                LAG_TASKS = int(group_info.get("lag", 0))

            pid = os.getpid()
            ip_address = await get_server_ip()
            current = copy.deepcopy(CURRENT_TASKS)
            heartbeat = json.dumps({
                "ip_address": ip_address,
                "pid": pid,
                "name": CONSUMER_NAME,
                "now": now.astimezone().isoformat(timespec="milliseconds"),
                "boot_at": BOOT_AT,
                "pending": PENDING_TASKS,
                "lag": LAG_TASKS,
                "done": DONE_TASKS,
                "failed": FAILED_TASKS,
                "current": current,
            })
            REDIS_CONN.zadd(CONSUMER_NAME, heartbeat, now.timestamp())
            logging.info(f"{CONSUMER_NAME} reported heartbeat: {heartbeat}")

            expired = REDIS_CONN.zcount(CONSUMER_NAME, 0, now.timestamp() - 60 * 30)
            if expired > 0:
                REDIS_CONN.zpopmin(CONSUMER_NAME, expired)

            # clean task executor
            if redis_lock.acquire():
                task_executors = REDIS_CONN.smembers("TASKEXE")
                for consumer_name in task_executors:
                    if consumer_name == CONSUMER_NAME:
                        continue
                    expired = REDIS_CONN.zcount(
                        consumer_name, now.timestamp() - WORKER_HEARTBEAT_TIMEOUT, now.timestamp() + 10
                    )
                    if expired == 0:
                        logging.info(f"{consumer_name} expired, removed")
                        REDIS_CONN.srem("TASKEXE", consumer_name)
                        REDIS_CONN.delete(consumer_name)
        except Exception:
            logging.exception("report_status got exception")
        finally:
            redis_lock.release()
        await asyncio.sleep(30)


async def task_manager():
    try:
        await handle_task()
    finally:
        task_limiter.release()


async def main():
    logging.info(r"""
======================================================================
    ____                      __  _
   /  _/___  ____ ____  _____/ /_(_)___  ____     ________  ______   _____  _____
   / // __ \/ __ `/ _ \/ ___/ __/ / __ \/ __ \   / ___/ _ \/ ___/ | / / _ \/ ___/
 _/ // / / / /_/ /  __(__  ) /_/ / /_/ / / / /  (__  )  __/ /   | |/ /  __/ /
/___/_/ /_/\__, /\___/____/\__/_/\____/_/ /_/  /____/\___/_/    |___/\___/_/
          /____/
======================================================================
    """)
    logging.info(f'MultiRAG version: {get_multirag_version()}')
    show_configs()
    settings.init_settings()
    settings.check_and_install_torch()
    check_and_install_mineru()
    logging.info(f'settings.EMBEDDING_CFG: {settings.EMBEDDING_CFG}')
    settings.print_rag_settings()
    if sys.platform != "win32":
        signal.signal(signal.SIGUSR1, start_tracemalloc_and_snapshot)
        signal.signal(signal.SIGUSR2, stop_tracemalloc)
    TRACE_MALLOC_ENABLED = int(os.environ.get('TRACE_MALLOC_ENABLED', "0"))
    if TRACE_MALLOC_ENABLED:
        start_tracemalloc_and_snapshot(None, None)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    report_task = asyncio.create_task(report_status())
    tasks = []
    try:
        while not stop_event.is_set():
            await task_limiter.acquire()
            t = asyncio.create_task(task_manager())
            tasks.append(t)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        report_task.cancel()
        await asyncio.gather(report_task, return_exceptions=True)
    logging.error("BUG!!! You should not reach here!!!")


if __name__ == "__main__":
    faulthandler.enable()
    init_root_logger(CONSUMER_NAME)
    asyncio.run(main())
