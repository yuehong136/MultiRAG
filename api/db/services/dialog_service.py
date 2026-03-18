# coding=utf-8
"""
@project: multirag
@Author：龙
@file： dialog_service.py
@date：2025/7/17 15:30
@desc:
"""
import asyncio
import logging
import binascii
import time
from functools import partial

import re
from copy import deepcopy
from timeit import default_timer as timer
from langfuse import Langfuse
from datetime import datetime
from sqlalchemy import select, func
from sqlalchemy.orm import Session

from api.db.db_models import Dialog
from api.db.services.common_service import CommonService
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.langfuse_service import TenantLangfuseService
from api.db.services.llm_service import LLMBundle
from api.db.services.tenant_llm_service import TenantLLMService
from core.graphrag.general.mind_map_extractor import MindMapExtractor
from core.app.tag import label_question
from core.advanced_rag import DeepResearcher
from core.nlp.search import index_name
from core.prompts.generator import kb_prompt, message_fit_in, keyword_extraction, full_question, chunks_format, \
    citation_prompt, cross_languages, PROMPT_JINJA_ENV, ASK_SUMMARY
from common.metadata_utils import apply_meta_data_filter
from common.token_utils import num_tokens_from_string
from core.utils.tavily_conn import Tavily
from common.constants import LLMType, StatusEnum, ParserType
from common.string_utils import remove_redundant_spaces
from common import settings
from common.time_utils import current_timestamp, datetime_format


def sync_async_generator(async_gen):
    """将异步生成器转换为同步生成器"""
    loop = asyncio.new_event_loop()
    try:
        while True:
            try:
                yield loop.run_until_complete(async_gen.__anext__())
            except StopAsyncIteration:
                break
    finally:
        loop.close()


class DialogService(CommonService):
    model = Dialog

    @classmethod
    def save(cls, db: Session, **kwargs):
        """Save a new record to database.

        This method creates a new record in the database with the provided field values,
        forcing an insert operation rather than an update.

        Args:
            db (Session): Database session.
            **kwargs: Record field values as keyword arguments.

        Returns:
            Model instance: The created record object.
        """
        # # 添加创建时间字段
        # kwargs["create_time"] = current_timestamp()
        # kwargs["create_date"] = datetime_format(datetime.now())
        # kwargs["update_time"] = current_timestamp()
        # kwargs["update_date"] = datetime_format(datetime.now())

        # 创建新的实例
        sample_obj = cls.model(**kwargs)

        # 添加到数据库会话并提交
        db.add(sample_obj)
        db.commit()
        db.refresh(sample_obj)

        return sample_obj

    @classmethod
    def update_many_by_id(cls, db: Session, data_list):
        """Update multiple records by their IDs.

        This method updates multiple records in the database, identified by their IDs.
        It automatically updates the update_time and update_date fields for each record.

        Args:
            db (Session): Database session.
            data_list (list): List of dictionaries containing record data to update.
                             Each dictionary must include an 'id' field.
        """
        try:
            for data in data_list:
                if 'id' not in data:
                    raise ValueError("Each data item must include an 'id' field")

                # 自动添加更新时间字段
                current_ts = current_timestamp()
                current_date = datetime_format(datetime.now())
                data["update_time"] = current_ts
                data["update_date"] = current_date

                # 获取要更新的记录ID
                record_id = data.pop('id')

                # 执行更新
                db.query(cls.model).filter(
                    cls.model.id == record_id
                ).update(data, synchronize_session=False)

            # 提交所有更改
            db.commit()

        except Exception as e:
            # 如果出现错误，回滚事务
            db.rollback()
            raise e

    @classmethod
    def get_list(cls, db: Session, tenant_id, page_number, items_per_page, orderby, is_desc, id, name):

        query = db.query(cls.model)

        if id:
            query = query.filter(cls.model.id == id)
        if name:
            query = query.filter(cls.model.name == name)

        query = query.filter(
            (cls.model.tenant_id == tenant_id) &
            (cls.model.status == StatusEnum.VALID.value)
        )

        # 根据 desc 参数确定排序方式
        order_col = getattr(cls.model, orderby)
        query = query.order_by(order_col.desc() if is_desc else order_col.asc())

        # Apply pagination
        query = query.offset((page_number - 1) * items_per_page).limit(items_per_page)

        # Fetch results and convert to dictionary format
        results = query.all()
        return [item.__dict__ for item in results]

    @classmethod
    def get_by_tenant_ids(cls, db: Session, joined_tenant_ids, user_id, page_number, items_per_page, orderby, desc,
                          keywords=None, parser_id=None):
        """
        获取对话列表（支持分页、搜索、排序）
        
        直接查询 update_date/create_date（DateTime 类型），与 ragflow 保持一致。
        SQLAlchemy 会自动将 DateTime 对象序列化为 ISO 格式字符串。
        """
        from api.db.db_models import User
        
        # 查询字段列表 - 使用 update_date/create_date 而不是 update_time/create_time
        fields = [
            cls.model.id,               # 0
            cls.model.tenant_id,        # 1
            cls.model.name,             # 2
            cls.model.description,      # 3
            cls.model.language,         # 4
            cls.model.llm_id,           # 5
            cls.model.llm_setting,      # 6
            cls.model.prompt_type,      # 7
            cls.model.prompt_config,    # 8
            cls.model.similarity_threshold,      # 9
            cls.model.vector_similarity_weight,  # 10
            cls.model.top_n,            # 11
            cls.model.top_k,            # 12
            cls.model.do_refer,         # 13
            cls.model.rerank_id,        # 14
            cls.model.kb_ids,           # 15
            cls.model.icon,             # 16
            cls.model.status,           # 17
            User.nickname,              # 18
            User.avatar.label("tenant_avatar"),  # 19
            cls.model.update_date,      # 20 - DateTime 类型
            cls.model.create_date,      # 21 - DateTime 类型
            cls.model.update_time,      # 22 - 毫秒时间戳（可选）
            cls.model.create_time,      # 23 - 毫秒时间戳（可选）
        ]

        # 构建查询表达式
        query = db.query(*fields).join(User, cls.model.tenant_id == User.id).filter(
            ((cls.model.tenant_id.in_(joined_tenant_ids)) | (cls.model.tenant_id == user_id)) &
            (cls.model.status == StatusEnum.VALID.value)
        )

        if keywords:
            query = query.filter(func.lower(cls.model.name).ilike(f"%{keywords.lower()}%"))

        if parser_id:
            query = query.filter(cls.model.parser_id == parser_id)

        # 根据 desc 参数确定排序方式
        if desc:
            query = query.order_by(getattr(cls.model, orderby).desc())
        else:
            query = query.order_by(getattr(cls.model, orderby).asc())

        # 获取总记录数
        total = query.count()

        # 条件分页
        if page_number and items_per_page:
            dialogs = query.offset((page_number - 1) * items_per_page).limit(items_per_page).all()
        else:
            dialogs = query.all()

        # 转换结果为字典 - 字段名与索引一一对应
        result = []
        for dlg in dialogs:
            dlg_dict = {
                'id': dlg[0],
                'tenant_id': dlg[1],
                'name': dlg[2],
                'description': dlg[3],
                'language': dlg[4],
                'llm_id': dlg[5],
                'llm_setting': dlg[6],
                'prompt_type': dlg[7],
                'prompt_config': dlg[8],
                'similarity_threshold': dlg[9],
                'vector_similarity_weight': dlg[10],
                'top_n': dlg[11],
                'top_k': dlg[12],
                'do_refer': dlg[13],
                'rerank_id': dlg[14],
                'kb_ids': dlg[15],
                'icon': dlg[16],
                'status': dlg[17],
                'nickname': dlg[18],
                'tenant_avatar': dlg[19],
                # DateTime 对象直接转为 ISO 格式字符串（与 ragflow 一致）
                'update_date': dlg[20].isoformat() if dlg[20] else None,
                'create_date': dlg[21].isoformat() if dlg[21] else None,
                'update_time': dlg[22],
                'create_time': dlg[23],
            }
            result.append(dlg_dict)

        return result, total

    @classmethod
    def get_all_dialogs_by_tenant_id(cls, db: Session, tenant_id: str) -> list[dict]:
        """根据tenant_id批量查询所有对话ID，使用分页避免内存溢出"""
        stmt = (
            select(cls.model.id)
            .where(cls.model.tenant_id == tenant_id)
            .order_by(cls.model.create_time.asc())
        )

        offset, limit = 0, 100
        res = []

        while True:
            try:
                d_batch = db.execute(
                    stmt.offset(offset).limit(limit)
                ).scalars().all()

                if not d_batch:
                    break

                res.extend([{"id": dialog_id} for dialog_id in d_batch])
                offset += limit
            except Exception:
                logging.exception("Failed to get dialog IDs for tenant_id=%s at offset %d", tenant_id, offset)
                break

        return res

def chat_solo(db, dialog, messages, stream=True):
    llm_type = TenantLLMService.llm_id2llm_type(dialog.llm_id)
    attachments = ""
    image_attachments = []
    image_files = []
    if "files" in messages[-1]:
        if llm_type == "chat":
            text_attachments, image_attachments = split_file_attachments(messages[-1]["files"])
        else:
            text_attachments, image_files = split_file_attachments(messages[-1]["files"], raw=True)
        attachments = "\n\n".join(text_attachments)

    if llm_type == "image2text":
        llm_model_config = TenantLLMService.get_model_config(db, dialog.tenant_id, LLMType.IMAGE2TEXT, dialog.llm_id)
    else:
        llm_model_config = TenantLLMService.get_model_config(db, dialog.tenant_id, LLMType.CHAT, dialog.llm_id)
    factory = llm_model_config.get("llm_factory", "") if llm_model_config else ""

    if llm_type == "image2text":
        chat_mdl = LLMBundle(db, dialog.tenant_id, LLMType.IMAGE2TEXT, dialog.llm_id)
    else:
        chat_mdl = LLMBundle(db, dialog.tenant_id, LLMType.CHAT, dialog.llm_id)

    prompt_config = dialog.prompt_config
    tts_mdl = None
    if prompt_config.get("tts"):
        tts_mdl = LLMBundle(db, dialog.tenant_id, LLMType.TTS)
    msg = [{"role": m["role"], "content": re.sub(r"##\d+\$\$", "", m["content"])} for m in messages if m["role"] != "system"]
    if attachments and msg:
        msg[-1]["content"] += attachments
    if llm_type == "chat" and image_attachments:
        convert_last_user_msg_to_multimodal(msg, image_attachments, factory)
    if stream:
        last_ans = ""
        delta_ans = ""
        for ans in chat_mdl.chat_streamly(prompt_config.get("system", ""), msg, dialog.llm_setting):
            answer = ans
            delta_ans = ans[len(last_ans):]
            if num_tokens_from_string(delta_ans) < 16:
                continue
            last_ans = answer
            yield {"answer": answer, "reference": {}, "audio_binary": tts(tts_mdl, delta_ans), "prompt": "",
                   "created_at": time.time()}
            delta_ans = ""
        if delta_ans:
            yield {"answer": answer, "reference": {}, "audio_binary": tts(tts_mdl, delta_ans), "prompt": "",
                   "created_at": time.time()}
    else:
        answer = chat_mdl.chat(prompt_config.get("system", ""), msg, dialog.llm_setting)
        user_content = msg[-1].get("content", "[content not available]")
        logging.debug("User: {}|Assistant: {}".format(user_content, answer))
        yield {"answer": answer, "reference": {}, "audio_binary": tts(tts_mdl, answer), "prompt": "", "created_at": time.time()}


