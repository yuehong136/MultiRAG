import logging
import inspect
from api.utils.log_utils import initRootLogger
initRootLogger(inspect.getfile(inspect.currentframe()))
for module in ["pdfminer"]:
    module_logger = logging.getLogger(module)
    module_logger.setLevel(logging.WARNING)
for module in ["sqlalchemy"]:
    module_logger = logging.getLogger(module)
    module_logger.handlers.clear()
    module_logger.propagate = True
import datetime
import json
import os
import hashlib
import copy
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from io import BytesIO

from pymilvus import MilvusException, DataType
from sqlalchemy.orm import Session

import numpy as np
import pandas as pd
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
from api.settings import retrievaler
from api.utils.file_utils import get_project_base_directory
from core.app import laws, paper, presentation, manual, qa, table, book, resume, picture, naive, one, audio, email, knowledge_graph
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

CONSUMER_NAME = "task_consumer_" + ("0" if len(sys.argv) < 2 else sys.argv[1])
PAYLOAD: Payload | None = None


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
        TaskService.update_progress(db, task_id, d)
    except Exception as e:
        logging.error("set_progress:({}), {}".format(task_id, str(e)))

    # db.close()
    if cancel:
        if PAYLOAD:
            PAYLOAD.ack()
            PAYLOAD = None
        os._exit(0)


