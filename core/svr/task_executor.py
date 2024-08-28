import datetime
import json
import logging
import os
import hashlib
import copy
import re
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from functools import partial

from pymilvus import MilvusException, DataType
from sqlalchemy.orm import Session, session

from api.db.database import SessionLocal
from api.db.services.file2document_service import File2DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.settings import retrievaler
from api.utils.api_utils import construct_json_result
from core.raptor import RecursiveAbstractiveProcessing4TreeOrganizedRetrieval as Raptor
from core.utils.minio_conn import MINIO
from core.settings import database_logger, SVR_QUEUE_NAME
from core.settings import cron_logger, DOC_MAXIMUM_SIZE
from multiprocessing import Pool
import numpy as np
# from elasticsearch_dsl import Q, Search
from multiprocessing.context import TimeoutError
from api.db.services.task_service import TaskService
from core.utils.milvus_conn import MILVUS_CONNECTION
from timeit import default_timer as timer
from core.utils import rmSpace, findMaxTm, num_tokens_from_string

from core.nlp import search, rag_tokenizer
from io import BytesIO
import pandas as pd

from core.app import laws, paper, presentation, manual, qa, table, book, resume, picture, naive, one, audio, email#, knowledge_graph

from api.db import LLMType, ParserType
from api.db.services.document_service import DocumentService
from api.db.services.llm_service import LLMBundle
from api.utils.file_utils import get_project_base_directory
from core.utils.redis_conn import REDIS_CONN

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
    # ParserType.KG.value: knowledge_graph
}


def set_progress(db: Session, task_id, from_page=0, to_page=-1,
                 prog=None, msg="Processing..."):
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
        cron_logger.error("set_progress:({}), {}".format(task_id, str(e)))

    db.close()
    if cancel:
        sys.exit()


def collect(db: Session):
    try:
        payload = REDIS_CONN.queue_consumer(SVR_QUEUE_NAME, "multi_rag_svr_task_broker", "multi_rag_svr_task_consumer")
        if not payload:
            time.sleep(1)
            return pd.DataFrame()
    except Exception as e:
        cron_logger.error("Get task event from queue exception:" + str(e))
        return pd.DataFrame()

    msg = payload.get_message()
    payload.ack()
    if not msg: return pd.DataFrame()

    if TaskService.do_cancel(db, msg["id"]):
        cron_logger.info("Task {} has been canceled.".format(msg["id"]))
        return pd.DataFrame()
    tasks = TaskService.get_tasks(db, msg["id"])

    assert tasks, "{} empty task!".format(msg["id"])

    tasks = pd.DataFrame(tasks)

    if msg.get("type", "") == "raptor":
        tasks["task_type"] = "raptor"
    return tasks


