# coding=utf-8
"""
@project: multirag
@Author：龙
@file： dialog_service.py
@date：2024/7/24 21:00
@desc:
"""
import logging
import binascii
import json
import json_repair
import os
import re
from collections import defaultdict
from copy import deepcopy
from timeit import default_timer as timer
import datetime
from datetime import timedelta
from sqlalchemy import asc
from sqlalchemy.orm import Session

from api.db import LLMType, StatusEnum, ParserType
# from api.db.database import db_connection
from api.db.db_models import Dialog, Conversation, db_connection
from api.db.services.common_service import CommonService
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMService, TenantLLMService, LLMBundle
from api import settings
from api.utils.file_utils import get_project_base_directory
from core.app.resume import forbidden_select_fields4resume
from core.nlp.search import index_name
from core.settings import TAG_FLD
from core.utils import rmSpace, num_tokens_from_string, encoder
from graphrag.utils import get_tags_from_cache, set_tags_to_cache


class DialogService(CommonService):
    model = Dialog

    @classmethod
    def get_list(cls, db: Session, tenant_id,
                 page_number, items_per_page, orderby, desc, id, name):

        query = db.query(cls.model)

        if id:
            query = query.filter(cls.model.id == id)
        if name:
            query = query.filter(cls.model.name == name)

        query = query.filter(
            (cls.model.tenant_id == tenant_id) &
            (cls.model.status == StatusEnum.VALID.value)
        )

        # Order by specified field in ascending or descending order
        order_clause = getattr(cls.model, orderby)
        query = query.order_by(desc(order_clause) if desc else asc(order_clause))

        # Apply pagination
        query = query.offset((page_number - 1) * items_per_page).limit(items_per_page)

        # Fetch results and convert to dictionary format
        results = query.all()
        return [item.__dict__ for item in results]


def message_fit_in(msg, max_length=4000):
    """
    检查消息是否能在给定的最大长度内适配，如果超出，则尝试调整消息内容以适应。

    :param msg: 消息列表，每个元素包含角色和内容。
    :param max_length: 允许的最大长度。
    :return: 调整后的消息长度和消息列表。
    """

    def count():
        """
        计算消息中所有内容的令牌总数。

        :return: 令牌总数。
        """
        nonlocal msg
        tks_cnts = []
        for m in msg:
            tks_cnts.append(
                {"role": m["role"], "count": num_tokens_from_string(m["content"])})
        total = 0
        for m in tks_cnts:
            total += m["count"]
        return total

    c = count()
    if c < max_length:
        return c, msg

    # 优先保留系统消息
    # 筛选出消息列表中所有角色为"system"的消息，以及最后一条消息
    msg_ = [m for m in msg[:-1] if m["role"] == "system"]
    if len(msg) > 1:
        msg_.append(msg[-1])
    msg = msg_

    # 初始化计数器
    c = count()

    # 如果当前消息长度小于最大长度限制，则返回当前消息长度和消息列表
    if c < max_length:
        return c, msg

    # 如果系统消息仍超出长度，尝试截断长消息
    ll = num_tokens_from_string(msg_[0]["content"])
    ll2 = num_tokens_from_string(msg_[-1]["content"])
    if ll / (ll + ll2) > 0.8:
        m = msg_[0]["content"]
        m = encoder.decode(encoder.encode(m)[:max_length - ll2])
        msg[0]["content"] = m
        return max_length, msg

    m = msg_[1]["content"]
    m = encoder.decode(encoder.encode(m)[:max_length - ll2])
    msg[1]["content"] = m
    return max_length, msg


def llm_id2llm_type(llm_id):
    llm_id, _ = TenantLLMService.split_model_name_and_factory(llm_id)
    fnm = os.path.join(get_project_base_directory(), "configs")
    llm_factories = json.load(open(os.path.join(fnm, "llm_factories.json"), "r", encoding="utf-8"))
    for llm_factory in llm_factories["factory_llm_infos"]:
        for llm in llm_factory["llm"]:
            if llm_id == llm["llm_name"]:
                return llm["mdl_type"].strip(",")[-1]