async def async_chat_solo(db, dialog, messages, stream=True):
    """异步版本的 chat_solo"""
    llm_type = TenantLLMService.llm_id2llm_type(dialog.llm_id)
    attachments = ""
    image_attachments = []
    image_files = []
    if "files" in messages[-1]:
        if llm_type == "chat":
            text_attachments, image_attachments = split_file_attachments(messages[-1]["files"])
        else:
            text_attachments, image_files = split_file_attachments(messages[-1]["files"], raw=True)
        attachments = "\n\n".join(text_attachments)

    if llm_type == "image2text":
        llm_model_config = TenantLLMService.get_model_config(db, dialog.tenant_id, LLMType.IMAGE2TEXT, dialog.llm_id)
    else:
        llm_model_config = TenantLLMService.get_model_config(db, dialog.tenant_id, LLMType.CHAT, dialog.llm_id)
    factory = llm_model_config.get("llm_factory", "") if llm_model_config else ""

    if llm_type == "image2text":
        chat_mdl = LLMBundle(db, dialog.tenant_id, LLMType.IMAGE2TEXT, dialog.llm_id)
    else:
        chat_mdl = LLMBundle(db, dialog.tenant_id, LLMType.CHAT, dialog.llm_id)

    prompt_config = dialog.prompt_config
    tts_mdl = None
    if prompt_config.get("tts"):
        tts_mdl = LLMBundle(db, dialog.tenant_id, LLMType.TTS)
    msg = [{"role": m["role"], "content": re.sub(r"##\d+\$\$", "", m["content"])} for m in messages if m["role"] != "system"]
    if attachments and msg:
        msg[-1]["content"] += attachments
    if llm_type == "chat" and image_attachments:
        convert_last_user_msg_to_multimodal(msg, image_attachments, factory)
    if stream:
        if llm_type == "chat":
            stream_iter = chat_mdl.async_chat_streamly_delta(prompt_config.get("system", ""), msg, dialog.llm_setting)
        else:
            stream_iter = chat_mdl.async_chat_streamly_delta(prompt_config.get("system", ""), msg, dialog.llm_setting, images=image_files)
        async for kind, value, state in _stream_with_think_delta(stream_iter):
            if kind == "marker":
                flags = {"start_to_think": True} if value == "<think>" else {"end_to_think": True}
                yield {"answer": "", "reference": {}, "audio_binary": None, "prompt": "", "created_at": time.time(), "final": False, **flags}
                continue
            yield {"answer": value, "reference": {}, "audio_binary": tts(tts_mdl, value), "prompt": "", "created_at": time.time(), "final": False}
    else:
        if llm_type == "chat":
            answer = await chat_mdl.async_chat(prompt_config.get("system", ""), msg, dialog.llm_setting)
        else:
            answer = await chat_mdl.async_chat(prompt_config.get("system", ""), msg, dialog.llm_setting, images=image_files)
        user_content = msg[-1].get("content", "[content not available]")
        logging.debug("User: {}|Assistant: {}".format(user_content, answer))
        yield {"answer": answer, "reference": {}, "audio_binary": tts(tts_mdl, answer), "prompt": "", "created_at": time.time()}


def get_models(db, dialog):
    embd_mdl, chat_mdl, rerank_mdl, tts_mdl = None, None, None, None
    kbs = KnowledgebaseService.get_by_ids(db, dialog.kb_ids)
    embedding_list = list(set([kb.embd_id for kb in kbs]))
    if len(embedding_list) > 1:
        raise Exception("**ERROR**: Knowledge bases use different embedding models.")

    if embedding_list:
        embd_mdl = LLMBundle(db, dialog.tenant_id, LLMType.EMBEDDING, embedding_list[0])
        if not embd_mdl:
            raise LookupError("Embedding model(%s) not found" % embedding_list[0])

    if TenantLLMService.llm_id2llm_type(dialog.llm_id) == "image2text":
        chat_mdl = LLMBundle(db, dialog.tenant_id, LLMType.IMAGE2TEXT, dialog.llm_id)
    else:
        chat_mdl = LLMBundle(db, dialog.tenant_id, LLMType.CHAT, dialog.llm_id)

    if dialog.rerank_id:
        rerank_mdl = LLMBundle(db, dialog.tenant_id, LLMType.RERANK, dialog.rerank_id)

    if dialog.prompt_config.get("tts"):
        tts_mdl = LLMBundle(db, dialog.tenant_id, LLMType.TTS)
    return kbs, embd_mdl, rerank_mdl, chat_mdl, tts_mdl


def split_file_attachments(files: list[dict] | None, raw: bool = False) -> tuple[list[str], list[str] | list[bytes]]:
    if not files:
        return [], []

    text_attachments = []
    if raw:
        file_contents, image_files = FileService.get_files(files, raw=True)
        for content in file_contents:
            if not isinstance(content, str):
                content = str(content)
            text_attachments.append(content)
        return text_attachments, image_files

    image_attachments = []
    for content in FileService.get_files(files, raw=False):
        if not isinstance(content, str):
            content = str(content)
        if content.strip().startswith("data:"):
            image_attachments.append(content.strip())
            continue
        text_attachments.append(content)
    return text_attachments, image_attachments


_DATA_URI_RE = re.compile(r"^data:(?P<mime>[^;]+);base64,(?P<b64>[A-Za-z0-9+/=\s]+)$")


def _parse_data_uri_or_b64(s: str, default_mime: str = "image/png") -> tuple[str, str]:
    s = (s or "").strip()
    match = _DATA_URI_RE.match(s)
    if match:
        mime = match.group("mime").strip()
        b64 = match.group("b64").strip()
        return mime, b64
    return default_mime, s


def _normalize_text_from_content(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for blk in content:
            if isinstance(blk, dict):
                if blk.get("type") in {"text", "input_text"}:
                    txt = blk.get("text")
                    if txt:
                        texts.append(str(txt))
                elif "text" in blk and isinstance(blk.get("text"), (str, int, float)):
                    texts.append(str(blk["text"]))
        return "\n".join(texts).strip()
    return str(content)


def convert_last_user_msg_to_multimodal(msg: list[dict], image_data_uris: list[str], factory: str) -> None:
    if not msg or not image_data_uris:
        return

    factory_norm = (factory or "").strip().lower()

    for idx in range(len(msg) - 1, -1, -1):
        if msg[idx].get("role") != "user":
            continue

        original_content = msg[idx].get("content", "")
        text = _normalize_text_from_content(original_content)

        if factory_norm == "gemini":
            parts = []
            if text:
                parts.append({"text": text})
            for image in image_data_uris:
                mime, b64 = _parse_data_uri_or_b64(str(image), default_mime="image/png")
                parts.append({"inline_data": {"mime_type": mime, "data": b64}})
            msg[idx]["content"] = parts
            return

        if factory_norm == "anthropic":
            blocks = []
            if text:
                blocks.append({"type": "text", "text": text})
            for image in image_data_uris:
                mime, b64 = _parse_data_uri_or_b64(str(image), default_mime="image/png")
                blocks.append(
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": mime, "data": b64},
                    }
                )
            msg[idx]["content"] = blocks
            return

        multimodal_content = []
        if isinstance(original_content, list):
            multimodal_content = deepcopy(original_content)
        else:
            text_content = "" if original_content is None else str(original_content)
            if text_content:
                multimodal_content.append({"type": "text", "text": text_content})

        for data_uri in image_data_uris:
            image_url = data_uri
            if not isinstance(image_url, str):
                image_url = str(image_url)
            if not image_url.startswith("data:"):
                image_url = f"data:image/png;base64,{image_url}"
            multimodal_content.append({"type": "image_url", "image_url": {"url": image_url}})

        msg[idx]["content"] = multimodal_content
        return


BAD_CITATION_PATTERNS = [
    re.compile(r"\(\s*ID\s*[: ]*\s*(\d+)\s*\)"),  # (ID: 12)
    re.compile(r"\[\s*ID\s*[: ]*\s*(\d+)\s*\]"),  # [ID: 12]
    re.compile(r"【\s*ID\s*[: ]*\s*(\d+)\s*】"),  # 【ID: 12】
    re.compile(r"ref\s*(\d+)", flags=re.IGNORECASE),  # ref12、REF 12
]

def repair_bad_citation_formats(answer: str, kbinfos: dict, idx: set):
    max_index = len(kbinfos["chunks"])

    def safe_add(i):
        if 0 <= i < max_index:
            idx.add(i)
            return True
        return False

    def find_and_replace(pattern, group_index=1, repl=lambda i: f"ID:{i}", flags=0):
        nonlocal answer

        def replacement(match):
            try:
                i = int(match.group(group_index))
                if safe_add(i):
                    return f"[{repl(i)}]"
            except Exception:
                pass
            return match.group(0)

        answer = re.sub(pattern, replacement, answer, flags=flags)

    for pattern in BAD_CITATION_PATTERNS:
        find_and_replace(pattern)

    return answer, idx


