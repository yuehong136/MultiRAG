# services_canvas_sqlalchemy.py

from __future__ import annotations

import json
import logging
import time
import traceback
from typing import Any, Iterable
from uuid import uuid4

import tiktoken
from sqlalchemy import select, func, asc
from sqlalchemy.orm import Session
from sqlalchemy.sql import desc as sa_desc

from agent.canvas import Canvas
from agent.component.llm import LLM
from api.db import TenantPermission
from api.db.db_models import (
    CanvasTemplate,
    User,
    UserCanvas,
    API4Conversation,
)
from api.db.services.common_service import CommonService
from api.db.services.api_service import API4ConversationService
from api.utils import get_uuid
from api.utils.api_utils import get_data_openai


# ---------------------------
# 通用 Service（SQLAlchemy）
# ---------------------------
class SACommonService(CommonService):
    """
    继承你现有的 CommonService，但将默认实现改成 SQLAlchemy 版本。
    需要你项目里的 CommonService 支持 __init__(model)，或者你可以直接把这里当独立基类。
    """
    def __init__(self, model):
        super().__init__(model)
        self.model = model

    @classmethod
    def get_by_id(cls, db: Session, id_: str):
        obj = db.get(cls.model, id_)
        return (obj is not None), obj

    @classmethod
    def save(cls, db: Session, **kwargs):
        obj = cls.model(**kwargs)
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj

    @classmethod
    def update_by_id(cls, db: Session, id_: str, values: dict):
        obj = db.get(cls.model, id_)
        if not obj:
            return 0
        for k, v in values.items():
            setattr(obj, k, v)
        db.add(obj)
        db.commit()
        return 1

    @classmethod
    def delete_by_id(cls, db: Session, id_: str):
        obj = db.get(cls.model, id_)
        if not obj:
            return 0
        db.delete(obj)
        db.commit()
        return 1

    @classmethod
    def query(cls, db: Session, **filters):
        stmt = select(cls.model)
        for k, v in filters.items():
            if v is None:
                continue
            stmt = stmt.where(getattr(cls.model, k) == v)
        rows = db.execute(stmt).scalars().all()
        return rows


# ---------------------------
# CanvasTemplateService
# ---------------------------
class CanvasTemplateService(SACommonService):
    model = CanvasTemplate

    def __init__(self):
        super().__init__(CanvasTemplate)

    @classmethod
    def get_all(cls, db: Session):
        stmt = select(cls.model)
        return db.execute(stmt).scalars().all()