def kb_prompt(kbinfos, max_tokens):
    knowledges = [ck["text"] for ck in kbinfos["chunks"]]
    used_token_count = 0
    chunks_num = 0
    for i, c in enumerate(knowledges):
        used_token_count += num_tokens_from_string(c)
        chunks_num += 1
        if max_tokens * 0.97 < used_token_count:
            knowledges = knowledges[:i]
            break
    with db_connection() as db:
        docs = DocumentService.get_by_ids(db, [ck["doc_id"] for ck in kbinfos["chunks"][:chunks_num]])
        docs = {d.id: d.meta_fields for d in docs}

    doc2chunks = defaultdict(lambda: {"chunks": [], "meta": []})
    for ck in kbinfos["chunks"][:chunks_num]:
        doc2chunks[ck["docnm_kwd"]]["chunks"].append(ck["text"])
        doc2chunks[ck["docnm_kwd"]]["meta"] = docs.get(ck["doc_id"], {})

    knowledges = []
    for nm, cks_meta in doc2chunks.items():
        txt = f"Document: {nm} \n"
        for k,v in cks_meta["meta"].items():
            txt += f"{k}: {v}\n"
        txt += "Relevant fragments as following:\n"
        for i, chunk in enumerate(cks_meta["chunks"], 1):
            txt += f"{i}. {chunk}\n"
        knowledges.append(txt)
    return knowledges


def label_question(db: Session, question, kbs):
    tags = None
    tag_kb_ids = []
    for kb in kbs:
        if kb.parser_config.get("tag_kb_ids"):
            tag_kb_ids.extend(kb.parser_config["tag_kb_ids"])
    if tag_kb_ids:
        all_tags = get_tags_from_cache(tag_kb_ids)
        if not all_tags:
            all_tags = settings.retrievaler.all_tags_in_portion(kb.tenant_id, tag_kb_ids)
            set_tags_to_cache(all_tags, tag_kb_ids)
        else:
            all_tags = json.loads(all_tags)
        tag_kbs = KnowledgebaseService.get_by_ids(db, tag_kb_ids)
        tags = settings.retrievaler.tag_query(question,
                                              list(set([kb.tenant_id for kb in tag_kbs])),
                                              tag_kb_ids,
                                              all_tags,
                                              kb.parser_config.get("topn_tags", 3)
                                              )
    return tags


