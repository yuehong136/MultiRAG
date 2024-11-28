import logging
import sys
from api.utils.log_utils import initRootLogger

CONSUMER_NO = "0" if len(sys.argv) < 2 else sys.argv[1]
initRootLogger(f"task_executor_{CONSUMER_NO}")
for module in ["pdfminer"]:
    module_logger = logging.getLogger(module)
    module_logger.setLevel(logging.WARNING)
for module in ["sqlalchemy"]:
    module_logger = logging.getLogger(module)
    module_logger.handlers.clear()
    module_logger.propagate = True
from datetime import datetime
import json
import os
import hashlib
import copy
import re
import sys
import time
# import traceback
import threading
from functools import partial
from io import BytesIO

from pymilvus import MilvusException, DataType
from sqlalchemy.orm import Session

import numpy as np
from multiprocessing.context import TimeoutError
from timeit import default_timer as timer

from api.db.database import SessionLocal
from api.db.services.dialog_service import keyword_extraction, question_proposal
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db import LLMType, ParserType
from api.db.services.document_service import DocumentService
from api.db.services.llm_service import LLMBundle
from api.db.services.task_service import TaskService
from api.db.services.file2document_service import File2DocumentService
from api import settings
from api.utils.file_utils import get_project_base_directory
from core.app import laws, paper, presentation, manual, qa, table, book, resume, picture, naive, one, audio, email, \
    knowledge_graph
from core.nlp import search, rag_tokenizer
from core.raptor import RecursiveAbstractiveProcessing4TreeOrganizedRetrieval as Raptor
from core.settings import SVR_QUEUE_NAME
from core.settings import DOC_MAXIMUM_SIZE
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
BOOT_AT = datetime.now().isoformat()
PENDING_TASKS = 0
LAG_TASKS = 0

mt_lock = threading.Lock()
DONE_TASKS = 0
FAILED_TASKS = 0
CURRENT_TASK = None


def set_progress(db: Session, task_id, from_page=0, to_page=-1, prog=None, msg="Processing..."):
    global PAYLOAD
    if prog is not None and prog < 0:
        msg = "[ERROR]" + msg
    cancel = TaskService.do_cancel(db, task_id)
    if cancel:
        msg += " [Canceled]"
        prog = -1

    if to_page > 0:
        if msg:
            msg = f"Page({from_page + 1}~{to_page + 1}): " + msg
    d = {"progress_msg": msg}
    if prog is not None:
        d["progress"] = prog
    try:
        logging.info(f"set_progress({task_id}), progress: {prog}, progress_msg: {msg}")
        TaskService.update_progress(db, task_id, d)
    except Exception:
        logging.exception(f"set_progress({task_id}) got exception")

    # db.close()
    if cancel:
        if PAYLOAD:
            PAYLOAD.ack()
            PAYLOAD = None
        os._exit(0)


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

    if TaskService.do_cancel(db, msg["id"]):
        with mt_lock:
            DONE_TASKS += 1
        logging.info("Task {} has been canceled.".format(msg["id"]))
        return None
    task = TaskService.get_task(db, msg["id"])

    # assert tasks, "{} empty task!".format(msg["id"])
    if not task:
        with mt_lock:
            DONE_TASKS += 1
        logging.warning("{} empty task!".format(msg["id"]))
        return None

    if msg.get("type", "") == "raptor":
        task["task_type"] = "raptor"
    return task


def get_storage_binary(bucket, name):
    return STORAGE_IMPL.get(bucket, name)