# ---------------------------
# UserCanvasService
# ---------------------------
class UserCanvasService(SACommonService):
    model = UserCanvas

    def __init__(self):
        super().__init__(UserCanvas)

    @classmethod
    def get_list(
        cls,
        db: Session,
        tenant_id: str,
        page_number: int,
        items_per_page: int,
        orderby: str,
        desc: bool,
        id: str | None,
        title: str | None,
    ):
        """
        等价 Peewee 版本：按 user_id(tenant) 过滤，支持 id/title 精确过滤，排序+分页，返回字典行。
        """
        # 选择列（与 Peewee dicts() 相当）
        fields = [
            cls.model.id,
            cls.model.avatar,
            cls.model.title,
            cls.model.dsl,
            cls.model.description,
            cls.model.permission,
            cls.model.update_time,
            cls.model.user_id,
            cls.model.create_time,
            cls.model.create_date,
            cls.model.update_date,
        ]

        base = select(*fields).select_from(cls.model).where(cls.model.user_id == tenant_id)
        if id:
            base = base.where(cls.model.id == id)
        if title:
            base = base.where(cls.model.title == title)

        order_col = getattr(cls.model, orderby)
        base = base.order_by(sa_desc(order_col) if desc else asc(order_col))

        # 分页
        stmt = base.offset((page_number - 1) * items_per_page).limit(items_per_page)

        rows = db.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    @classmethod
    def get_by_tenant_id(cls, db: Session, pid: str):
        """
        返回 (True, dict) / (False, None)
        等价 Peewee：join User 取 nickname / avatar
        """
        try:
            fields = [
                cls.model.id,
                cls.model.avatar,
                cls.model.title,
                cls.model.dsl,
                cls.model.description,
                cls.model.permission,
                cls.model.update_time,
                cls.model.user_id,
                cls.model.create_time,
                cls.model.create_date,
                cls.model.update_date,
                User.nickname,
                User.avatar.label("tenant_avatar"),
            ]
            stmt = (
                select(*fields)
                .select_from(cls.model)
                .join(User, cls.model.user_id == User.id)
                .where(cls.model.id == pid)
            )
            row = db.execute(stmt).mappings().first()
            if not row:
                return False, None
            return True, dict(row)
        except Exception as e:
            logging.exception(e)
            return False, None

    @classmethod
    def get_by_tenant_ids(
        cls,
        db: Session,
        joined_tenant_ids: list[str],
        user_id: str,
        page_number: int,
        items_per_page: int,
        orderby: str,
        desc: bool,
        keywords: str | None,
    ):
        """
        TEAM 可见 + 自己的；支持 keywords（title 模糊）；排序+分页；返回(列表, 总数)
        """
        fields = [
            cls.model.id,
            cls.model.avatar,
            cls.model.title,
            cls.model.dsl,
            cls.model.description,
            cls.model.permission,
            User.nickname,
            User.avatar.label("tenant_avatar"),
            cls.model.update_time,
        ]

        cond_team = (cls.model.user_id.in_(joined_tenant_ids)) & (
            cls.model.permission == TenantPermission.TEAM.value
        )
        cond_self = (cls.model.user_id == user_id)

        base = (
            select(*fields)
            .select_from(cls.model)
            .join(User, cls.model.user_id == User.id)
            .where(cond_team | cond_self)
        )

        if keywords:
            base = base.where(func.lower(cls.model.title).contains(keywords.lower()))

        order_col = getattr(cls.model, orderby)
        base = base.order_by(sa_desc(order_col) if desc else asc(order_col))

        # total
        total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()

        # page
        stmt = base.offset((page_number - 1) * items_per_page).limit(items_per_page)
        rows = db.execute(stmt).mappings().all()
        return [dict(r) for r in rows], total


# ---------------------------
# 推理流程（SSE / OpenAI 兼容）
# ---------------------------

def completion(
    db: Session,
    tenant_id: str,
    agent_id: str,
    session_id: str | None = None,
    **kwargs,
) -> Iterable[str]:
    """
    FastAPI 里可直接作为 StreamingResponse 的迭代器：
        return StreamingResponse(completion(db, tenant_id, agent_id, **payload), media_type="text/event-stream")

    逻辑 1: 复用/创建会话
    逻辑 2: 逐步 run 并 SSE 输出
    逻辑 3: 写入消息/引用/错误，并更新会话 DSL
    """
    query = kwargs.get("query", "") or kwargs.get("question", "") or ""
    files = kwargs.get("files", []) or []
    inputs = kwargs.get("inputs", {}) or {}
    user_id = kwargs.get("user_id", "") or ""

    # 组装 canvas & conversation
    if session_id:
        ok, conv = API4ConversationService.get_by_id(db, session_id)
        assert ok, "Session not found!"
        if not conv.message:
            conv.message = []
        canvas = Canvas(json.dumps(conv.dsl), tenant_id, session_id)
    else:
        ok, cvs = UserCanvasService.get_by_id(db, agent_id)
        assert ok, "Agent not found."
        assert cvs.user_id == tenant_id, "You do not own the agent."
        dsl_str = cvs.dsl if isinstance(cvs.dsl, str) else json.dumps(cvs.dsl, ensure_ascii=False)
        session_id = get_uuid()
        canvas = Canvas(dsl_str, tenant_id, session_id)

        conv_dict = {
            "id": session_id,
            "dialog_id": cvs.id,
            "user_id": user_id,
            "message": [],
            "source": "agent",
            "dsl": dsl_str,
        }
        # save 并转实体
        API4ConversationService.save(db, **conv_dict)
        conv = API4Conversation(**conv_dict)
        if not conv.message:
            conv.message = []

    # 记录用户消息
    message_id = str(uuid4())
    conv.message.append({"role": "user", "content": query, "id": message_id})

    # 流式运行
    txt = ""
    for ans in canvas.run(query=query, files=files, user_id=user_id, inputs=inputs):
        ans["session_id"] = session_id
        if ans.get("event") == "message":
            # 拼接最终 assistant 文本
            try:
                txt += ans["data"]["content"]
            except Exception:
                pass
        yield "data:" + json.dumps(ans, ensure_ascii=False) + "\n\n"

    # 结束：写入 assistant 消息、引用、错误，并更新持久层
    conv.message.append(
        {"role": "assistant", "content": txt, "created_at": time.time(), "id": message_id}
    )
    try:
        conv.reference = canvas.get_reference()
        conv.errors = canvas.error
    except Exception:
        pass

    API4ConversationService.append_message(db, conv.id, conv.to_dict())