def chat(dialog, messages, db, stream=True, **kwargs):
    # 确保最后一条消息是用户的消息
    assert messages[-1]["role"] == "user", "The last content of this conversation is not from user."
    # todo ragflow用这个方法实现了无kb时应用对话。但我们通过前后端交互对接实现了，所以注释掉这部分
    # if not dialog.kb_ids and not dialog.prompt_config.get("tavily_api_key"):
    #     for ans in chat_solo(db, dialog, messages, stream):
    #         yield ans
    #     return
    chat_start_ts = timer()

    llm_type = TenantLLMService.llm_id2llm_type(dialog.llm_id)
    if llm_type == "image2text":
        llm_model_config = TenantLLMService.get_model_config(db, dialog.tenant_id, LLMType.IMAGE2TEXT, dialog.llm_id)
    else:
        llm_model_config = TenantLLMService.get_model_config(db, dialog.tenant_id, LLMType.CHAT, dialog.llm_id)

    factory = llm_model_config.get("llm_factory", "") if llm_model_config else ""
    max_tokens = llm_model_config.get("max_tokens", 8192)

    check_llm_ts = timer()

    langfuse_tracer = None
    trace_context = {}
    langfuse_keys = TenantLangfuseService.filter_by_tenant(db, tenant_id=dialog.tenant_id)
    if langfuse_keys:
        langfuse = Langfuse(public_key=langfuse_keys.public_key, secret_key=langfuse_keys.secret_key, host=langfuse_keys.host)
        try:
            if langfuse.auth_check():
                langfuse_tracer = langfuse
                trace_id = langfuse_tracer.create_trace_id()
                trace_context = {"trace_id": trace_id}
        except Exception:
            # Skip langfuse tracing if connection fails
            pass

    check_langfuse_tracer_ts = timer()
    kbs, embd_mdl, rerank_mdl, chat_mdl, tts_mdl = get_models(db, dialog)
    toolcall_session, tools = kwargs.get("toolcall_session"), kwargs.get("tools")
    if toolcall_session and tools:
        chat_mdl.bind_tools(toolcall_session, tools)
    bind_models_ts = timer()

    kb_names = list([kb.name for kb in kbs])
    kb_tenant_ids = [kb.tenant_id for kb in kbs]  # 1:1 with kb_names, for correct collection lookup
    print("正在检索的知识库 --> ", kb_names)

    retriever = settings.retriever
    questions = [m["content"] for m in messages if m["role"] == "user"][-3:]
    filter_exp = kwargs["filter_condition"] if "filter_condition" in kwargs else ""
    attachments = kwargs["doc_ids"].split(",") if "doc_ids" in kwargs else []
    attachments_ = ""
    if "doc_ids" in messages[-1]:
        attachments = messages[-1]["doc_ids"]
    image_attachments = []
    image_files = []
    if "files" in messages[-1]:
        if llm_type == "chat":
            text_attachments, image_attachments = split_file_attachments(messages[-1]["files"])
        else:
            text_attachments, image_files = split_file_attachments(messages[-1]["files"], raw=True)
        attachments_ = "\n\n".join(text_attachments)

    prompt_config = dialog.prompt_config
    internet_enabled = kwargs.get("internet") is not False
    field_map = KnowledgebaseService.get_field_map(db, dialog.kb_ids)
    # 如果字段映射存在，尝试使用SQL检索答案
    if field_map:
        logging.debug("Use SQL to retrieval:{}".format(questions[-1]))
        ans = use_sql(questions[-1], field_map, kb_tenant_ids, kb_names, chat_mdl, prompt_config.get("quote", True), dialog.kb_ids)
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
            prompt_config["system"] = prompt_config["system"].replace("{%s}" % p["key"], " ")

    if len(questions) > 1 and prompt_config.get("refine_multiturn"):
        questions = [asyncio.run(full_question(tenant_id=dialog.tenant_id, llm_id=dialog.llm_id, messages=messages))]
    else:
        questions = questions[-1:]

    if prompt_config.get("cross_languages"):
        questions = [asyncio.run(cross_languages(dialog.tenant_id, dialog.llm_id, questions[0], prompt_config["cross_languages"]))]

    if dialog.meta_data_filter:
        metas = DocMetadataService.get_flatted_meta_by_kbs(db, dialog.kb_ids)
        attachments = asyncio.run(apply_meta_data_filter(dialog.meta_data_filter, metas, questions[-1], chat_mdl, attachments))

    if prompt_config.get("keyword", False):
        questions[-1] += asyncio.run(keyword_extraction(chat_mdl, questions[-1]))

    refine_question_ts = timer()

    thought = ""
    kbinfos = {"total": 0, "chunks": [], "doc_aggs": []}
    knowledges = []

    # 检查prompt_config中是否包含"knowledge"参数，以决定是否进行知识检索
    if attachments is not None and "knowledge" in [p["key"] for p in prompt_config["parameters"]]:
        tenant_ids = list(set([kb.tenant_id for kb in kbs]))
        knowledges = []
        prompt_config_for_reasoning = prompt_config
        if not internet_enabled:
            prompt_config_for_reasoning = dict(prompt_config)
            prompt_config_for_reasoning.pop("tavily_api_key", None)
        if prompt_config.get("reasoning", False) or kwargs.get("reasoning"):
            reasoner = DeepResearcher(
                chat_mdl,
                prompt_config_for_reasoning,
                partial(
                    retriever.retrieval,
                    filter_exp="",
                    embd_mdl=embd_mdl,
                    tenant_id=kb_tenant_ids,
                    kb_names=kb_names,
                    page=1,
                    page_size=dialog.top_n,
                    similarity_threshold=0.2,
                    vector_similarity_weight=0.3,
                    doc_ids=attachments,
                    search_mode=dialog.search_mode,
                    kb_ids=dialog.kb_ids
                )
            )

            for think in sync_async_generator(reasoner.thinking(kbinfos, attachments_ + " ".join(questions))):
                if isinstance(think, str):
                    thought = think
                    knowledges = [t for t in think.split("\n") if t]
                elif stream:
                    yield think
        else:
            if embd_mdl:
                kbinfos = asyncio.run(retriever.retrieval(
                    " ".join(questions),
                    filter_exp,
                    embd_mdl,
                    kb_tenant_ids,
                    kb_names,
                    1,
                    dialog.top_n,
                    dialog.similarity_threshold,
                    dialog.vector_similarity_weight,
                    doc_ids=attachments,
                    top=1024,
                    aggs=True,
                    rerank_mdl=rerank_mdl,
                    rank_feature=label_question(db, " ".join(questions), kbs),
                    search_mode=dialog.search_mode,
                    kb_ids = dialog.kb_ids
                ))
                if prompt_config.get("toc_enhance"):
                    cks = asyncio.run(retriever.retrieval_by_toc(" ".join(questions), kbinfos["chunks"], tenant_ids, kb_names, chat_mdl, dialog.top_n))
                    if cks:
                        kbinfos["chunks"] = cks
                kbinfos["chunks"] = retriever.retrieval_by_children(kbinfos["chunks"], tenant_ids)
            if internet_enabled and prompt_config.get("tavily_api_key"):
                tav = Tavily(prompt_config["tavily_api_key"])
                tav_res = tav.retrieve_chunks(" ".join(questions))
                kbinfos["chunks"].extend(tav_res["chunks"])
                kbinfos["doc_aggs"].extend(tav_res["doc_aggs"])
            if prompt_config.get("use_kg"):
                ck = asyncio.run(settings.kg_retriever.retrieval(" ".join(questions), tenant_ids, dialog.kb_ids, embd_mdl, LLMBundle(db, dialog.tenant_id, LLMType.CHAT)))
                if ck["content_with_weight"]:
                    kbinfos["chunks"].insert(0, ck)

            knowledges = kb_prompt(kbinfos, max_tokens)

    logging.debug("{}->{}".format(" ".join(questions), "\n->".join(knowledges)))

    retrieval_ts = timer()
    if not knowledges and prompt_config.get("empty_response"):
        empty_res = prompt_config["empty_response"]
        yield {"answer": empty_res, "reference": kbinfos, "prompt": "\n\n### Query:\n%s" % " ".join(questions), "audio_binary": tts(tts_mdl, empty_res)}
        return {"answer": prompt_config["empty_response"], "reference": kbinfos}

    kwargs["knowledge"] = "\n------\n" + "\n\n------\n\n".join(knowledges)
    gen_conf = dialog.llm_setting

    msg = [{"role": "system", "content": prompt_config["system"].format(**kwargs) + attachments_}]
    prompt4citation = ""
    if knowledges and (prompt_config.get("quote", True) and kwargs.get("quote", True)):
        prompt4citation = citation_prompt()
    msg.extend([{"role": m["role"], "content": re.sub(r"##\d+\$\$", "", m["content"])} for m in messages if m["role"] != "system"])
    used_token_count, msg = message_fit_in(msg, int(max_tokens * 0.95))
    if llm_type == "chat" and image_attachments:
        convert_last_user_msg_to_multimodal(msg, image_attachments, factory)

    # 检查消息列表的长度是否至少为2
    assert len(msg) >= 2, f"message_fit_in has bug: {msg}"
    prompt = msg[0]["content"]

    # 调整生成配置中的最大token数
    if "max_tokens" in gen_conf:
        gen_conf["max_tokens"] = min(gen_conf["max_tokens"], max_tokens - used_token_count)

    def decorate_answer(answer):
        nonlocal embd_mdl, prompt_config, knowledges, kwargs, kbinfos, prompt, retrieval_ts, questions, langfuse_tracer

        refs = []
        ans = answer.split("</think>")
        think = ""
        if len(ans) == 2:
            think = ans[0] + "</think>"
            answer = ans[1]

        if knowledges and (prompt_config.get("quote", True) and kwargs.get("quote", True)):
            idx = set([])
            if embd_mdl and not re.search(r"\[ID:([0-9]+)\]", answer):
                answer, idx = retriever.insert_citations(
                    answer,
                    [ck["content_ltks"] for ck in kbinfos["chunks"]],
                    [ck["vector"] for ck in kbinfos["chunks"]],
                    embd_mdl,
                    tkweight=1 - dialog.vector_similarity_weight,
                    vtweight=dialog.vector_similarity_weight
                )
            else:
                for match in re.finditer(r"\[ID:([0-9]+)\]", answer):
                    i = int(match.group(1))
                    if i < len(kbinfos["chunks"]):
                        idx.add(i)

            answer, idx = repair_bad_citation_formats(answer, kbinfos, idx)

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
        check_langfuse_tracer_cost = (check_langfuse_tracer_ts - check_llm_ts) * 1000
        bind_embedding_time_cost = (bind_models_ts - check_langfuse_tracer_ts) * 1000
        refine_question_time_cost = (refine_question_ts - bind_models_ts) * 1000
        retrieval_time_cost = (retrieval_ts - refine_question_ts) * 1000
        generate_result_time_cost = (finish_chat_ts - retrieval_ts) * 1000

        tk_num = num_tokens_from_string(think + answer)
        prompt += "\n\n### Query:\n%s" % " ".join(questions)
        prompt = (
            f"{prompt}\n\n"
            "## Time elapsed:\n"
            f"  - Total: {total_time_cost:.1f}ms\n"
            f"  - Check LLM: {check_llm_time_cost:.1f}ms\n"
            f"  - Check Langfuse tracer: {check_langfuse_tracer_cost:.1f}ms\n"
            f"  - Bind models: {bind_embedding_time_cost:.1f}ms\n"
            f"  - Query refinement(LLM): {refine_question_time_cost:.1f}ms\n"
            f"  - Retrieval: {retrieval_time_cost:.1f}ms\n"
            f"  - Generate answer: {generate_result_time_cost:.1f}ms\n\n"
            "## Token usage:\n"
            f"  - Generated tokens(approximately): {tk_num}\n"
            f"  - Token speed: {int(tk_num / (generate_result_time_cost / 1000.0))}/s"
        )

        # Add a condition check to call the end method only if langfuse_tracer exists
        if langfuse_tracer and "langfuse_generation" in locals():
            langfuse_output = "\n" + re.sub(r"^.*?(### Query:.*)", r"\1", prompt, flags=re.DOTALL)
            langfuse_output = {"time_elapsed:": re.sub(r"\n", "  \n", langfuse_output), "created_at": time.time()}
            langfuse_generation.update(output=langfuse_output)
            langfuse_generation.end()

        return {"answer": think + answer, "reference": refs, "prompt": re.sub(r"\n", "  \n", prompt), "created_at": time.time()}


    if langfuse_tracer:
        langfuse_generation = langfuse_tracer.start_generation(
            trace_context=trace_context, name="chat", model=llm_model_config["llm_name"], input={"prompt": prompt, "prompt4citation": prompt4citation, "messages": msg}
        )


    if stream:
        last_ans = ""
        answer = ""
        if llm_type == "chat":
            stream_gen = chat_mdl.chat_streamly(prompt + prompt4citation, msg[1:], gen_conf)
        else:
            stream_gen = chat_mdl.chat_streamly(prompt + prompt4citation, msg[1:], gen_conf, images=image_files)
        for ans in stream_gen:
            if thought:
                ans = re.sub(r"^.*</think>", "", ans, flags=re.DOTALL)
            answer = ans
            delta_ans = ans[len(last_ans):]
            if num_tokens_from_string(delta_ans) < 16:
                continue
            last_ans = answer
            yield {"answer": thought + answer, "reference": {}, "audio_binary": tts(tts_mdl, delta_ans)}
        delta_ans = answer[len(last_ans):]
        if delta_ans:
            yield {"answer": thought + answer, "reference": {}, "audio_binary": tts(tts_mdl, delta_ans)}
        yield decorate_answer(thought + answer)
    else:
        if llm_type == "chat":
            answer = chat_mdl.chat(prompt + prompt4citation, msg[1:], gen_conf)
        else:
            answer = chat_mdl.chat(prompt + prompt4citation, msg[1:], gen_conf, images=image_files)
        user_content = msg[-1].get("content", "[content not available]")
        logging.debug("User: {}|Assistant: {}".format(user_content, answer))
        res = decorate_answer(answer)
        res["audio_binary"] = tts(tts_mdl, answer)
        yield res

    return None


