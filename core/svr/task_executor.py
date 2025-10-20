import random
import sys
import threading
import time

from api.db.db_models import SessionLocal, db_connection
from api.utils.api_utils import timeout
from api.utils.log_utils import init_root_logger, get_project_base_directory
from graphrag.general.index import run_graphrag
from graphrag.utils import get_llm_cache, set_llm_cache, get_tags_from_cache, set_tags_to_cache
from core.prompts.prompts import keyword_extraction, question_proposal, content_tagging

import logging
# for module in ["pdfminer"]:
#     module_logger = logging.getLogger(module)
#     module_logger.setLevel(logging.WARNING)
# for module in ["sqlalchemy"]:
#     module_logger = logging.getLogger(module)
#     module_logger.handlers.clear()
#     module_logger.propagate = True
from datetime import datetime
import json
import os
import xxhash
import copy
import re
from functools import partial
from io import BytesIO

from pymilvus import MilvusException, DataType

import numpy as np
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.orm import Session

from multiprocessing.context import TimeoutError
from timeit import default_timer as timer
import tracemalloc
import signal
import trio
# import exceptiongroup
import faulthandler

from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db import LLMType, ParserType
from api.db.services.document_service import DocumentService
from api.db.services.llm_service import LLMBundle
from api.db.services.task_service import TaskService, has_canceled
from api.db.services.file2document_service import File2DocumentService
from api import settings
from api.versions import get_multirag_version
from core.app import laws, paper, presentation, manual, qa, table, book, resume, picture, naive, one, audio, \
    email, tag
from core.nlp import search, rag_tokenizer
from core.raptor import RecursiveAbstractiveProcessing4TreeOrganizedRetrieval as Raptor
from core.settings import DOC_MAXIMUM_SIZE, DOC_BULK_SIZE, EMBEDDING_BATCH_SIZE, SVR_CONSUMER_GROUP_NAME, get_svr_queue_name, get_svr_queue_names, print_rag_settings, TAG_FLD, PAGERANK_FLD
from core.utils import rmSpace, num_tokens_from_string, truncate
from core.utils.redis_conn import REDIS_CONN, RedisDistributedLock
from core.utils.storage_factory import STORAGE_IMPL
from graphrag.utils import chat_limiter

BATCH_SIZE = 64

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
MAX_CONCURRENT_CHUNK_BUILDERS = int(os.environ.get('MAX_CONCURRENT_CHUNK_BUILDERS', "1"))
MAX_CONCURRENT_MINIO = int(os.environ.get('MAX_CONCURRENT_MINIO', '10'))
task_limiter = trio.Semaphore(MAX_CONCURRENT_TASKS)
chunk_limiter = trio.CapacityLimiter(MAX_CONCURRENT_CHUNK_BUILDERS)
embed_limiter = trio.CapacityLimiter(MAX_CONCURRENT_CHUNK_BUILDERS)
minio_limiter = trio.CapacityLimiter(MAX_CONCURRENT_MINIO)
kg_limiter = trio.CapacityLimiter(2)
WORKER_HEARTBEAT_TIMEOUT = int(os.environ.get('WORKER_HEARTBEAT_TIMEOUT', '120'))
stop_event = threading.Event()


def signal_handler(sig, frame):
    logging.info("Received interrupt signal, shutting down...")
    stop_event.set()
    time.sleep(1)
    sys.exit(0)


# SIGUSR1 handler: start tracemalloc and take snapshot
def start_tracemalloc_and_snapshot(signum, frame):
    if not tracemalloc.is_tracing():
        logging.info("start tracemalloc")
        tracemalloc.start()
    else:
        logging.info("tracemalloc is already running")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    snapshot_file = f"snapshot_{timestamp}.trace"
    snapshot_file = os.path.abspath(os.path.join(get_project_base_directory(), "logs", f"{os.getpid()}_snapshot_{timestamp}.trace"))

    snapshot = tracemalloc.take_snapshot()
    snapshot.dump(snapshot_file)
    current, peak = tracemalloc.get_traced_memory()
    if sys.platform == "win32":
        import  psutil
        process = psutil.Process()
        max_rss = process.memory_info().rss / 1024
    else:
        import resource
        max_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    logging.info(f"taken snapshot {snapshot_file}. max RSS={max_rss / 1000:.2f} MB, current memory usage: {current / 10**6:.2f} MB, Peak memory usage: {peak / 10**6:.2f} MB")

