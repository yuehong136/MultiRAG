# services_canvas_sqlalchemy.py

from __future__ import annotations

import json
import logging
import time
from uuid import uuid4

import tiktoken
from sqlalchemy import select, func, asc, or_, and_
from sqlalchemy.orm import Session
from sqlalchemy.sql import desc as sa_desc

from agent.canvas import Canvas
from agent.a2ui import validate_client_a2ui_messages
from api.db import TenantPermission, CanvasCategory
from api.db.db_models import CanvasTemplate, User, UserCanvas, UserCanvasVersion
from api.db.services.common_service import CommonService
from api.db.services.api_service import API4ConversationService
from api.db.services.user_canvas_version import UserCanvasVersionService
from common.misc_utils import get_uuid
from api.utils.api_utils import get_data_openai


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
        # will get all permitted agents, be cautious
        fields = [
            cls.model.id,
            cls.model.avatar,
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
                    "avatar": agent.avatar,
                    "title": agent.title,
                    "permission": agent.permission,
                    "canvas_type": agent.canvas_type,
                    "canvas_category": agent.canvas_category
                })
            offset += limit
        return res

    @classmethod
    def get_by_canvas_id(cls, db: Session, pid: str):
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
    def get_basic_info_by_canvas_ids(cls, db: Session, canvas_ids: list[str]):
        """
        Get basic info for multiple canvases by their IDs.
        
        Args:
            db: Database session
            canvas_ids: List of canvas IDs
            
        Returns:
            List of canvas info dicts with id, avatar, user_id, title, permission, canvas_category
        """
        fields = [
            cls.model.id,
            cls.model.avatar,
            cls.model.user_id,
            cls.model.title,
            cls.model.permission,
            cls.model.canvas_category
        ]
        stmt = select(*fields).where(cls.model.id.in_(canvas_ids))
        rows = db.execute(stmt).mappings().all()
        return [dict(r) for r in rows]

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
        canvas_category=None,
    ):
        """
        根据租户ID列表获取；支持 keywords（title 模糊）；排序+分页；返回(列表, 总数)
        """
        fields = [
            cls.model.id,
            cls.model.avatar,
            cls.model.title,
            cls.model.description,
            cls.model.permission,
            cls.model.user_id.label("tenant_id"),
            User.nickname,
            User.avatar.label("tenant_avatar"),
            cls.model.update_time,
            cls.model.canvas_category,
        ]

        base = (
            select(*fields)
            .select_from(cls.model)
            .join(User, cls.model.user_id == User.id)
            .where(
                or_(
                    and_(
                        cls.model.user_id.in_(joined_tenant_ids),
                        cls.model.permission == TenantPermission.TEAM.value,
                    ),
                    cls.model.user_id == user_id,
                )
            )
        )

        if keywords:
            base = base.where(func.lower(cls.model.title).contains(keywords.lower()))

        if canvas_category:
            base = base.where(cls.model.canvas_category == canvas_category)

        order_col = getattr(cls.model, orderby)
        base = base.order_by(sa_desc(order_col) if desc else asc(order_col))

        # total
        total = db.execute(select(func.count()).select_from(base.subquery())).scalar_one()

        # page
        if page_number and items_per_page:
            stmt = base.offset((page_number - 1) * items_per_page).limit(items_per_page)
        else:
            stmt = base
        rows = db.execute(stmt).mappings().all()
        agents_list = [dict(r) for r in rows]

        # Get latest release time for each canvas
        if agents_list:
            canvas_ids = [a['id'] for a in agents_list]
            release_stmt = (
                select(
                    UserCanvasVersion.user_canvas_id,
                    func.max(UserCanvasVersion.create_time).label("release_time"),
                )
                .where(
                    UserCanvasVersion.user_canvas_id.in_(canvas_ids),
                    UserCanvasVersion.release == True,  # noqa: E712
                )
                .group_by(UserCanvasVersion.user_canvas_id)
            )
            release_rows = db.execute(release_stmt).all()
            release_time_map = {r.user_canvas_id: r.release_time for r in release_rows}

            for agent in agents_list:
                agent['release_time'] = release_time_map.get(agent['id'])

        return agents_list, total

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

    @classmethod
    def get_agent_dsl_with_release(
        cls,
        db: Session,
        agent_id: str,
        release_mode: bool = False,
        tenant_id: str | None = None,
    ) -> tuple[UserCanvas, str]:
        cvs = cls.get_by_id(db, agent_id)
        if not cvs:
            raise LookupError("Agent not found.")
        if tenant_id and cvs.user_id != tenant_id:
            raise PermissionError("You do not own the agent.")

        if release_mode:
            released_version = UserCanvasVersionService.get_latest_released(db, agent_id)
            if not released_version:
                raise PermissionError("No available published version")
            dsl = released_version.dsl
        else:
            dsl = cvs.dsl

        if not isinstance(dsl, str):
            dsl = json.dumps(dsl, ensure_ascii=False)

        return cvs, dsl