async def async_chat(dialog, messages, db, stream=True, **kwargs):
    """异步版本的 chat"""
    logging.debug("Begin async_chat")
    # 确保最后一条消息是用户的消息
    assert messages[-1]["role"] == "user", "The last content of this conversation is not from user."
    chat_start_ts = timer()

    llm_type = TenantLLMService.llm_id2llm_type(dialog.llm_id)
    if llm_type == "image2text":
        llm_model_config = TenantLLMService.get_model_config(db, dialog.tenant_id, LLMType.IMAGE2TEXT, dialog.llm_id)
    else:
        llm_model_config = TenantLLMService.get_model_config(db, dialog.tenant_id, LLMType.CHAT, dialog.llm_id)

    factory = llm_model_config.get("llm_factory", "") if llm_model_config else ""
    max_tokens = llm_model_config.get("max_tokens", 8192)

    check_llm_ts = timer()

    langfuse_tracer = None
    trace_context = {}
    langfuse_keys = TenantLangfuseService.filter_by_tenant(db, tenant_id=dialog.tenant_id)
    if langfuse_keys:
        langfuse = Langfuse(public_key=langfuse_keys.public_key, secret_key=langfuse_keys.secret_key, host=langfuse_keys.host)
        try:
            if langfuse.auth_check():
                langfuse_tracer = langfuse
                trace_id = langfuse_tracer.create_trace_id()
                trace_context = {"trace_id": trace_id}
        except Exception:
            # Skip langfuse tracing if connection fails
            pass

    check_langfuse_tracer_ts = timer()
    kbs, embd_mdl, rerank_mdl, chat_mdl, tts_mdl = get_models(db, dialog)
    toolcall_session, tools = kwargs.get("toolcall_session"), kwargs.get("tools")
    if toolcall_session and tools:
        chat_mdl.bind_tools(toolcall_session, tools)
    bind_models_ts = timer()

    kb_names = list([kb.name for kb in kbs])
    kb_tenant_ids = [kb.tenant_id for kb in kbs]  # 1:1 with kb_names, for correct collection lookup
    print("正在检索的知识库 --> ", kb_names)

    retriever = settings.retriever
    questions = [m["content"] for m in messages if m["role"] == "user"][-3:]
    filter_exp = kwargs["filter_condition"] if "filter_condition" in kwargs else ""
    attachments = kwargs["doc_ids"].split(",") if "doc_ids" in kwargs else []
    attachments_ = ""
    if "doc_ids" in messages[-1]:
        attachments = messages[-1]["doc_ids"]
    image_attachments = []
    image_files = []
    if "files" in messages[-1]:
        if llm_type == "chat":
            text_attachments, image_attachments = split_file_attachments(messages[-1]["files"])
        else:
            text_attachments, image_files = split_file_attachments(messages[-1]["files"], raw=True)
        attachments_ = "\n\n".join(text_attachments)

    prompt_config = dialog.prompt_config
    internet_enabled = kwargs.get("internet") is not False
    field_map = KnowledgebaseService.get_field_map(db, dialog.kb_ids)
    logging.debug(f"field_map retrieved: {field_map}")
    # 如果字段映射存在，尝试使用SQL检索答案
    if field_map:
        logging.debug("Use SQL to retrieval:{}".format(questions[-1]))
        ans = await use_sql(questions[-1], field_map, kb_tenant_ids, kb_names, chat_mdl, prompt_config.get("quote", True), dialog.kb_ids)
        # For aggregate queries (COUNT, SUM, etc.), chunks may be empty but answer is still valid
        if ans and (ans.get("reference", {}).get("chunks") or ans.get("answer")):
            yield ans
            return
        else:
            logging.debug("SQL failed or returned no results, falling back to vector search")

    param_keys = [p["key"] for p in prompt_config["parameters"]]
    logging.debug(f"attachments={attachments}, param_keys={param_keys}, embd_mdl={embd_mdl}")

    # 处理提示配置中的参数，确保必要的参数存在
    for p in prompt_config["parameters"]:
        if p["key"] == "knowledge":
            continue
        if p["key"] not in kwargs and not p["optional"]:
            raise KeyError("Miss parameter: " + p["key"])
        if p["key"] not in kwargs:
            prompt_config["system"] = prompt_config["system"].replace("{%s}" % p["key"], " ")

    if len(questions) > 1 and prompt_config.get("refine_multiturn"):
        questions = [await full_question(tenant_id=dialog.tenant_id, llm_id=dialog.llm_id, messages=messages)]
    else:
        questions = questions[-1:]

    if prompt_config.get("cross_languages"):
        questions = [await cross_languages(dialog.tenant_id, dialog.llm_id, questions[0], prompt_config["cross_languages"])]

    if dialog.meta_data_filter:
        metas = DocMetadataService.get_flatted_meta_by_kbs(db, dialog.kb_ids)
        attachments = await apply_meta_data_filter(dialog.meta_data_filter, metas, questions[-1], chat_mdl, attachments)

    if prompt_config.get("keyword", False):
        questions[-1] += await keyword_extraction(chat_mdl, questions[-1])

    refine_question_ts = timer()

    thought = ""
    kbinfos = {"total": 0, "chunks": [], "doc_aggs": []}
    knowledges = []

    # 检查prompt_config中是否包含"knowledge"参数，以决定是否进行知识检索
    if attachments is not None and "knowledge" in param_keys:
        logging.debug("Proceeding with retrieval")
        tenant_ids = list(set([kb.tenant_id for kb in kbs]))
        knowledges = []
        prompt_config_for_reasoning = prompt_config
        if not internet_enabled:
            prompt_config_for_reasoning = dict(prompt_config)
            prompt_config_for_reasoning.pop("tavily_api_key", None)
        if prompt_config.get("reasoning", False) or kwargs.get("reasoning"):
            reasoner = DeepResearcher(
                chat_mdl,
                prompt_config_for_reasoning,
                partial(
                    retriever.retrieval,
                    filter_exp="",
                    embd_mdl=embd_mdl,
                    tenant_id=kb_tenant_ids,
                    kb_names=kb_names,
                    page=1,
                    page_size=dialog.top_n,
                    similarity_threshold=0.2,
                    vector_similarity_weight=0.3,
                    doc_ids=attachments,
                    search_mode=dialog.search_mode,
                    kb_ids=dialog.kb_ids
                )
            )

            queue = asyncio.Queue()
            async def callback(msg: str):
                nonlocal queue
                await queue.put(msg + "<br/>")

            await callback("<START_DEEP_RESEARCH>")
            task = asyncio.create_task(reasoner.research(kbinfos, questions[-1], questions[-1], callback=callback))
            while True:
                msg = await queue.get()
                if msg.find("<START_DEEP_RESEARCH>") == 0:
                    yield {"answer": "", "reference": {}, "audio_binary": None, "final": False, "start_to_think": True}
                elif msg.find("<END_DEEP_RESEARCH>") == 0:
                    yield {"answer": "", "reference": {}, "audio_binary": None, "final": False, "end_to_think": True}
                    break
                else:
                    yield {"answer": msg, "reference": {}, "audio_binary": None, "final": False}

            await task

        else:
            if embd_mdl:
                kbinfos = await retriever.retrieval(
                    " ".join(questions),
                    filter_exp,
                    embd_mdl,
                    kb_tenant_ids,
                    kb_names,
                    1,
                    dialog.top_n,
                    dialog.similarity_threshold,
                    dialog.vector_similarity_weight,
                    doc_ids=attachments,
                    top=1024,
                    aggs=True,
                    rerank_mdl=rerank_mdl,
                    rank_feature=label_question(db, " ".join(questions), kbs),
                    search_mode=dialog.search_mode,
                    kb_ids=dialog.kb_ids
                )
                if prompt_config.get("toc_enhance"):
                    cks = await retriever.retrieval_by_toc(" ".join(questions), kbinfos["chunks"], tenant_ids, kb_names, chat_mdl, dialog.top_n)
                    if cks:
                        kbinfos["chunks"] = cks
                kbinfos["chunks"] = retriever.retrieval_by_children(kbinfos["chunks"], tenant_ids)
            if internet_enabled and prompt_config.get("tavily_api_key"):
                tav = Tavily(prompt_config["tavily_api_key"])
                tav_res = tav.retrieve_chunks(" ".join(questions))
                kbinfos["chunks"].extend(tav_res["chunks"])
                kbinfos["doc_aggs"].extend(tav_res["doc_aggs"])
            if prompt_config.get("use_kg"):
                ck = await settings.kg_retriever.retrieval(" ".join(questions), tenant_ids, dialog.kb_ids, embd_mdl, LLMBundle(db, dialog.tenant_id, LLMType.CHAT))
                if ck["content_with_weight"]:
                    kbinfos["chunks"].insert(0, ck)

    knowledges = kb_prompt(kbinfos, max_tokens)

    logging.debug("{}->{}".format(" ".join(questions), "\n->".join(knowledges)))

    retrieval_ts = timer()
    if not knowledges and prompt_config.get("empty_response"):
        empty_res = prompt_config["empty_response"]
        yield {"answer": empty_res, "reference": kbinfos, "prompt": "\n\n### Query:\n%s" % " ".join(questions),
               "audio_binary": tts(tts_mdl, empty_res), "final": True}
        return

    kwargs["knowledge"] = "\n------\n" + "\n\n------\n\n".join(knowledges)
    gen_conf = dialog.llm_setting

    msg = [{"role": "system", "content": prompt_config["system"].format(**kwargs) + attachments_}]
    prompt4citation = ""
    if knowledges and (prompt_config.get("quote", True) and kwargs.get("quote", True)):
        prompt4citation = citation_prompt()
    msg.extend([{"role": m["role"], "content": re.sub(r"##\d+\$\$", "", m["content"])} for m in messages if m["role"] != "system"])
    used_token_count, msg = message_fit_in(msg, int(max_tokens * 0.95))
    if llm_type == "chat" and image_attachments:
        convert_last_user_msg_to_multimodal(msg, image_attachments, factory)

    assert len(msg) >= 2, f"message_fit_in has bug: {msg}"
    prompt = msg[0]["content"]

    if "max_tokens" in gen_conf:
        gen_conf["max_tokens"] = min(gen_conf["max_tokens"], max_tokens - used_token_count)

    def decorate_answer(answer):
        nonlocal embd_mdl, prompt_config, knowledges, kwargs, kbinfos, prompt, retrieval_ts, questions, langfuse_tracer

        refs = []
        ans = answer.split("</think>")
        think = ""
        if len(ans) == 2:
            think = ans[0] + "</think>"
            answer = ans[1]

        if knowledges and (prompt_config.get("quote", True) and kwargs.get("quote", True)):
            idx = set([])
            if embd_mdl and not re.search(r"\[ID:([0-9]+)\]", answer):
                answer, idx = retriever.insert_citations(
                    answer,
                    [ck["content_ltks"] for ck in kbinfos["chunks"]],
                    [ck["vector"] for ck in kbinfos["chunks"]],
                    embd_mdl,
                    tkweight=1 - dialog.vector_similarity_weight,
                    vtweight=dialog.vector_similarity_weight,
                )
            else:
                for match in re.finditer(r"\[ID:([0-9]+)\]", answer):
                    i = int(match.group(1))
                    if i < len(kbinfos["chunks"]):
                        idx.add(i)

            answer, idx = repair_bad_citation_formats(answer, kbinfos, idx)

            idx = set([kbinfos["chunks"][int(i)]["doc_id"] for i in idx])
            recall_docs = [d for d in kbinfos["doc_aggs"] if d["doc_id"] in idx]
            if not recall_docs:
                recall_docs = kbinfos["doc_aggs"]
            kbinfos["doc_aggs"] = recall_docs

            refs = deepcopy(kbinfos)
            for c in refs["chunks"]:
                if c.get("vector"):
                    del c["vector"]

        if answer.lower().find("invalid key") >= 0 or answer.lower().find("invalid api") >= 0:
            answer += " Please set LLM API-Key in 'User Setting -> Model providers -> API-Key'"
        finish_chat_ts = timer()

        total_time_cost = (finish_chat_ts - chat_start_ts) * 1000
        check_llm_time_cost = (check_llm_ts - chat_start_ts) * 1000
        check_langfuse_tracer_cost = (check_langfuse_tracer_ts - check_llm_ts) * 1000
        bind_embedding_time_cost = (bind_models_ts - check_langfuse_tracer_ts) * 1000
        refine_question_time_cost = (refine_question_ts - bind_models_ts) * 1000
        retrieval_time_cost = (retrieval_ts - refine_question_ts) * 1000
        generate_result_time_cost = (finish_chat_ts - retrieval_ts) * 1000

        tk_num = num_tokens_from_string(think + answer)
        prompt += "\n\n### Query:\n%s" % " ".join(questions)
        prompt = (
            f"{prompt}\n\n"
            "## Time elapsed:\n"
            f"  - Total: {total_time_cost:.1f}ms\n"
            f"  - Check LLM: {check_llm_time_cost:.1f}ms\n"
            f"  - Check Langfuse tracer: {check_langfuse_tracer_cost:.1f}ms\n"
            f"  - Bind models: {bind_embedding_time_cost:.1f}ms\n"
            f"  - Query refinement(LLM): {refine_question_time_cost:.1f}ms\n"
            f"  - Retrieval: {retrieval_time_cost:.1f}ms\n"
            f"  - Generate answer: {generate_result_time_cost:.1f}ms\n\n"
            "## Token usage:\n"
            f"  - Generated tokens(approximately): {tk_num}\n"
            f"  - Token speed: {int(tk_num / (generate_result_time_cost / 1000.0))}/s"
        )

        if langfuse_tracer and "langfuse_generation" in locals():
            langfuse_output = "\n" + re.sub(r"^.*?(### Query:.*)", r"\1", prompt, flags=re.DOTALL)
            langfuse_output = {"time_elapsed:": re.sub(r"\n", "  \n", langfuse_output), "created_at": time.time()}
            langfuse_generation.update(output=langfuse_output)
            langfuse_generation.end()

        return {"answer": think + answer, "reference": refs, "prompt": re.sub(r"\n", "  \n", prompt), "created_at": time.time()}

    if langfuse_tracer:
        langfuse_generation = langfuse_tracer.start_generation(
            trace_context=trace_context, name="chat", model=llm_model_config["llm_name"], input={"prompt": prompt, "prompt4citation": prompt4citation, "messages": msg}
        )

    if stream:
        if llm_type == "chat":
            stream_iter = chat_mdl.async_chat_streamly_delta(prompt + prompt4citation, msg[1:], gen_conf)
        else:
            stream_iter = chat_mdl.async_chat_streamly_delta(prompt + prompt4citation, msg[1:], gen_conf, images=image_files)
        last_state = None
        async for kind, value, state in _stream_with_think_delta(stream_iter):
            last_state = state
            if kind == "marker":
                flags = {"start_to_think": True} if value == "<think>" else {"end_to_think": True}
                yield {"answer": "", "reference": {}, "audio_binary": None, "final": False, **flags}
                continue
            yield {"answer": value, "reference": {}, "audio_binary": tts(tts_mdl, value), "final": False}
        full_answer = last_state.full_text if last_state else ""
        if full_answer:
            final = decorate_answer(thought + full_answer)
            final["final"] = True
            final["audio_binary"] = None
            final["answer"] = ""
            yield final
    else:
        if llm_type == "chat":
            answer = await chat_mdl.async_chat(prompt + prompt4citation, msg[1:], gen_conf)
        else:
            answer = await chat_mdl.async_chat(prompt + prompt4citation, msg[1:], gen_conf, images=image_files)
        user_content = msg[-1].get("content", "[content not available]")
        logging.debug("User: {}|Assistant: {}".format(user_content, answer))
        res = decorate_answer(answer)
        res["audio_binary"] = tts(tts_mdl, answer)
        yield res

    return