def completion_openai(
    db: Session,
    tenant_id: str,
    agent_id: str,
    question: str,
    session_id: str | None = None,
    stream: bool = True,
    **kwargs,
) -> Iterable[str | dict]:
    """
    OpenAI 兼容输出（SSE/non-SSE）。
    - 流模式下：yield 带 "data: {...}\n\n" 的块，最后 "data: [DONE]\n\n"
    - 非流模式：yield 最终完整对象（非 data: 包装）
    """
    enc = tiktoken.get_encoding("cl100k_base")

    ok, cvs = UserCanvasService.get_by_id(db, agent_id)
    if not ok:
        yield get_data_openai(id=session_id, model=agent_id, content="**ERROR**: Agent not found.")
        return
    if cvs.user_id != tenant_id:
        yield get_data_openai(id=session_id, model=agent_id, content="**ERROR**: You do not own the agent")
        return

    dsl_str = cvs.dsl if isinstance(cvs.dsl, str) else json.dumps(cvs.dsl, ensure_ascii=False)
    canvas = Canvas(dsl_str, tenant_id)
    canvas.reset()
    message_id = str(uuid4())

    # 新会话：处理 preset param，写入 prologue
    if not session_id:
        query_params = canvas.get_preset_param()
        if query_params:
            for ele in query_params:
                if not ele.get("optional"):
                    if not kwargs.get(ele["key"]):
                        text = f"`{ele['key']}` is required"
                        yield get_data_openai(
                            id=None,
                            model=agent_id,
                            content=text,
                            completion_tokens=len(enc.encode(text)),
                            prompt_tokens=len(enc.encode(question or "")),
                        )
                        return
                    ele["value"] = kwargs[ele["key"]]
                else:
                    if kwargs.get(ele["key"]):
                        ele["value"] = kwargs[ele["key"]]
                    else:
                        if "value" in ele:
                            ele.pop("value")

        # 刷新 DSL & 创建会话
        cvs_dsl_json = json.loads(str(canvas))
        session_id = get_uuid()
        conv_dict = {
            "id": session_id,
            "dialog_id": cvs.id,
            "user_id": kwargs.get("user_id", "") if isinstance(kwargs, dict) else "",
            "message": [{"role": "assistant", "content": canvas.get_prologue(), "created_at": time.time()}],
            "source": "agent",
            "dsl": cvs_dsl_json,
        }
        API4ConversationService.save(db, **conv_dict)
        ok, conv = API4ConversationService.get_by_id(db, session_id)

        if not conv.message:
            conv.message = []
        conv.message.append({"role": "user", "content": question, "id": message_id})

        canvas.messages.append({"role": "user", "content": question, "id": message_id})
        canvas.add_user_input(question)

        if not conv.reference:
            conv.reference = []
        conv.reference.append({"chunks": [], "doc_aggs": []})

    else:
        ok, conv = API4ConversationService.get_by_id(db, session_id)
        if not ok:
            yield get_data_openai(id=session_id, model=agent_id, content="**ERROR**: Session not found!")
            return

        canvas = Canvas(json.dumps(conv.dsl), tenant_id)
        canvas.messages.append({"role": "user", "content": question, "id": message_id})
        canvas.add_user_input(question)

        if not conv.message:
            conv.message = []
        conv.message.append({"role": "user", "content": question, "id": message_id})

        if not conv.reference:
            conv.reference = []
        conv.reference.append({"chunks": [], "doc_aggs": []})

    final_ans = {"reference": [], "content": ""}
    prompt_tokens = len(enc.encode(str(question)))

    if stream:
        try:
            completion_tokens = 0
            for ans in canvas.run(stream=True, bypass_begin=True):
                if ans.get("running_status"):
                    # 增量内容
                    delta = ans.get("content", "")
                    completion_tokens += len(enc.encode(delta))
                    payload = get_data_openai(
                        id=session_id,
                        model=agent_id,
                        content=delta,
                        object="chat.completion.chunk",
                        completion_tokens=completion_tokens,
                        prompt_tokens=prompt_tokens,
                    )
                    yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
                    continue

                # 最终块（含 content/reference 等）
                for k in ans.keys():
                    final_ans[k] = ans[k]

                completion_tokens += len(enc.encode(final_ans.get("content", "")))
                payload = get_data_openai(
                    id=session_id,
                    model=agent_id,
                    content=final_ans["content"],
                    object="chat.completion.chunk",
                    finish_reason="stop",
                    completion_tokens=completion_tokens,
                    prompt_tokens=prompt_tokens,
                )
                yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"

            # 写回会话：assistant 内容/引用/DSL
            canvas.messages.append(
                {"role": "assistant", "content": final_ans["content"], "created_at": time.time(), "id": message_id}
            )
            canvas.history.append(("assistant", final_ans["content"]))
            if final_ans.get("reference"):
                canvas.reference.append(final_ans["reference"])

            # 更新 DSL & 持久化
            conv.dsl = json.loads(str(canvas))
            API4ConversationService.append_message(db, conv.id, conv.to_dict())

            yield "data: [DONE]\n\n"

        except Exception as e:
            traceback.print_exc()
            conv.dsl = json.loads(str(canvas))
            API4ConversationService.append_message(db, conv.id, conv.to_dict())
            err_text = "**ERROR**: " + str(e)
            payload = get_data_openai(
                id=session_id,
                model=agent_id,
                content=err_text,
                finish_reason="stop",
                completion_tokens=len(enc.encode(err_text)),
                prompt_tokens=prompt_tokens,
            )
            yield "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"
            yield "data: [DONE]\n\n"

    else:
        # 非流：聚合完整文本后一次性返回
        try:
            all_text = ""
            for answer in canvas.run(stream=False, bypass_begin=True):
                if answer.get("running_status"):
                    continue
                content = "\n".join(answer["content"]) if "content" in answer else ""
                final_ans["content"] = content
                final_ans["reference"] = answer.get("reference", [])
                all_text += content

            final_ans["content"] = all_text

            # 写回
            canvas.messages.append(
                {"role": "assistant", "content": final_ans["content"], "created_at": time.time(), "id": message_id}
            )
            canvas.history.append(("assistant", final_ans["content"]))
            if final_ans.get("reference"):
                canvas.reference.append(final_ans["reference"])
            conv.dsl = json.loads(str(canvas))
            API4ConversationService.append_message(db, conv.id, conv.to_dict())

            yield get_data_openai(
                id=session_id,
                model=agent_id,
                content=final_ans["content"],
                finish_reason="stop",
                completion_tokens=len(enc.encode(final_ans["content"])),
                prompt_tokens=prompt_tokens,
                param=canvas.get_preset_param(),
            )

        except Exception as e:
            traceback.print_exc()
            conv.dsl = json.loads(str(canvas))
            API4ConversationService.append_message(db, conv.id, conv.to_dict())
            err_text = "**ERROR**: " + str(e)
            yield get_data_openai(
                id=session_id,
                model=agent_id,
                content=err_text,
                finish_reason="stop",
                completion_tokens=len(enc.encode(err_text)),
                prompt_tokens=prompt_tokens,
            )