def get_minio_binary(bucket, name):
    return MINIO.get(bucket, name)


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
        bucket, name = File2DocumentService.get_minio_address(db, doc_id=row["doc_id"])
        binary = get_minio_binary(bucket, name)
        cron_logger.info(
            "From minio({}) {}/{}".format(timer() - st, row["location"], row["name"]))
        # cks = chunker.chunk(row["name"], binary=binary, from_page=row["from_page"],
        #                     to_page=row["to_page"], lang=row["language"], callback=callback,
        #                     kb_id=row["kb_id"], parser_config=row["parser_config"], tenant_id=row["tenant_id"])
        # cron_logger.info(
        #     "Chunkking({}) {}/{}".format(timer() - st, row["location"], row["name"]))
    except TimeoutError as e:
        callback(-1, f"Internal server error: Fetch file from minio timeout. Could you try it again.")
        cron_logger.error(
            "Minio {}/{}: Fetch file from minio timeout.".format(row["location"], row["name"]))
        return
    except Exception as e:
        if re.search("(No such file|not found)", str(e)):
            callback(-1, "Can not find file <%s> from minio. Could you try it again?" % row["name"])
        else:
            callback(-1, f"Get file from minio: %s" %
                     str(e).replace("'", ""))
        traceback.print_exc()
        return

    try:
        cks = chunker.chunk(row["name"], binary=binary, from_page=row["from_page"],
                            to_page=row["to_page"], lang=row["language"], callback=callback,
                            kb_id=row["kb_id"], parser_config=row["parser_config"], tenant_id=row["tenant_id"])
        cron_logger.info(
            "Chunking({}) {}/{}".format(timer() - st, row["location"], row["name"]))
    except Exception as e:
        callback(-1, f"Internal server error while chunking: %s" %
                     str(e).replace("'", ""))
        cron_logger.error(
            "Chunking {}/{}: {}".format(row["location"], row["name"], str(e)))
        traceback.print_exc()
        return

    docs = []
    doc = {
        "doc_id": row["doc_id"],
        "kb_id": [str(row["kb_id"])]
    }
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
        output_buffer = BytesIO()
        if isinstance(d["image"], bytes):
            output_buffer = BytesIO(d["image"])
        else:
            d["image"].save(output_buffer, format='JPEG')

        st = timer()
        MINIO.put(row["kb_id"], d["pk"], output_buffer.getvalue())
        el += timer() - st
        d["img_id"] = "{}-{}".format(row["kb_id"], d["pk"])
        del d["image"]
        docs.append(d)
    cron_logger.info("MINIO PUT({}):{}".format(row["name"], el))

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
    # for record in data:
    #     for field in schema['fields']:
    #         field_name = field['name']
    #         field_type = field['type']
    #         if field_name in record:
    #             if field_type == DataType.FLOAT_VECTOR and not isinstance(record[field_name], list):
    #                 record[field_name] = list(record[field_name])
    #             elif field_type == DataType.VARCHAR:
    #                 # 如果是 VARCHAR 类型，并且字段名称是 kb_id，则将其转换为字符串
    #                 if field_name == "kb_id":
    #                     record[field_name] = ','.join(record[field_name]) if isinstance(record[field_name],
    #                                                                                     list) else str(
    #                         record[field_name])
    #                 else:
    #                     record[field_name] = str(record[field_name])
    #             # 添加更多类型转换逻辑
    # return data
# def convert_data_types(data, schema):
#     converted_data = []
#     for record in data:
#         new_record = {}
#         for field in schema['fields']:
#             field_name = field['name']
#             field_type = field['type']
#             if field_name in record:
#                 if field_type == DataType.FLOAT_VECTOR:
#                     new_record[field_name] = record[field_name]
#                 elif field_type == DataType.VARCHAR:
#                     if field_name == "kb_id" and isinstance(record[field_name], list):
#                         new_record[field_name] = ','.join(record[field_name])
#                     else:
#                         new_record[field_name] = str(record[field_name])
#                 elif field_type == DataType.FLOAT:
#                     new_record[field_name] = float(record[field_name])
#                 else:
#                     new_record[field_name] = str(record[field_name])
#             else:
#                 # 对于缺失字段，填充默认值
#                 if field_type == DataType.FLOAT_VECTOR:
#                     new_record[field_name] = [0.0] * field['params']['dim']  # 使用实际维度
#                 elif field_type == DataType.VARCHAR:
#                     new_record[field_name] = ""
#                 elif field_type == DataType.FLOAT:
#                     new_record[field_name] = 0.0
#                 else:
#                     new_record[field_name] = None
#         converted_data.append(new_record)
#     return converted_data

def get_schema(collection_name):
    schema = MILVUS_CONNECTION.describe_collection(collection_name)
    print("Schema of the collection:", schema)
    return schema