async def use_sql(question, field_map, tenant_id, kb_names, chat_mdl, quota=True, kb_ids=None):
    logging.debug(f"use_sql: Question: {question}")

    doc_engine = settings.DOC_ENGINE.lower()

    # Construct the full table name
    base_table = index_name(tenant_id, kb_names)
    if doc_engine == "infinity" and kb_ids and len(kb_ids) == 1:
        table_name = f"{base_table}_{kb_ids[0]}"
        logging.debug(f"use_sql: Using Infinity table name: {table_name}")
    else:
        table_name = base_table
        logging.debug(f"use_sql: Using table name: {table_name}")

    def is_row_count_question(q: str) -> bool:
        q = (q or "").lower()
        if not re.search(r"\bhow many rows\b|\bnumber of rows\b|\brow count\b|多少行|多少条|总数", q):
            return False
        return bool(re.search(r"\bdataset\b|\btable\b|\bspreadsheet\b|\bexcel\b|数据|表格|记录", q))

    # Engine-specific SQL prompts and user_prompt
    json_field_names = list(field_map.keys())
    uses_chunk_data = doc_engine in ("infinity", "milvus", "oceanbase")

    if doc_engine == "infinity":
        row_count_override = f"SELECT COUNT(*) AS rows FROM {table_name}" if is_row_count_question(question) else None
        sys_prompt = """You are a Database Administrator. Write SQL for a table with JSON 'chunk_data' column.

JSON Extraction: json_extract_string(chunk_data, '$.FieldName')
Numeric Cast: CAST(json_extract_string(chunk_data, '$.FieldName') AS INTEGER/FLOAT)
NULL Check: json_extract_isnull(chunk_data, '$.FieldName') == false

RULES:
1. Use EXACT field names (case-sensitive) from the list below
2. For SELECT: include doc_id, docnm, and json_extract_string() for requested fields
3. For COUNT: use COUNT(*) or COUNT(DISTINCT json_extract_string(...))
4. Add AS alias for extracted field names
5. DO NOT select 'content' field
6. Only add NULL check (json_extract_isnull() == false) in WHERE clause when:
   - Question asks to "show me" or "display" specific columns
   - Question mentions "not null" or "excluding null"
   - Add NULL check for count specific column
   - DO NOT add NULL check for COUNT(*) queries
7. Output ONLY the SQL, no explanations"""
        user_prompt = """Table: {}
Fields (EXACT case): {}
{}
Question: {}
Write SQL using json_extract_string() with exact field names. Include doc_id, docnm for data queries. Only SQL.""".format(
            table_name,
            ", ".join(json_field_names),
            "\n".join([f"  - {field}" for field in json_field_names]),
            question
        )
    elif doc_engine == "oceanbase":
        row_count_override = f"SELECT COUNT(*) AS rows FROM {table_name}" if is_row_count_question(question) else None
        sys_prompt = """You are a Database Administrator. Write SQL for a table with JSON 'chunk_data' column.

JSON Extraction: json_extract_string(chunk_data, '$.FieldName')
Numeric Cast: CAST(json_extract_string(chunk_data, '$.FieldName') AS INTEGER/FLOAT)
NULL Check: json_extract_isnull(chunk_data, '$.FieldName') == false

RULES:
1. Use EXACT field names (case-sensitive) from the list below
2. For SELECT: include doc_id, docnm_kwd, and json_extract_string() for requested fields
3. For COUNT: use COUNT(*) or COUNT(DISTINCT json_extract_string(...))
4. Add AS alias for extracted field names
5. DO NOT select 'content' field
6. Only add NULL check (json_extract_isnull() == false) in WHERE clause when:
   - Question asks to "show me" or "display" specific columns
   - Question mentions "not null" or "excluding null"
   - Add NULL check for count specific column
   - DO NOT add NULL check for COUNT(*) queries (COUNT(*) counts all rows including nulls)
7. Output ONLY the SQL, no explanations"""
        user_prompt = """Table: {}
Fields (EXACT case): {}
{}
Question: {}
Write SQL using json_extract_string() with exact field names. Include doc_id, docnm_kwd for data queries. Only SQL.""".format(
            table_name,
            ", ".join(json_field_names),
            "\n".join([f"  - {field}" for field in json_field_names]),
            question
        )
    elif doc_engine == "milvus":
        row_count_override = f"SELECT COUNT(*) AS rows FROM {table_name}" if is_row_count_question(question) else None
        sys_prompt = """You are a Database Administrator. Write SQL for a Milvus collection with JSON 'chunk_data' column.

Field Access: chunk_data["FieldName"]
String comparison: chunk_data["FieldName"] == 'value'
Numeric comparison: chunk_data["FieldName"] > 100

RULES:
1. Use EXACT field names (case-sensitive) from the list below
2. For SELECT: include doc_id, docnm_kwd, and chunk_data["field"] for requested fields
3. For COUNT: use COUNT(*) or COUNT(DISTINCT chunk_data["field"])
4. For SUM/AVG/MIN/MAX: use SUM(chunk_data["field"]) etc.
5. Add AS alias for chunk_data fields
6. DO NOT select 'content_with_weight' field
7. In WHERE clause use: chunk_data["field"] == 'value' or chunk_data["field"] > 100
8. Output ONLY the SQL, no explanations"""
        user_prompt = """Table: {}
Fields (EXACT case): {}
{}
Question: {}
Write SQL using chunk_data["field"] with exact field names. Include doc_id, docnm_kwd for data queries. Only SQL.""".format(
            table_name,
            ", ".join(json_field_names),
            "\n".join([f"  - {field}" for field in json_field_names]),
            question
        )
    else:
        row_count_override = None
        sys_prompt = """You are a Database Administrator. Write SQL queries.

RULES:
1. Use EXACT field names from the schema below (e.g., product_tks, not product)
2. Quote field names starting with digit: "123_field"
3. Add IS NOT NULL in WHERE clause when:
   - Question asks to "show me" or "display" specific columns
4. Include doc_id/docnm_kwd in non-aggregate statement
5. Output ONLY the SQL, no explanations"""
        user_prompt = """Table: {}
Available fields:
{}
Question: {}
Write SQL using exact field names above. Include doc_id, docnm_kwd for data queries. Only SQL.""".format(
            table_name,
            "\n".join([f"  - {k} ({v})" for k, v in field_map.items()]),
            question
        )

    tried_times = 0

    async def get_table():
        nonlocal sys_prompt, user_prompt, question, tried_times, row_count_override
        if row_count_override:
            sql = row_count_override
        else:
            sql = await chat_mdl.async_chat(sys_prompt, [{"role": "user", "content": user_prompt}], {"temperature": 0.06})
        logging.debug(f"use_sql: Raw SQL from LLM: {repr(sql[:500])}")
        # Remove think blocks if present
        sql = re.sub(r"</think>\n.*?\n\s*", "", sql, flags=re.DOTALL)
        sql = re.sub(r"思考\n.*?\n", "", sql, flags=re.DOTALL)
        # Remove markdown code blocks
        sql = re.sub(r"```(?:sql)?\s*", "", sql, flags=re.IGNORECASE)
        sql = re.sub(r"```\s*$", "", sql, flags=re.IGNORECASE)
        # Remove trailing semicolons
        sql = sql.rstrip().rstrip(';').strip()

        # Add kb_id filter for non-Infinity engines (Infinity already has it in table name)
        if doc_engine != "infinity" and kb_ids:
            if len(kb_ids) == 1:
                kb_filter = f"kb_id = '{kb_ids[0]}'"
            else:
                kb_filter = "(" + " OR ".join([f"kb_id = '{kb_id}'" for kb_id in kb_ids]) + ")"

            if "where " not in sql.lower():
                o = sql.lower().split("order by")
                if len(o) > 1:
                    sql = o[0] + f" WHERE {kb_filter}  order by " + o[1]
                else:
                    sql += f" WHERE {kb_filter}"
            elif "kb_id =" not in sql.lower() and "kb_id=" not in sql.lower():
                sql = re.sub(r"\bwhere\b ", f"where {kb_filter} and ", sql, flags=re.IGNORECASE)

        logging.debug(f"{question} get SQL(refined): {sql}")
        tried_times += 1
        logging.debug(f"use_sql: Executing SQL retrieval (attempt {tried_times})")
        tbl = settings.retriever.sql_retrieval(sql, format="json")
        if tbl is None:
            logging.debug("use_sql: SQL retrieval returned None")
            return None, sql
        logging.debug(f"use_sql: SQL retrieval completed, got {len(tbl.get('rows', []))} rows")
        return tbl, sql

    try:
        tbl, sql = await get_table()
        logging.debug(f"use_sql: Initial SQL execution SUCCESS. SQL: {sql}")
    except Exception as e:
        logging.warning(f"use_sql: Initial SQL execution FAILED with error: {e}")
        # Build engine-specific retry prompt
        if uses_chunk_data:
            if doc_engine in ("infinity", "oceanbase"):
                syntax_hint = "json_extract_string(chunk_data, '$.field_name')"
            else:
                syntax_hint = 'chunk_data["field_name"]'
            user_prompt = """
Table name: {};
JSON fields available in 'chunk_data' column (use these exact names in {}):
{}

Question: {}
Please write the SQL using {} with the field names from the list above. Only SQL, no explanations.

The SQL error you provided last time is as follows:
{}

Please correct the error and write SQL again using {} syntax with the correct field names. Only SQL, no explanations.
""".format(table_name, syntax_hint, "\n".join([f"  - {field}" for field in json_field_names]),
           question, syntax_hint, e, syntax_hint)
        else:
            user_prompt = """
Table name: {};
Table of database fields are as follows (use the field names directly in SQL):
{}

Question are as follows:
{}
Please write the SQL using the exact field names above, only SQL, without any other explanations or text.

The SQL error you provided last time is as follows:
{}

Please correct the error and write SQL again using the exact field names above, only SQL, without any other explanations or text.
""".format(table_name, "\n".join([f"{k} ({v})" for k, v in field_map.items()]), question, e)
        try:
            tbl, sql = await get_table()
            logging.debug(f"use_sql: Retry SQL execution SUCCESS. SQL: {sql}")
        except Exception:
            logging.error("use_sql: Retry SQL execution also FAILED, returning None")
            return

    if tbl is None or "error" in tbl or "rows" not in tbl or len(tbl["rows"]) == 0:
        logging.warning(f"use_sql: No valid rows returned, returning None")
        return None

    logging.debug(f"use_sql: Proceeding with {len(tbl['rows'])} rows to build answer")

    # Case-insensitive column index matching (aligned with ragflow)
    docid_idx = set([ii for ii, c in enumerate(tbl["columns"]) if c["name"].lower() == "doc_id"])
    doc_name_idx = set([ii for ii, c in enumerate(tbl["columns"]) if c["name"].lower() in ["docnm_kwd", "docnm"]])

    logging.debug(f"use_sql: All columns: {[(i, c['name']) for i, c in enumerate(tbl['columns'])]}")
    logging.debug(f"use_sql: docid_idx={docid_idx}, doc_name_idx={doc_name_idx}")

    column_idx = [ii for ii in range(len(tbl["columns"])) if ii not in (docid_idx | doc_name_idx)]

    # Helper: map column names to display names
    def map_column_name(col_name):
        if col_name.lower() == "count(star)":
            return "COUNT(*)"
        as_match = re.search(r'\s+AS\s+([^\s,)]+)', col_name, re.IGNORECASE)
        if as_match:
            alias = as_match.group(1).strip('"\'')
            if alias in field_map:
                return re.sub(r"(/.*|（[^（）]+）)", "", field_map[alias])
            for field_key, display_value in field_map.items():
                if field_key.lower() == alias.lower():
                    return re.sub(r"(/.*|（[^（）]+）)", "", display_value)
            return alias
        if col_name in field_map:
            return re.sub(r"(/.*|（[^（）]+）)", "", field_map[col_name])
        col_lower = col_name.lower()
        for field_key, display_value in field_map.items():
            if field_key.lower() == col_lower:
                return re.sub(r"(/.*|（[^（）]+）)", "", display_value)
        result = col_name
        for field_name, display_name in field_map.items():
            result = result.replace(field_name, display_name)
        result = re.sub(r"(/.*|（[^（）]+）)", "", result)
        return result

    # Compose Markdown table header
    columns = (
        "|" + "|".join([map_column_name(tbl["columns"][i]["name"]) for i in column_idx])
        + ("|Source|" if docid_idx and doc_name_idx else "|")
    )
    line = "|" + "|".join(["------" for _ in range(len(column_idx))]) + ("|------|" if docid_idx and doc_name_idx else "|")

    # Build rows using dict-based access (robust against column order)
    rows = []
    for row_idx, r in enumerate(tbl["rows"]):
        row_dict = {tbl["columns"][i]["name"]: r[i] for i in range(len(tbl["columns"])) if i < len(r)}
        row_values = []
        for col_idx in column_idx:
            col_name = tbl["columns"][col_idx]["name"]
            value = row_dict.get(col_name, " ")
            row_values.append(remove_redundant_spaces(str(value)).replace("None", " "))
        if docid_idx and doc_name_idx:
            row_values.append(f" ##{row_idx}$$")
        row_str = "|" + "|".join(row_values) + "|"
        if re.sub(r"[ |]+", "", row_str):
            rows.append(row_str)
    rows = "\n".join(rows)
    rows = re.sub(r"T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+Z)?\|", "|", rows)

    if not docid_idx or not doc_name_idx:
        logging.warning(f"use_sql: SQL missing doc_id or docnm field. docid_idx={docid_idx}, doc_name_idx={doc_name_idx}. SQL: {sql}")
        # For aggregate queries, fetch doc_id/docnm separately to provide source chunks
        if re.search(r"(count|sum|avg|max|min|distinct)\s*\(", sql.lower()):
            answer = "\n".join([columns, line, rows])
            where_match = re.search(r"\bwhere\b(.+?)(?:\bgroup by\b|\border by\b|\blimit\b|$)", sql, re.IGNORECASE)
            if where_match:
                where_clause = where_match.group(1).strip()
                docnm_field = "docnm" if doc_engine == "infinity" else "docnm_kwd"
                chunks_sql = f"select doc_id, {docnm_field} from {table_name} where {where_clause}"
                if "limit" not in chunks_sql.lower():
                    chunks_sql += " limit 20"
                logging.debug(f"use_sql: Fetching chunks with SQL: {chunks_sql}")
                try:
                    chunks_tbl = settings.retriever.sql_retrieval(chunks_sql, format="json")
                    if chunks_tbl and chunks_tbl.get("rows") and len(chunks_tbl["rows"]) > 0:
                        chunks_did_idx = next((i for i, c in enumerate(chunks_tbl["columns"]) if c["name"].lower() == "doc_id"), None)
                        chunks_dn_idx = next((i for i, c in enumerate(chunks_tbl["columns"]) if c["name"].lower() in ["docnm_kwd", "docnm"]), None)
                        if chunks_did_idx is not None and chunks_dn_idx is not None:
                            chunks = [{"doc_id": r[chunks_did_idx], "docnm_kwd": r[chunks_dn_idx]} for r in chunks_tbl["rows"]]
                            doc_aggs = {}
                            for r in chunks_tbl["rows"]:
                                did, dn = r[chunks_did_idx], r[chunks_dn_idx]
                                if did not in doc_aggs:
                                    doc_aggs[did] = {"doc_name": dn, "count": 0}
                                doc_aggs[did]["count"] += 1
                            doc_aggs_list = [{"doc_id": did, "doc_name": d["doc_name"], "count": d["count"]} for did, d in doc_aggs.items()]
                            logging.debug(f"use_sql: Returning aggregate answer with {len(chunks)} chunks from {len(doc_aggs)} documents")
                            return {"answer": answer, "reference": {"chunks": chunks, "doc_aggs": doc_aggs_list}, "prompt": sys_prompt}
                except Exception as e:
                    logging.warning(f"use_sql: Failed to fetch chunks: {e}")
            return {"answer": answer, "reference": {"chunks": [], "doc_aggs": []}, "prompt": sys_prompt}
        return {"answer": "\n".join([columns, line, rows]), "reference": {"chunks": [], "doc_aggs": []}, "prompt": sys_prompt}

    docid_idx = list(docid_idx)[0]
    doc_name_idx = list(doc_name_idx)[0]
    doc_aggs = {}
    for r in tbl["rows"]:
        if r[docid_idx] not in doc_aggs:
            doc_aggs[r[docid_idx]] = {"doc_name": r[doc_name_idx], "count": 0}
        doc_aggs[r[docid_idx]]["count"] += 1

    result = {
        "answer": "\n".join([columns, line, rows]),
        "reference": {
            "chunks": [{"doc_id": r[docid_idx], "docnm_kwd": r[doc_name_idx]} for r in tbl["rows"]],
            "doc_aggs": [{"doc_id": did, "doc_name": d["doc_name"], "count": d["count"]} for did, d in doc_aggs.items()],
        },
        "prompt": sys_prompt,
    }
    logging.debug(f"use_sql: Returning answer with {len(result['reference']['chunks'])} chunks from {len(doc_aggs)} documents")
    return result