# ---------------------------
# 推理流程（SSE / OpenAI 兼容）
# ---------------------------
async def completion(
    db: Session,
    tenant_id: str,
    agent_id: str,
    session_id: str | None = None,
    **kwargs,
):
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
    a2ui_messages = validate_client_a2ui_messages(kwargs.get("a2ui"))
    metadata = kwargs.get("metadata") if isinstance(kwargs.get("metadata"), dict) else {}
    user_id = kwargs.get("user_id", "") or ""
    custom_header = kwargs.get("custom_header", "")
    release_mode = str(kwargs.get("release", "")).strip().lower()

    # 组装 canvas & conversation
    if session_id:
        conv = API4ConversationService.get_by_id(db, session_id)
        if not conv:
            raise LookupError("Session not found!")
        if not conv.message:
            conv.message = []
        if not isinstance(conv.dsl, str):
            conv.dsl = json.dumps(conv.dsl, ensure_ascii=False)
        canvas = Canvas(conv.dsl, tenant_id, agent_id, canvas_id=agent_id, custom_header=custom_header)
    else:
        cvs, dsl = UserCanvasService.get_agent_dsl_with_release(
            db,
            agent_id,
            release_mode=release_mode == "true",
            tenant_id=tenant_id,
        )
        session_id = get_uuid()
        canvas = Canvas(dsl, tenant_id, agent_id, canvas_id=cvs.id, custom_header=custom_header)
        canvas.reset()
        conv_dict = {
            "id": session_id,
            "dialog_id": cvs.id,
            "user_id": user_id,
            "message": [],
            "source": "agent",
            "dsl": dsl,
            "reference": []
        }
        # Use the persisted instance so SQLAlchemy-side defaults are populated.
        conv = API4ConversationService.save(db, **conv_dict)
        if not conv.message:
            conv.message = []

    # 记录用户消息
    message_id = str(uuid4())
    user_message = {"role": "user", "content": query, "id": message_id, "files": files}
    if a2ui_messages:
        user_message["a2ui"] = a2ui_messages
    if metadata:
        user_message["metadata"] = metadata
    conv.message.append(user_message)

    # 流式运行
    txt = ""
    a2ui_commands = []
    a2ui_surface_ids = set()
    async for ans in canvas.run(
        query=query,
        files=files,
        user_id=user_id,
        inputs=inputs,
        a2ui=a2ui_messages,
        metadata=metadata,
    ):
        ans["session_id"] = session_id
        if ans["event"] == "message":
            txt += ans["data"]["content"]
            if ans["data"].get("start_to_think", False):
                txt += "<think>"
            elif ans["data"].get("end_to_think", False):
                txt += "</think>"
        elif ans["event"] == "a2ui_command":
            data = ans.get("data") or {}
            commands = data.get("commands") if isinstance(data, dict) else None
            surface_ids = data.get("surface_ids") if isinstance(data, dict) else None
            if isinstance(commands, list):
                a2ui_commands.extend(commands)
            if isinstance(surface_ids, list):
                a2ui_surface_ids.update(x for x in surface_ids if isinstance(x, str))
            elif isinstance(data, dict) and isinstance(data.get("surface_id"), str):
                a2ui_surface_ids.add(data["surface_id"])
        yield "data:" + json.dumps(ans, ensure_ascii=False) + "\n\n"

    # 结束：写入 assistant 消息、引用、错误，并更新持久层
    assistant_message = {"role": "assistant", "content": txt, "created_at": time.time(), "id": message_id}
    if a2ui_commands:
        assistant_message["a2ui"] = {
            "commands": a2ui_commands,
            "surface_ids": sorted(a2ui_surface_ids),
        }
    conv.message.append(assistant_message)
    conv.reference = canvas.get_reference()
    conv.errors = canvas.error
    conv.dsl = str(canvas)

    API4ConversationService.append_message(db, conv.id, conv.to_dict())


async def completion_openai(
    db: Session,
    tenant_id: str,
    agent_id: str,
    question: str,
    session_id: str | None = None,
    stream: bool = True,
    **kwargs,
):
    """
    OpenAI 兼容适配器，基于 completion() 函数封装。
    - 调用 completion() 获取内部 SSE 流
    - 解析并转换为 OpenAI 格式
    - 流模式：yield "data: {...}\\n\\n"，最后 "data: [DONE]\\n\\n"
    - 非流模式：yield 最终完整对象
    """
    tiktoken_encoder = tiktoken.get_encoding("cl100k_base")
    prompt_tokens = len(tiktoken_encoder.encode(str(question)))
    user_id = kwargs.get("user_id", "")

    if stream:
        completion_tokens = 0
        try:
            async for ans in completion(
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

                completion_tokens += len(tiktoken_encoder.encode(content_piece))

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
                    completion_tokens=len(tiktoken_encoder.encode(err_text)),
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
            async for ans in completion(
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

            completion_tokens = len(tiktoken_encoder.encode(all_content))

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
                completion_tokens=len(tiktoken_encoder.encode(err_text)),
                content=err_text,
                finish_reason="stop",
                param=None,
            )