def embedding(docs, mdl, parser_config={}, callback=None):
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
    db = SessionLocal()
    rows = collect(db)
    if len(rows) == 0:
        return

    for _, r in rows.iterrows():
        callback = partial(set_progress, db, r["id"], r["from_page"], r["to_page"])
        try:
            embd_mdl = LLMBundle(db, r["tenant_id"], LLMType.EMBEDDING, llm_name=r["embd_id"], lang=r["language"])
        except Exception as e:
            callback(-1, msg=str(e))
            cron_logger.error(str(e))
            continue

        if r.get("task_type", "") == "raptor":
            try:
                chat_mdl = LLMBundle(db, r["tenant_id"], LLMType.CHAT, llm_name=r["llm_id"], lang=r["language"])
                cks, tk_count = run_raptor(r, chat_mdl, embd_mdl, callback)
            except Exception as e:
                callback(-1, msg=str(e))
                cron_logger.error(str(e))
                continue
        else:
            st = timer()
            cks = build(r, db)
            cron_logger.info("Build chunks({}): {}".format(r["name"], timer() - st))
            if cks is None:
                continue
            if not cks:
                callback(1., "No chunk! Done!")
                continue
            # TODO: exception handler
            ## set_progress(r["did"], -1, "ERROR: ")
            callback(
                msg="Finished slicing files(%d). Start to embedding the content." %
                    len(cks))
            st = timer()
            try:
                tk_count = embedding(cks, embd_mdl, r["parser_config"], callback)
            except Exception as e:
                callback(-1, "Embedding error:{}".format(str(e)))
                cron_logger.error(str(e))
                tk_count = 0
            cron_logger.info("Embedding elapsed({}): {:.2f}".format(r["name"], timer() - st))
            callback(msg="Finished embedding({:.2f})! Start to build index!".format(timer() - st))

        kb_id = DocumentService.get_by_doc_id(db, r["doc_id"])["kb_id"]
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        init_kb(r, kb.name)

        chunk_count = len(set([c["pk"] for c in cks]))
        st = timer()
        milvus_r = ""
        milvus_bulk_size = 16

        # 获取集合的schema
        schema = get_schema(search.index_name_one(r["tenant_id"], kb.name))

        # for b in range(0, len(cks), milvus_bulk_size):
        #     # 转换数据类型
        #     converted_data = convert_data_types(cks[b:b + milvus_bulk_size], schema)
        #     # try:
        #     #     MILVUS_CONNECTION.bulk_upsert_to_milvus(search.index_name_one(r["tenant_id"], kb.name),
        #     #                                             converted_data)
        #     # except Exception as e:
        #     #     print("Upsert error:", e)
        #     #     # 打印更多调试信息
        #     #     print("Data being inserted:", converted_data)
        #     #
        #     # if b % 128 == 0:
        #     #     callback(prog=0.8 + 0.1 * (b + 1) / len(cks), msg="")
        #
        #     try:
        #         MILVUS_CONNECTION.upsert(
        #             collection_name=search.index_name_one(r["tenant_id"], kb.name),
        #             data=converted_data
        #         )
        #         print("Successfully upserted records to Milvus")
        #     except Exception as e:
        #         print("Upsert error:", e)
        #         # Print more debug information
        #         print("Data being inserted:", converted_data)
        #
        #     if b % 128 == 0:
        #         callback(prog=0.8 + 0.1 * (b + 1) / len(cks), msg="")
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
                print("Successfully inserted record to Milvus")
            except Exception as e:
                print("Insert error:", e)
                # 打印更多调试信息
                print("Data being inserted:", converted_record)
        cron_logger.info("Indexing elapsed({}): {:.2f}".format(r["name"], timer() - st))
        if milvus_r:
            callback(-1, f"Insert chunk error, detail info please check logs/api/cron_logger.log. Please also check Milvus status!")
            # ELASTICSEARCH.deleteByQuery(
            #     Q("match", doc_id=r["doc_id"]), idxnm=search.index_name(r["tenant_id"], kb.name))
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
            cron_logger.error(str(milvus_r))
        else:
            if TaskService.do_cancel(db, r["id"]):
                # ELASTICSEARCH.deleteByQuery(
                #     Q("match", doc_id=r["doc_id"]), idxnm=search.index_name(r["tenant_id"], kb.name))
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
            callback(1., "Done!")
            DocumentService.increment_chunk_num(
                db, r["doc_id"], r["kb_id"], tk_count, chunk_count, 0)
            cron_logger.info(
                "Chunk doc({}), token({}), chunks({}), elapsed:{:.2f}".format(
                    r["id"], tk_count, len(cks), timer() - st))


def report_status():
    id = "0" if len(sys.argv) < 2 else sys.argv[1]
    while True:
        try:
            obj = REDIS_CONN.get("TASKEXE")
            if not obj: obj = {}
            else: obj = json.load(obj)
            if id not in obj: obj[id] = []
            obj[id].append(timer()*1000)
            obj[id] = obj[id][-60:]
            REDIS_CONN.set_obj("TASKEXE", obj, 60*2)
        except Exception as e:
            print("[Exception]:", str(e))
        time.sleep(60)

if __name__ == "__main__":
    sqlalchemy_logger = logging.getLogger('sqlalchemy')
    sqlalchemy_logger.propagate = False
    sqlalchemy_logger.addHandler(database_logger.handlers[0])
    sqlalchemy_logger.setLevel(database_logger.level)

    # exe = ThreadPoolExecutor(max_workers=1)
    # exe.submit(report_status)

    while True:
        main()