def clean_tts_text(text: str) -> str:
    if not text:
        return ""

    text = text.encode("utf-8", "ignore").decode("utf-8", "ignore")

    text = re.sub(r"[\x00-\x08\x0B-\x0C\x0E-\x1F\x7F]", "", text)

    emoji_pattern = re.compile(
        "[\U0001F600-\U0001F64F"
        "\U0001F300-\U0001F5FF"
        "\U0001F680-\U0001F6FF"
        "\U0001F1E0-\U0001F1FF"
        "\U00002700-\U000027BF"
        "\U0001F900-\U0001F9FF"
        "\U0001FA70-\U0001FAFF"
        "\U0001FAD0-\U0001FAFF]+",
        flags=re.UNICODE
    )
    text = emoji_pattern.sub("", text)

    text = re.sub(r"\s+", " ", text).strip()

    MAX_LEN = 500
    if len(text) > MAX_LEN:
        text = text[:MAX_LEN]

    return text


def tts(tts_mdl, text):
    if not tts_mdl or not text:
        return None
    text = clean_tts_text(text)
    if not text:
        return None
    bin = b""
    try:
        for chunk in tts_mdl.tts(text):
            bin += chunk
    except Exception as e:
        logging.error(f"TTS failed: {e}, text={text!r}")
        return None
    return binascii.hexlify(bin).decode("utf-8")


