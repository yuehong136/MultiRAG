import sys
from api.utils.log_utils import initRootLogger

CONSUMER_NO = "0" if len(sys.argv) < 2 else sys.argv[1]
CONSUMER_NAME = "task_executor_" + CONSUMER_NO
initRootLogger(CONSUMER_NAME)
import logging
for module in ["pdfminer"]:
    module_logger = logging.getLogger(module)
    module_logger.setLevel(logging.WARNING)
for module in ["sqlalchemy"]:
    module_logger = logging.getLogger(module)
    module_logger.handlers.clear()
    module_logger.propagate = True
from datetime import datetime
from graphrag.utils import get_llm_cache, set_llm_cache
import json
import os
import xxhash
import copy
import re
import time
import threading
from functools import partial
from io import BytesIO

from pymilvus import MilvusException, DataType

import numpy as np
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.orm import Session

from multiprocessing.context import TimeoutError
from timeit import default_timer as timer
import tracemalloc

from api.db.database import SessionLocal
from api.db.services.dialog_service import keyword_extraction, question_proposal
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db import LLMType, ParserType, TaskStatus
from api.db.services.document_service import DocumentService
from api.db.services.llm_service import LLMBundle
from api.db.services.task_service import TaskService
from api.db.services.file2document_service import File2DocumentService
from api import settings
from api.versions import get_multirag_version
from api.utils.file_utils import get_project_base_directory
from core.app import laws, paper, presentation, manual, qa, table, book, resume, picture, naive, one, audio, email, \
    knowledge_graph
from core.nlp import search, rag_tokenizer
from core.raptor import RecursiveAbstractiveProcessing4TreeOrganizedRetrieval as Raptor
from core.settings import DOC_MAXIMUM_SIZE, SVR_QUEUE_NAME, print_multirag_settings
from core.utils import rmSpace, num_tokens_from_string
from core.utils.milvus_conn import MILVUS_CONNECTION
from core.utils.redis_conn import REDIS_CONN, Payload
from core.utils.storage_factory import STORAGE_IMPL

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
    ParserType.KG.value: knowledge_graph
}

CONSUMER_NAME = "task_consumer_" + CONSUMER_NO
PAYLOAD: Payload | None = None
BOOT_AT = datetime.now().astimezone().isoformat(timespec="milliseconds")
PENDING_TASKS = 0
LAG_TASKS = 0

mt_lock = threading.Lock()
DONE_TASKS = 0
FAILED_TASKS = 0
CURRENT_TASK = None


class TaskCanceledException(Exception):
    def __init__(self, msg):
        self.msg = msg


def set_progress(db: Session, task_id, from_page=0, to_page=-1, prog=None, msg="Processing..."):
    global PAYLOAD
    if prog is not None and prog < 0:
        msg = "[ERROR]" + msg
    try:
        cancel = TaskService.do_cancel(db, task_id)
    except NoResultFound:
        logging.warning(f"set_progress task {task_id} is unknown")
        if PAYLOAD:
            PAYLOAD.ack()
            PAYLOAD = None
        return
    if cancel:
        msg += " [Canceled]"
        prog = -1

    if to_page > 0:
        if msg:
            msg = f"Page({from_page + 1}~{to_page + 1}): " + msg
    if msg:
        msg = datetime.now().strftime("%H:%M:%S") + " " + msg
    d = {"progress_msg": msg}
    if prog is not None:
        d["progress"] = prog
    logging.info(f"set_progress({task_id}), progress: {prog}, progress_msg: {msg}")
    try:
        TaskService.update_progress(db, task_id, d)
    except NoResultFound:
        logging.warning(f"set_progress task {task_id} is unknown")
        if PAYLOAD:
            PAYLOAD.ack()
            PAYLOAD = None
        return

    db.close()
    if cancel and PAYLOAD:
        PAYLOAD.ack()
        PAYLOAD = None
        raise TaskCanceledException(msg)