def build(row, db: Session):
    if row["size"] > DOC_MAXIMUM_SIZE:
        set_progress(db, row["id"], prog=-1, msg="File size exceeds( <= %dMb )" %
                                                 (int(DOC_MAXIMUM_SIZE / 1024 / 1024)))
        return []

    callback = partial(
        set_progress,
        db,
        row["id"],
        row["from_page"],
        row["to_page"])
    chunker = FACTORY[row["parser_id"].lower()]
    try:
        st = timer()
        bucket, name = File2DocumentService.get_storage_address(db, doc_id=row["doc_id"])
        binary = get_storage_binary(bucket, name)
        logging.info(
            "From minio({}) {}/{}".format(timer() - st, row["location"], row["name"]))
    except TimeoutError:
        callback(-1, "Internal server error: Fetch file from minio timeout. Could you try it again.")
        logging.exception(
            "Minio {}/{} got timeout: Fetch file from minio timeout.".format(row["location"], row["name"]))
        raise
    except Exception as e:
        if re.search("(No such file|not found)", str(e)):
            callback(-1, "Can not find file <%s> from minio. Could you try it again?" % row["name"])
        else:
            callback(-1, "Get file from minio: %s" % str(e).replace("'", ""))
        logging.exception("Chunking {}/{} got exception".format(row["location"], row["name"]))
        # traceback.print_exc()
        raise

    try:
        cks = chunker.chunk(row["name"], binary=binary, from_page=row["from_page"],
                            to_page=row["to_page"], lang=row["language"], callback=callback,
                            kb_id=row["kb_id"], parser_config=row["parser_config"], tenant_id=row["tenant_id"])
        logging.info(
            "Chunking({}) {}/{}".format(timer() - st, row["location"], row["name"]))
    except Exception as e:
        callback(-1, "Internal server error while chunking: %s" % str(e).replace("'", ""))
        logging.exception("Chunking {}/{} got exception".format(row["location"], row["name"]))
        # traceback.print_exc()
        raise

    docs = []
    doc = {
        "doc_id": row["doc_id"],
        "kb_id": [str(row["kb_id"])]
    }
    # 如果 row["auth"] 有值，则将其添加到 doc 字典中
    if "auth" in row and row["auth"]:
        doc["auth"] = row["auth"]
    el = 0
    for ck in cks:
        d = copy.deepcopy(doc)
        d.update(ck)
        md5 = hashlib.md5()
        md5.update((ck["content_with_weight"] +
                    str(d["doc_id"])).encode("utf-8"))
        d["pk"] = md5.hexdigest()
        d["create_time"] = str(datetime.now()).replace("T", " ")[:19]
        d["create_timestamp_flt"] = datetime.now().timestamp()

        # if row["parser_config"].get("auto_keywords", 0):
        #     chat_mdl = LLMBundle(db, row["tenant_id"], LLMType.CHAT, llm_name=row["llm_id"], lang=row["language"])
        #     d["important_kwd"] = keyword_extraction(chat_mdl, ck["content_with_weight"],
        #                                             row["parser_config"]["auto_keywords"]).split(",")
        #     d["important_tks"] = rag_tokenizer.tokenize(" ".join(d["important_kwd"]))
        #
        # if row["parser_config"].get("auto_questions", 0):
        #     chat_mdl = LLMBundle(db, row["tenant_id"], LLMType.CHAT, llm_name=row["llm_id"], lang=row["language"])
        #     qst = question_proposal(chat_mdl, ck["content_with_weight"], row["parser_config"]["auto_keywords"])
        #     ck["content_with_weight"] = f"Question: \n{qst}\n\nAnswer:\n" + ck["content_with_weight"]
        #     qst = rag_tokenizer.tokenize(qst)
        #     if "content_ltks" in ck:
        #         ck["content_ltks"] += " " + qst
        #     if "content_sm_ltks" in ck:
        #         ck["content_sm_ltks"] += " " + rag_tokenizer.fine_grained_tokenize(qst)

        # 将数组字段转换为 JSON 字符串
        d["page_num_int"] = json.dumps(d.get("page_num_int", []))
        d["position_int"] = json.dumps(d.get("position_int", []))
        d["top_int"] = json.dumps(d.get("top_int", []))
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
            STORAGE_IMPL.put(row["kb_id"], d["pk"], output_buffer.getvalue())
            el += timer() - st
        except Exception:
            logging.exception(
                "Saving image of chunk {}/{}/{} got exception".format(row["location"], row["name"], d["_id"]))
            # traceback.print_exc()
            raise

        d["img_id"] = "{}-{}".format(row["kb_id"], d["pk"])
        del d["image"]
        docs.append(d)
    logging.info("MINIO PUT({}):{}".format(row["name"], el))

    if row["parser_config"].get("auto_keywords", 0):
        st = timer()
        callback(msg="Start to generate keywords for every chunk ...")
        chat_mdl = LLMBundle(db, row["tenant_id"], LLMType.CHAT, llm_name=row["llm_id"], lang=row["language"])
        for d in docs:
            d["important_kwd"] = keyword_extraction(chat_mdl, d["content_with_weight"],
                                                    row["parser_config"]["auto_keywords"]).split(",")
            d["important_tks"] = rag_tokenizer.tokenize(" ".join(d["important_kwd"]))
        callback(msg="Keywords generation completed in {:.2f}s".format(timer() - st))

    if row["parser_config"].get("auto_questions", 0):
        st = timer()
        callback(msg="Start to generate questions for every chunk ...")
        chat_mdl = LLMBundle(db, row["tenant_id"], LLMType.CHAT, llm_name=row["llm_id"], lang=row["language"])
        for d in docs:
            qst = question_proposal(chat_mdl, d["content_with_weight"], row["parser_config"]["auto_questions"])
            d["content_with_weight"] = f"Question: \n{qst}\n\nAnswer:\n" + d["content_with_weight"]
            qst = rag_tokenizer.tokenize(qst)
            if "content_ltks" in d:
                d["content_ltks"] += " " + qst
            if "content_sm_ltks" in d:
                d["content_sm_ltks"] += " " + rag_tokenizer.fine_grained_tokenize(qst)
        callback(msg="Question generation completed in {:.2f}s".format(timer() - st))

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
    batch_size = 32
    tts, cnts = [rmSpace(d["title_tks"]) for d in docs if d.get("title_tks")], [
        re.sub(r"</?(table|td|caption|tr|th)( [^<>]{0,12})?>", " ", d["content_with_weight"]) for d in docs]
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
    raptor(chunks, row["parser_config"]["raptor"]["random_seed"], callback)
    doc = {
        "doc_id": row["doc_id"],
        "kb_id": [str(row["kb_id"])],
        "docnm_kwd": row["name"],
        "title_tks": rag_tokenizer.tokenize(row["name"])
    }
    res = []
    tk_count = 0
    for content, vctr in chunks[original_length:]:
        d = copy.deepcopy(doc)
        md5 = hashlib.md5()
        md5.update((content + str(d["doc_id"])).encode("utf-8"))
        d["pk"] = md5.hexdigest()
        d["create_time"] = str(datetime.now()).replace("T", " ")[:19]
        d["create_timestamp_flt"] = datetime.now().timestamp()
        d["vector"] = vctr.tolist()
        d["text"] = content
        d["content_ltks"] = rag_tokenizer.tokenize(content)
        d["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(d["content_ltks"])
        res.append(d)
        tk_count += num_tokens_from_string(content)
    return res, tk_count


def do_handle_task(db, r):
    # 将 Row 转换为字典，确保可以修改字段
    r = r._asdict() if hasattr(r, "_asdict") else dict(r)

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
    r['auth'] = convert_auth(r.get('auth'))

    callback = partial(set_progress, db, r["id"], r["from_page"], r["to_page"])
    try:
        embd_mdl = LLMBundle(db, r["tenant_id"], LLMType.EMBEDDING, llm_name=r["embd_id"], lang=r["language"])
    except Exception as e:
        callback(-1, msg=str(e))
        raise
    if r.get("task_type", "") == "raptor":
        try:
            chat_mdl = LLMBundle(db, r["tenant_id"], LLMType.CHAT, llm_name=r["llm_id"], lang=r["language"])
            cks, tk_count = run_raptor(r, chat_mdl, embd_mdl, callback)
        except Exception as e:
            callback(-1, msg=str(e))
            raise
    else:
        st = timer()
        cks = build(r, db)
        logging.info("Build chunks({}): {}".format(r["name"], timer() - st))
        if cks is None:
            return
        if not cks:
            callback(1., "No chunk! Done!")
            return
            # TODO: exception handler
            ## set_progress(r["did"], -1, "ERROR: ")
        callback(msg="Finished slicing files ({} chunks in {:.2f}s). Start to embedding the content.".format(len(cks),
                                                                                                             timer() - st))
        st = timer()
        try:
            tk_count = embedding(cks, embd_mdl, r["parser_config"], callback)
        except Exception as e:
            callback(-1, "Embedding error:{}".format(str(e)))
            logging.exception("run_rembedding got exception")
            tk_count = 0
            raise
        logging.info("Embedding elapsed({}): {:.2f}".format(r["name"], timer() - st))
        callback(msg="Finished embedding (in {:.2f}s)! Start to build index!".format(timer() - st))

    kb_id = DocumentService.get_by_doc_id(db, r["doc_id"])["kb_id"]
    kb = KnowledgebaseService.get_by_id(db, kb_id)
    init_kb(r, kb.name)

    chunk_count = len(set([c["pk"] for c in cks]))
    st = timer()
    milvus_r = ""
    successful_inserts = []  # 用于记录成功插入的记录信息
    failed_inserts = []  # 可选：记录失败的记录（便于排查问题）
    # 获取集合的schema
    schema = get_schema(search.index_name_one(r["tenant_id"], kb.name))

    # 逐条插入数据
    for record in cks:
        # 转换数据类型
        converted_record = convert_data_types(record, schema)

        try:
            # 使用 Milvus 的插入方法插入数据
            milvus_r = MILVUS_CONNECTION.insert(
                collection_name=search.index_name_one(r["tenant_id"], kb.name),
                data=converted_record
            )
            successful_inserts.append(milvus_r)  # 记录成功的插入结果
        except Exception:
            failed_inserts.append(record)  # 记录失败的记录
            callback(-1, f"Insert chunk error, detail info please check log file. Please also check Milvus status!")
            collection_name = search.index_name_one(r["tenant_id"], kb.name)
            try:
                if MILVUS_CONNECTION.has_collection(collection_name):
                    MILVUS_CONNECTION.delete(
                        collection_name=collection_name,
                        filter=f"doc_id == '{{doc_id}}'".format(doc_id=r["doc_id"])
                    )
            except MilvusException as e:
                return e
            logging.exception("Insert error:")
            logging.error("Data being inserted:", converted_record)
    # 结束时记录总耗时
    insertion_total_time = timer() -st

    # 输出总的插入成功信息和统计
    if successful_inserts:
        total_insert_count = sum(item["insert_count"] for item in successful_inserts)
        logging.info(f"Total successful inserts into Milvus's {search.index_name_one(r["tenant_id"], kb.name)}: {total_insert_count} ")
        # logging.info(f"Milvus insert details: {successful_inserts}")

    # 输出总的 Insertion elapsed 时长
    logging.info(f"Total Insertion elapsed: {insertion_total_time:.2f}")

    if failed_inserts:
        logging.warning(f"Failed inserts count: {len(failed_inserts)}")
        logging.warning(f"Failed insert records: {failed_inserts}")

    if TaskService.do_cancel(db, r["id"]):
        # 构建 Milvus 集合名称
        collection_name = search.index_name_one(r["tenant_id"], kb.name)
        # 检查集合是否存在并删除 Milvus 中的数据
        try:
            if MILVUS_CONNECTION.has_collection(collection_name):
                MILVUS_CONNECTION.delete(
                    collection_name=collection_name,
                    filter=f"doc_id == '{{doc_id}}'".format(doc_id=r["doc_id"])
                    # filter=f"doc_id == '{doc.id}'"
                )
        except MilvusException as e:
            return e
        return
    callback(msg="Indexing elapsed in {:.2f}s.".format(timer() - st))
    callback(1., "Done!")
    DocumentService.increment_chunk_num(
        db, r["doc_id"], r["kb_id"], tk_count, chunk_count, 0)
    logging.info(
        "Chunk doc({}), token({}), chunks({}), elapsed:{:.2f}".format(
            r["id"], tk_count, len(cks), timer() - st))


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
                        task_dict = {key: str(value) for key, value in vars(
                            task).items()}  # 通用对象转换为字典
                    logging.info(f"handle_task begin for task {json.dumps(task_dict)}")
                    with mt_lock:
                        CURRENT_TASK = copy.deepcopy(task_dict)
                    do_handle_task(db, task)
                    with mt_lock:
                        DONE_TASKS += 1
                        CURRENT_TASK = None
                    logging.info(f"handle_task done for task {json.dumps(task_dict)}")
                except Exception:
                    with mt_lock:
                        FAILED_TASKS += 1
                        CURRENT_TASK = None
                    logging.exception(f"handle_task got exception for task {json.dumps(task_dict)}")
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
            group_info = REDIS_CONN.queue_info(SVR_QUEUE_NAME, "rag_flow_svr_task_broker")
            if group_info is not None:
                PENDING_TASKS = int(group_info["pending"])
                LAG_TASKS = int(group_info["lag"])

            with mt_lock:
                heartbeat = json.dumps({
                    "name": CONSUMER_NAME,
                    "now": now.isoformat(),
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


def main():
    settings.init_settings()
    background_thread = threading.Thread(target=report_status)
    background_thread.daemon = True
    background_thread.start()

    while True:
        handle_task()


if __name__ == "__main__":
    main()