class _ThinkStreamState:
    def __init__(self) -> None:
        self.full_text = ""
        self.last_idx = 0
        self.endswith_think = False
        self.last_full = ""
        self.last_model_full = ""
        self.in_think = False
        self.buffer = ""


def _next_think_delta(state: _ThinkStreamState) -> str:
    full_text = state.full_text
    if full_text == state.last_full:
        return ""
    state.last_full = full_text
    delta_ans = full_text[state.last_idx:]

    if delta_ans.find("<think>") == 0:
        state.last_idx += len("<think>")
        return "<think>"
    if delta_ans.find("<think>") > 0:
        delta_text = full_text[state.last_idx:state.last_idx + delta_ans.find("<think>")]
        state.last_idx += delta_ans.find("<think>")
        return delta_text
    if delta_ans.endswith("</think>"):
        state.endswith_think = True
    elif state.endswith_think:
        state.endswith_think = False
        return "</think>"

    state.last_idx = len(full_text)
    if full_text.endswith("</think>"):
        state.last_idx -= len("</think>")
    return re.sub(r"(<think>|</think>)", "", delta_ans)


async def _stream_with_think_delta(stream_iter, min_tokens: int = 16):
    state = _ThinkStreamState()
    async for chunk in stream_iter:
        if not chunk:
            continue
        if chunk.startswith(state.last_model_full):
            new_part = chunk[len(state.last_model_full):]
            state.last_model_full = chunk
        else:
            new_part = chunk
            state.last_model_full += chunk
        if not new_part:
            continue
        state.full_text += new_part
        delta = _next_think_delta(state)
        if not delta:
            continue
        if delta in ("<think>", "</think>"):
            if delta == "<think>" and state.in_think:
                continue
            if delta == "</think>" and not state.in_think:
                continue
            if state.buffer:
                yield ("text", state.buffer, state)
                state.buffer = ""
            state.in_think = delta == "<think>"
            yield ("marker", delta, state)
            continue
        state.buffer += delta
        if num_tokens_from_string(state.buffer) < min_tokens:
            continue
        yield ("text", state.buffer, state)
        state.buffer = ""

    if state.buffer:
        yield ("text", state.buffer, state)
        state.buffer = ""
    if state.endswith_think:
        yield ("marker", "</think>", state)