def chat(dialog, messages, db: Session, stream=True, **kwargs):
    # 确保最后一条消息是用户的消息
    assert messages[-1]["role"] == "user", "The last content of this conversation is not from user."

    chat_start_ts = timer()

    # Get llm model name and model provider name
    llm_id, model_provider = TenantLLMService.split_model_name_and_factory(dialog.llm_id)

    # Get llm model instance by model and provide name
    llm = LLMService.query(db, llm_name=llm_id) if not model_provider else LLMService.query(db, llm_name=llm_id,
                                                                                            fid=model_provider)

    if not llm:
        # Model name is provided by tenant, but not system built-in
        llm = TenantLLMService.query(db, tenant_id=dialog.tenant_id, llm_name=llm_id) if not model_provider else \
            TenantLLMService.query(db, tenant_id=dialog.tenant_id, llm_name=llm_id, llm_factory=model_provider)
        print("TenantLLMService.query result:", llm)
        if not llm:
            # 如果仍然查询不到，则抛出异常
            raise LookupError("LLM(%s) not found" % dialog.llm_id)
        max_tokens = 8192
    else:
        max_tokens = llm[0].max_tokens

    check_llm_ts = timer()

    kbs = KnowledgebaseService.get_by_ids(db, dialog.kb_ids)

    # 提取并去重知识库的嵌入ID
    embedding_list = list(set([kb.embd_id for kb in kbs]))

    kb_names = list([kb.name for kb in kbs])
    print("正在检索的知识库 --> ", kb_names)
    if len(embedding_list) > 1:
        # 如果没有，则返回一条错误消息，指示知识库使用不同的嵌入模型
        yield {"answer": "**ERROR**: Knowledge bases use different embedding models.", "reference": []}
        return {"answer": "**ERROR**: Knowledge bases use different embedding models.", "reference": []}

    retriever = settings.retrievaler

    # 提取用户提出的问题
    questions = [m["content"] for m in messages if m["role"] == "user"]
    filter_exp = kwargs["filter_condition"] if "filter_condition" in kwargs else ""
    attachments = kwargs["doc_ids"].split(",") if "doc_ids" in kwargs else None
    if "doc_ids" in messages[-1]:
        attachments = messages[-1]["doc_ids"]
        for m in messages[:-1]:
            if "doc_ids" in m:
                attachments.extend(m["doc_ids"])

    create_retriever_ts = timer()

    if len(embedding_list) != 0:
        embd_mdl = LLMBundle(db, dialog.tenant_id, LLMType.EMBEDDING, embedding_list[0])
        if not embd_mdl:
            raise LookupError("Embedding model(%s) not found" % embedding_list[0])

    bind_embedding_ts = timer()

    if llm_id2llm_type(dialog.llm_id) == "image2text":
        chat_mdl = LLMBundle(db, dialog.tenant_id, LLMType.IMAGE2TEXT, dialog.llm_id)
    else:
        chat_mdl = LLMBundle(db, dialog.tenant_id, LLMType.CHAT, dialog.llm_id)

    bind_llm_ts = timer()

    # 获取提示配置和字段映射
    prompt_config = dialog.prompt_config
    field_map = KnowledgebaseService.get_field_map(db, dialog.kb_ids)
    tts_mdl = None
    if prompt_config.get("tts"):
        tts_mdl = LLMBundle(db, dialog.tenant_id, LLMType.TTS)
    # 如果字段映射存在，尝试使用SQL检索答案
    # 检查field_map是否为空，如果不为空，则执行以下操作
    if field_map:
        # 使用日志记录器记录使用SQL进行检索的信息
        logging.debug("Use SQL to retrieval:{}".format(questions[-1]))
        # 调用use_sql函数尝试使用SQL查询获取答案
        ans = use_sql(questions[-1], field_map, dialog.tenant_id, kb_names, chat_mdl, prompt_config.get("quote", True))
        # 如果查询到答案，则通过yield返回，并结束函数执行
        if ans:
            yield ans
            return

    # 处理提示配置中的参数，确保必要的参数存在
    # 遍历配置文件中定义的参数，为每个参数检查是否提供了相应的值
    for p in prompt_config["parameters"]:
        # 跳过名为"knowledge"的参数，因为它在这个上下文中不被处理
        if p["key"] == "knowledge":
            continue
        # 如果参数不是可选的，并且没有在kwargs中找到对应的值，抛出KeyError
        if p["key"] not in kwargs and not p["optional"]:
            raise KeyError("Miss parameter: " + p["key"])
        # 如果参数是可选的，并且没有提供值，将配置中的占位符替换为空格
        if p["key"] not in kwargs:
            prompt_config["system"] = prompt_config["system"].replace(
                "{%s}" % p["key"], " ")

    if len(questions) > 1 and prompt_config.get("refine_multiturn"):
        questions = [full_question(db, dialog.tenant_id, dialog.llm_id, messages)]
    else:
        questions = questions[-1:]

    refine_question_ts = timer()

    # 如果存在重新排序模型，初始化重新排序模型
    rerank_mdl = None
    if dialog.rerank_id:
        rerank_mdl = LLMBundle(db, dialog.tenant_id, LLMType.RERANK, dialog.rerank_id)

    # 添加问题以确保长度足够
    # 根据问题列表的长度，复制最后一个问题，目的是为了后续的知识抽取和问答融合
    bind_reranker_ts = timer()
    generate_keyword_ts = bind_reranker_ts

    # 检查prompt_config中是否包含"knowledge"参数，以决定是否进行知识检索
    if "knowledge" not in [p["key"] for p in prompt_config["parameters"]]:
        # 如果不包含，则初始化知识信息为一个空的字典
        kbinfos = {"total": 0, "chunks": [], "doc_aggs": []}
    else:
        # 如果包含"knowledge"参数，且设置了关键字提取，那么对最后一个问题进行关键字提取
        if prompt_config.get("keyword", False):
            questions[-1] += keyword_extraction(chat_mdl, questions[-1])
            generate_keyword_ts = timer()

        kbinfos = retriever.retrieval(" ".join(questions), filter_exp, embd_mdl, dialog.tenant_id, kb_names, 1,
                                      dialog.top_n,
                                      dialog.similarity_threshold,
                                      dialog.vector_similarity_weight,
                                      doc_ids=attachments,
                                      top=1024, aggs=False, rerank_mdl=rerank_mdl,
                                      rank_feature=label_question(db, " ".join(questions), kbs)
                                      )

        if prompt_config.get("use_kg"):
            ck = settings.kg_retrievaler.retrieval(" ".join(questions),
                                              dialog.tenant_id,
                                              kb_names,
                                              embd_mdl,
                                              LLMBundle(db, dialog.tenant_id, LLMType.CHAT))
            if ck["content_with_weight"]:
                kbinfos["chunks"].insert(0, ck)

    retrieval_ts = timer()

    knowledges = kb_prompt(kbinfos, max_tokens)

    # # 如果需要自我检索并且内容不相关，尝试重写问题
    # if dialog.prompt_config.get("self_rag") and not relevant(dialog.tenant_id, dialog.llm_id, questions[-1],
    #                                                          knowledges, db):
    #     questions[-1] = rewrite(dialog.tenant_id, dialog.llm_id, questions[-1], db)
    #     kbinfos = retrievaler.retrieval(" ".join(questions), filter_exp, embd_mdl, dialog.tenant_id, kb_names, 1, dialog.top_n,
    #                                     dialog.similarity_threshold,
    #                                     dialog.vector_similarity_weight,
    #                                     doc_ids=attachments,
    #                                     top=1024, aggs=False, rerank_mdl=rerank_mdl)
    #     knowledges = [ck["text"] for ck in kbinfos["chunks"]]
    logging.debug(
        "{}->{}".format(" ".join(questions), "\n->".join(knowledges)))

    # 如果没有知识并且配置了空响应，返回空响应
    if not knowledges and prompt_config.get("empty_response"):
        empty_res = prompt_config["empty_response"]
        yield {"answer": empty_res, "reference": kbinfos, "audio_binary": tts(tts_mdl, empty_res)}
        return {"answer": prompt_config["empty_response"], "reference": kbinfos}

    kwargs["knowledge"] = "\n------\n" + "\n\n------\n\n".join(knowledges)
    gen_conf = dialog.llm_setting

    # 拼接系统提示和消息内容
    # 初始化消息列表，包含系统消息
    msg = [{"role": "system", "content": prompt_config["system"].format(**kwargs)}]

    # 将非系统消息添加到消息列表中
    msg.extend([{"role": m["role"], "content": re.sub(r"##\d+\$\$", "", m["content"])}
                for m in messages if m["role"] != "system"])
    # 确保消息内容不超过LLM的最大token数
    # 调用message_fit_in函数，检查消息是否能在给定的最大令牌数限制内适配
    # 使用最大令牌数的97%作为参考，以确保消息尽可能接近限制而不超过
    used_token_count, msg = message_fit_in(msg, int(max_tokens * 0.97))

    # 断言消息长度至少为2，以验证message_fit_in函数的正确性
    # 如果消息长度小于2，说明函数可能存在bug，需要进行调试
    assert len(msg) >= 2, f"message_fit_in has bug: {msg}"
    prompt = msg[0]["content"]
    prompt += "\n\n### Query:\n%s" % " ".join(questions)

    # 调整生成配置中的最大token数
    if "max_tokens" in gen_conf:
        gen_conf["max_tokens"] = min(
            gen_conf["max_tokens"],
            max_tokens - used_token_count)

    def decorate_answer(answer):
        nonlocal prompt_config, knowledges, kwargs, kbinfos, prompt, retrieval_ts

        finish_chat_ts = timer()

        refs = []
        # 如果需要插入引用文献，处理回答内容
        if knowledges and (prompt_config.get("quote", True) and kwargs.get("quote", True)):
            answer, idx = retriever.insert_citations(answer,
                                                     [ck["content_ltks"] for ck in kbinfos["chunks"]],
                                                     [ck["vector"] for ck in kbinfos["chunks"]],
                                                     embd_mdl,
                                                     tkweight=1 - dialog.vector_similarity_weight,
                                                     vtweight=dialog.vector_similarity_weight)
            idx = set([kbinfos["chunks"][int(i)]["doc_id"] for i in idx])
            recall_docs = [d for d in kbinfos["doc_aggs"] if d["doc_id"] in idx]
            if not recall_docs:
                recall_docs = kbinfos["doc_aggs"]
            kbinfos["doc_aggs"] = recall_docs

            # 删除引用文献中的向量信息
            refs = deepcopy(kbinfos)
            for c in refs["chunks"]:
                if c.get("vector"):
                    del c["vector"]

        # 如果回答中包含无效API key的提示，添加设置API key的提示
        if answer.lower().find("invalid key") >= 0 or answer.lower().find("invalid api") >= 0:
            answer += " Please set LLM API-Key in 'User Setting -> Model providers -> API-Key'"
        finish_chat_ts = timer()

        total_time_cost = (finish_chat_ts - chat_start_ts) * 1000
        check_llm_time_cost = (check_llm_ts - chat_start_ts) * 1000
        create_retriever_time_cost = (create_retriever_ts - check_llm_ts) * 1000
        bind_embedding_time_cost = (bind_embedding_ts - create_retriever_ts) * 1000
        bind_llm_time_cost = (bind_llm_ts - bind_embedding_ts) * 1000
        refine_question_time_cost = (refine_question_ts - bind_llm_ts) * 1000
        bind_reranker_time_cost = (bind_reranker_ts - refine_question_ts) * 1000
        generate_keyword_time_cost = (generate_keyword_ts - bind_reranker_ts) * 1000
        retrieval_time_cost = (retrieval_ts - generate_keyword_ts) * 1000
        generate_result_time_cost = (finish_chat_ts - retrieval_ts) * 1000

        prompt = f"{prompt}\n\n - Total: {total_time_cost:.1f}ms\n  - Check LLM: {check_llm_time_cost:.1f}ms\n  - Create retriever: {create_retriever_time_cost:.1f}ms\n  - Bind embedding: {bind_embedding_time_cost:.1f}ms\n  - Bind LLM: {bind_llm_time_cost:.1f}ms\n  - Tune question: {refine_question_time_cost:.1f}ms\n  - Bind reranker: {bind_reranker_time_cost:.1f}ms\n  - Generate keyword: {generate_keyword_time_cost:.1f}ms\n  - Retrieval: {retrieval_time_cost:.1f}ms\n  - Generate answer: {generate_result_time_cost:.1f}ms"
        return {"answer": answer, "reference": refs, "prompt": re.sub(r"\n", "  \n", prompt)}

    if stream:
        last_ans = ""
        answer = ""
        for ans in chat_mdl.chat_streamly(prompt, msg[1:], gen_conf):
            answer = ans
            delta_ans = ans[len(last_ans):]
            if num_tokens_from_string(delta_ans) < 16:
                continue
            last_ans = answer
            yield {"answer": answer, "reference": {}, "audio_binary": tts(tts_mdl, delta_ans)}
        delta_ans = answer[len(last_ans):]
        if delta_ans:
            yield {"answer": answer, "reference": {}, "audio_binary": tts(tts_mdl, delta_ans)}
        yield decorate_answer(answer)
    else:
        answer = chat_mdl.chat(prompt, msg[1:], gen_conf)
        user_content = msg[-1].get("content", "[content not available]")
        logging.debug("User: {}|Assistant: {}".format(user_content, answer))
        res = decorate_answer(answer)
        res["audio_binary"] = tts(tts_mdl, answer)
        yield res