# SIGUSR2 handler: stop tracemalloc
def stop_tracemalloc(signum, frame):
    if tracemalloc.is_tracing():
        logging.info("stop tracemalloc")
        tracemalloc.stop()
    else:
        logging.info("tracemalloc not running")

class TaskCanceledException(Exception):
    def __init__(self, msg):
        self.msg = msg


# def set_progress(db: Session, task_id, from_page=0, to_page=-1, prog=None, msg="Processing..."):
#     """
#     同步执行的进度更新工具函数。
#     - 不再把 db 传进 TaskService.update_progress；
#     - 直接调用 _update_progress_sync，避免协程里再 await；
#     - 避免关闭外层传进来的 db（由上层负责）。
#     """
#     try:
#         if prog is not None and prog < 0:
#             msg = "[ERROR]" + msg
#
#         cancel = TaskService.do_cancel(db, task_id)
#         if cancel:
#             msg += " [Canceled]"
#             prog = -1
#
#         if to_page > 0 and msg and from_page < to_page:
#             msg = f"Page({from_page + 1}~{to_page + 1}): " + msg
#         if msg:
#             msg = datetime.now().strftime("%H:%M:%S") + " " + msg
#
#         info = {"progress_msg": msg}
#         if prog is not None:
#             info["progress"] = prog
#
#         # ★ 关键：同步直接调用核心，不传 db
#         TaskService._update_progress_sync(task_id, info)
#
#         if cancel:
#             raise TaskCanceledException(msg)
#
#         logging.info(f"set_progress({task_id}), progress: {prog}, progress_msg: {msg}")
#
#     except NoResultFound:
#         logging.warning("set_progress(%s): 记录不存在，无法更新进度", task_id)
#     except Exception:
#         logging.exception(f"set_progress({task_id}), progress: {prog}, progress_msg: {msg}, got exception")
def set_progress(db: Session, task_id, from_page=0, to_page=-1, prog=None, msg="Processing..."):
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

        TaskService.update_progress(db, task_id, d)
        db.commit()
        db.close()
        if cancel:
            raise TaskCanceledException(msg)
        logging.info(f"set_progress({task_id}), progress: {prog}, progress_msg: {msg}")
    except NoResultFound:
        logging.warning("set_progress(%s): 记录不存在，无法更新进度", task_id)
    except Exception:
        logging.exception(f"set_progress({task_id}), progress: {prog}, progress_msg: {msg}, got exception")


async def collect(db: Session):
    global CONSUMER_NAME, DONE_TASKS, FAILED_TASKS
    global UNACKED_ITERATOR

    svr_queue_names = get_svr_queue_names()
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
    task = TaskService.get_task(db, msg["id"])
    if task:
        canceled = has_canceled(task["id"])

    if not task or canceled:
        state = "is unknown" if not task else "has been cancelled"
        FAILED_TASKS += 1
        logging.warning(f"collect task {msg['id']} {state}")
        redis_msg.ack()
        return None, None
    task["task_type"] = msg.get("task_type", "")
    return redis_msg, task


async def get_storage_binary(bucket, name):
    return await trio.to_thread.run_sync(lambda: STORAGE_IMPL.get(bucket, name))


