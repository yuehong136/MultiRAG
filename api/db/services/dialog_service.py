# coding=utf-8
"""
@project: multirag
@Author：龙
@file： dialog_service.py
@date：2024/7/24 21:00
@desc:
"""
import binascii
import json
import os
import re
from copy import deepcopy
from timeit import default_timer as timer

from sqlalchemy import asc
from sqlalchemy.orm import Session

from api.db import LLMType, StatusEnum
from api.db.db_models import Dialog, Conversation
from api.db.services.common_service import CommonService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMService, TenantLLMService, LLMBundle
from api.settings import chat_logger, retrievaler
from api.utils.file_utils import get_project_base_directory
from core.app.resume import forbidden_select_fields4resume
from core.nlp import keyword_extraction
from core.nlp.search import index_name
from core.utils import rmSpace, num_tokens_from_string, encoder


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


class ConversationService(CommonService):
    model = Conversation


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
    msg_.append(msg[-1])
    msg = msg_

    # 初始化计数器
    c = count()

    # 如果当前消息长度小于最大长度限制，则返回当前消息长度和消息列表
    if c < max_length:
        return c, msg

    # 如果系统消息仍超出长度，尝试截断长消息
    ll = num_tokens_from_string(msg_[0]["content"])
    l = num_tokens_from_string(msg_[-1]["content"])
    if ll / (ll + l) > 0.8:
        m = msg_[0]["content"]
        m = encoder.decode(encoder.encode(m)[:max_length - l])
        msg[0]["content"] = m
        return max_length, msg

    m = msg_[1]["content"]
    m = encoder.decode(encoder.encode(m)[:max_length - l])
    msg[1]["content"] = m
    return max_length, msg


def llm_id2llm_type(llm_id):
    llm_id = llm_id.split("@")[0]
    fnm = os.path.join(get_project_base_directory(), "configs")
    llm_factories = json.load(open(os.path.join(fnm, "llm_factories.json"), "r", encoding="utf-8"))
    for llm_factory in llm_factories["factory_llm_infos"]:
        for llm in llm_factory["llm"]:
            if llm_id == llm["llm_name"]:
                return llm["mdl_type"].strip(",")[-1]


