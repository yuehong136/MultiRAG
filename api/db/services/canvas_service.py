# services_canvas_sqlalchemy.py

from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import aclosing
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

import tiktoken
from sqlalchemy import and_, asc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from sqlalchemy.sql import desc as sa_desc

from agent.a2ui import validate_client_a2ui_messages
from agent.canvas import Canvas
from api.db import CanvasCategory, TenantPermission, UserTenantRole
from api.db.db_models import API4Conversation, CanvasTemplate, User, UserCanvas, UserCanvasVersion, UserTenant
from api.db.services.api_service import API4ConversationService
from api.db.services.common_service import CommonService
from api.db.services.user_canvas_version import UserCanvasVersionService
from api.identity.run_context import RunContext
from api.utils.api_utils import get_data_openai
from common.constants import StatusEnum
from common.misc_utils import get_uuid


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
    def get_list(cls, db: Session, tenant_id: str, page_number: int, items_per_page: int, orderby: str, desc: bool, id: str | None, title: str | None, canvas_category=CanvasCategory.Agent):
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
        fields = [cls.model.id, cls.model.avatar, cls.model.title, cls.model.permission, cls.model.canvas_type, cls.model.canvas_category]
        # find team agents and owned agents
        query = (
            db.query(*fields)
            .filter(or_(and_(cls.model.user_id.in_(tenant_ids), cls.model.permission == TenantPermission.TEAM.value), cls.model.user_id == user_id))
            .order_by(cls.model.create_time.asc())
        )

        # maybe cause slow query by deep paginate, optimize later
        offset, limit = 0, 50
        res = []
        while True:
            ag_batch = query.offset(offset).limit(limit).all()
            if not ag_batch:
                break
            # 将查询结果转换为字典
            for agent in ag_batch:
                res.append({"avatar": agent.avatar, "title": agent.title, "permission": agent.permission, "canvas_type": agent.canvas_type, "canvas_category": agent.canvas_category})
            offset += limit
        return res

    @classmethod
    def get_by_canvas_id(cls, db: Session, pid: str) -> tuple[bool, dict[str, Any] | None]:
        try:
            fields = [
                cls.model.id,
                cls.model.avatar,
                cls.model.title,
                cls.model.dsl,
                cls.model.description,
                cls.model.permission,
                cls.model.release,
                cls.model.update_time,
                cls.model.user_id,
                cls.model.create_time,
                cls.model.create_date,
                cls.model.update_date,
                cls.model.canvas_category,
                User.nickname,
                User.avatar.label("tenant_avatar"),
            ]
            stmt = select(*fields).select_from(cls.model).join(User, cls.model.user_id == User.id).where(cls.model.id == pid)
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
        fields = [cls.model.id, cls.model.avatar, cls.model.user_id, cls.model.title, cls.model.permission, cls.model.canvas_category]
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
            canvas_ids = [a["id"] for a in agents_list]
            release_stmt = (
                select(
                    UserCanvasVersion.user_canvas_id,
                    func.max(UserCanvasVersion.create_time).label("release_time"),
                )
                .where(
                    UserCanvasVersion.user_canvas_id.in_(canvas_ids),
                    UserCanvasVersion.release == True,
                )
                .group_by(UserCanvasVersion.user_canvas_id)
            )
            release_rows = db.execute(release_stmt).all()
            release_time_map = {r.user_canvas_id: r.release_time for r in release_rows}

            for agent in agents_list:
                agent["release_time"] = release_time_map.get(agent["id"])

        return agents_list, total

    @classmethod
    def accessible(cls, db: Session, canvas_id: str, tenant_id: str) -> bool:
        """Check whether the given tenant can access the canvas."""
        from api.db.services.user_service import UserTenantService

        exists, canvas = UserCanvasService.get_by_canvas_id(db, canvas_id)
        if not exists or not canvas:
            return False

        if canvas["user_id"] == tenant_id:
            return True
        tenant_ids = [t.tenant_id for t in UserTenantService.query(db=db, user_id=tenant_id)]
        if canvas["user_id"] not in tenant_ids:
            return False
        if canvas["permission"] != TenantPermission.TEAM.value:
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


@dataclass(frozen=True)
class PreparedAgentRun:
    agent_id: str
    caller_id: str
    runtime_tenant_id: str
    dsl: str
    version_title: str | None
    conversation: dict[str, Any] | None = None


class PublishedAgentVersionUnavailable(LookupError):
    """The authorized Agent has no published snapshot to run."""