@timeout(60*80, 1)
async def build_chunks(task, progress_callback, db: Session):
    if task["size"] > DOC_MAXIMUM_SIZE:
        set_progress(db, task["id"], prog=-1, msg="File size exceeds( <= %dMb )" %
                                                 (int(DOC_MAXIMUM_SIZE / 1024 / 1024)))
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
            cks = await trio.to_thread.run_sync(lambda: chunker.chunk(task["name"], binary=binary, from_page=task["from_page"],
                                to_page=task["to_page"], lang=task["language"], callback=progress_callback,
                                kb_id=task["kb_id"], parser_config=task["parser_config"], tenant_id=task["tenant_id"]))
        logging.info(
            "Chunking({}) {}/{}".format(timer() - st, task["location"], task["name"]))
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
    # 如果 row["auth"] 有值，则将其添加到 doc 字典中
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

            with BytesIO() as output_buffer:
                if isinstance(d["image"], bytes):
                    output_buffer.write(d["image"])
                    output_buffer.seek(0)
                else:
                    # If the image is in RGBA mode, convert it to RGB mode before saving it in JPEG format.
                    if d["image"].mode in ("RGBA", "P"):
                        converted_image = d["image"].convert("RGB")
                        # d["image"].close()  # Close original image
                        d["image"] = converted_image
                    try:
                        d["image"].save(output_buffer, format='JPEG')
                    except OSError as e:
                        logging.warning(
                            "Saving image of chunk {}/{}/{} got exception, ignore: {}".format(task["location"], task["name"], d["id"], str(e)))

                async with minio_limiter:
                    await trio.to_thread.run_sync(
                        lambda: STORAGE_IMPL.put(task["kb_id"], d["pk"], output_buffer.getvalue()))
                d["img_id"] = "{}-{}".format(task["kb_id"], d["pk"])
                if not isinstance(d["image"], bytes):
                    d["image"].close()
                del d["image"]  # Remove image reference
                docs.append(d)
        except Exception:
            logging.exception("Saving image of chunk {}/{}/{} got exception".format(task["location"], task["name"], d["pk"]))
            raise

    async with trio.open_nursery() as nursery:
        for ck in cks:
            nursery.start_soon(upload_to_minio, doc, ck)

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
                    cached = await trio.to_thread.run_sync(lambda: keyword_extraction(chat_mdl, d["content_with_weight"], topn))
                set_llm_cache(chat_mdl.llm_name, d["content_with_weight"], cached, "keywords", {"topn": topn})
            if cached:
                d["important_kwd"] = cached.split(",")
                d["important_tks"] = rag_tokenizer.tokenize(" ".join(d["important_kwd"]))
            return

        async with trio.open_nursery() as nursery:
            for d in docs:
                nursery.start_soon(doc_keyword_extraction, chat_mdl, d, task["parser_config"]["auto_keywords"])
        progress_callback(msg="Keywords generation {} chunks completed in {:.2f}s".format(len(docs), timer() - st))

    if task["parser_config"].get("auto_questions", 0):
        st = timer()
        progress_callback(msg="Start to generate questions for every chunk ...")
        chat_mdl = LLMBundle(db, task["tenant_id"], LLMType.CHAT, llm_name=task["llm_id"], lang=task["language"])

        async def doc_question_proposal(chat_mdl, d, topn):
            cached = get_llm_cache(chat_mdl.llm_name, d["content_with_weight"], "question", {"topn": topn})
            if not cached:
                async with chat_limiter:
                    cached = await trio.to_thread.run_sync(lambda: question_proposal(chat_mdl, d["content_with_weight"], topn))
                set_llm_cache(chat_mdl.llm_name, d["content_with_weight"], cached, "question", {"topn": topn})
            if cached:
                d["question_kwd"] = cached.split("\n")
                d["question_tks"] = rag_tokenizer.tokenize("\n".join(d["question_kwd"]))

        async with trio.open_nursery() as nursery:
            for d in docs:
                nursery.start_soon(doc_question_proposal, chat_mdl, d, task["parser_config"]["auto_questions"])
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
            all_tags = settings.retrievaler.all_tags_in_portion(tenant_id, kb_ids, S)
            set_tags_to_cache(kb_ids, all_tags)
        else:
            all_tags = json.loads(all_tags)

        chat_mdl = LLMBundle(db, task["tenant_id"], LLMType.CHAT, llm_name=task["llm_id"], lang=task["language"])

        docs_to_tag = []
        for d in docs:
            task_canceled = has_canceled(task["id"])
            if task_canceled:
                progress_callback(-1, msg="Task has been canceled.")
                return
            if settings.retrievaler.tag_content(tenant_id, kb_ids, d, all_tags, topn_tags=topn_tags, S=S) and len(d[TAG_FLD]) > 0:
                examples.append({"content": d["content_with_weight"], TAG_FLD: d[TAG_FLD]})
            else:
                docs_to_tag.append(d)

        async def doc_content_tagging(chat_mdl, d, topn_tags):
            cached = get_llm_cache(chat_mdl.llm_name, d["content_with_weight"], all_tags, {"topn": topn_tags})
            if not cached:
                picked_examples = random.choices(examples, k=2) if len(examples)>2 else examples
                if not picked_examples:
                    picked_examples.append({"content": "This is an example", TAG_FLD: {'example': 1}})
                async with chat_limiter:
                    cached = await trio.to_thread.run_sync(lambda: content_tagging(chat_mdl, d["content_with_weight"], all_tags, picked_examples, topn=topn_tags))
                if cached:
                    cached = json.dumps(cached)
            if cached:
                set_llm_cache(chat_mdl.llm_name, d["content_with_weight"], cached, all_tags, {"topn": topn_tags})
                d[TAG_FLD] = json.loads(cached)
        async with trio.open_nursery() as nursery:
            for d in docs_to_tag:
                nursery.start_soon(doc_content_tagging, chat_mdl, d, topn_tags)
        progress_callback(msg="Tagging {} chunks completed in {:.2f}s".format(len(docs), timer() - st))

    return docs