def chat(dialog, messages, db: Session, stream=True, **kwargs):
    # 确保最后一条消息是用户的消息
    assert messages[-1]["role"] == "user", "The last content of this conversation is not from user."
    st = timer()
    tmp = dialog.llm_id.split("@")
    fid = None
    llm_id = tmp[0]
    if len(tmp) > 1:
        fid = tmp[1]

    # 从数据库中查询LLM模型
    llm = LLMService.query(db, llm_name=llm_id) if not fid else LLMService.query(db, llm_name=llm_id, fid=fid)
    # print("LLMService.query result:", llm)
    if not llm:
        # 如果查询不到，则根据租户ID查询LLM模型
        llm = TenantLLMService.query(db, tenant_id=dialog.tenant_id, llm_name=llm_id) if not fid else \
            TenantLLMService.query(db, tenant_id=dialog.tenant_id, llm_name=llm_id, llm_factory=fid)
        print("TenantLLMService.query result:", llm)
        if not llm:
            # 如果仍然查询不到，则抛出异常
            raise LookupError("LLM(%s) not found" % dialog.llm_id)
        max_tokens = 8192
    else:
        max_tokens = llm[0].max_tokens

    # 获取知识库并检查是否使用相同的嵌入模型
    # 通过知识库ID检索知识库信息
    kbs = KnowledgebaseService.get_by_ids(db, dialog.kb_ids)

    # 提取并去重知识库的嵌入ID
    embd_nms = list(set([kb.embd_id for kb in kbs]))

    kb_names = list([kb.name for kb in kbs])
    # print("embd_nms:", embd_nms)
    print("kb_names:", kb_names)
    # 检查所有知识库是否使用相同的嵌入模型
    # todo 没做向量模型内容，后续需要改
    if len(embd_nms) > 1:
        # 如果没有，则返回一条错误消息，指示知识库使用不同的嵌入模型
        yield {"answer": "**ERROR**: Knowledge bases use different embedding models.", "reference": []}
        return {"answer": "**ERROR**: Knowledge bases use different embedding models.", "reference": []}

    # 提取用户提出的问题
    questions = [m["content"] for m in messages if m["role"] == "user"]
    filter_exp = kwargs["filter_condition"] if "filter_condition" in kwargs else ""
    attachments = kwargs["doc_ids"].split(",") if "doc_ids" in kwargs else None
    if "doc_ids" in messages[-1]:
        attachments = messages[-1]["doc_ids"]
        for m in messages[:-1]:
            if "doc_ids" in m:
                attachments.extend(m["doc_ids"])
    if len(embd_nms) != 0:
        embd_mdl = LLMBundle(db, dialog.tenant_id, LLMType.EMBEDDING, embd_nms[0])
    chat_mdl = LLMBundle(db, dialog.tenant_id, LLMType.CHAT, dialog.llm_id)

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
        chat_logger.info("Use SQL to retrieval:{}".format(questions[-1]))
        # 调用use_sql函数尝试使用SQL查询获取答案
        ans = use_sql(questions[-1], field_map, dialog.tenant_id, chat_mdl, prompt_config.get("quote", True))
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

    # 如果存在重新排序模型，初始化重新排序模型
    rerank_mdl = None
    if dialog.rerank_id:
        rerank_mdl = LLMBundle(db, dialog.tenant_id, LLMType.RERANK, dialog.rerank_id)

    # 添加问题以确保长度足够
    # 根据问题列表的长度，复制最后一个问题，目的是为了后续的知识抽取和问答融合
    for _ in range(len(questions) // 2):
        questions.append(questions[-1])

    # 检查prompt_config中是否包含"knowledge"参数，以决定是否进行知识检索
    if "knowledge" not in [p["key"] for p in prompt_config["parameters"]]:
        # 如果不包含，则初始化知识信息为一个空的字典
        kbinfos = {"total": 0, "chunks": [], "doc_aggs": []}
    else:
        # 如果包含"knowledge"参数，且设置了关键字提取，那么对最后一个问题进行关键字提取
        if prompt_config.get("keyword", False):
            questions[-1] += keyword_extraction(chat_mdl, questions[-1])

        kbinfos = retrievaler.retrieval(" ".join(questions), filter_exp, embd_mdl, dialog.tenant_id, kb_names, 1,
                                        dialog.top_n,
                                        dialog.similarity_threshold,
                                        dialog.vector_similarity_weight,
                                        doc_ids=attachments,
                                        top=1024, aggs=False, rerank_mdl=rerank_mdl)

    # 从kbinfos中提取出知识内容及其权重，存储在一个列表中
    knowledges = [ck["text"] for ck in kbinfos["chunks"]]
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
    chat_logger.info(
        "{}->{}".format(" ".join(questions), "\n->".join(knowledges)))
    retrieval_tm = timer()

    # 如果没有知识并且配置了空响应，返回空响应
    if not knowledges and prompt_config.get("empty_response"):
        empty_res = prompt_config["empty_response"]
        yield {"answer": empty_res, "reference": kbinfos, "audio_binary": tts(tts_mdl, empty_res)}
        return {"answer": prompt_config["empty_response"], "reference": kbinfos}

    kwargs["knowledge"] = "\n\n------\n\n".join(knowledges)
    gen_conf = dialog.llm_setting
    # print(gen_conf)

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
        nonlocal prompt_config, knowledges, kwargs, kbinfos, prompt, retrieval_tm
        refs = []
        # 如果需要插入引用文献，处理回答内容
        if knowledges and (prompt_config.get("quote", True) and kwargs.get("quote", True)):
            answer, idx = retrievaler.insert_citations(answer,
                                                       [ck["content_ltks"] for ck in kbinfos["chunks"]],
                                                       [ck["vector"] for ck in kbinfos["chunks"]],
                                                       embd_mdl,
                                                       tkweight=1 - dialog.vector_similarity_weight,
                                                       vtweight=dialog.vector_similarity_weight)
            idx = set([kbinfos["chunks"][int(i)]["doc_id"] for i in idx])
            recall_docs = [d for d in kbinfos["doc_aggs"] if d["doc_id"] in idx]
            if not recall_docs: recall_docs = kbinfos["doc_aggs"]
            kbinfos["doc_aggs"] = recall_docs

            # 删除引用文献中的向量信息
            refs = deepcopy(kbinfos)
            for c in refs["chunks"]:
                if c.get("vector"):
                    del c["vector"]

        # 如果回答中包含无效API key的提示，添加设置API key的提示
        if answer.lower().find("invalid key") >= 0 or answer.lower().find("invalid api") >= 0:
            answer += " Please set LLM API-Key in 'User Setting -> Model Providers -> API-Key'"
        # return {"answer": answer, "reference": refs, "prompt": prompt}
        done_tm = timer()
        prompt += "\n\n### Elapsed\n  - Retrieval: %.1f ms\n  - LLM: %.1f ms" % (
        (retrieval_tm - st) * 1000, (done_tm - st) * 1000)
        return {"answer": answer, "reference": refs, "prompt": prompt}

    # # 根据是否启用流式输出生成回答
    # if stream:
    #     # 初始化答案变量，用于存储模型生成的解答
    #     answer = ""
    #     # 使用chat_streamly方法以流式处理方式获取答案
    #     for ans in chat_mdl.chat_streamly(msg[0]["content"], msg[1:], gen_conf):
    #         # 更新答案变量为最新的解答
    #         answer = ans
    #         # 生成并yield一个包含当前答案和空引用的字典
    #         yield {"answer": answer, "reference": {}}
    #     # 处理完成后，对最终答案进行装饰并yield
    #     yield decorate_answer(answer)
    # else:
    #     # 使用chat方法直接获取答案
    #     # answer = chat_mdl.chat(msg[0]["content"], msg[1:], gen_conf)
    #     answer = chat_mdl.chat(prompt, msg[1:], gen_conf)
    #     # 记录对话日志，包含用户消息和助手的回答
    #     chat_logger.info("User: {}|Assistant: {}".format(
    #         msg[-1]["content"], answer))
    #     # 对答案进行装饰并yield
    #     yield decorate_answer(answer)

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
        chat_logger.info("User: {}|Assistant: {}".format(
            msg[-1]["content"], answer))
        res = decorate_answer(answer)
        res["audio_binary"] = tts(tts_mdl, answer)
        yield res


def use_sql(question, field_map, tenant_id, chat_mdl, quota=True):
    sys_prompt = "你是一个DBA。你需要这对以下表的字段结构，根据用户的问题列表，写出最后一个问题对应的SQL。"
    user_promt = """
        表名：{}；
        数据库表字段说明如下：
        {}
        
        问题如下：
        {}
        请写出SQL, 且只要SQL，不要有其他说明及文字。
    """.format(
        index_name(tenant_id),
        "\n".join([f"{k}: {v}" for k, v in field_map.items()]),
        question
    )
    tried_times = 0

    def get_table():
        nonlocal sys_prompt, user_promt, question, tried_times
        sql = chat_mdl.chat(sys_prompt, [{"role": "user", "content": user_promt}], {
            "temperature": 0.06})
        print(user_promt, sql)
        chat_logger.info(f"“{question}”==>{user_promt} get SQL: {sql}")
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

        chat_logger.info(f"“{question}” get SQL(refined): {sql}")
        tried_times += 1
        return retrievaler.sql_retrieval(sql, format="json"), sql

    tbl, sql = get_table()
    if tbl is None:
        return None
    if tbl.get("error") and tried_times <= 2:
        user_promt = """
        表名：{}；
        数据库表字段说明如下：
        {}

        问题如下：
        {}

        你上一次给出的错误SQL如下：
        {}

        后台报错如下：
        {}

        请纠正SQL中的错误再写一遍，且只要SQL，不要有其他说明及文字。
        """.format(
            index_name(tenant_id),
            "\n".join([f"{k}: {v}" for k, v in field_map.items()]),
            question, sql, tbl["error"]
        )
        tbl, sql = get_table()
        chat_logger.info("TRY it again: {}".format(sql))

    chat_logger.info("GET table: {}".format(tbl))
    print(tbl)
    if tbl.get("error") or len(tbl["rows"]) == 0:
        return None

    docid_idx = set([ii for ii, c in enumerate(
        tbl["columns"]) if c["name"] == "doc_id"])
    docnm_idx = set([ii for ii, c in enumerate(
        tbl["columns"]) if c["name"] == "docnm_kwd"])
    clmn_idx = [ii for ii in range(
        len(tbl["columns"])) if ii not in (docid_idx | docnm_idx)]

    # compose markdown table
    clmns = "|" + "|".join([re.sub(r"(/.*|（[^（）]+）)", "", field_map.get(tbl["columns"][i]["name"],
                                                                        tbl["columns"][i]["name"])) for i in
                            clmn_idx]) + ("|Source|" if docid_idx and docid_idx else "|")

    line = "|" + "|".join(["------" for _ in range(len(clmn_idx))]) + \
           ("|------|" if docid_idx and docid_idx else "")

    rows = ["|" +
            "|".join([rmSpace(str(r[i])) for i in clmn_idx]).replace("None", " ") +
            "|" for r in tbl["rows"]]
    if quota:
        rows = "\n".join([r + f" ##{ii}$$ |" for ii, r in enumerate(rows)])
    else:
        rows = "\n".join([r + f" ##{ii}$$ |" for ii, r in enumerate(rows)])
    rows = re.sub(r"T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+Z)?\|", "|", rows)

    if not docid_idx or not docnm_idx:
        chat_logger.warning("SQL missing field: " + sql)
        return {
            "answer": "\n".join([clmns, line, rows]),
            "reference": {"chunks": [], "doc_aggs": []},
            "prompt": sys_prompt
        }

    docid_idx = list(docid_idx)[0]
    docnm_idx = list(docnm_idx)[0]
    doc_aggs = {}
    for r in tbl["rows"]:
        if r[docid_idx] not in doc_aggs:
            doc_aggs[r[docid_idx]] = {"doc_name": r[docnm_idx], "count": 0}
        doc_aggs[r[docid_idx]]["count"] += 1
    return {
        "answer": "\n".join([clmns, line, rows]),
        "reference": {"chunks": [{"doc_id": r[docid_idx], "docnm_kwd": r[docnm_idx]} for r in tbl["rows"]],
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
    if not contents: return False
    contents = "Documents: \n" + "   - ".join(contents)
    contents = f"Question: {question}\n" + contents
    if num_tokens_from_string(contents) >= chat_mdl.max_length - 4:
        contents = encoder.decode(encoder.encode(contents)[:chat_mdl.max_length - 4])
    ans = chat_mdl.chat(prompt, [{"role": "user", "content": contents}], {"temperature": 0.01})
    if ans.lower().find("yes") >= 0: return True
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
    prompt = f"""
Role: A helpful assistant
Task: Generate a full user question that would follow the conversation.
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

######################

# Real Data
## Conversation
{conv}
###############
    """
    ans = chat_mdl.chat(prompt, [{"role": "user", "content": "Output: "}], {"temperature": 0.2})
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
    embd_nms = list(set([kb.embd_id for kb in kbs]))
    # todo 测试kg，并开放下面写法
    # is_kg = all([kb.parser_id == ParserType.KG for kb in kbs])
    # retr = retrievaler if not is_kg else kg_retrievaler

    embd_mdl = LLMBundle(db, tenant_id, LLMType.EMBEDDING, embd_nms[0])
    chat_mdl = LLMBundle(db, tenant_id, LLMType.CHAT)
    max_tokens = chat_mdl.max_length
    filter_exp = ""  # todo 暂时不提供权限过滤的查询，如果需要这边需要完善
    kb_names = list([kb.name for kb in kbs])
    kbinfos = retrievaler.retrieval(question, filter_exp, embd_mdl, tenant_id, kb_names, 1, 12, 0.1, 0.3, aggs=False)
    knowledges = [ck["text"] for ck in kbinfos["chunks"]]

    used_token_count = 0
    for i, c in enumerate(knowledges):
        used_token_count += num_tokens_from_string(c)
        if max_tokens * 0.97 < used_token_count:
            knowledges = knowledges[:i]
            break

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
        answer, idx = retrievaler.insert_citations(answer,
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
        if not recall_docs: recall_docs = kbinfos["doc_aggs"]
        kbinfos["doc_aggs"] = recall_docs
        refs = deepcopy(kbinfos)
        for c in refs["chunks"]:
            if c.get("vector"):
                del c["vector"]

        if answer.lower().find("invalid key") >= 0 or answer.lower().find("invalid api") >= 0:
            answer += " Please set LLM API-Key in 'User Setting -> Model Providers -> API-Key'"
        return {"answer": answer, "reference": refs}

    answer = ""
    for ans in chat_mdl.chat_streamly(prompt, msg, {"temperature": 0.1}):
        answer = ans
        yield {"answer": answer, "reference": {}}
    yield decorate_answer(answer)