def use_sql(question, field_map, tenant_id, kb_names, chat_mdl, quota=True):
    sys_prompt = "You are a Database Administrator. You need to check the fields of the following tables based on the user's list of questions and write the SQL corresponding to the last question."
    user_prompt = """
    Table name: {};
    Table of database fields are as follows:
    {}

    Question are as follows:
    {}
    Please write the SQL, only SQL, without any other explanations or text.
    """.format(
        index_name(tenant_id, kb_names),
        "\n".join([f"{k}: {v}" for k, v in field_map.items()]),
        question
    )
    tried_times = 0

    def get_table():
        nonlocal sys_prompt, user_prompt, question, tried_times
        sql = chat_mdl.chat(sys_prompt, [{"role": "user", "content": user_prompt}], {"temperature": 0.06})
        logging.debug(f"{question} ==> {user_prompt} get SQL: {sql}")
        sql = re.sub(r"[\r\n]+", " ", sql.lower())
        sql = re.sub(r".*select ", "select ", sql.lower())
        sql = re.sub(r" +", " ", sql)
        sql = re.sub(r"([;；]|```).*", "", sql)
        if sql[:len("select ")] != "select ":
            return None, None
        if not re.search(r"((sum|avg|max|min)\(|group by )", sql.lower()):
            if sql[:len("select *")] != "select *":
                sql = "select doc_id,docnm_kwd," + sql[6:]
            else:
                flds = []
                for k in field_map.keys():
                    if k in forbidden_select_fields4resume:
                        continue
                    if len(flds) > 11:
                        break
                    flds.append(k)
                sql = "select doc_id,docnm_kwd," + ",".join(flds) + sql[8:]

        print(f"“{question}” get SQL(refined): {sql}")

        logging.debug(f"{question} get SQL(refined): {sql}")
        tried_times += 1
        return settings.retrievaler.sql_retrieval(sql, format="json"), sql

    tbl, sql = get_table()
    if tbl is None:
        return None
    if tbl.get("error") and tried_times <= 2:
        user_prompt = """
        Table name: {};
        Table of database fields are as follows:
        {}

        Question are as follows:
        {}
        Please write the SQL, only SQL, without any other explanations or text.


        The SQL error you provided last time is as follows:
        {}

        Error issued by database as follows:
        {}

        Please correct the error and write SQL again, only SQL, without any other explanations or text.
        """.format(
            index_name(tenant_id, kb_names),
            "\n".join([f"{k}: {v}" for k, v in field_map.items()]),
            question, sql, tbl["error"]
        )
        tbl, sql = get_table()
        logging.debug("TRY it again: {}".format(sql))

    logging.debug("GET table: {}".format(tbl))
    print(tbl)
    if tbl.get("error") or len(tbl["rows"]) == 0:
        return None

    docid_idx = set([ii for ii, c in enumerate(
        tbl["columns"]) if c["name"] == "doc_id"])
    doc_name_idx = set([ii for ii, c in enumerate(
        tbl["columns"]) if c["name"] == "docnm_kwd"])
    column_idx = [ii for ii in range(
        len(tbl["columns"])) if ii not in (docid_idx | doc_name_idx)]

    # compose Markdown table
    columns = "|" + "|".join([re.sub(r"(/.*|（[^（）]+）)", "", field_map.get(tbl["columns"][i]["name"],
                                                                          tbl["columns"][i]["name"])) for i in
                              column_idx]) + ("|Source|" if docid_idx and docid_idx else "|")

    line = "|" + "|".join(["------" for _ in range(len(column_idx))]) + \
           ("|------|" if docid_idx and docid_idx else "")

    rows = ["|" +
            "|".join([rmSpace(str(r[i])) for i in column_idx]).replace("None", " ") +
            "|" for r in tbl["rows"]]
    rows = [r for r in rows if re.sub(r"[ |]+", "", r)]
    if quota:
        rows = "\n".join([r + f" ##{ii}$$ |" for ii, r in enumerate(rows)])
    else:
        rows = "\n".join([r + f" ##{ii}$$ |" for ii, r in enumerate(rows)])
    rows = re.sub(r"T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+Z)?\|", "|", rows)

    if not docid_idx or not doc_name_idx:
        logging.warning("SQL missing field: " + sql)
        return {
            "answer": "\n".join([columns, line, rows]),
            "reference": {"chunks": [], "doc_aggs": []},
            "prompt": sys_prompt
        }

    docid_idx = list(docid_idx)[0]
    doc_name_idx = list(doc_name_idx)[0]
    doc_aggs = {}
    for r in tbl["rows"]:
        if r[docid_idx] not in doc_aggs:
            doc_aggs[r[docid_idx]] = {"doc_name": r[doc_name_idx], "count": 0}
        doc_aggs[r[docid_idx]]["count"] += 1
    return {
        "answer": "\n".join([columns, line, rows]),
        "reference": {"chunks": [{"doc_id": r[docid_idx], "docnm_kwd": r[doc_name_idx]} for r in tbl["rows"]],
                      "doc_aggs": [{"doc_id": did, "doc_name": d["doc_name"], "count": d["count"]} for did, d in
                                   doc_aggs.items()]},
        "prompt": sys_prompt
    }