async def init_kb(row, kb_name):
    """
    初始化知识库，创建集合并设置索引

    Args:
        row: 任务数据行
        kb_name: 知识库名称
    """
    idxnm = search.index_name_one(row["tenant_id"], kb_name)
    if await trio.to_thread.run_sync(lambda: settings.docStoreConn.has_collection(idxnm)):
        return

    # 加载基础mapping配置
    mapping_path = os.path.join(get_project_base_directory(), "configs", "mapping.json")
    mapping = await trio.to_thread.run_sync(lambda: json.load(open(mapping_path, 'r')))

    # 获取当前嵌入模型的向量维度
    vector_dim = None
    try:
        if "embd_id" in row and row["tenant_id"]:
            with db_connection() as db:
                embedding_model = LLMBundle(db, row["tenant_id"], LLMType.EMBEDDING,
                                            llm_name=row["embd_id"], lang=row.get("language", "en"))
                # 生成一个示例向量以获取维度
                sample_vec, _ = embedding_model.encode(["测试文本"])
                if len(sample_vec) > 0:
                    vector_dim = len(sample_vec[0])
                    logging.info(f"当前embedding模型维度: {vector_dim}")
                else:
                    logging.warning("无法确定嵌入模型维度，将使用默认维度")
    except Exception as e:
        logging.warning(f"获取嵌入模型维度失败: {str(e)}")

    # todo 后续可以按照ragflow一样，直接内置多个向量字段，避免先执行向量化获取维度动态创建
    # 自动维度字典
    auto_dimensions = {}

    # 更新mapping中的向量维度
    if vector_dim:
        # 更新标准vector字段的维度
        for template in mapping["mappings"]["dynamic_templates"]:
            if "standard_vector_template" in template:
                template["standard_vector_template"]["mapping"]["dims"] = vector_dim

        # 添加维度特定字段
        auto_dimensions["vector"] = vector_dim
        auto_dimensions[f"q_{vector_dim}_vec"] = vector_dim

    # 创建集合
    await trio.to_thread.run_sync(lambda: settings.docStoreConn.create_collection_with_mapping(idxnm, mapping, auto_dimensions))