def collect(db: Session):
    global CONSUMER_NAME, PAYLOAD, DONE_TASKS, FAILED_TASKS
    try:
        PAYLOAD = REDIS_CONN.get_unacked_for(CONSUMER_NAME, SVR_QUEUE_NAME, "multi_rag_svr_task_broker")
        if not PAYLOAD:
            PAYLOAD = REDIS_CONN.queue_consumer(SVR_QUEUE_NAME, "multi_rag_svr_task_broker", CONSUMER_NAME)
        if not PAYLOAD:
            time.sleep(1)
            return None
    except Exception:
        logging.exception("Get task event from queue exception")
        return None

    msg = PAYLOAD.get_message()
    if not msg:
        return None

    task = None
    canceled = False
    try:
        task = TaskService.get_task(db, msg["id"])
        if task:
            doc = DocumentService.get_by_id(db, task["doc_id"])
            canceled = doc.run == TaskStatus.CANCEL.value or doc.progress < 0
    except NoResultFound:
        pass
    except Exception:
        logging.exception("collect get_task exception")
    if not task or canceled:
        state = "is unknown" if not task else "has been cancelled"
        with mt_lock:
            DONE_TASKS += 1
        logging.info(f"collect task {msg['id']} {state}")
        return None

    if msg.get("type", "") == "raptor":
        task["task_type"] = "raptor"
    return task


def get_storage_binary(bucket, name):
    return STORAGE_IMPL.get(bucket, name)


def build_chunks(task, progress_callback, db: Session):
    if task["size"] > DOC_MAXIMUM_SIZE:
        set_progress(db, task["id"], prog=-1, msg="File size exceeds( <= %dMb )" %
                                                 (int(DOC_MAXIMUM_SIZE / 1024 / 1024)))
        return []

    chunker = FACTORY[task["parser_id"].lower()]
    try:
        st = timer()
        bucket, name = File2DocumentService.get_storage_address(db, doc_id=task["doc_id"])
        binary = get_storage_binary(bucket, name)
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
        cks = chunker.chunk(task["name"], binary=binary, from_page=task["from_page"],
                            to_page=task["to_page"], lang=task["language"], callback=progress_callback,
                            kb_id=task["kb_id"], parser_config=task["parser_config"], tenant_id=task["tenant_id"])
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
        doc["pagerank_fea"] = int(task["pagerank"])
    el = 0
    for ck in cks:
        d = copy.deepcopy(doc)
        d.update(ck)
        d["pk"] = xxhash.xxh64((ck["content_with_weight"] + str(d["doc_id"])).encode("utf-8")).hexdigest()
        d["create_time"] = str(datetime.now()).replace("T", " ")[:19]
        d["create_timestamp_flt"] = datetime.now().timestamp()
        d["page_num_int"] = d.get("page_num_int", [])
        d["position_int"] = d.get("position_int", [])
        d["top_int"] = d.get("top_int", [])
        # if not d.get("image"):
        #     docs.append(d)
        #     continue
        if "image" not in d:
            docs.append(d)  # 如果 image 字段不存在，则直接添加到 docs
            continue
        elif d["image"] is None:
            del d["image"]  # 如果 image 字段为空，则删除该条记录的image
            docs.append(d)
            continue
        try:
            output_buffer = BytesIO()
            if isinstance(d["image"], bytes):
                output_buffer = BytesIO(d["image"])
            else:
                d["image"].save(output_buffer, format='JPEG')

            st = timer()
            STORAGE_IMPL.put(task["kb_id"], d["pk"], output_buffer.getvalue())
            el += timer() - st
        except Exception:
            logging.exception("Saving image of chunk {}/{}/{} got exception".format(task["location"], task["name"], d["pk"]))
            raise

        d["img_id"] = "{}-{}".format(task["kb_id"], d["pk"])
        del d["image"]
        docs.append(d)
    logging.info("MINIO PUT({}):{}".format(task["name"], el))

    if task["parser_config"].get("auto_keywords", 0):
        st = timer()
        progress_callback(msg="Start to generate keywords for every chunk ...")
        chat_mdl = LLMBundle(db, task["tenant_id"], LLMType.CHAT, llm_name=task["llm_id"], lang=task["language"])
        for d in docs:
            cached = get_llm_cache(chat_mdl.llm_name, d["content_with_weight"], "keywords",
                                   {"topn": task["parser_config"]["auto_keywords"]})
            if not cached:
                cached = keyword_extraction(chat_mdl, d["content_with_weight"],
                                            task["parser_config"]["auto_keywords"])
                if cached:
                    set_llm_cache(chat_mdl.llm_name, d["content_with_weight"], cached, "keywords",
                                  {"topn": task["parser_config"]["auto_keywords"]})

            d["important_kwd"] = cached.split(",")
            d["important_tks"] = rag_tokenizer.tokenize(" ".join(d["important_kwd"]))
        progress_callback(msg="Keywords generation completed in {:.2f}s".format(timer() - st))

    if task["parser_config"].get("auto_questions", 0):
        st = timer()
        progress_callback(msg="Start to generate questions for every chunk ...")
        chat_mdl = LLMBundle(db, task["tenant_id"], LLMType.CHAT, llm_name=task["llm_id"], lang=task["language"])
        for d in docs:
            cached = get_llm_cache(chat_mdl.llm_name, d["content_with_weight"], "question",
                                   {"topn": task["parser_config"]["auto_questions"]})
            if not cached:
                cached = question_proposal(chat_mdl, d["content_with_weight"], task["parser_config"]["auto_questions"])
                if cached:
                    set_llm_cache(chat_mdl.llm_name, d["content_with_weight"], cached, "question",
                                  {"topn": task["parser_config"]["auto_questions"]})

            d["question_kwd"] = cached.split("\n")
            d["question_tks"] = rag_tokenizer.tokenize("\n".join(d["question_kwd"]))
        progress_callback(msg="Question generation completed in {:.2f}s".format(timer() - st))

    return docs