def relevant(tenant_id, llm_id, question, contents: list, db: Session):
    if llm_id2llm_type(llm_id) == "image2text":
        chat_mdl = LLMBundle(db, tenant_id, LLMType.IMAGE2TEXT, llm_id)
    else:
        chat_mdl = LLMBundle(db, tenant_id, LLMType.CHAT, llm_id)
    prompt = """
        You are a grader assessing relevance of a retrieved document to a user question. 
        It does not need to be a stringent test. The goal is to filter out erroneous retrievals.
        If the document contains keyword(s) or semantic meaning related to the user question, grade it as relevant. 
        Give a binary score 'yes' or 'no' score to indicate whether the document is relevant to the question.
        No other words needed except 'yes' or 'no'.
    """
    if not contents:
        return False
    contents = "Documents: \n" + "   - ".join(contents)
    contents = f"Question: {question}\n" + contents
    if num_tokens_from_string(contents) >= chat_mdl.max_length - 4:
        contents = encoder.decode(encoder.encode(contents)[:chat_mdl.max_length - 4])
    ans = chat_mdl.chat(prompt, [{"role": "user", "content": contents}], {"temperature": 0.01})
    if ans.lower().find("yes") >= 0:
        return True
    return False


def rewrite(tenant_id, llm_id, question, db: Session):
    if llm_id2llm_type(llm_id) == "image2text":
        chat_mdl = LLMBundle(db, tenant_id, LLMType.IMAGE2TEXT, llm_id)
    else:
        chat_mdl = LLMBundle(db, tenant_id, LLMType.CHAT, llm_id)
    prompt = """
        You are an expert at query expansion to generate a paraphrasing of a question.
        I can't retrieval relevant information from the knowledge base by using user's question directly.     
        You need to expand or paraphrase user's question by multiple ways such as using synonyms words/phrase, 
        writing the abbreviation in its entirety, adding some extra descriptions or explanations, 
        changing the way of expression, translating the original question into another language (English/Chinese), etc. 
        And return 5 versions of question and one is from translation.
        Just list the question. No other words are needed.
    """
    ans = chat_mdl.chat(prompt, [{"role": "user", "content": question}], {"temperature": 0.8})
    return ans