def ask(db: Session, question, kb_ids, tenant_id, chat_llm_name=None, search_config=None):
    if search_config is None:
        search_config = {}
    doc_ids = search_config.get("doc_ids", [])
    rerank_mdl = None
    kb_ids = search_config.get("kb_ids", kb_ids)
    chat_llm_name = search_config.get("chat_id", chat_llm_name)
    rerank_id = search_config.get("rerank_id", "")
    meta_data_filter = search_config.get("meta_data_filter")

    kbs = KnowledgebaseService.get_by_ids(db, kb_ids)
    embedding_list = list(set([kb.embd_id for kb in kbs]))

    is_knowledge_graph = all([kb.parser_id == ParserType.KG for kb in kbs])
    retriever = settings.retriever if not is_knowledge_graph else settings.kg_retriever

    embd_mdl = LLMBundle(db, tenant_id, LLMType.EMBEDDING, embedding_list[0])
    chat_mdl = LLMBundle(db, tenant_id, LLMType.CHAT, chat_llm_name)
    if rerank_id:
        rerank_mdl = LLMBundle(db, tenant_id, LLMType.RERANK, rerank_id)
    max_tokens = chat_mdl.max_length
    tenant_ids = list([kb.tenant_id for kb in kbs])

    if meta_data_filter:
        metas = DocMetadataService.get_flatted_meta_by_kbs(db, kb_ids)
        doc_ids = asyncio.run(apply_meta_data_filter(meta_data_filter, metas, question, chat_mdl, doc_ids))

    filter_exp = ""  # todo 暂时不提供权限过滤的查询，如果需要这边需要完善
    kb_names = list([kb.name for kb in kbs])
    kbinfos = asyncio.run(retriever.retrieval(
        question=question,
        filter_exp=filter_exp,
        embd_mdl=embd_mdl,
        tenant_id=tenant_ids,
        kb_names=kb_names,
        page=1,
        page_size=12,
        similarity_threshold=search_config.get("similarity_threshold", 0.1),
        vector_similarity_weight=search_config.get("vector_similarity_weight", 0.3),
        top=search_config.get("top_k", 1024),
        doc_ids=doc_ids,
        aggs=True,
        rerank_mdl=rerank_mdl,
        rank_feature=label_question(db, question, kbs),
        search_mode=None #todo 无法传递应用里的配置，所以只能使用一种默认检索模式
    ))
    knowledges = kb_prompt(kbinfos, max_tokens)
    sys_prompt = PROMPT_JINJA_ENV.from_string(ASK_SUMMARY).render(knowledge="\n".join(knowledges))

    msg = [{"role": "user", "content": question}]

    def decorate_answer(answer):
        nonlocal knowledges, kbinfos, sys_prompt
        answer, idx = retriever.insert_citations(answer, [ck["content_ltks"] for ck in kbinfos["chunks"]], [ck["vector"] for ck in kbinfos["chunks"]], embd_mdl, tkweight=0.7, vtweight=0.3)
        idx = set([kbinfos["chunks"][int(i)]["doc_id"] for i in idx])
        recall_docs = [d for d in kbinfos["doc_aggs"] if d["doc_id"] in idx]
        if not recall_docs:
            recall_docs = kbinfos["doc_aggs"]
        kbinfos["doc_aggs"] = recall_docs
        refs = deepcopy(kbinfos)
        for c in refs["chunks"]:
            if c.get("vector"):
                del c["vector"]

        if answer.lower().find("invalid key") >= 0 or answer.lower().find("invalid api") >= 0:
            answer += " Please set LLM API-Key in 'User Setting -> Model providers -> API-Key'"
        refs["chunks"] = chunks_format(refs)
        return {"answer": answer, "reference": refs}

    answer = ""
    for ans in chat_mdl.chat_streamly(sys_prompt, msg, {"temperature": 0.1}):
        answer = ans
        yield {"answer": answer, "reference": {}}
    yield decorate_answer(answer)


async def async_ask(db: Session, question, kb_ids, tenant_id, chat_llm_name=None, search_config=None):
    """异步版本的 ask"""
    if search_config is None:
        search_config = {}
    doc_ids = search_config.get("doc_ids", [])
    rerank_mdl = None
    kb_ids = search_config.get("kb_ids", kb_ids)
    chat_llm_name = search_config.get("chat_id", chat_llm_name)
    rerank_id = search_config.get("rerank_id", "")
    meta_data_filter = search_config.get("meta_data_filter")

    kbs = KnowledgebaseService.get_by_ids(db, kb_ids)
    embedding_list = list(set([kb.embd_id for kb in kbs]))

    is_knowledge_graph = all([kb.parser_id == ParserType.KG for kb in kbs])
    retriever = settings.retriever if not is_knowledge_graph else settings.kg_retriever

    embd_mdl = LLMBundle(db, tenant_id, LLMType.EMBEDDING, embedding_list[0])
    chat_mdl = LLMBundle(db, tenant_id, LLMType.CHAT, chat_llm_name)
    if rerank_id:
        rerank_mdl = LLMBundle(db, tenant_id, LLMType.RERANK, rerank_id)
    max_tokens = chat_mdl.max_length
    tenant_ids = list([kb.tenant_id for kb in kbs])

    if meta_data_filter:
        metas = DocMetadataService.get_flatted_meta_by_kbs(db, kb_ids)
        doc_ids = await apply_meta_data_filter(meta_data_filter, metas, question, chat_mdl, doc_ids)

    filter_exp = ""
    kb_names = list([kb.name for kb in kbs])
    kbinfos = await retriever.retrieval(
        question=question,
        filter_exp=filter_exp,
        embd_mdl=embd_mdl,
        tenant_id=tenant_ids,
        kb_names=kb_names,
        page=1,
        page_size=12,
        similarity_threshold=search_config.get("similarity_threshold", 0.1),
        vector_similarity_weight=search_config.get("vector_similarity_weight", 0.3),
        top=search_config.get("top_k", 1024),
        doc_ids=doc_ids,
        aggs=True,
        rerank_mdl=rerank_mdl,
        rank_feature=label_question(db, question, kbs),
        search_mode=None
    )

    knowledges = kb_prompt(kbinfos, max_tokens)
    sys_prompt = PROMPT_JINJA_ENV.from_string(ASK_SUMMARY).render(knowledge="\n".join(knowledges))

    msg = [{"role": "user", "content": question}]

    def decorate_answer(answer):
        nonlocal knowledges, kbinfos, sys_prompt
        answer, idx = retriever.insert_citations(answer, [ck["content_ltks"] for ck in kbinfos["chunks"]], [ck["vector"] for ck in kbinfos["chunks"]], embd_mdl, tkweight=0.7, vtweight=0.3)
        idx = set([kbinfos["chunks"][int(i)]["doc_id"] for i in idx])
        recall_docs = [d for d in kbinfos["doc_aggs"] if d["doc_id"] in idx]
        if not recall_docs:
            recall_docs = kbinfos["doc_aggs"]
        kbinfos["doc_aggs"] = recall_docs
        refs = deepcopy(kbinfos)
        for c in refs["chunks"]:
            if c.get("vector"):
                del c["vector"]

        if answer.lower().find("invalid key") >= 0 or answer.lower().find("invalid api") >= 0:
            answer += " Please set LLM API-Key in 'User Setting -> Model providers -> API-Key'"
        refs["chunks"] = chunks_format(refs)
        return {"answer": answer, "reference": refs}

    stream_iter = chat_mdl.async_chat_streamly_delta(sys_prompt, msg, {"temperature": 0.1})
    last_state = None
    async for kind, value, state in _stream_with_think_delta(stream_iter):
        last_state = state
        if kind == "marker":
            flags = {"start_to_think": True} if value == "<think>" else {"end_to_think": True}
            yield {"answer": "", "reference": {}, "final": False, **flags}
            continue
        yield {"answer": value, "reference": {}, "final": False}
    full_answer = last_state.full_text if last_state else ""
    final = decorate_answer(full_answer)
    final["final"] = True
    final["answer"] = ""
    yield final


async def gen_mindmap(db: Session, question, kb_ids, tenant_id, search_config=None):
    if search_config is None:
        search_config = {}
    meta_data_filter = search_config.get("meta_data_filter", {})
    doc_ids = search_config.get("doc_ids", [])
    rerank_id = search_config.get("rerank_id", "")
    rerank_mdl = None
    kbs = KnowledgebaseService.get_by_ids(db, kb_ids)
    if not kbs:
        return {"error": "No KB selected"}
    embedding_list = list(set([kb.embd_id for kb in kbs]))
    tenant_ids = list(set([kb.tenant_id for kb in kbs]))
    kb_names = list(set([kb.name for kb in kbs]))

    embd_mdl = LLMBundle(db, tenant_id, LLMType.EMBEDDING, llm_name=embedding_list[0])
    chat_mdl = LLMBundle(db, tenant_id, LLMType.CHAT, llm_name=search_config.get("chat_id", ""))
    if rerank_id:
        rerank_mdl = LLMBundle(db, tenant_id, LLMType.RERANK, rerank_id)

    if meta_data_filter:
        metas = DocMetadataService.get_flatted_meta_by_kbs(db, kb_ids)
        doc_ids = await apply_meta_data_filter(meta_data_filter, metas, question, chat_mdl, doc_ids)

    ranks = await settings.retriever.retrieval(
        question=question,
        filter_exp="",
        embd_mdl=embd_mdl,
        tenant_id=tenant_ids,
        kb_names=kb_names,
        page=1,
        page_size=12,
        similarity_threshold=search_config.get("similarity_threshold", 0.2),
        vector_similarity_weight=search_config.get("vector_similarity_weight", 0.3),
        top=search_config.get("top_k", 1024),
        doc_ids=doc_ids,
        aggs=False,
        rerank_mdl=rerank_mdl,
        rank_feature=label_question(db, question, kbs),
        kb_ids=kb_ids,
    )
    mindmap = MindMapExtractor(chat_mdl)
    contents = [c.get("content_with_weight") or c.get("text") for c in ranks["chunks"]]
    contents = [c for c in contents if isinstance(c, str) and c]
    mind_map = await mindmap(contents)
    return mind_map.output