def init_kb(row, kb_name):
    idxnm = search.index_name_one(row["tenant_id"], kb_name)
    if MILVUS_CONNECTION.has_collection(idxnm):
        return
    mapping_path = os.path.join(get_project_base_directory(), "configs", "mapping.json")
    with open(mapping_path, 'r') as f:
        mapping = json.load(f)
    MILVUS_CONNECTION.create_collection_with_mapping(idxnm, mapping)


def convert_data_types(data, schema):
    for field in schema['fields']:
        field_name = field['name']
        field_type = field['type']
        if field_name not in data:
            # 根据字段类型填充空值
            if field_type == DataType.FLOAT_VECTOR:
                data[field_name] = [0.0] * field['params']['dim']
            elif field_type == DataType.VARCHAR:
                data[field_name] = ""
            elif field_type == DataType.FLOAT:
                data[field_name] = 0.0
            elif field_type == DataType.INT64:
                data[field_name] = 0
            elif field_type == DataType.ARRAY:
                data[field_name] = []
            else:
                data[field_name] = None
        else:
            # 转换数据类型
            if field_type == DataType.FLOAT_VECTOR:
                if not isinstance(data[field_name], list):
                    data[field_name] = list(data[field_name])
            elif field_type == DataType.VARCHAR:
                if isinstance(data[field_name], list):
                    data[field_name] = ','.join(data[field_name])
                else:
                    data[field_name] = str(data[field_name])
            elif field_type == DataType.FLOAT:
                data[field_name] = float(data[field_name])
            elif field_type == DataType.INT64:
                data[field_name] = int(data[field_name])
            elif field_type == DataType.JSON:
                if isinstance(data[field_name], list):
                    data[field_name] = json.dumps(data[field_name])
                else:
                    data[field_name] = str(data[field_name])
    return data


def get_schema(collection_name):
    schema = MILVUS_CONNECTION.describe_collection(collection_name)
    # print("Schema of the collection:", schema)
    return schema


def embedding(docs, mdl, parser_config=None, callback=None):
    if parser_config is None:
        parser_config = {}
    batch_size = 16
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
        tts_ = np.array([])
        for i in range(0, len(tts), batch_size):
            vts, c = mdl.encode(tts[i: i + batch_size])
            if len(tts_) == 0:
                tts_ = vts
            else:
                tts_ = np.concatenate((tts_, vts), axis=0)
            tk_count += c
            callback(prog=0.6 + 0.1 * (i + 1) / len(tts), msg="")
        tts = tts_

    cnts_ = np.array([])
    for i in range(0, len(cnts), batch_size):
        vts, c = mdl.encode(cnts[i: i + batch_size])
        if len(cnts_) == 0:
            cnts_ = vts
        else:
            cnts_ = np.concatenate((cnts_, vts), axis=0)
        tk_count += c
        callback(prog=0.7 + 0.2 * (i + 1) / len(cnts), msg="")
    cnts = cnts_

    title_w = float(parser_config.get("filename_embd_weight", 0.1))
    vects = (title_w * tts + (1 - title_w) *
             cnts) if len(tts) == len(cnts) else cnts

    assert len(vects) == len(docs)
    for i, d in enumerate(docs):
        v = vects[i].tolist()
        d["vector"] = v
    return tk_count