def keyword_extraction(chat_mdl, content, topn=3):
    prompt = f"""
Role: You're a text analyzer. 
Task: extract the most important keywords/phrases of a given piece of text content.
Requirements: 
  - Summarize the text content, and give top {topn} important keywords/phrases.
  - The keywords MUST be in language of the given piece of text content.
  - The keywords are delimited by ENGLISH COMMA.
  - Keywords ONLY in output.

### Text Content 
{content}

"""
    msg = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Output: "}
    ]
    _, msg = message_fit_in(msg, chat_mdl.max_length)
    kwd = chat_mdl.chat(prompt, msg[1:], {"temperature": 0.2})
    if isinstance(kwd, tuple):
        kwd = kwd[0]
    kwd = re.sub(r"<think>.*</think>", "", kwd)
    if kwd.find("**ERROR**") >= 0:
        return ""
    return kwd


def question_proposal(chat_mdl, content, topn=3):
    prompt = f"""
Role: You're a text analyzer. 
Task:  propose {topn} questions about a given piece of text content.
Requirements: 
  - Understand and summarize the text content, and propose top {topn} important questions.
  - The questions SHOULD NOT have overlapping meanings.
  - The questions SHOULD cover the main content of the text as much as possible.
  - The questions MUST be in language of the given piece of text content.
  - One question per line.
  - Question ONLY in output.

### Text Content 
{content}

"""
    msg = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Output: "}
    ]
    _, msg = message_fit_in(msg, chat_mdl.max_length)
    kwd = chat_mdl.chat(prompt, msg[1:], {"temperature": 0.2})
    if isinstance(kwd, tuple):
        kwd = kwd[0]
    kwd = re.sub(r"<think>.*</think>", "", kwd)
    if kwd.find("**ERROR**") >= 0:
        return ""
    return kwd