def convert_data_types(data, schema):
    """
    转换数据类型以匹配Milvus模式，确保所有必要字段都有值

    Args:
        data: 文档数据字典
        schema: Milvus集合模式

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

    # 处理动态向量字段 (q_*_vec)
    vector_fields = [k for k in result.keys() if re.match(r'q_\d+_vec', k)]
    for vector_field in vector_fields:
        if vector_field not in schema_fields:
            # 如果这是一个新的向量字段，记录一下但保留它
            logging.info(f"发现新的向量字段 {vector_field}，保留在数据中")

    return result


async def get_schema(collection_name):
    schema = await trio.to_thread.run_sync(lambda: settings.docStoreConn.describe_collection(collection_name))
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
        tts.append(rmSpace(d.get("docnm_kwd", "Title")))
        c = "\n".join(d.get("question_kwd", []))
        if not c:
            c = d["content_with_weight"]
        c = re.sub(r"</?(table|td|caption|tr|th)( [^<>]{0,12})?>", " ", c)
        if not c:
            c = "None"
        cnts.append(c)

    tk_count = 0
    if len(tts) == len(cnts):
        vts, c = await trio.to_thread.run_sync(lambda: mdl.encode(tts[0: 1]))
        tts = np.concatenate([vts for _ in range(len(tts))], axis=0)
        tk_count += c

    @timeout(60)
    def batch_encode(txts):
        nonlocal mdl
        return mdl.encode([truncate(c, mdl.max_length-10) for c in txts])

    cnts_ = np.array([])
    for i in range(0, len(cnts), EMBEDDING_BATCH_SIZE):
        async with embed_limiter:
            vts, c = await trio.to_thread.run_sync(lambda: batch_encode(cnts[i: i + EMBEDDING_BATCH_SIZE]))
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
    vects = (title_w * tts + (1 - title_w) * cnts) if len(tts) == len(cnts) else cnts

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


@timeout(3600)
async def run_raptor(row, chat_mdl, embd_mdl, vector_size, callback=None):
    chunks = []
    if vector_size != 768:
        vctr_nm = "q_%d_vec"%vector_size
    else:
        vctr_nm = "vector"
    for d in settings.retrievaler.chunk_list(row["doc_id"], row["tenant_id"], [str(row["kb_id"])],
                                             fields=["content_with_weight", vctr_nm]):
        chunks.append((d["content_with_weight"], np.array(d[vctr_nm])))

    raptor = Raptor(
        row["parser_config"]["raptor"].get("max_cluster", 64),
        chat_mdl,
        embd_mdl,
        row["parser_config"]["raptor"]["prompt"],
        row["parser_config"]["raptor"]["max_token"],
        row["parser_config"]["raptor"]["threshold"]
    )
    original_length = len(chunks)
    chunks = await raptor(chunks, row["parser_config"]["raptor"]["random_seed"], callback)
    doc = {
        "doc_id": row["doc_id"],
        "kb_id": [str(row["kb_id"])],
        "docnm_kwd": row["name"],
        "title_tks": rag_tokenizer.tokenize(row["name"])
    }
    if row["pagerank"]:
        doc[PAGERANK_FLD] = int(row["pagerank"])
    res = []
    tk_count = 0
    for content, vctr in chunks[original_length:]:
        d = copy.deepcopy(doc)
        d["pk"] = xxhash.xxh64((content + str(d["doc_id"])).encode("utf-8")).hexdigest()
        d["create_time"] = str(datetime.now()).replace("T", " ")[:19]
        d["create_timestamp_flt"] = datetime.now().timestamp()
        d[vctr_nm] = vctr.tolist()
        d["content_with_weight"] = content
        d["content_ltks"] = rag_tokenizer.tokenize(content)
        d["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(d["content_ltks"])
        res.append(d)
        tk_count += num_tokens_from_string(content)
    return res, tk_count


@timeout(60*60*2, 1)
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

    # 处理 auth 列
    task['auth'] = convert_auth(task.get('auth'))

    task_id = task["id"]
    task_from_page = task["from_page"]
    task_to_page = task["to_page"]
    task_tenant_id = task["tenant_id"]
    task_embedding_id = task["embd_id"]
    task_language = task["language"]
    task_llm_id = task["llm_id"]
    task_dataset_id = task["kb_id"]
    task_doc_id = task["doc_id"]
    task_document_name = task["name"]
    task_parser_config = task["parser_config"]
    task_start_ts = timer()

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
        return

    try:
        # bind embedding model
        embedding_model = LLMBundle(db, task_tenant_id, LLMType.EMBEDDING, llm_name=task_embedding_id, lang=task_language)
        vts, _ = embedding_model.encode(["ok"])
        vector_size = len(vts[0])
    except Exception as e:
        error_message = f'Fail to bind embedding model: {str(e)}'
        progress_callback(-1, msg=error_message)
        logging.exception(error_message)
        raise

    # init_kb(task, vector_size)
    kb_id = DocumentService.get_by_doc_id(db, task_doc_id)["kb_id"]
    kb_name = KnowledgebaseService.get_by_id(db, kb_id).name
    await init_kb(task, kb_name)

    # Either using RAPTOR or Standard chunking methods
    if task.get("task_type", "") == "raptor":
        # bind LLM for raptor
        chat_model = LLMBundle(db, task_tenant_id, LLMType.CHAT, llm_name=task_llm_id, lang=task_language)
        # run RAPTOR
        async with kg_limiter:
            chunks, token_count = await run_raptor(task, chat_model, embedding_model, vector_size, progress_callback)
    # Either using graphrag or Standard chunking methods
    elif task.get("task_type", "") == "graphrag":
        if not task_parser_config.get("graphrag", {}).get("use_graphrag", False):
            progress_callback(prog=-1.0, msg="Internal configuration error.")
            return
        graphrag_conf = task["kb_parser_config"].get("graphrag", {})
        start_ts = timer()
        chat_model = LLMBundle(db, task_tenant_id, LLMType.CHAT, llm_name=task_llm_id, lang=task_language)
        with_resolution = graphrag_conf.get("resolution", False)
        with_community = graphrag_conf.get("community", False)
        async with kg_limiter:
            await run_graphrag(task, task_language, with_resolution, with_community, chat_model, embedding_model, progress_callback)
        progress_callback(prog=1.0, msg="Knowledge Graph done ({:.2f}s)".format(timer() - start_ts))
        return
    else:
        # Standard chunking methods
        start_ts = timer()
        chunks = await build_chunks(task, progress_callback, db)
        logging.info("Build document {}: {:.2f}s".format(task_document_name, timer() - start_ts))
        if not chunks:
            progress_callback(1., msg=f"No chunk built from {task_document_name}")
            return
        # TODO: exception handler
        ## set_progress(task["did"], -1, "ERROR: ")
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

    chunk_count = len(set([chunk["pk"] for chunk in chunks]))
    # 记录开始时间
    start_ts = timer()

    # 用于记录成功和失败的插入信息
    successful_inserts = []
    failed_inserts = []

    # 获取集合 schema，用于做数据类型转换
    schema = await get_schema(search.index_name_one(task_tenant_id, kb_name))
    collection_name = search.index_name_one(task_tenant_id, kb_name)

    async def delete_image(kb_id, chunk_id):
        try:
            async with minio_limiter:
                STORAGE_IMPL.delete(kb_id, chunk_id)
        except Exception:
            logging.exception("Deleting image of chunk {}/{}/{} got exception".format(task["location"], task["name"], chunk_id))
            raise

    # 循环分批插入
    for b in range(0, chunk_count, DOC_BULK_SIZE):
        # 取出本批次要插入的 chunks
        chunk_batch = chunks[b: b + DOC_BULK_SIZE]

        # 将本批次内的数据先做类型转换
        converted_batch = []
        for chunk in chunk_batch:
            # 选项1：对小数据保持同步
            converted_chunk = convert_data_types(chunk, schema)
            # 选项2：对大数据使用异步
            # converted_chunk = await convert_data_types_async(chunk, schema)
            converted_batch.append(converted_chunk)

        doc_store_result = {}
        try:
            # 调用自定义的 settings.docStoreConn.insert 方法
            doc_store_result = await trio.to_thread.run_sync(lambda: settings.docStoreConn.insert(
                collection_name=collection_name,
                data=converted_batch
            ))

            # 可选：检查 insert_count 是否与本批次长度一致
            # 如果你的需求是一定要完全插入成功才算成功，可以加如下校验：
            if doc_store_result.get("insert_count", 0) != len(converted_batch):
                error_message = (
                    f"Insert count mismatch: expected {len(converted_batch)}, "
                    f"got {doc_store_result.get('insert_count', 0)}."
                )
                progress_callback(-1, msg=error_message)
                raise Exception(error_message)

        except Exception:
            # 如果出现异常，记录失败并进行删除回滚
            failed_inserts.extend(chunk_batch)
            progress_callback(
                -1,
                "Insert chunk error, detail info please check log file. Please also check Milvus status!"
            )
            try:
                if settings.docStoreConn.has_collection(collection_name):
                    # 删除本批次已经尝试插入的记录（这里按 doc_id 删除，可根据业务实际情况调整 filter 条件）
                    for chunk in chunk_batch:
                        if "doc_id" in chunk:
                            settings.docStoreConn.delete(
                                collection_name=collection_name,
                                filter=f"doc_id == '{chunk['doc_id']}'"
                            )
            except MilvusException as e:
                return e  # 可根据需要改成 raise 或其它处理
            logging.exception("Insert error:")
            logging.error("Data being inserted: %s", converted_batch)
            return  # 出错后直接退出

        # 若执行到此，说明插入成功，记录插入结果
        successful_inserts.append(doc_store_result)

        task_canceled = has_canceled(task_id)
        if task_canceled:
            progress_callback(-1, msg="Task has been canceled.")
            return

        # 每插入 128 批，做一次进度回调（可自定义触发频率）
        if b % 128 == 0:
            progress = 0.8 + 0.1 * (b + 1) / chunk_count
            progress_callback(prog=progress, msg="")

        # 拼接本批次 chunk_ids 并更新到 TaskService
        # （需要你确保 chunk 内有 "id" 这个字段）
        chunk_ids = [chunk["pk"] for chunk in chunk_batch]
        chunk_ids_str = " ".join(chunk_ids)
        try:
            TaskService.update_chunk_ids(db, task["id"], chunk_ids_str)
        except NoResultFound:
            logging.warning(f"do_handle_task update_chunk_ids failed since task {task['id']} is unknown.")
            # 如果 TaskService 中没有这个 task，则删除已插入数据并退出
            try:
                if settings.docStoreConn.has_collection(collection_name):
                    for chunk in chunk_batch:
                        if "doc_id" in chunk:
                            await trio.to_thread.run_sync(lambda: settings.docStoreConn.delete(
                                collection_name=collection_name,
                                filter=f"doc_id == '{chunk['doc_id']}'"
                            ))
            except MilvusException as e:
                return e
            async with trio.open_nursery() as nursery:
                for chunk_id in chunk_ids:
                    nursery.start_soon(delete_image, task_dataset_id, chunk_id)
            progress_callback(-1, msg=f"Chunk updates failed since task {task['id']} is unknown.")
            return

    logging.info("Indexing doc({}), page({}-{}), chunks({}), elapsed: {:.2f}".format(task_document_name, task_from_page,
                                                                                     task_to_page, len(chunks),
                                                                                     timer() - start_ts))

    # 分批插入循环结束后，统计总耗时
    insertion_total_time = timer() - start_ts

    # 统计成功插入数量
    if successful_inserts:
        # 根据返回的 insert_count 求和
        total_insert_count = sum(item.get("insert_count", 0) for item in successful_inserts)
        logging.info(
            f"Total successful inserts into Milvus's {collection_name}: {total_insert_count}"
        )

    logging.info(f"Total Insertion elapsed: {insertion_total_time:.2f}")

    # 如果有失败的插入，打印警告（可根据实际需求做进一步处理）
    if failed_inserts:
        logging.warning(f"Failed inserts count: {len(failed_inserts)}")
        logging.warning(f"Failed insert records: {failed_inserts}")

    # 如果任务被取消，则清理已插入的数据并返回
    if TaskService.do_cancel(db, task_id):
        try:
            if await trio.to_thread.run_sync(lambda: settings.docStoreConn.has_collection(collection_name)):
                await trio.to_thread.run_sync(lambda: settings.docStoreConn.delete(
                    collection_name=collection_name,
                    filter=f"doc_id == '{task_doc_id}'"
                ))
        except MilvusException as e:
            return e
        return

    # 最后更新统计信息
    DocumentService.increment_chunk_num(db, task_doc_id, task_dataset_id, token_count, chunk_count, 0)

    # 做一次进度回调
    time_cost = timer() - start_ts
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
                await trio.sleep(5)
                return
            try:
                # 转换为可序列化的字典
                if hasattr(task, "_asdict"):  # 检查是否为 RowProxy
                    task_dict = task._asdict()
                elif isinstance(task, dict):  # 如果已经是字典
                    task_dict = task
                else:
                    task_dict = {key: str(value) for key, value in vars(task).items()}  # 通用对象转换为字典
                logging.info(f"handle_task begin for task {json.dumps(task_dict)}")
                CURRENT_TASKS[task["id"]] = copy.deepcopy(task)
                await do_handle_task(db, task)
                DONE_TASKS += 1
                CURRENT_TASKS.pop(task["id"], None)
                logging.info(f"handle_task done for task {json.dumps(task)}")
            except Exception as e:
                FAILED_TASKS += 1
                CURRENT_TASKS.pop(task["id"], None)
                try:
                    err_msg = str(e)
                    # while isinstance(e, exceptiongroup.ExceptionGroup):
                    while isinstance(e, ExceptionGroup):
                        e = e.exceptions[0]
                        err_msg += ' -- ' + str(e)
                    set_progress(db, task["id"], prog=-1, msg=f"[Exception]: {err_msg}")
                except Exception:
                    pass
                logging.exception(f"handle_task got exception for task {json.dumps(task)}")

            redis_msg.ack()
        except Exception:
            logging.exception(f"Error in main loop")
            db.rollback()  # 回滚事务
            raise
        else:
            db.commit()  # 提交事务


async def report_status():
    global CONSUMER_NAME, BOOT_AT, PENDING_TASKS, LAG_TASKS, DONE_TASKS, FAILED_TASKS
    REDIS_CONN.sadd("TASKEXE", CONSUMER_NAME)
    redis_lock = RedisDistributedLock("clean_task_executor", lock_value=CONSUMER_NAME, timeout=60)
    while True:
        try:
            now = datetime.now()
            group_info = REDIS_CONN.queue_info(get_svr_queue_name(0), SVR_CONSUMER_GROUP_NAME)
            if group_info is not None:
                PENDING_TASKS = int(group_info.get("pending", 0))
                LAG_TASKS = int(group_info.get("lag", 0))

            current = copy.deepcopy(CURRENT_TASKS)
            heartbeat = json.dumps({
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
        await trio.sleep(30)


async def task_manager():
    try:
        await handle_task()
    finally:
        task_limiter.release()


async def main():
    logging.info(r"""