async def prepare_agent_run(db: AsyncSession, agent_id: str, caller_id: str, *, session_id: str | None = None, release_mode: bool = False) -> PreparedAgentRun:
    """Authorize the REST run and select its exact DSL before any stream starts.

    Require active, joined TEAM members for non-owner runs. The owner-only SDK
    and canvas update helpers retain their separate authorization contracts.
    """
    canvas = await db.scalar(select(UserCanvas).join(User, UserCanvas.user_id == User.id).where(UserCanvas.id == agent_id))
    if canvas is None:
        raise LookupError("Agent not found.")
    if canvas.user_id != caller_id:
        membership = await db.scalar(
            select(UserTenant.id)
            .where(
                UserTenant.user_id == caller_id,
                UserTenant.tenant_id == canvas.user_id,
                UserTenant.status == StatusEnum.VALID.value,
                UserTenant.role.in_([UserTenantRole.NORMAL, UserTenantRole.ADMIN]),
            )
            .limit(1)
        )
        if canvas.permission != TenantPermission.TEAM.value or membership is None:
            raise PermissionError("Only authorized users can run this agent.")
    conversation = None
    if session_id:
        conv = await db.get(API4Conversation, session_id)
        if conv is None:
            raise LookupError("Session not found!")
        if conv.dialog_id != agent_id:
            raise PermissionError("Session does not belong to the requested agent.")
        conversation = copy.deepcopy(conv.to_dict())
        dsl, version_title = conv.dsl, conv.version_title
    elif release_mode:
        version = await db.scalar(
            select(UserCanvasVersion).where(UserCanvasVersion.user_canvas_id == agent_id, UserCanvasVersion.release.is_(True)).order_by(UserCanvasVersion.create_time.desc()).limit(1)
        )
        if version is None:
            raise PublishedAgentVersionUnavailable("No available published version")
        dsl, version_title = version.dsl, version.title
    else:
        dsl, version_title = canvas.dsl, None
    return PreparedAgentRun(agent_id, caller_id, canvas.user_id, dsl if isinstance(dsl, str) else json.dumps(dsl, ensure_ascii=False), version_title, conversation)


def agent_event_error(event: dict[str, Any]) -> str | None:
    """Read explicit execution failures without confusing trace warnings."""
    if event.get("event") != "error" and event.get("code", event.get("retcode", 0)) in (0, None):
        return None
    data = event.get("data")
    detail = data.get("error") or data.get("message") or data.get("content") if isinstance(data, dict) else data
    return str(event.get("message") or event.get("retmsg") or detail or "Agent execution failed.")