def full_question(db: Session, tenant_id, llm_id, messages):
    if llm_id2llm_type(llm_id) == "image2text":
        chat_mdl = LLMBundle(db, tenant_id, LLMType.IMAGE2TEXT, llm_id)
    else:
        chat_mdl = LLMBundle(db, tenant_id, LLMType.CHAT, llm_id)
    conv = []
    for m in messages:
        if m["role"] not in ["user", "assistant"]:
            continue
        conv.append("{}: {}".format(m["role"].upper(), m["content"]))
    conv = "\n".join(conv)
    today = datetime.date.today().isoformat()
    yesterday = (datetime.date.today() - timedelta(days=1)).isoformat()
    tomorrow = (datetime.date.today() + timedelta(days=1)).isoformat()
    prompt = f"""
Role: A helpful assistant

Task and steps: 
    1. Generate a full user question that would follow the conversation.
    2. If the user's question involves relative date, you need to convert it into absolute date based on the current date, which is {today}. For example: 'yesterday' would be converted to {yesterday}.

Requirements & Restrictions:
  - Text generated MUST be in the same language of the original user's question.
  - If the user's latest question is completely, don't do anything, just return the original question.
  - DON'T generate anything except a refined question.

######################
-Examples-
######################

# Example 1
## Conversation
USER: What is the name of Donald Trump's father?
ASSISTANT:  Fred Trump.
USER: And his mother?
###############
Output: What's the name of Donald Trump's mother?

------------
# Example 2
## Conversation
USER: What is the name of Donald Trump's father?
ASSISTANT:  Fred Trump.
USER: And his mother?
ASSISTANT:  Mary Trump.
User: What's her full name?
###############
Output: What's the full name of Donald Trump's mother Mary Trump?

------------
# Example 3
## Conversation
USER: What's the weather today in London?
ASSISTANT:  Cloudy.
USER: What's about tomorrow in Rochester?
###############
Output: What's the weather in Rochester on {tomorrow}?
######################

# Real Data
## Conversation
{conv}
###############
    """
    ans = chat_mdl.chat(prompt, [{"role": "user", "content": "Output: "}], {"temperature": 0.2})
    ans = re.sub(r"<think>.*</think>", "", ans)
    return ans if ans.find("**ERROR**") < 0 else messages[-1]["content"]


def tts(tts_mdl, text):
    if not tts_mdl or not text:
        return
    bin = b""
    for chunk in tts_mdl.tts(text):
        bin += chunk
    return binascii.hexlify(bin).decode("utf-8")