def collect(db: Session):
    global CONSUMER_NAME, PAYLOAD
    try:
        PAYLOAD = REDIS_CONN.get_unacked_for(CONSUMER_NAME, SVR_QUEUE_NAME, "multi_rag_svr_task_broker")
        if not PAYLOAD:
            PAYLOAD = REDIS_CONN.queue_consumer(SVR_QUEUE_NAME, "multi_rag_svr_task_broker", CONSUMER_NAME)
        if not PAYLOAD:
            time.sleep(1)
            return pd.DataFrame()
    except Exception as e:
        logging.error("Get task event from queue exception:" + str(e))
        return pd.DataFrame()

    msg = PAYLOAD.get_message()
    if not msg:
        return pd.DataFrame()

    if TaskService.do_cancel(db, msg["id"]):
        logging.info("Task {} has been canceled.".format(msg["id"]))
        return pd.DataFrame()
    tasks = TaskService.get_tasks(db, msg["id"])

    # assert tasks, "{} empty task!".format(msg["id"])
    if not tasks:
        logging.warning("{} empty task!".format(msg["id"]))
        return []

    tasks = pd.DataFrame(tasks)

    if msg.get("type", "") == "raptor":
        tasks["task_type"] = "raptor"
    return tasks


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
        logging.error(
            "Minio {}/{}: Fetch file from minio timeout.".format(row["location"], row["name"]))
        return
    except Exception as e:
        if re.search("(No such file|not found)", str(e)):
            callback(-1, "Can not find file <%s> from minio. Could you try it again?" % row["name"])
        else:
            callback(-1, "Get file from minio: %s" % str(e).replace("'", ""))
        traceback.print_exc()
        return

    try:
        cks = chunker.chunk(row["name"], binary=binary, from_page=row["from_page"],
                            to_page=row["to_page"], lang=row["language"], callback=callback,
                            kb_id=row["kb_id"], parser_config=row["parser_config"], tenant_id=row["tenant_id"])
        logging.info(
            "Chunking({}) {}/{}".format(timer() - st, row["location"], row["name"]))
    except Exception as e:
        callback(-1, "Internal server error while chunking: %s" % str(e).replace("'", ""))
        logging.error(
            "Chunking {}/{}: {}".format(row["location"], row["name"], str(e)))
        traceback.print_exc()
        return

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
        d["create_time"] = str(datetime.datetime.now()).replace("T", " ")[:19]
        d["create_timestamp_flt"] = datetime.datetime.now().timestamp()

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
        except Exception as e:
            logging.error(str(e))
            traceback.print_exc()

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
        callback(msg="Keywords generation completed in {:.2f}s".format(timer()-st))

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
        callback(msg="Question generation completed in {:.2f}s".format(timer()-st))

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
    for d in retrievaler.chunk_list(row["doc_id"], row["tenant_id"], fields=["content_with_weight", vctr_nm]):
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
        d["create_time"] = str(datetime.datetime.now()).replace("T", " ")[:19]
        d["create_timestamp_flt"] = datetime.datetime.now().timestamp()
        d["vector"] = vctr.tolist()
        d["text"] = content
        d["content_ltks"] = rag_tokenizer.tokenize(content)
        d["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(d["content_ltks"])
        res.append(d)
        tk_count += num_tokens_from_string(content)
    return res, tk_count


def main():
    # 使用 with 语句确保事务的正确结束和会话关闭
    with SessionLocal() as db:
        try:
            db = SessionLocal()
            rows = collect(db)
            if len(rows) == 0:
                return
            # 预处理 auth 列，转换为列表，处理 None 值
            def convert_auth(auth_str):
                if auth_str is None:
                    return []  # 如果 auth 为 None，转换为空列表
                try:
                    return json.loads(auth_str) if isinstance(auth_str, str) else auth_str
                except json.JSONDecodeError:
                    logging.error(f"Failed to decode auth field: {auth_str}")
                    return []  # 解析失败时，返回空列表

            rows['auth'] = rows['auth'].apply(convert_auth)

            for _, r in rows.iterrows():
                callback = partial(set_progress, db, r["id"], r["from_page"], r["to_page"])
                try:
                    embd_mdl = LLMBundle(db, r["tenant_id"], LLMType.EMBEDDING, llm_name=r["embd_id"], lang=r["language"])
                except Exception as e:
                    callback(-1, msg=str(e))
                    logging.error(str(e))
                    continue

                if r.get("task_type", "") == "raptor":
                    try:
                        chat_mdl = LLMBundle(db, r["tenant_id"], LLMType.CHAT, llm_name=r["llm_id"], lang=r["language"])
                        cks, tk_count = run_raptor(r, chat_mdl, embd_mdl, callback)
                    except Exception as e:
                        callback(-1, msg=str(e))
                        logging.error(str(e))
                        continue
                else:
                    st = timer()
                    cks = build(r, db)
                    logging.info("Build chunks({}): {}".format(r["name"], timer() - st))
                    if cks is None:
                        continue
                    if not cks:
                        callback(1., "No chunk! Done!")
                        continue
                    # TODO: exception handler
                    ## set_progress(r["did"], -1, "ERROR: ")
                    callback(
                        msg="Finished slicing files ({} chunks in {:.2f}s). Start to embedding the content.".format(
                            len(cks), timer() - st)
                    )
                    st = timer()
                    try:
                        tk_count = embedding(cks, embd_mdl, r["parser_config"], callback)
                    except Exception as e:
                        callback(-1, "Embedding error:{}".format(str(e)))
                        logging.error(str(e))
                        tk_count = 0
                    logging.info("Embedding elapsed({}): {:.2f}".format(r["name"], timer() - st))
                    callback(msg="Finished embedding (in {:.2f}s)! Start to build index!".format(timer() - st))

                kb_id = DocumentService.get_by_doc_id(db, r["doc_id"])["kb_id"]
                kb = KnowledgebaseService.get_by_id(db, kb_id)
                init_kb(r, kb.name)

                chunk_count = len(set([c["pk"] for c in cks]))
                st = timer()
                milvus_r = ""
                # 获取集合的schema
                schema = get_schema(search.index_name_one(r["tenant_id"], kb.name))

                # 逐条插入数据
                for record in cks:
                    # 转换数据类型
                    converted_record = convert_data_types(record, schema)

                    try:
                        # 使用 Milvus 的插入方法插入数据
                        MILVUS_CONNECTION.insert(
                            collection_name=search.index_name_one(r["tenant_id"], kb.name),
                            data=converted_record
                        )
                        logging.info("Successfully inserted record to Milvus")
                    except Exception as e:
                        logging.error("Insert error:", e)
                        logging.error("Data being inserted:", converted_record)
                logging.info("Indexing elapsed({}): {:.2f}".format(r["name"], timer() - st))
                if milvus_r:
                    callback(-1, f"Insert chunk error, detail info please check logs/api/logging.log. Please also check Milvus status!")
                    # 构建 Milvus 集合名称
                    collection_name = search.index_name_one(r["tenant_id"], kb.name)
                    # 检查集合是否存在并删除 Milvus 中的数据
                    try:
                        if MILVUS_CONNECTION.has_collection(collection_name):
                            MILVUS_CONNECTION.delete(
                                collection_name=collection_name,
                                filter=f"doc_id == '{{doc_id}}'".format(doc_id=r["doc_id"])
                            )
                    except MilvusException as e:
                        return e
                    logging.error(str(milvus_r))
                else:
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
                        continue
                    callback(msg="Indexing elapsed in {:.2f}s.".format(timer() - st))
                    callback(1., "Done!")
                    DocumentService.increment_chunk_num(
                        db, r["doc_id"], r["kb_id"], tk_count, chunk_count, 0)
                    logging.info(
                        "Chunk doc({}), token({}), chunks({}), elapsed:{:.2f}".format(
                            r["id"], tk_count, len(cks), timer() - st))
        except Exception as e:
            logging.error(f"Error in main loop: {str(e)}")
            db.rollback()  # 回滚事务
            raise
        else:
            db.commit()  # 提交事务


def report_status():
    global CONSUMER_NAME
    while True:
        try:
            obj = REDIS_CONN.get("TASKEXE")
            if not obj: obj = {}
            else: obj = json.loads(obj)
            if CONSUMER_NAME not in obj: obj[CONSUMER_NAME] = []
            obj[CONSUMER_NAME].append(timer())
            obj[CONSUMER_NAME] = obj[CONSUMER_NAME][-60:]
            REDIS_CONN.set_obj("TASKEXE", obj, 60*2)
        except Exception as e:
            print("[Exception]:", str(e))
        time.sleep(30)


if __name__ == "__main__":
    exe = ThreadPoolExecutor(max_workers=1)
    exe.submit(report_status)

    while True:
        main()
        if PAYLOAD:
            PAYLOAD.ack()
            PAYLOAD = None