# ---------------------------
# 推理流程（SSE / OpenAI 兼容）
# ---------------------------
async def completion(
    db: AsyncSession,
    tenant_id: str,
    agent_id: str,
    session_id: str | None = None,
    run_context: RunContext | None = None,
    prepared_run: PreparedAgentRun | None = None,
    **kwargs: Any,
) -> AsyncGenerator[str, None]:
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
    if run_context is not None and run_context.tenant_id != tenant_id:
        raise PermissionError("run identity context is inconsistent")
    legacy_user_id = kwargs.get("user_id", "") or ""
    user_id = run_context.platform_user_id if run_context is not None and run_context.principal is not None else legacy_user_id
    custom_header = kwargs.get("custom_header", "")
    release_mode = str(kwargs.get("release", "")).strip().lower()
    is_new_session = not session_id
    if prepared_run is not None and (prepared_run.agent_id != agent_id or prepared_run.caller_id != tenant_id):
        raise PermissionError("Prepared run identity is inconsistent.")

    def _setup(s: Session) -> tuple[dict[str, Any], str, str]:
        """会话与 DSL 装配。产物**只有纯 dict/str**——ORM 对象不得跨下方的流式期存活。"""
        if prepared_run is not None:
            if session_id:
                if prepared_run.conversation is None or prepared_run.conversation["id"] != session_id:
                    raise PermissionError("Prepared session is inconsistent.")
                return dict(prepared_run.conversation), prepared_run.dsl, agent_id
            return (
                {
                    "id": get_uuid(),
                    "dialog_id": agent_id,
                    "user_id": user_id,
                    "message": [],
                    "source": "agent",
                    "dsl": prepared_run.dsl,
                    "reference": [],
                    "version_title": prepared_run.version_title,
                },
                prepared_run.dsl,
                agent_id,
            )
        if session_id:
            conv = API4ConversationService.get_by_id(s, session_id)
            if not conv:
                raise LookupError("Session not found!")
            if not conv.message:
                conv.message = []
            if not isinstance(conv.dsl, str):
                conv.dsl = json.dumps(conv.dsl, ensure_ascii=False)
            return conv.to_dict(), conv.dsl, agent_id

        cvs, dsl = UserCanvasService.get_agent_dsl_with_release(
            s,
            agent_id,
            release_mode=release_mode == "true",
            tenant_id=tenant_id,
        )
        # 记录建会话时 canvas 的版本标题（按 release_mode 取已发布/最新版本）
        version_title = UserCanvasVersionService.get_latest_version_title(s, cvs.id, release_mode=release_mode == "true")
        # Use the persisted instance so SQLAlchemy-side defaults are populated.
        conv = API4ConversationService.save(
            s,
            id=get_uuid(),
            dialog_id=cvs.id,
            user_id=user_id,
            message=[],
            source="agent",
            dsl=dsl,
            reference=[],
            version_title=version_title,
        )
        if not conv.message:
            conv.message = []
        return conv.to_dict(), dsl, cvs.id

    conv, dsl, canvas_id = await db.run_sync(_setup)  # TODO(async-phase4)
    session_id = conv["id"]

    def _build_canvas() -> Canvas:
        # A task id identifies one execution attempt, not the reusable Agent.
        # Reusing agent_id here made concurrent runs share cancel/log Redis keys.
        canvas = Canvas(
            dsl,
            prepared_run.runtime_tenant_id if prepared_run is not None else tenant_id,
            task_id=uuid4().hex,
            canvas_id=canvas_id,
            custom_header=custom_header,
            run_context=run_context,
        )
        canvas.artifact_session_id = session_id
        if run_context is not None and run_context.principal is not None:
            # The trusted platform user is carried only by RunContext.  Clear
            # any persisted/attacker-controlled DSL value instead of making it
            # model-visible through ``sys.user_id``.
            canvas.globals["sys.user_id"] = ""
        if is_new_session:
            canvas.reset()
        return canvas

    # 组件 __init__ 各自开连接查模型配置（reset 还打 Redis）——整体入线程池
    canvas = await asyncio.to_thread(_build_canvas)
    if prepared_run is not None and is_new_session:
        # Invalid Canvas construction must not leave a successful session row.
        conv = await db.run_sync(lambda s: API4ConversationService.save(s, **conv).to_dict())
    conv["message"] = conv.get("message") or []

    # setup 产物已全是纯 dict/str（无 ORM 对象存活、save 自带 commit）——此处安全结束
    # autobegin 的读事务，把连接还回池：canvas.run 是分钟级流式，不能让连接全程以
    # idle-in-transaction 钉死（AGENTS.md 判例：有 ORM 对象存活时 rollback 会过期状态）
    await db.rollback()

    # 记录用户消息
    message_id = str(uuid4())
    user_message = {"role": "user", "content": query, "id": message_id, "files": files}
    if a2ui_messages:
        user_message["a2ui"] = a2ui_messages
    if metadata:
        user_message["metadata"] = metadata
    conv["message"].append(user_message)
    attempted_message_count = len(conv["message"])

    # 流式运行
    txt = ""
    a2ui_commands = []
    a2ui_surface_ids = set()
    run_kwargs: dict[str, Any] = {
        "query": query,
        "files": files,
        "inputs": inputs,
        "a2ui": a2ui_messages,
        "metadata": metadata,
    }
    if run_context is None or run_context.principal is None:
        run_kwargs["user_id"] = legacy_user_id
    terminal_frames: list[dict[str, Any]] = []

    def failure_frame(message: str) -> str:
        return "data:" + json.dumps({"event": "error", "code": 100, "message": message, "data": {"error": message}, "session_id": session_id}, ensure_ascii=False) + "\n\n"

    async def persist_failure(message: str) -> None:
        # Keep the attempted user input and failure, without an assistant success.
        conv["message"] = conv["message"][:attempted_message_count]
        conv["errors"] = message
        conv["dsl"] = str(canvas)
        written = await db.run_sync(lambda s: API4ConversationService.append_message(s, conv["id"], conv))
        if written != 1:
            raise RuntimeError("Failed to persist agent failure.")

    try:
        async with aclosing(canvas.run(**run_kwargs)) as run_events:
            async for ans in run_events:
                ans["session_id"] = session_id
                failure = agent_event_error(ans) or (str(canvas.error) if canvas.error else None)
                if failure:
                    await persist_failure(failure)
                    yield failure_frame(failure)
                    return
                if ans.get("event") == "message_end":
                    terminal_frames.append(ans)
                    continue
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
        if canvas.error:
            await persist_failure(str(canvas.error))
            yield failure_frame(str(canvas.error))
            return

        # 结束：写入 assistant 消息、引用、错误，并更新持久层
        assistant_message = {"role": "assistant", "content": txt, "created_at": time.time(), "id": message_id}
        if a2ui_commands:
            assistant_message["a2ui"] = {
                "commands": a2ui_commands,
                "surface_ids": sorted(a2ui_surface_ids),
            }
        conv["message"].append(assistant_message)
        conv["reference"] = canvas.get_reference()
        conv["errors"] = canvas.error
        conv["dsl"] = str(canvas)

        written = await db.run_sync(lambda s: API4ConversationService.append_message(s, conv["id"], conv))  # TODO(async-phase4)
        if written != 1:
            raise RuntimeError("Failed to persist agent result.")
        for terminal in terminal_frames:
            yield "data:" + json.dumps(terminal, ensure_ascii=False) + "\n\n"
    except Exception as error:
        await persist_failure(str(error))
        yield failure_frame(str(error))
    finally:
        canvas.cancel_task()


