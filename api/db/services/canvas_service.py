# services_canvas_sqlalchemy.py

from __future__ import annotations

import json
import logging
import time
import traceback
from typing import Any, Iterable
from uuid import uuid4

import tiktoken
from sqlalchemy import select, func, asc, or_, and_
from sqlalchemy.orm import Session
from sqlalchemy.sql import desc as sa_desc

from agent.canvas import Canvas
from agent.component.llm import LLM
from api.db import TenantPermission, CanvasCategory
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
# CanvasTemplateService
# ---------------------------
class CanvasTemplateService(CommonService):
    model = CanvasTemplate

    def __init__(self):
        super().__init__(CanvasTemplate)


class DataFlowTemplateService(CommonService):
    """
    Alias of CanvasTemplateService
    """
    model = CanvasTemplate

    def __init__(self):
        super().__init__(CanvasTemplate)

# ---------------------------
# UserCanvasService
# ---------------------------
class UserCanvasService(CommonService):
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
        canvas_category=CanvasCategory.Agent
    ):
        """
        等价 Peewee 版本：按 user_id(tenant) 过滤，支持 id/title 精确过滤，排序+分页，返回字典行。
        """
        # 选择列（与 Peewee dicts() 默认返回所有字段等价）
        columns = list(cls.model.__table__.columns)

        base = select(*columns).select_from(cls.model).where(cls.model.user_id == tenant_id)
        if id:
            base = base.where(cls.model.id == id)
        if title:
            base = base.where(cls.model.title == title)
        base = base.where(cls.model.canvas_category == canvas_category)

        order_col = getattr(cls.model, orderby)
        base = base.order_by(sa_desc(order_col) if desc else asc(order_col))

        # 分页
        stmt = base.offset((page_number - 1) * items_per_page).limit(items_per_page)

        rows = db.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

    @classmethod
    def get_all_agents_by_tenant_ids(cls, db: Session, tenant_ids: list, user_id: str):
        """
        根据租户ID列表获取所有有权限的Agent
        
        Args:
            db: 数据库会话
            tenant_ids: 租户ID列表
            user_id: 用户ID
            
        Returns:
            list: Agent字典列表
        """
        # will get all permitted agents, be cautious
        fields = [
            cls.model.id,
            cls.model.title,
            cls.model.permission,
            cls.model.canvas_type,
            cls.model.canvas_category
        ]
        # find team agents and owned agents
        query = db.query(*fields).filter(
            or_(
                and_(
                    cls.model.user_id.in_(tenant_ids),
                    cls.model.permission == TenantPermission.TEAM.value
                ),
                cls.model.user_id == user_id
            )
        ).order_by(cls.model.create_time.asc())
        
        # maybe cause slow query by deep paginate, optimize later
        offset, limit = 0, 50
        res = []
        while True:
            ag_batch = query.offset(offset).limit(limit).all()
            if not ag_batch:
                break
            # 将查询结果转换为字典
            for agent in ag_batch:
                res.append({
                    "title": agent.title,
                    "permission": agent.permission,
                    "canvas_type": agent.canvas_type,
                    "canvas_category": agent.canvas_category
                })
            offset += limit
        return res

    @classmethod
    def get_by_canvas_id(cls, db: Session, pid: str):
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
                cls.model.canvas_category,
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
        canvas_category=CanvasCategory.Agent,
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
            cls.model.canvas_category,
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

        base = base.where(cls.model.canvas_category == canvas_category)

        order_col = getattr(cls.model, orderby)
        base = base.order_by(sa_desc(order_col) if desc else asc(order_col))

        # total
        total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()

        # page
        stmt = base.offset((page_number - 1) * items_per_page).limit(items_per_page)
        rows = db.execute(stmt).mappings().all()
        return [dict(r) for r in rows], total

    @classmethod
    def accessible(cls, db: Session, canvas_id: str, tenant_id: str) -> bool:
        """Check whether the given tenant can access the canvas."""
        from api.db.services.user_service import UserTenantService

        exists, canvas = UserCanvasService.get_by_canvas_id(db, canvas_id)
        if not exists or not canvas:
            return False

        tenant_ids = [t.tenant_id for t in UserTenantService.query(db=db, user_id=tenant_id)]
        if canvas["user_id"] != canvas_id and canvas["user_id"] not in tenant_ids:
            return False
        return True


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
        if not isinstance(conv.dsl, str):
            conv.dsl = json.dumps(conv.dsl, ensure_ascii=False)
        canvas = Canvas(conv.dsl, tenant_id, agent_id)
    else:
        ok, cvs = UserCanvasService.get_by_id(db, agent_id)
        assert ok, "Agent not found."
        assert cvs.user_id == tenant_id, "You do not own the agent."
        dsl_str = cvs.dsl if isinstance(cvs.dsl, str) else json.dumps(cvs.dsl, ensure_ascii=False)
        session_id = get_uuid()
        canvas = Canvas(cvs.dsl, tenant_id, agent_id)
        canvas.reset()
        conv_dict = {
            "id": session_id,
            "dialog_id": cvs.id,
            "user_id": user_id,
            "message": [],
            "source": "agent",
            "dsl": dsl_str,
            "reference": []
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
        if ans["event"] == "message":
            txt += ans["data"]["content"]
        yield "data:" + json.dumps(ans, ensure_ascii=False) + "\n\n"

    # 结束：写入 assistant 消息、引用、错误，并更新持久层
    conv.message.append(
        {"role": "assistant", "content": txt, "created_at": time.time(), "id": message_id}
    )
    conv.reference = canvas.get_reference()
    conv.errors = canvas.error
    conv.dsl = str(canvas)

    API4ConversationService.append_message(db, conv.id, conv.to_dict())


def completionOpenAI(
    db: Session,
    tenant_id: str,
    agent_id: str,
    question: str,
    session_id: str | None = None,
    stream: bool = True,
    **kwargs,
) -> Iterable[str | dict]:
    """
    OpenAI 兼容适配器，基于 completion() 函数封装。
    - 调用 completion() 获取内部 SSE 流
    - 解析并转换为 OpenAI 格式
    - 流模式：yield "data: {...}\\n\\n"，最后 "data: [DONE]\\n\\n"
    - 非流模式：yield 最终完整对象
    """
    tiktokenenc = tiktoken.get_encoding("cl100k_base")
    prompt_tokens = len(tiktokenenc.encode(str(question)))
    user_id = kwargs.get("user_id", "")

    if stream:
        completion_tokens = 0
        try:
            for ans in completion(
                db=db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                query=question,
                user_id=user_id,
                **kwargs,
            ):
                if isinstance(ans, str):
                    try:
                        # 移除 "data:" 前缀并解析 JSON
                        ans = json.loads(ans[5:])
                    except Exception as e:
                        logging.exception(f"Canvas OpenAI adapter parse answer failed: {e}")
                        continue

                # 检查是否有答案内容
                if ans.get("event") not in ["message", "message_end"]:
                    continue

                content_piece = ""
                if ans["event"] == "message":
                    content_piece = ans["data"]["content"]

                completion_tokens += len(tiktokenenc.encode(content_piece))

                openai_data = get_data_openai(
                        id=session_id or str(uuid4()),
                        model=agent_id,
                        content=content_piece,
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        stream=True
                    )

                if ans.get("data", {}).get("reference", None):
                    openai_data["choices"][0]["delta"]["reference"] = ans["data"]["reference"]

                yield "data: " + json.dumps(openai_data, ensure_ascii=False) + "\n\n"

            yield "data: [DONE]\n\n"

        except Exception as e:
            logging.exception(e)
            err_text = f"**ERROR**: {str(e)}"
            yield "data: " + json.dumps(
                get_data_openai(
                    id=session_id or str(uuid4()),
                    model=agent_id,
                    content=err_text,
                    finish_reason="stop",
                    prompt_tokens=prompt_tokens,
                    completion_tokens=len(tiktokenenc.encode(err_text)),
                    stream=True,
                ),
                ensure_ascii=False,
            ) + "\n\n"
            yield "data: [DONE]\n\n"

    else:
        # 非流模式：聚合所有内容后一次性返回
        try:
            all_content = ""
            reference = {}
            for ans in completion(
                db=db,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                query=question,
                user_id=user_id,
                **kwargs,
            ):
                if isinstance(ans, str):
                    ans = json.loads(ans[5:])
                if ans.get("event") not in ["message", "message_end"]:
                    continue

                if ans["event"] == "message":
                    all_content += ans["data"]["content"]

                if ans.get("data", {}).get("reference", None):
                    reference.update(ans["data"]["reference"])

            completion_tokens = len(tiktokenenc.encode(all_content))

            openai_data = get_data_openai(
                id=session_id or str(uuid4()),
                model=agent_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                content=all_content,
                finish_reason="stop",
                param=None
            )

            if reference:
                openai_data["choices"][0]["message"]["reference"] = reference

            yield openai_data
        except Exception as e:
            logging.exception(e)
            err_text = f"**ERROR**: {str(e)}"
            yield get_data_openai(
                id=session_id or str(uuid4()),
                model=agent_id,
                prompt_tokens=prompt_tokens,
                completion_tokens=len(tiktokenenc.encode(err_text)),
                content=err_text,
                finish_reason="stop",
                param=None,
            )