def run_raptor(row, chat_mdl, embd_mdl, callback=None):
    vts, _ = embd_mdl.encode(["ok"])
    vctr_nm = "vector"
    chunks = []
    for d in settings.retrievaler.chunk_list(row["doc_id"], row["tenant_id"], fields=["content_with_weight", vctr_nm]):
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
    chunks = raptor(chunks, row["parser_config"]["raptor"]["random_seed"], callback)
    doc = {
        "doc_id": row["doc_id"],
        "kb_id": [str(row["kb_id"])],
        "docnm_kwd": row["name"],
        "title_tks": rag_tokenizer.tokenize(row["name"])
    }
    if row.get("pagerank"):
        doc["pagerank_fea"] = int(row["pagerank"])
    res = []
    tk_count = 0
    for content, vctr in chunks[original_length:]:
        d = copy.deepcopy(doc)
        d["pk"] = xxhash.xxh64((content + str(d["doc_id"])).encode("utf-8")).hexdigest()
        d["create_time"] = str(datetime.now()).replace("T", " ")[:19]
        d["create_timestamp_flt"] = datetime.now().timestamp()
        d["vector"] = vctr.tolist()
        d["text"] = content
        d["content_ltks"] = rag_tokenizer.tokenize(content)
        d["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(d["content_ltks"])
        res.append(d)
        tk_count += num_tokens_from_string(content)
    return res, tk_count


def do_handle_task(db, task):
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

    # prepare the progress callback function
    progress_callback = partial(set_progress, db, task_id, task_from_page, task_to_page)

    try:
        task_canceled = TaskService.do_cancel(db, task_id)
    except NoResultFound:
        logging.warning(f"task {task_id} is unknown")
        return
    if task_canceled:
        progress_callback(-1, msg="Task has been canceled.")
        return

    try:
        # bind embedding model
        embedding_model = LLMBundle(db, task_tenant_id, LLMType.EMBEDDING, llm_name=task_embedding_id, lang=task_language)
    except Exception as e:
        error_message = f'Fail to bind embedding model: {str(e)}'
        progress_callback(-1, msg=error_message)
        logging.exception(error_message)
        raise


    # Either using RAPTOR or Standard chunking methods
    if task.get("task_type", "") == "raptor":
        try:
            # bind LLM for raptor
            chat_model = LLMBundle(db, task_tenant_id, LLMType.CHAT, llm_name=task_llm_id, lang=task_language)

            # run RAPTOR
            chunks, token_count = run_raptor(task, chat_model, embedding_model, progress_callback)
        except TaskCanceledException:
            raise
        except Exception as e:
            progress_callback(-1, msg=f'Fail to bind LLM used by RAPTOR: {str(e)}')
            raise
    else:
        # Standard chunking methods
        start_ts = timer()
        chunks = build_chunks(task, progress_callback, db)
        logging.info("Build document {}: {:.2f}s".format(task_document_name, timer() - start_ts))
        if chunks is None:
            return
        if not chunks:
            progress_callback(1., msg=f"No chunk built from {task_document_name}")
            return
        # TODO: exception handler
        ## set_progress(task["did"], -1, "ERROR: ")
        progress_callback(msg="Generate {} chunks".format(len(chunks)))
        start_ts = timer()
        try:
            token_count = embedding(chunks, embedding_model, task_parser_config, progress_callback)
        except Exception as e:
            error_message = "Generate embedding error:{}".format(str(e))
            progress_callback(-1, error_message)
            logging.exception(error_message)
            token_count = 0
            raise
        progress_message = "Embedding chunks ({:.2f}s)".format(timer() - start_ts)
        logging.info(progress_message)
        progress_callback(msg=progress_message)

    kb_id = DocumentService.get_by_doc_id(db, task_doc_id)["kb_id"]
    kb = KnowledgebaseService.get_by_id(db, kb_id)
    init_kb(task, kb.name)

    chunk_count = len(set([chunk["pk"] for chunk in chunks]))
    # 记录开始时间
    start_ts = timer()

    # 分批大小，可根据需要自行调整
    milvus_bulk_size = 4

    # 用于记录成功和失败的插入信息
    successful_inserts = []
    failed_inserts = []

    # 获取集合 schema，用于做数据类型转换
    schema = get_schema(search.index_name_one(task_tenant_id, kb.name))
    collection_name = search.index_name_one(task_tenant_id, kb.name)
    # 循环分批插入
    for b in range(0, chunk_count, milvus_bulk_size):
        # 取出本批次要插入的 chunks
        chunk_batch = chunks[b: b + milvus_bulk_size]

        # 将本批次内的数据先做类型转换
        converted_batch = []
        for chunk in chunk_batch:
            converted_chunk = convert_data_types(chunk, schema)
            converted_batch.append(converted_chunk)

        doc_store_result = {}
        try:
            # 调用你自定义的 MILVUS_CONNECTION.insert 方法
            doc_store_result = MILVUS_CONNECTION.insert(
                collection_name=collection_name,
                data=converted_batch
            )
            # 由于异常会在内部抛出，这里若能执行到此处，说明插入已成功
            # doc_store_result 形如：{"insert_count": x, "ids": [...]}

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
                if MILVUS_CONNECTION.has_collection(collection_name):
                    # 删除本批次已经尝试插入的记录（这里按 doc_id 删除，可根据业务实际情况调整 filter 条件）
                    for chunk in chunk_batch:
                        if "doc_id" in chunk:
                            MILVUS_CONNECTION.delete(
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

        # 每插入 128 批，做一次进度回调（可自定义触发频率）
        if b % 128 == 0:
            progress = 0.8 + 0.1 * (b + 1) / chunk_count
            progress_callback(prog=progress, msg="")

        # 拼接本批次 chunk_ids 并更新到 TaskService
        # （需要你确保 chunk 内有 "id" 这个字段）
        chunk_ids = [chunk["id"] for chunk in chunk_batch if "id" in chunk]
        chunk_ids_str = " ".join(chunk_ids)
        try:
            TaskService.update_chunk_ids(db, task["id"], chunk_ids_str)
        except NoResultFound:
            logging.warning(
                f"do_handle_task update_chunk_ids failed since task {task['id']} is unknown."
            )
            # 如果 TaskService 中没有这个 task，则删除已插入数据并退出
            try:
                if MILVUS_CONNECTION.has_collection(collection_name):
                    for chunk in chunk_batch:
                        if "doc_id" in chunk:
                            MILVUS_CONNECTION.delete(
                                collection_name=collection_name,
                                filter=f"doc_id == '{chunk['doc_id']}'"
                            )
            except MilvusException as e:
                return e
            return

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
            if MILVUS_CONNECTION.has_collection(collection_name):
                MILVUS_CONNECTION.delete(
                    collection_name=collection_name,
                    filter=f"doc_id == '{task_doc_id}'"
                )
        except MilvusException as e:
            return e
        return

    # 最后更新统计信息
    DocumentService.increment_chunk_num(
        db,
        task_doc_id,
        task_dataset_id,
        token_count,
        chunk_count,
        0
    )

    # 做一次进度回调
    time_cost = timer() - start_ts
    progress_callback(prog=1.0, msg=f"Done ({time_cost:.2f}s)")
    logging.info(
        "Chunk doc(%s), token(%s), chunks(%s), elapsed:%.2f",
        task_id, token_count, len(chunks), time_cost
    )
    # start_ts = timer()
    # doc_store_result = ""
    # successful_inserts = []  # 用于记录成功插入的记录信息
    # failed_inserts = []  # 可选：记录失败的记录（便于排查问题）
    # # 获取集合的schema
    # schema = get_schema(search.index_name_one(task_tenant_id, kb.name))
    #
    # # 逐条插入数据
    # for chunk in chunks:
    #     # 转换数据类型
    #     converted_chunk = convert_data_types(chunk, schema)
    #
    #     try:
    #         # 使用 Milvus 的插入方法插入数据
    #         doc_store_result = MILVUS_CONNECTION.insert(
    #             collection_name=search.index_name_one(task_tenant_id, kb.name),
    #             data=converted_chunk
    #         )
    #         successful_inserts.append(doc_store_result)  # 记录成功的插入结果
    #     except Exception:
    #         failed_inserts.append(chunk)  # 记录失败的记录
    #         progress_callback(-1, f"Insert chunk error, detail info please check log file. Please also check Milvus status!")
    #         collection_name = search.index_name_one(task_tenant_id, kb.name)
    #         try:
    #             if MILVUS_CONNECTION.has_collection(collection_name):
    #                 MILVUS_CONNECTION.delete(
    #                     collection_name=collection_name,
    #                     filter=f"doc_id == '{{doc_id}}'".format(doc_id=task["doc_id"])
    #                 )
    #         except MilvusException as e:
    #             return e
    #         logging.exception("Insert error:")
    #         logging.error("Data being inserted:", converted_chunk)
    # # 结束时记录总耗时
    # insertion_total_time = timer() - start_ts
    #
    # # 输出总的插入成功信息和统计
    # if successful_inserts:
    #     total_insert_count = sum(item["insert_count"] for item in successful_inserts)
    #     logging.info(f"Total successful inserts into Milvus's {search.index_name_one(task_tenant_id, kb.name)}: {total_insert_count} ")
    #     # logging.info(f"Milvus insert details: {successful_inserts}")
    #
    # # 输出总的 Insertion elapsed 时长
    # logging.info(f"Total Insertion elapsed: {insertion_total_time:.2f}")
    #
    # if failed_inserts:
    #     logging.warning(f"Failed inserts count: {len(failed_inserts)}")
    #     logging.warning(f"Failed insert records: {failed_inserts}")
    #
    # if TaskService.do_cancel(db, task_id):
    #     # 构建 Milvus 集合名称
    #     collection_name = search.index_name_one(task_tenant_id, kb.name)
    #     # 检查集合是否存在并删除 Milvus 中的数据
    #     try:
    #         if MILVUS_CONNECTION.has_collection(collection_name):
    #             MILVUS_CONNECTION.delete(
    #                 collection_name=collection_name,
    #                 filter=f"doc_id == '{{doc_id}}'".format(doc_id=task_doc_id)
    #             )
    #     except MilvusException as e:
    #         return e
    #     return
    #
    # DocumentService.increment_chunk_num(db, task_doc_id, task_dataset_id, token_count, chunk_count, 0)
    #
    # time_cost = timer() - start_ts
    # progress_callback(prog=1.0, msg="Done ({:.2f}s)".format(time_cost))
    # logging.info("Chunk doc({}), token({}), chunks({}), elapsed:{:.2f}".format(task_id, token_count, len(chunks), time_cost))


def handle_task():
    global PAYLOAD, mt_lock, DONE_TASKS, FAILED_TASKS, CURRENT_TASK
    with SessionLocal() as db:
        task_dict = None  # 确保变量初始化
        try:
            task = collect(db)
            if task:
                try:
                    # 转换为可序列化的字典
                    if hasattr(task, "_asdict"):  # 检查是否为 RowProxy
                        task_dict = task._asdict()
                    elif isinstance(task, dict):  # 如果已经是字典
                        task_dict = task
                    else:
                        task_dict = {key: str(value) for key, value in vars(task).items()}  # 通用对象转换为字典
                    logging.info(f"handle_task begin for task {json.dumps(task_dict)}")
                    with mt_lock:
                        CURRENT_TASK = copy.deepcopy(task)
                    do_handle_task(db, task)
                    with mt_lock:
                        DONE_TASKS += 1
                        CURRENT_TASK = None
                    logging.info(f"handle_task done for task {json.dumps(task)}")
                except TaskCanceledException:
                    with mt_lock:
                        DONE_TASKS += 1
                        CURRENT_TASK = None
                    try:
                        set_progress(db, task["id"], prog=-1, msg="handle_task got TaskCanceledException")
                    except Exception:
                        pass
                    logging.debug("handle_task got TaskCanceledException", exc_info=True)
                except Exception as e:
                    with mt_lock:
                        FAILED_TASKS += 1
                        CURRENT_TASK = None
                    try:
                        set_progress(db, task["id"], prog=-1, msg=f"[Exception]: {e}")
                    except Exception:
                        pass
                    logging.exception(f"handle_task got exception for task {json.dumps(task)}")
            if PAYLOAD:
                PAYLOAD.ack()
                PAYLOAD = None
        except Exception:
            logging.exception(f"Error in main loop")
            db.rollback()  # 回滚事务
            raise
        else:
            db.commit()  # 提交事务


def report_status():
    global CONSUMER_NAME, BOOT_AT, PENDING_TASKS, LAG_TASKS, mt_lock, DONE_TASKS, FAILED_TASKS, CURRENT_TASK
    REDIS_CONN.sadd("TASKEXE", CONSUMER_NAME)
    while True:
        try:
            now = datetime.now()
            group_info = REDIS_CONN.queue_info(SVR_QUEUE_NAME, "multi_rag_svr_task_broker")
            if group_info is not None:
                PENDING_TASKS = int(group_info.get("pending", 0))
                LAG_TASKS = int(group_info.get("lag", 0))

            with mt_lock:
                heartbeat = json.dumps({
                    "name": CONSUMER_NAME,
                    "now": now.astimezone().isoformat(timespec="milliseconds"),
                    "boot_at": BOOT_AT,
                    "pending": PENDING_TASKS,
                    "lag": LAG_TASKS,
                    "done": DONE_TASKS,
                    "failed": FAILED_TASKS,
                    "current": CURRENT_TASK,
                })
            REDIS_CONN.zadd(CONSUMER_NAME, heartbeat, now.timestamp())
            logging.info(f"{CONSUMER_NAME} reported heartbeat: {heartbeat}")

            expired = REDIS_CONN.zcount(CONSUMER_NAME, 0, now.timestamp() - 60 * 30)
            if expired > 0:
                REDIS_CONN.zpopmin(CONSUMER_NAME, expired)
        except Exception:
            logging.exception("report_status got exception")
        time.sleep(30)



def analyze_heap(snapshot1: tracemalloc.Snapshot, snapshot2: tracemalloc.Snapshot, snapshot_id: int, dump_full: bool):
    msg = ""
    if dump_full:
        stats2 = snapshot2.statistics('lineno')
        msg += f"{CONSUMER_NAME} memory usage of snapshot {snapshot_id}:\n"
        for stat in stats2[:10]:
            msg += f"{stat}\n"
    stats1_vs_2 = snapshot2.compare_to(snapshot1, 'lineno')
    msg += f"{CONSUMER_NAME} memory usage increase from snapshot {snapshot_id - 1} to snapshot {snapshot_id}:\n"
    for stat in stats1_vs_2[:10]:
        msg += f"{stat}\n"
    msg += f"{CONSUMER_NAME} detailed traceback for the top memory consumers:\n"
    for stat in stats1_vs_2[:3]:
        msg += '\n'.join(stat.traceback.format())
    logging.info(msg)


def main():
#     logging.info(r"""
# ┌─────────────────────────── Task Starting ────────────────────────────┐
# │   ______           __      ______                     __             │
# │  /_  __/___ ______/ /__   / ____/  _____  _______  __/ /_____  _____ │
# │   / / / __ `/ ___/ //_/  / __/ | |/_/ _ \/ ___/ / / / __/ __ \/ ___/ │
# │  / / / /_/ (__  ) ,<    / /____>  </  __/ /__/ /_/ / /_/ /_/ / /     │
# │ /_/  \__,_/____/_/|_|  /_____/_/|_|\___/\___/\__,_/\__/\____/_/      │
# │                                                                      │
# └──────────────────────────── LOG Showing ─────────────────────────────┘
#         """)
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
    print_multirag_settings()
    background_thread = threading.Thread(target=report_status)
    background_thread.daemon = True
    background_thread.start()

    TRACE_MALLOC_DELTA = int(os.environ.get('TRACE_MALLOC_DELTA', "0"))
    TRACE_MALLOC_FULL = int(os.environ.get('TRACE_MALLOC_FULL', "0"))
    if TRACE_MALLOC_DELTA > 0:
        if TRACE_MALLOC_FULL < TRACE_MALLOC_DELTA:
            TRACE_MALLOC_FULL = TRACE_MALLOC_DELTA
        tracemalloc.start()
        snapshot1 = tracemalloc.take_snapshot()
    while True:
        handle_task()
        num_tasks = DONE_TASKS + FAILED_TASKS
        if TRACE_MALLOC_DELTA> 0 and num_tasks > 0 and num_tasks % TRACE_MALLOC_DELTA == 0:
            snapshot2 = tracemalloc.take_snapshot()
            analyze_heap(snapshot1, snapshot2, int(num_tasks/TRACE_MALLOC_DELTA), num_tasks % TRACE_MALLOC_FULL == 0)
            snapshot1 = snapshot2
            snapshot2 = None


if __name__ == "__main__":
    main()