======================================================================
  ______           __      ______                     __
 /_  __/___ ______/ /__   / ____/  _____  _______  __/ /_____  _____
  / / / __ `/ ___/ //_/  / __/ | |/_/ _ \/ ___/ / / / __/ __ \/ ___/
 / / / /_/ (__  ) ,<    / /____>  </  __/ /__/ /_/ / /_/ /_/ / /
/_/  \__,_/____/_/|_|  /_____/_/|_|\___/\___/\__,_/\__/\____/_/
======================================================================
    """)
    logging.info(f'TaskExecutor - MultiRAG version: {get_multirag_version()}')
    settings.init_settings()
    print_rag_settings()
    if sys.platform != "win32":
        signal.signal(signal.SIGUSR1, start_tracemalloc_and_snapshot)
        signal.signal(signal.SIGUSR2, stop_tracemalloc)
    TRACE_MALLOC_ENABLED = int(os.environ.get('TRACE_MALLOC_ENABLED', "0"))
    if TRACE_MALLOC_ENABLED:
        start_tracemalloc_and_snapshot(None, None)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    async with trio.open_nursery() as nursery:
        nursery.start_soon(report_status)
        while not stop_event.is_set():
            await task_limiter.acquire()
            nursery.start_soon(task_manager)
    logging.error("BUG!!! You should not reach here!!!")

if __name__ == "__main__":
    faulthandler.enable()
    init_root_logger(CONSUMER_NAME)
    trio.run(main)