def ask(db: Session, question, kb_ids, tenant_id):
    kbs = KnowledgebaseService.get_by_ids(db, kb_ids)
    embedding_list = list(set([kb.embd_id for kb in kbs]))

    is_knowledge_graph = all([kb.parser_id == ParserType.KG for kb in kbs])
    retriever = settings.retrievaler if not is_knowledge_graph else settings.kg_retrievaler

    embd_mdl = LLMBundle(db, tenant_id, LLMType.EMBEDDING, embedding_list[0])
    chat_mdl = LLMBundle(db, tenant_id, LLMType.CHAT)
    max_tokens = chat_mdl.max_length
    tenant_ids = list(set([kb.tenant_id for kb in kbs]))

    filter_exp = ""  # todo 暂时不提供权限过滤的查询，如果需要这边需要完善
    kb_names = list([kb.name for kb in kbs])
    # kbinfos = retriever.retrieval(question, filter_exp, embd_mdl, tenant_id, kb_names, 1, 12, 0.1, 0.3, aggs=False)
    kbinfos = retriever.retrieval(question, filter_exp, embd_mdl, tenant_ids, kb_names,
                                  1, 12, 0.1, 0.3, aggs=False,
                                  rank_feature=label_question(db, question, kbs)
                                  )
    knowledges = kb_prompt(kbinfos, max_tokens)

    prompt = """
    Role: You're a smart assistant. Your name is Miss R.
    Task: Summarize the information from knowledge bases and answer user's question.
    Requirements and restriction:
      - DO NOT make things up, especially for numbers.
      - If the information from knowledge is irrelevant with user's question, JUST SAY: Sorry, no relevant information provided.
      - Answer with markdown format text.
      - Answer in language of user's question.
      - DO NOT make things up, especially for numbers.

    ### Information from knowledge bases
    %s

    The above is information from knowledge bases.

    """ % "\n".join(knowledges)
    msg = [{"role": "user", "content": question}]

    def decorate_answer(answer):
        nonlocal knowledges, kbinfos, prompt
        answer, idx = retriever.insert_citations(answer,
                                                 [ck["content_ltks"]
                                                  for ck in kbinfos["chunks"]],
                                                 [ck["vector"]
                                                  for ck in kbinfos["chunks"]],
                                                 embd_mdl,
                                                 tkweight=0.7,
                                                 vtweight=0.3)
        idx = set([kbinfos["chunks"][int(i)]["doc_id"] for i in idx])
        recall_docs = [
            d for d in kbinfos["doc_aggs"] if d["doc_id"] in idx]
        if not recall_docs:
            recall_docs = kbinfos["doc_aggs"]
        kbinfos["doc_aggs"] = recall_docs
        refs = deepcopy(kbinfos)
        for c in refs["chunks"]:
            if c.get("vector"):
                del c["vector"]

        if answer.lower().find("invalid key") >= 0 or answer.lower().find("invalid api") >= 0:
            answer += " Please set LLM API-Key in 'User Setting -> Model providers -> API-Key'"
        return {"answer": answer, "reference": refs}

    answer = ""
    for ans in chat_mdl.chat_streamly(prompt, msg, {"temperature": 0.1}):
        answer = ans
        yield {"answer": answer, "reference": {}}
    yield decorate_answer(answer)



def content_tagging(chat_mdl, content, all_tags, examples, topn=3):
    prompt = f"""
Role: You're a text analyzer. 

Task: Tag (put on some labels) to a given piece of text content based on the examples and the entire tag set.

Steps:: 
  - Comprehend the tag/label set.
  - Comprehend examples which all consist of both text content and assigned tags with relevance score in format of JSON.
  - Summarize the text content, and tag it with top {topn} most relevant tags from the set of tag/label and the corresponding relevance score.

Requirements
  - The tags MUST be from the tag set.
  - The output MUST be in JSON format only, the key is tag and the value is its relevance score.
  - The relevance score must be range from 1 to 10.
  - Keywords ONLY in output.

# TAG SET
{", ".join(all_tags)}

"""
    for i, ex in enumerate(examples):
        prompt += """
# Examples {}
### Text Content
{}

Output:
{}

        """.format(i, ex["content"], json.dumps(ex[TAG_FLD], indent=2, ensure_ascii=False))

    prompt += f"""
# Real Data
### Text Content
{content}

"""
    msg = [
        {"role": "system", "content": prompt},
        {"role": "user", "content": "Output: "}
    ]
    _, msg = message_fit_in(msg, chat_mdl.max_length)
    kwd = chat_mdl.chat(prompt, msg[1:], {"temperature": 0.5})
    if isinstance(kwd, tuple):
        kwd = kwd[0]
    kwd = re.sub(r"<think>.*</think>", "", kwd)
    if kwd.find("**ERROR**") >= 0:
        raise Exception(kwd)

    try:
        return json_repair.loads(kwd)
    except json_repair.JSONDecodeError:
        try:
            result = kwd.replace(prompt[:-1], '').replace('user', '').replace('model', '').strip()
            result = '{' + result.split('{')[1].split('}')[0] + '}'
            return json_repair.loads(result)
        except Exception as e:
            logging.exception(f"JSON parsing error: {result} -> {e}")
            raise e