async def completion_openai(
    db: AsyncSession,
    tenant_id: str,
    agent_id: str,
    question: str,
    session_id: str | None = None,
    stream: bool = True,
    **kwargs: Any,
) -> AsyncGenerator[str | dict[str, Any], None]:
    """
    OpenAI 兼容适配器，基于 completion() 函数封装。
    - 调用 completion() 获取内部 SSE 流
    - 解析并转换为 OpenAI 格式
    - 流模式成功才发送 [DONE]；失败发送 error 对象，不伪造 choices
    - 非流模式：成功返回完整对象；失败返回 error 对象
    """
    tiktoken_encoder = tiktoken.get_encoding("cl100k_base")
    prompt_tokens = len(tiktoken_encoder.encode(str(question)))
    run_kwargs = {**kwargs, "query": question}
    run_kwargs.setdefault("user_id", "")

    async def responses() -> AsyncGenerator[dict[str, Any], None]:
        async with aclosing(completion(db=db, tenant_id=tenant_id, agent_id=agent_id, session_id=session_id, **run_kwargs)) as answers:
            async for answer in answers:
                parsed = json.loads(answer[5:]) if isinstance(answer, str) else answer
                if not isinstance(parsed, dict):
                    raise ValueError("Invalid agent completion event.")
                failure = agent_event_error(parsed)
                if failure:
                    raise RuntimeError(failure)
                yield parsed

    def error_payload(error: Exception) -> dict[str, Any]:
        return {"error": {"message": str(error) or "Agent completion failed.", "type": "server_error", "code": 100}}

    if stream:
        completion_tokens = 0
        try:
            async for ans in responses():
                # 检查是否有答案内容
                if ans.get("event") not in ["message", "message_end"]:
                    continue

                content_piece = ""
                if ans["event"] == "message":
                    content_piece = ans["data"]["content"]

                completion_tokens += len(tiktoken_encoder.encode(content_piece))

                openai_data = get_data_openai(id=session_id or str(uuid4()), model=agent_id, content=content_piece, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, stream=True)

                if ans.get("data", {}).get("reference", None):
                    openai_data["choices"][0]["delta"]["reference"] = ans["data"]["reference"]

                yield "data: " + json.dumps(openai_data, ensure_ascii=False) + "\n\n"

            yield "data: [DONE]\n\n"

        except Exception as e:
            logging.exception(e)
            yield "data: " + json.dumps(error_payload(e), ensure_ascii=False) + "\n\n"

    else:
        # 非流模式：聚合所有内容后一次性返回
        try:
            all_content = ""
            reference = {}
            async for ans in responses():
                if ans.get("event") not in ["message", "message_end"]:
                    continue

                if ans["event"] == "message":
                    all_content += ans["data"]["content"]

                if ans.get("data", {}).get("reference", None):
                    reference.update(ans["data"]["reference"])

            completion_tokens = len(tiktoken_encoder.encode(all_content))

            openai_data = get_data_openai(
                id=session_id or str(uuid4()), model=agent_id, prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, content=all_content, finish_reason="stop", param=None
            )

            if reference:
                openai_data["choices"][0]["message"]["reference"] = reference

            yield openai_data
        except Exception as e:
            logging.exception(e)
            yield error_payload(e)
