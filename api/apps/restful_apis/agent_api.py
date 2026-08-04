"""Canonical RESTful agent management and runtime API."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import hmac
import inspect
import ipaddress
import json
import logging
import time
from functools import partial
from typing import Any
from urllib.parse import quote_plus

import jwt
from fastapi import APIRouter, Depends, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from agent.component.llm import LLM
from agent.dsl_migration import normalize_chunker_dsl
from api.apps import manager
from api.apps.services.canvas_replica_service import CanvasReplicaService
from api.db import CanvasCategory
from api.db.db_models import Task, get_async_db, get_db
from api.db.services.canvas_service import (
    API4ConversationService,
    CanvasTemplateService,
    UserCanvasService,
    completion_openai,
)
from api.db.services.canvas_service import (
    completion as agent_completion,
)
from api.db.services.document_service import DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.pipeline_operation_log_service import PipelineOperationLogService
from api.db.services.task_service import CANVAS_DEBUG_DOC_ID, TaskService, queue_dataflow
from api.db.services.user_canvas_version import UserCanvasVersionService
from api.db.services.user_service import TenantService
from api.utils.api_utils import Principal, async_current_user, get_data_error_result, get_json_result, server_error_response
from common import settings
from common.constants import RetCode
from common.misc_utils import get_uuid, thread_pool_exec
from core.flow.pipeline import Pipeline
from core.nlp import search
from core.utils.redis_conn import REDIS_CONN

router = APIRouter()


# ==================== Pydantic Models ====================


class SaveCanvasRequest(BaseModel):
    id: str | None = Field(None, description="Canvas ID，更新时必填")
    dsl: str | dict = Field(..., description="Canvas DSL配置")
    title: str = Field(..., description="Canvas标题")
    description: str | None = Field(None, description="描述")
    avatar: str | None = Field(None, description="头像URL")
    permission: str | None = Field(None, description="权限设置")
    canvas_category: str | None = Field(None, description="Canvas类别")
    release: bool | str = Field(default="", description="是否发布")


class UpdateAgentRequest(BaseModel):
    dsl: str | dict | None = None
    title: str | None = None
    description: str | None = None
    avatar: str | None = None
    permission: str | None = None
    canvas_category: str | None = None
    release: bool | str | None = None


class RemoveCanvasRequest(BaseModel):
    canvas_ids: list[str] = Field(..., description="要删除的Canvas ID列表")


class CompletionRequest(BaseModel):
    id: str = Field(..., description="Canvas ID")
    query: str | None = Field("", description="查询文本")
    files: list[dict] | None = Field([], description="文件列表")
    inputs: dict | None = Field({}, description="输入参数")
    user_id: str | None = Field(None, description="用户ID")


class RerunRequest(BaseModel):
    id: str = Field(..., description="Pipeline操作日志ID")
    dsl: dict = Field(..., description="DSL配置")
    component_id: str = Field(..., description="组件ID")


class ResetRequest(BaseModel):
    id: str = Field(..., description="Canvas ID")


class DebugRequest(BaseModel):
    params: dict = Field(..., description="调试参数")


class TestDBConnectRequest(BaseModel):
    db_type: str = Field(..., description="数据库类型")
    database: str = Field(..., description="数据库名")
    username: str = Field(..., description="用户名")
    host: str = Field(..., description="主机地址")
    port: int = Field(..., description="端口号")
    password: str = Field(..., description="密码")


class CanvasSettingRequest(BaseModel):
    id: str = Field(..., description="Canvas ID")
    title: str = Field(..., description="标题")
    permission: str = Field(..., description="权限")
    description: str | None = Field(None, description="描述")
    avatar: str | None = Field(None, description="头像URL")


def _normalize_agent_session(conversation: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(conversation)
    normalized["messages"] = normalized.pop("message", normalized.get("messages", []))
    for message in normalized["messages"]:
        message.pop("prompt", None)
    normalized["agent_id"] = normalized.pop("dialog_id", normalized.get("agent_id"))

    references = normalized.pop("reference", [])
    if isinstance(references, dict):
        if "chunks" in references:
            references = [references]
        else:
            references = [value for _, value in sorted(references.items(), key=lambda item: int(item[0]))]
    assistant_messages = [message for index, message in enumerate(normalized["messages"]) if index != 0 and message.get("role") != "user"]
    for message, reference in zip(assistant_messages, references or []):
        message["reference"] = [
            {
                "id": chunk.get("chunk_id", chunk.get("id")),
                "content": chunk.get("content_with_weight", chunk.get("content")),
                "document_id": chunk.get("doc_id", chunk.get("document_id")),
                "document_name": chunk.get("docnm_kwd", chunk.get("document_name")),
                "dataset_id": chunk.get("kb_id", chunk.get("dataset_id")),
                "image_id": chunk.get("image_id", chunk.get("img_id")),
                "positions": chunk.get("positions", chunk.get("position_int")),
            }
            for chunk in reference.get("chunks", [])
            if isinstance(chunk, dict)
        ]
    return normalized


# ==================== API Endpoints ====================


@router.get("/agents/templates", summary="获取Canvas模板列表", response_description="成功获取模板列表")
def templates(db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取所有Agent类型的Canvas模板

    概要：返回系统预定义的Canvas模板列表，用户可以基于这些模板快速创建新的Canvas。

    返回：
    - list: Canvas模板列表，每个模板包含：
        - id: 模板ID
        - title: 模板标题
        - description: 模板描述
        - dsl: 模板DSL配置
        - avatar: 模板图标
        - canvas_category: 模板类别

    功能：
    1. 查询所有Agent类别的模板
    2. 返回模板详细信息

    业务场景：
    - 用户创建新Canvas时选择模板
    - 快速复制常用配置
    - 学习Canvas的DSL结构
    """
    templates_list = CanvasTemplateService.get_all(db)
    return get_json_result(data=[c.to_dict() for c in templates_list])


@router.delete("/agents/{agent_id}", summary="删除Canvas", response_description="成功删除Canvas")
def rm(agent_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    批量删除Canvas

    概要：删除指定的一个或多个Canvas，只有Canvas所有者有权限执行此操作。

    参数：
    - **request_body**: 请求体
        - canvas_ids: Canvas ID列表，支持批量删除

    返回：
    - dict: 操作结果
        - data: True 表示删除成功

    功能：
    1. 遍历Canvas ID列表
    2. 验证用户对每个Canvas的权限
    3. 执行删除操作

    权限要求：
    - 用户必须是Canvas的所有者

    异常处理：
    - 如果用户无权限，返回 OPERATING_ERROR

    注意：
    - 删除操作不可逆
    - 删除Canvas不会删除关联的会话记录
    - 建议删除前确认
    """
    if not UserCanvasService.query(db, user_id=user.id, id=agent_id):
        return get_json_result(data=False, retmsg="Only owner of canvas authorized for this operation.", retcode=RetCode.OPERATING_ERROR)
    UserCanvasService.delete_by_id(db, agent_id)
    return get_json_result(data=True)


@router.post("/agents", summary="保存Canvas", response_description="成功保存Canvas")
def save(request_body: SaveCanvasRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    创建或更新Canvas配置

    概要：保存Canvas的DSL配置、标题等信息，支持创建新Canvas或更新现有Canvas。

    参数：
    - **request_body**: 请求体
        - id: Canvas ID（更新时必填，创建时可选）
        - dsl: Canvas的DSL配置，可以是字符串或字典
        - title: Canvas标题
        - description: 描述（可选）
        - avatar: 头像URL（可选）
        - permission: 权限设置（可选）
        - canvas_category: Canvas类别（可选，默认为Agent）

    返回：
    - dict: 保存后的Canvas信息

    功能：
    1. **创建模式**（无id）：
       - 检查标题是否重复
       - 生成新的Canvas ID
       - 保存Canvas配置
       - 创建版本历史记录
    2. **更新模式**（有id）：
       - 验证用户权限
       - 更新Canvas配置
       - 创建版本历史记录
       - 清理旧版本（保留有限数量）

    DSL处理：
    - 如果DSL是字典，会自动转换为JSON字符串
    - 保存前会确保DSL格式正确

    版本管理：
    - 每次保存都会创建版本快照
    - 版本命名格式：{title}_{timestamp}
    - 自动清理过期版本

    权限要求：
    - 创建：任何登录用户都可创建
    - 更新：只有所有者可更新

    异常处理：
    - 如果标题重复，返回 "{title} already exists"
    - 如果保存失败，返回 "Fail to save canvas"
    - 如果无权限更新，返回 OPERATING_ERROR

    注意：
    - 标题会自动去除首尾空格
    - DSL配置会被序列化存储
    - 支持增量更新（只传需要更新的字段）
    """
    req = request_body.model_dump()
    if req.pop("id", None) is not None:
        return get_data_error_result(retmsg="Use PUT /agents/{agent_id} to update an agent.")
    req["release"] = bool(req.get("release", ""))

    # 处理DSL格式
    try:
        req["dsl"] = CanvasReplicaService.normalize_dsl(req["dsl"])
    except ValueError as e:
        return get_data_error_result(retmsg=str(e))

    cate = req.get("canvas_category") or CanvasCategory.Agent
    req["canvas_category"] = cate

    req["user_id"] = user.id
    req["title"] = req["title"].strip()
    if UserCanvasService.query(db, user_id=user.id, title=req["title"], canvas_category=cate):
        return get_data_error_result(retmsg=f"{req['title']} already exists.")
    req["id"] = get_uuid()
    if not UserCanvasService.save(db, **req):
        return get_data_error_result(retmsg="Fail to save canvas.")

    # 保存版本
    UserCanvasVersionService.save_or_replace_latest(
        db,
        user_canvas_id=req["id"],
        dsl=req["dsl"],
        title=UserCanvasVersionService.build_version_title(getattr(user, "nickname", user.id), req.get("title")),
        release=req.get("release"),
    )
    replica_ok = CanvasReplicaService.replace_for_set(
        canvas_id=req["id"],
        tenant_id=str(user.id),
        runtime_user_id=str(user.id),
        dsl=req["dsl"],
        canvas_category=req.get("canvas_category", cate),
        title=req.get("title", ""),
    )
    if not replica_ok:
        return get_data_error_result(retmsg="canvas saved, but replica sync failed.")
    return get_json_result(data=req)


@router.put("/agents/{agent_id}", summary="更新Agent", response_description="成功更新Agent")
def update_agent(
    agent_id: str,
    request_body: UpdateAgentRequest,
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    if not UserCanvasService.query(db, user_id=user.id, id=agent_id):
        return get_json_result(data=False, retmsg="Only owner of canvas authorized for this operation.", retcode=RetCode.OPERATING_ERROR)

    current = UserCanvasService.get_by_id(db, agent_id)
    if not current:
        return get_data_error_result(retmsg="canvas not found.")

    updates = request_body.model_dump(exclude_unset=True, exclude_none=True)
    if "title" in updates:
        updates["title"] = updates["title"].strip()
    if "dsl" in updates:
        try:
            updates["dsl"] = CanvasReplicaService.normalize_dsl(updates["dsl"])
        except ValueError as error:
            return get_data_error_result(retmsg=str(error))

    release = updates.pop("release", None)
    if updates:
        UserCanvasService.update_by_id(db, agent_id, updates)

    if "dsl" in updates:
        title = updates.get("title") or current.title
        category = updates.get("canvas_category") or current.canvas_category
        UserCanvasVersionService.save_or_replace_latest(
            db,
            user_canvas_id=agent_id,
            dsl=updates["dsl"],
            title=UserCanvasVersionService.build_version_title(getattr(user, "nickname", user.id), title),
            release=bool(release) if release is not None else False,
        )
        if not CanvasReplicaService.replace_for_set(
            canvas_id=agent_id,
            tenant_id=str(user.id),
            runtime_user_id=str(user.id),
            dsl=updates["dsl"],
            canvas_category=category,
            title=title,
        ):
            return get_data_error_result(retmsg="agent saved, but replica sync failed.")

    return get_json_result(data=True)


def get(canvas_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取指定Canvas的详细信息

    概要：根据Canvas ID查询并返回Canvas的完整配置信息。

    参数：
    - **canvas_id**: Canvas ID

    返回：
    - dict: Canvas详细信息

    权限要求：
    - 用户必须有权限访问该Canvas

    异常处理：
    - 如果Canvas不存在或无权限，返回 "canvas not found"
    """
    if not UserCanvasService.accessible(db, canvas_id, user.id):
        return get_data_error_result(retmsg="canvas not found.")

    exists, canvas = UserCanvasService.get_by_canvas_id(db, canvas_id)
    if not exists or not canvas:
        return get_data_error_result(retmsg="canvas not found.")
    canvas["dsl"] = normalize_chunker_dsl(canvas.get("dsl", {}))
    try:
        CanvasReplicaService.bootstrap(
            canvas_id=canvas_id,
            tenant_id=str(user.id),
            runtime_user_id=str(user.id),
            dsl=canvas.get("dsl"),
            canvas_category=canvas.get("canvas_category", CanvasCategory.Agent),
            title=canvas.get("title", ""),
        )
    except ValueError as e:
        return get_data_error_result(retmsg=str(e))

    # Get the last publication time (latest released version's update_time)
    last_publish_time = None
    versions = UserCanvasVersionService.list_by_canvas_id(db, canvas_id)
    if versions:
        released_versions = [v for v in versions if v.release]
        if released_versions:
            released_versions.sort(key=lambda x: x.update_time, reverse=True)
            last_publish_time = released_versions[0].update_time

    canvas["last_publish_time"] = last_publish_time

    if canvas.get("canvas_category") == CanvasCategory.DataFlow:
        datasets = KnowledgebaseService.query(db, pipeline_id=canvas_id)
        canvas["datasets"] = [{"id": dataset.id, "name": dataset.name, "avatar": dataset.avatar} for dataset in datasets]

    return get_json_result(data=canvas)


@router.post("/agents/chat/completion", summary="运行Canvas", response_description="成功执行Canvas")
async def run(request_body: dict[str, Any], db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)):
    """
    执行Canvas推理任务

    概要：运行指定的Canvas，支持Agent和DataFlow两种类型，使用SSE流式返回结果。

    参数：
    - **request_body**: 请求体
        - id: Canvas ID
        - query: 查询文本（Agent模式使用）
        - files: 文件列表（DataFlow模式使用）
        - inputs: 输入参数
        - user_id: 用户ID（可选）

    返回：
    - **Agent模式**: SSE流，实时返回推理过程
    - **DataFlow模式**: JSON，包含任务ID

    功能：
    1. **Agent模式**（canvas_category == Agent）：
       - 初始化Canvas
       - 流式执行推理
       - 实时返回结果（SSE格式）
       - 更新Canvas状态

    2. **DataFlow模式**（canvas_category == DataFlow）：
       - 创建Pipeline
       - 提交异步任务到队列
       - 返回任务ID用于追踪

    SSE事件格式：
    - data: {JSON对象}
    - event类型：message, message_end, error等
    - 包含session_id、content等字段

    权限要求：
    - 用户必须有权限访问该Canvas

    异常处理：
    - 如果无权限，返回 OPERATING_ERROR
    - 如果Canvas不存在，返回 "canvas not found"
    - Agent执行错误通过SSE返回错误事件
    - DataFlow错误直接返回错误信息

    注意：
    - Agent模式使用SSE流式响应，连接保持打开
    - DataFlow模式异步执行，不阻塞请求
    - 执行完成后会自动保存Canvas状态
    - 支持文件上传和多轮对话
    """
    req = dict(request_body)
    agent_id = req.pop("agent_id", None)
    openai_compatible = bool(req.pop("openai-compatible", False))
    if not agent_id:
        return get_data_error_result(retmsg="`agent_id` is required.")

    session_id = req.get("session_id")
    if session_id:
        conversation = await db.run_sync(lambda session: API4ConversationService.get_by_id(session, session_id))  # TODO(async-phase4)
        if conversation is None:
            return get_data_error_result(retmsg="Session not found!")
        if conversation.dialog_id != agent_id:
            return get_json_result(data=False, retmsg="Session does not belong to the requested agent.", retcode=RetCode.OPERATING_ERROR)
        if not await db.run_sync(lambda session: UserCanvasService.accessible(session, agent_id, user.id)):  # TODO(async-phase4)
            return get_json_result(data=False, retmsg="Only authorized users can access this agent session.", retcode=RetCode.OPERATING_ERROR)

    if openai_compatible:
        messages = req.pop("messages", [])
        if not messages:
            return get_data_error_result(retmsg="You must provide at least one message.")
        question = next((message.get("content", "") for message in reversed(messages) if message.get("role") == "user"), "")
        stream = bool(req.pop("stream", False))
        completion = completion_openai(
            db,
            user.id,
            agent_id,
            question,
            session_id=req.pop("session_id", None),
            stream=stream,
            **req,
        )
        if stream:
            return StreamingResponse(
                completion,
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
            )
        async for response in completion:
            return response
        return get_data_error_result(retmsg="Agent completion returned no response.")

    if session_id:
        return await exp_agent_completion(agent_id, req, db, user)

    req["id"] = agent_id
    query = req.get("query", "")
    files = req.get("files", [])
    inputs = req.get("inputs", {})
    tenant_id = str(user.id)
    runtime_user_id = req.get("user_id") or tenant_id
    user_id = str(runtime_user_id)

    if not await db.run_sync(lambda s: UserCanvasService.accessible(s, req["id"], tenant_id)):  # TODO(async-phase4)
        return get_json_result(data=False, retmsg="Only owner of canvas authorized for this operation.", retcode=RetCode.OPERATING_ERROR)

    replica_payload = CanvasReplicaService.load_for_run(
        canvas_id=req["id"],
        tenant_id=tenant_id,
        runtime_user_id=user_id,
    )

    if not replica_payload:
        return get_data_error_result(retmsg="canvas replica not found, please call /get/<canvas_id> first.")

    replica_dsl = replica_payload.get("dsl", {})
    canvas_title = replica_payload.get("title", "")
    canvas_category = replica_payload.get("canvas_category", CanvasCategory.Agent)
    dsl_str = json.dumps(replica_dsl, ensure_ascii=False)

    # DataFlow模式（只取所需字段，不让 ORM 对象活到流式期）
    stored_category = await db.run_sync(lambda s: UserCanvasService.get_by_id(s, req["id"]).canvas_category)  # TODO(async-phase4)
    if stored_category == CanvasCategory.DataFlow:
        task_id = get_uuid()
        Pipeline(dsl_str, tenant_id=tenant_id, doc_id=CANVAS_DEBUG_DOC_ID, task_id=task_id, flow_id=req["id"])
        ok, error_message = await db.run_sync(lambda s: queue_dataflow(s, user_id, req["id"], task_id, CANVAS_DEBUG_DOC_ID, files if files else None, 0))  # TODO(async-phase4)
        if not ok:
            return get_data_error_result(retmsg=error_message)
        return get_json_result(data={"message_id": task_id})

    # Agent模式 - SSE流式响应
    try:
        # 组件 __init__ 各自开连接查模型配置——整体入线程池
        canvas = await asyncio.to_thread(Canvas, dsl_str, tenant_id, canvas_id=req["id"])
    except Exception as e:
        return server_error_response(e)

    # setup 产物已全是纯值（无 ORM 对象存活）——结束 autobegin 的读事务，避免连接在
    # 下方分钟级的 SSE 流式期间以 idle-in-transaction 状态钉死
    await db.rollback()

    async def sse():
        nonlocal canvas, user_id
        try:
            async for ans in canvas.run(query=query, files=files, user_id=user_id, inputs=inputs):
                yield "data:" + json.dumps(ans, ensure_ascii=False) + "\n\n"

            commit_ok = CanvasReplicaService.commit_after_run(
                canvas_id=req["id"],
                tenant_id=tenant_id,
                runtime_user_id=user_id,
                dsl=json.loads(str(canvas)),
                canvas_category=canvas_category,
                title=canvas_title,
            )
            if not commit_ok:
                logging.error(
                    "Canvas runtime replica commit failed: canvas_id=%s tenant_id=%s runtime_user_id=%s",
                    req["id"],
                    tenant_id,
                    user_id,
                )

        except Exception as e:
            logging.exception(e)
            yield "data:" + json.dumps({"code": 500, "message": str(e), "data": False}, ensure_ascii=False) + "\n\n"
        finally:
            canvas.cancel_task()

    return StreamingResponse(sse(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})


async def exp_agent_completion(
    canvas_id: str,
    request_body: dict[str, Any],
    db: AsyncSession,
    user: Principal,
):
    """
    实验性Agent Completion端点，支持trace返回。

    通过 canvas_id 指定Agent，流式执行并返回SSE事件。
    当 return_trace=True 时，node_finished 事件会附带完整 trace 链。
    """
    tenant_id = user.id
    return_trace = bool(request_body.get("return_trace", False))

    if not request_body.get("stream", True):
        full_content = ""
        reference: dict[str, Any] = {}
        final_answer: dict[str, Any] = {}
        trace_items: list[dict[str, Any]] = []
        structured_output: dict[str, Any] = {}
        async for answer in agent_completion(db=db, tenant_id=tenant_id, agent_id=canvas_id, **request_body):
            try:
                parsed = json.loads(answer[5:]) if isinstance(answer, str) else answer
                if parsed.get("event") == "message":
                    full_content += parsed["data"]["content"]
                if parsed.get("data", {}).get("reference"):
                    reference.update(parsed["data"]["reference"])
                if parsed.get("event") == "node_finished":
                    data = parsed.get("data", {})
                    outputs = data.get("outputs", {})
                    component_id = data.get("component_id")
                    if component_id is not None and "structured" in outputs:
                        structured_output[component_id] = copy.deepcopy(outputs["structured"])
                    if return_trace:
                        trace_items.append({"component_id": component_id, "trace": [copy.deepcopy(data)]})
                final_answer = parsed
            except Exception as error:
                return get_json_result(data=f"**ERROR**: {error!s}")

        if not final_answer:
            return get_data_error_result(retmsg="Agent completion returned no events.")
        final_answer.setdefault("data", {})["content"] = full_content
        final_answer["data"]["reference"] = reference
        if structured_output:
            final_answer["data"]["structured"] = structured_output
        if return_trace:
            final_answer["data"]["trace"] = trace_items
        return get_json_result(data=final_answer)

    async def generate():
        trace_items = []
        async for answer in agent_completion(db=db, tenant_id=tenant_id, agent_id=canvas_id, **request_body):
            if isinstance(answer, str):
                try:
                    ans = json.loads(answer[5:])  # remove "data:"
                except Exception:
                    continue
            else:
                ans = answer

            event = ans.get("event")
            if event == "node_finished":
                if return_trace:
                    data = ans.get("data", {})
                    trace_items.append(
                        {
                            "component_id": data.get("component_id"),
                            "trace": [copy.deepcopy(data)],
                        }
                    )
                    ans.setdefault("data", {})["trace"] = trace_items
                    answer = "data:" + json.dumps(ans, ensure_ascii=False) + "\n\n"
                yield answer

            if event not in ("message", "message_end", "a2ui_command"):
                continue

            yield answer

        yield "data:[DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/agents/rerun", summary="重新运行Pipeline", response_description="成功重新运行")
def rerun(request_body: RerunRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    重新运行Pipeline处理任务

    概要：清除文档的处理结果并重新执行Pipeline流程，从指定的组件开始运行。

    参数：
    - **request_body**: 请求体
        - id: Pipeline操作日志ID
        - dsl: DSL配置
        - component_id: 要运行的组件ID

    返回：
    - dict: 操作结果
        - data: True 表示成功提交重新运行任务

    功能：
    1. 获取文档信息
    2. 检查文档处理状态（不能重复运行）
    3. 删除向量数据库中的chunks
    4. 清空文档的统计数据
    5. 删除旧的任务记录
    6. 更新DSL配置
    7. 提交新的处理任务到队列

    清理内容：
    - 向量数据库中的所有chunks
    - chunk_num、token_num统计
    - 进度和错误信息
    - 旧的任务记录

    异常处理：
    - 如果文档不存在，返回 "Document not found"
    - 如果文档正在处理，返回 "is processing..."

    注意：
    - 只能重新运行已完成或失败的文档
    - 重新运行会清除所有处理结果
    - 任务会从指定组件开始执行
    """
    req = request_body.model_dump()
    doc = PipelineOperationLogService.get_documents_info(db, req["id"])
    if not doc:
        return get_data_error_result(retmsg="Document not found.")

    doc = doc[0]
    if 0 < doc["progress"] < 1:
        return get_data_error_result(retmsg=f"`{doc['name']}` is processing...")

    # 删除向量数据库中的数据
    if settings.docStoreConn.index_exist(search.index_name(user.id, [doc["kb_name"]]), doc["kb_id"]):
        settings.docStoreConn.delete({"doc_id": doc["id"]}, search.index_name(user.id, [doc["kb_name"]]), doc["kb_id"])

    # 清空统计数据
    doc["progress_msg"] = ""
    doc["chunk_num"] = 0
    doc["token_num"] = 0
    DocumentService.clear_chunk_num_when_rerun(db, doc["id"])
    DocumentService.update_by_id(db, doc["id"], doc)

    # 删除旧任务
    TaskService.filter_delete(db, [Task.doc_id == doc["id"]])

    # 更新DSL配置
    dsl = req["dsl"]
    dsl["path"] = [req["component_id"]]
    PipelineOperationLogService.update_by_id(db, req["id"], {"dsl": dsl})

    # 提交新任务
    queue_dataflow(db, tenant_id=user.id, flow_id=req["id"], task_id=get_uuid(), doc_id=doc["id"], priority=0, rerun=True)

    return get_json_result(data=True)


def cancel(task_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    取消正在运行的任务

    概要：设置任务的取消标记，通知任务执行器停止处理。

    参数：
    - **task_id**: 任务ID

    返回：
    - dict: 操作结果
        - data: True 表示取消标记已设置

    功能：
    1. 在Redis中设置取消标记
    2. 任务执行器检测到标记后会停止执行

    注意：
    - 取消操作是异步的，不会立即停止任务
    - 任务可能需要几秒钟才能完全停止
    - 已完成的处理不会回滚
    """
    try:
        REDIS_CONN.set(f"{task_id}-cancel", "x")
    except Exception as e:
        logging.exception(e)
    return get_json_result(data=True)


@router.post("/agents/{agent_id}/reset", summary="重置Canvas", response_description="成功重置Canvas")
def reset(agent_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    重置Canvas状态

    概要：清除Canvas的运行时状态，恢复到初始配置。

    参数：
    - **request_body**: 请求体
        - id: Canvas ID

    返回：
    - dict: 重置后的DSL配置

    功能：
    1. 验证用户权限
    2. 加载Canvas配置
    3. 调用Canvas.reset()清除状态
    4. 保存重置后的配置
    5. 返回新的DSL

    重置内容：
    - 清除对话历史
    - 重置组件状态
    - 清除临时变量
    - 保留DSL结构

    权限要求：
    - 用户必须是Canvas的所有者

    异常处理：
    - 如果无权限，返回 OPERATING_ERROR
    - 如果Canvas不存在，返回 "canvas not found"

    注意：
    - 重置不会删除历史会话记录
    - 只影响当前Canvas的运行时状态
    """
    req = {"id": agent_id}

    if not UserCanvasService.accessible(db, req["id"], user.id):
        return get_json_result(data=False, retmsg="Only owner of canvas authorized for this operation.", retcode=RetCode.OPERATING_ERROR)

    try:
        user_canvas = UserCanvasService.get_by_id(db, req["id"])
        if not user_canvas:
            return get_data_error_result(retmsg="canvas not found.")

        canvas = Canvas(json.dumps(user_canvas.dsl), user.id, canvas_id=user_canvas.id)
        canvas.reset()
        req["dsl"] = json.loads(str(canvas))
        UserCanvasService.update_by_id(db, req["id"], {"dsl": req["dsl"]})

        return get_json_result(data=req["dsl"])
    except Exception as e:
        return server_error_response(e)


@router.post("/agents/{canvas_id}/upload", summary="上传文件到Canvas", response_description="成功上传文件")
async def upload(canvas_id: str, url: str | None = Query(None, description="URL地址，用于下载网页内容"), file: list[UploadFile] | None = File(None), db: AsyncSession = Depends(get_async_db)):
    """
    上传文件到Canvas或从URL下载内容

    概要：支持两种方式：1) 直接上传文件，2) 通过URL抓取网页内容。

    参数：
    - **canvas_id**: Canvas ID
    - **url**: URL地址（可选），用于爬取网页内容
    - **file**: 上传的文件（可选）

    返回：
    - dict: 文件信息
        - id: 文件唯一标识
        - name: 文件名
        - size: 文件大小
        - extension: 文件扩展名
        - mime_type: MIME类型
        - created_by: 创建者ID
        - created_at: 创建时间
        - preview_url: 预览URL

    功能：
    1. **URL模式**（提供url参数）：
       - 使用crawl4ai爬取网页
       - 支持生成PDF和Markdown
       - 自动识别内容类型
       - 存储到文件系统

    2. **文件上传模式**（提供file参数）：
       - 接收上传的文件
       - 检查文件健康状态
       - 存储到文件系统
       - 返回文件元信息

    支持的文件类型：
    - PDF文档
    - HTML文件
    - Office文档
    - 图片文件
    - 文本文件

    URL爬取特性：
    - 自动生成PDF
    - 提取Markdown格式内容
    - 内容剪枝优化
    - 支持动态页面

    异常处理：
    - 如果Canvas不存在，返回 "canvas not found"
    - 如果URL爬取失败，返回错误信息
    - 如果文件健康检查失败，返回错误信息

    注意：
    - URL爬取可能需要较长时间
    - 文件会存储在用户的存储空间中
    - 自动检测PDF文件的完整性
    """
    exists, canvas = await db.run_sync(lambda s: UserCanvasService.get_by_canvas_id(s, canvas_id))  # TODO(async-phase4)
    if not exists or not canvas:
        return get_data_error_result(retmsg="canvas not found.")

    user_id = canvas["user_id"]
    file_objs = file if file else []
    try:
        if len(file_objs) == 1:
            return get_json_result(data=await FileService.upload_info(db, user_id, file_objs[0], url))
        results = [await FileService.upload_info(db, user_id, f) for f in file_objs]
        return get_json_result(data=results)
    except Exception as e:
        return server_error_response(e)


@router.get("/agents/{id}/components/{component_id}/input-form", summary="获取组件输入表单", response_description="成功获取输入表单")
def input_form(id: str, component_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取Canvas组件的输入表单配置

    概要：返回指定组件的输入参数定义，用于动态生成表单界面。

    参数：
    - **id**: Canvas ID
    - **component_id**: 组件ID

    返回：
    - dict: 输入表单配置
        - 参数名称和类型
        - 默认值
        - 验证规则
        - 描述信息

    功能：
    1. 验证Canvas访问权限
    2. 加载Canvas配置
    3. 获取指定组件
    4. 返回组件的输入表单定义

    业务场景：
    - 动态生成参数配置界面
    - 验证用户输入
    - 提供参数说明

    权限要求：
    - 用户必须是Canvas的所有者

    异常处理：
    - 如果Canvas不存在，返回 "canvas not found"
    - 如果无权限，返回 OPERATING_ERROR
    """
    try:
        user_canvas = UserCanvasService.get_by_id(db, id)
        if not user_canvas:
            return get_data_error_result(retmsg="canvas not found.")

        if not UserCanvasService.query(db, user_id=user.id, id=id):
            return get_json_result(data=False, retmsg="Only owner of canvas authorized for this operation.", retcode=RetCode.OPERATING_ERROR)

        canvas = Canvas(json.dumps(user_canvas.dsl), user.id, canvas_id=user_canvas.id)
        return get_json_result(data=canvas.get_component_input_form(component_id))
    except Exception as e:
        return server_error_response(e)


@router.post("/agents/{agent_id}/components/{component_id}/debug", summary="调试组件", response_description="成功执行组件调试")
async def debug(
    agent_id: str,
    component_id: str,
    request_body: DebugRequest,
    db: AsyncSession = Depends(get_async_db),
    user: Principal = Depends(async_current_user),
):
    """
    调试单个Canvas组件

    概要：独立运行指定组件，用于测试组件配置和参数。

    参数：
    - **request_body**: 请求体
        - id: Canvas ID
        - component_id: 要调试的组件ID
        - params: 调试参数

    返回：
    - dict: 组件的输出结果

    功能：
    1. 验证Canvas访问权限
    2. 重置Canvas状态
    3. 获取指定组件
    4. 设置调试输入
    5. 执行组件
    6. 返回输出结果

    调试特性：
    - 不影响Canvas的实际状态
    - 支持LLM组件的流式输出聚合
    - 可以多次调试不同参数

    权限要求：
    - 用户必须是Canvas的所有者

    异常处理：
    - 如果无权限，返回 OPERATING_ERROR
    - 执行错误返回详细错误信息

    注意：
    - 调试模式下输出会完整返回，不是流式
    - LLM组件的流式输出会被聚合
    - 不会保存到Canvas状态中
    """
    req = request_body.model_dump()
    req["id"] = agent_id
    req["component_id"] = component_id

    if not await db.run_sync(lambda s: UserCanvasService.accessible(s, req["id"], user.id)):  # TODO(async-phase4)
        return get_json_result(data=False, retmsg="Only owner of canvas authorized for this operation.", retcode=RetCode.OPERATING_ERROR)

    try:
        dsl_str, stored_canvas_id = await db.run_sync(  # TODO(async-phase4)
            lambda s: (lambda cvs: (json.dumps(cvs.dsl), cvs.id))(UserCanvasService.get_by_id(s, req["id"]))
        )

        def _build_canvas() -> Canvas:
            canvas = Canvas(dsl_str, user.id, canvas_id=stored_canvas_id)
            canvas.reset()
            return canvas

        # 组件 __init__ 各自开连接查模型配置（reset 还打 Redis）——整体入线程池
        canvas = await asyncio.to_thread(_build_canvas)
        canvas.message_id = get_uuid()

        # setup 产物已全是纯值——结束读事务，避免连接在下方组件执行（可能分钟级 LLM）期间钉死
        await db.rollback()

        component = canvas.get_component(req["component_id"])["obj"]
        component.reset()

        if isinstance(component, LLM):
            component.set_debug_inputs(req["params"])

        # 同步 invoke 会在事件循环上 asyncio.run 组件的 async 实现（靠 nest_asyncio 补丁），
        # 直接走组件自己的 async 入口
        await component.invoke_async(**{k: o["value"] for k, o in req["params"].items()})
        outputs = component.output()

        # 处理流式输出
        for k in outputs.keys():
            if isinstance(outputs[k], partial):
                txt = ""
                iter_obj = outputs[k]()
                if inspect.isasyncgen(iter_obj):
                    async for c in iter_obj:
                        txt += c
                else:
                    for c in iter_obj:
                        txt += c
                outputs[k] = txt

        return get_json_result(data=outputs)
    except Exception as e:
        return server_error_response(e)


@router.post("/agents/test_db_connection", summary="测试数据库连接", response_description="成功测试数据库连接")
def test_db_connect(request_body: TestDBConnectRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    测试数据库连接配置是否正确

    概要：验证数据库连接参数，确保可以成功连接到目标数据库。

    参数：
    - **request_body**: 请求体
        - db_type: 数据库类型（mysql, mariadb, postgres, mssql, IBM DB2）
        - database: 数据库名
        - username: 用户名
        - host: 主机地址
        - port: 端口号
        - password: 密码

    返回：
    - dict: 测试结果
        - data: "Database Connection Successful!" 表示连接成功

    支持的数据库：
    1. **MySQL/MariaDB**: 使用SQLAlchemy + pymysql
    2. **PostgreSQL**: 使用SQLAlchemy + psycopg2
    3. **MS SQL Server**: 使用pyodbc，需要ODBC Driver 17
    4. **IBM DB2**: 使用ibm_db

    功能：
    1. 根据数据库类型创建连接
    2. 执行简单查询验证连接
    3. 关闭连接
    4. 返回测试结果

    测试查询：
    - MySQL/PostgreSQL: SELECT 1
    - MS SQL Server: SELECT 1
    - IBM DB2: SELECT 1 FROM sysibm.sysdummy1

    异常处理：
    - 如果数据库类型不支持，返回 "Unsupported database type"
    - 如果连接失败，返回详细错误信息

    注意：
    - 连接测试后立即关闭
    - 不会执行任何数据修改操作
    - 需要安装对应数据库的驱动
    """
    req = request_body.model_dump()

    try:
        if req["db_type"] in ["mysql", "mariadb"]:
            url = f"mysql+pymysql://{quote_plus(req['username'])}:{quote_plus(req['password'])}@{req['host']}:{req['port']}/{req['database']}"
            engine = create_engine(url)
        elif req["db_type"] == "oceanbase":
            url = f"mysql+pymysql://{quote_plus(req['username'])}:{quote_plus(req['password'])}@{req['host']}:{req['port']}/{req['database']}?charset=utf8mb4"
            engine = create_engine(url)
        elif req["db_type"] == "postgres":
            url = f"postgresql+psycopg2://{quote_plus(req['username'])}:{quote_plus(req['password'])}@{req['host']}:{req['port']}/{req['database']}"
            engine = create_engine(url)
        elif req["db_type"] == "mssql":
            import pyodbc

            connection_string = f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={req['host']},{req['port']};DATABASE={req['database']};UID={req['username']};PWD={req['password']};"
            db_conn = pyodbc.connect(connection_string)
            cursor = db_conn.cursor()
            cursor.execute("SELECT 1")
            cursor.close()
        elif req["db_type"] == "IBM DB2":
            import ibm_db

            conn_str = f"DATABASE={req['database']};HOSTNAME={req['host']};PORT={req['port']};PROTOCOL=TCPIP;UID={req['username']};PWD={req['password']};"
            redacted_conn_str = f"DATABASE={req['database']};HOSTNAME={req['host']};PORT={req['port']};PROTOCOL=TCPIP;UID={req['username']};PWD=****;"
            logging.info(redacted_conn_str)
            conn = ibm_db.connect(conn_str, "", "")
            stmt = ibm_db.exec_immediate(conn, "SELECT 1 FROM sysibm.sysdummy1")
            ibm_db.fetch_assoc(stmt)
            ibm_db.close(conn)
            return get_json_result(data="Database Connection Successful!")
        elif req["db_type"] == "trino":

            def _parse_catalog_schema(db_name: str):
                if not db_name:
                    return None, None
                if "." in db_name:
                    catalog_name, schema_name = db_name.split(".", 1)
                elif "/" in db_name:
                    catalog_name, schema_name = db_name.split("/", 1)
                else:
                    catalog_name, schema_name = db_name, "default"
                return catalog_name, schema_name

            try:
                import os

                import trino
            except Exception as e:
                return server_error_response(f"Missing dependency 'trino'. Please install: pip install trino, detail: {e}")

            catalog, schema = _parse_catalog_schema(req["database"])
            if not catalog:
                return server_error_response("For Trino, 'database' must be 'catalog.schema' or at least 'catalog'.")

            http_scheme = "https" if os.environ.get("TRINO_USE_TLS", "0") == "1" else "http"

            auth = None
            if http_scheme == "https" and req.get("password"):
                auth = trino.auth.BasicAuthentication(req.get("username") or "ragflow", req["password"])

            conn = trino.dbapi.connect(
                host=req["host"], port=int(req["port"] or 8080), user=req["username"] or "ragflow", catalog=catalog, schema=schema or "default", http_scheme=http_scheme, auth=auth
            )
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchall()
            cur.close()
            conn.close()
            return get_json_result(data="Database Connection Successful!")
        else:
            return server_error_response("Unsupported database type.")

        if req["db_type"] != "mssql":
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            engine.dispose()

        return get_json_result(data="Database Connection Successful!")
    except Exception as e:
        return server_error_response(e)


@router.get("/agents/{canvas_id}/versions", summary="获取Canvas版本历史", response_description="成功获取版本列表")
def getlistversion(canvas_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取Canvas的所有历史版本

    概要：返回指定Canvas的版本历史列表，按时间倒序排列。

    参数：
    - **canvas_id**: Canvas ID

    返回：
    - list: 版本列表，每个版本包含：
        - id: 版本ID
        - user_canvas_id: Canvas ID
        - dsl: 该版本的DSL配置
        - title: 版本标题（包含时间戳）
        - create_time: 创建时间
        - update_time: 更新时间

    功能：
    1. 查询Canvas的所有版本记录
    2. 按更新时间倒序排序
    3. 返回版本列表

    版本命名：
    - 格式：{Canvas标题}_{时间戳}
    - 例如：MyAgent_2024_01_15_10_30_45

    业务场景：
    - 查看Canvas的修改历史
    - 恢复到之前的版本
    - 比较不同版本的配置

    异常处理：
    - 如果查询失败，返回错误信息

    注意：
    - 版本按时间倒序排列，最新的在前
    - 系统会自动清理过期版本
    """
    if not UserCanvasService.accessible(db, canvas_id, user.id):
        return get_json_result(data=False, retmsg="Only owner of canvas authorized for this operation.", retcode=RetCode.OPERATING_ERROR)
    try:
        versions = UserCanvasVersionService.list_by_canvas_id(db, canvas_id)
        version_list = sorted([v.to_dict() for v in versions], key=lambda x: x["update_time"] * -1)
        return get_json_result(data=version_list)
    except Exception as e:
        return get_data_error_result(retmsg=f"Error getting history files: {e}")


@router.get("/agents/{canvas_id}/versions/{version_id}", summary="获取特定版本详情", response_description="成功获取版本详情")
def getversion(canvas_id: str, version_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取Canvas特定版本的详细配置

    概要：根据版本ID查询并返回该版本的完整DSL配置。

    参数：
    - **version_id**: 版本ID

    返回：
    - dict: 版本详细信息

    业务场景：
    - 查看历史版本的具体配置
    - 恢复到指定版本
    - 比较版本差异

    异常处理：
    - 如果版本不存在，返回 None
    - 查询错误返回错误信息
    """
    if not UserCanvasService.accessible(db, canvas_id, user.id):
        return get_json_result(data=False, retmsg="Only owner of canvas authorized for this operation.", retcode=RetCode.OPERATING_ERROR)
    try:
        version = UserCanvasVersionService.get_by_id(db, version_id)
        if version and str(version.user_canvas_id) == str(canvas_id):
            return get_json_result(data=version.to_dict())
        return get_data_error_result(retmsg="Version not found.")
    except Exception as e:
        return get_json_result(data=f"Error getting history file: {e}")


@router.get("/agents", summary="列出Canvas", response_description="成功获取Canvas列表")
def list_canvas(
    keywords: str = Query("", description="搜索关键词"),
    page: int = Query(0, description="页码"),
    page_size: int = Query(0, description="每页数量"),
    orderby: str = Query("create_time", description="排序字段"),
    desc: bool = Query(True, description="是否降序"),
    canvas_category: str | None = Query(None, description="Canvas类别"),
    owner_ids: str = Query("", description="所有者ID列表，逗号分隔"),
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    """
    获取Canvas列表

    概要：查询用户可访问的Canvas列表，支持搜索、筛选、排序和分页。

    参数：
    - **keywords**: 搜索关键词，匹配Canvas标题
    - **page**: 页码，0表示不分页
    - **page_size**: 每页数量，0表示不分页
    - **orderby**: 排序字段，默认按创建时间
    - **desc**: 是否降序排列
    - **canvas_category**: Canvas类别筛选
    - **owner_ids**: 所有者ID列表，逗号分隔

    返回：
    - dict: 包含以下字段
        - canvas: Canvas列表
        - total: 总数量

    功能：
    1. **默认模式**（无owner_ids）：
       - 获取用户加入的所有租户
       - 查询这些租户下的Canvas
       - 包括用户自己创建的Canvas

    2. **指定所有者模式**（有owner_ids）：
       - 只查询指定所有者的Canvas
       - 不分页，返回所有结果

    权限逻辑：
    - 可以看到同租户下其他人的Canvas（如果设置为TEAM权限）
    - 可以看到自己创建的所有Canvas

    业务场景：
    - Canvas管理界面
    - 团队Canvas浏览
    - 搜索特定Canvas

    注意：
    - owner_ids为空时使用默认权限逻辑
    - 支持跨租户查询（如果在多个租户中）
    """
    owner_id_list = [owner_id for owner_id in owner_ids.strip().split(",") if owner_id]
    tenants = TenantService.get_joined_tenants_by_user_id(db, user.id)
    authorized_owner_ids = {member.tenant_id for member in tenants}
    authorized_owner_ids.add(user.id)

    if not owner_id_list:
        # 默认模式：获取用户有权限的所有Canvas
        canvas, total = UserCanvasService.get_by_tenant_ids(db, list(authorized_owner_ids), user.id, page, page_size, orderby, desc, keywords, canvas_category)
    else:
        # 指定所有者模式
        if set(owner_id_list) - authorized_owner_ids:
            return get_json_result(data=False, retmsg="Only authorized owner_ids can be queried.", retcode=RetCode.OPERATING_ERROR)
        canvas, total = UserCanvasService.get_by_tenant_ids(db, owner_id_list, user.id, 0, 0, orderby, desc, keywords, canvas_category)

    return get_json_result(data={"canvas": canvas, "total": total})


def setting(request_body: CanvasSettingRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    更新Canvas的基本设置

    概要：修改Canvas的标题、描述、权限和头像等基本信息。

    参数：
    - **request_body**: 请求体
        - id: Canvas ID
        - title: 标题
        - permission: 权限设置
        - description: 描述（可选）
        - avatar: 头像URL（可选）

    返回：
    - dict: 更新的记录数

    功能：
    1. 验证用户权限
    2. 获取当前Canvas信息
    3. 更新指定字段
    4. 保存到数据库

    可更新字段：
    - title: Canvas标题
    - description: 详细描述
    - permission: 权限（PRIVATE/TEAM）
    - avatar: 头像图片URL

    权限要求：
    - 用户必须是Canvas的所有者

    异常处理：
    - 如果无权限，返回 OPERATING_ERROR
    - 如果Canvas不存在，返回 "canvas not found"

    注意：
    - 只更新提供的字段
    - 不会影响DSL配置
    """
    req = request_body.model_dump()
    req["user_id"] = user.id

    if not UserCanvasService.accessible(db, req["id"], user.id):
        return get_json_result(data=False, retmsg="Only owner of canvas authorized for this operation.", retcode=RetCode.OPERATING_ERROR)

    flow = UserCanvasService.get_by_id(db, req["id"])
    if not flow:
        return get_data_error_result(retmsg="canvas not found.")

    flow_dict = flow.to_dict()
    flow_dict["title"] = req["title"]

    for key in ["description", "permission", "avatar"]:
        if value := req.get(key):
            flow_dict[key] = value

    num = UserCanvasService.update_by_id(db, req["id"], flow_dict)
    return get_json_result(data=num)


@router.get("/agents/{canvas_id}/logs/{message_id}", summary="追踪Canvas执行日志", response_description="成功获取执行日志")
def trace(canvas_id: str, message_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取Canvas执行过程的详细日志

    概要：查询指定消息的执行日志，用于调试和追踪Canvas的运行过程。

    参数：
    - **canvas_id**: Canvas ID
    - **message_id**: 消息ID（任务ID）

    返回：
    - dict: 执行日志，包含：
        - 组件执行顺序
        - 每个组件的输入输出
        - 执行时间
        - 错误信息

    功能：
    1. 从Redis获取日志数据
    2. 解析JSON格式的日志
    3. 返回结构化日志

    日志内容：
    - 执行路径
    - 组件状态
    - 中间结果
    - 性能指标

    业务场景：
    - 调试Canvas执行问题
    - 分析性能瓶颈
    - 追踪数据流

    注意：
    - 日志有过期时间
    - 如果日志不存在返回空对象
    - 日志存储在Redis中
    """
    if not UserCanvasService.accessible(db, canvas_id, user.id):
        return get_json_result(data=False, retmsg="Only owner of canvas authorized for this operation.", retcode=RetCode.OPERATING_ERROR)
    try:
        binary = REDIS_CONN.get(f"{canvas_id}-{message_id}-logs")
        if not binary:
            return get_json_result(data={})
        return get_json_result(data=json.loads(binary.encode("utf-8")))
    except Exception as e:
        logging.exception(e)
        return get_json_result(data={})


@router.get("/agents/{canvas_id}/sessions", summary="获取Canvas会话列表", response_description="成功获取会话列表")
def sessions(
    canvas_id: str,
    user_id: str | None = Query(None, description="用户ID筛选"),
    exp_user_id: str | None = Query(None, description="实验用户ID筛选"),
    page: int = Query(1, description="页码"),
    page_size: int = Query(30, description="每页数量"),
    orderby: str = Query("update_time", description="排序字段"),
    desc: bool = Query(True, description="是否降序"),
    keywords: str | None = Query(None, description="搜索关键词"),
    from_date: str | None = Query(None, description="开始日期"),
    to_date: str | None = Query(None, description="结束日期"),
    dsl: bool = Query(True, description="是否包含DSL"),
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    """
    获取Canvas的会话历史

    概要：查询指定Canvas的所有对话会话，支持多维度筛选和分页。

    参数：
    - **canvas_id**: Canvas ID
    - **user_id**: 用户ID，筛选特定用户的会话
    - **page**: 页码
    - **page_size**: 每页数量
    - **orderby**: 排序字段
    - **desc**: 是否降序
    - **keywords**: 搜索关键词
    - **from_date**: 开始日期
    - **to_date**: 结束日期
    - **dsl**: 是否包含DSL配置

    返回：
    - dict: 包含以下字段
        - total: 总会话数
        - sessions: 会话列表

    会话信息包含：
    - session_id: 会话ID
    - user_id: 用户ID
    - message: 消息历史
    - dsl: Canvas配置（可选）
    - create_time: 创建时间
    - update_time: 更新时间

    功能：
    1. 验证Canvas访问权限
    2. 根据条件筛选会话
    3. 分页返回结果

    权限要求：
    - 用户必须是Canvas的所有者

    筛选条件：
    - 按用户筛选
    - 按时间范围筛选
    - 关键词搜索

    异常处理：
    - 如果无权限，返回 OPERATING_ERROR

    注意：
    - dsl字段可能较大，按需获取
    - 支持日期范围查询
    """
    tenant_id = user.id

    if not UserCanvasService.accessible(db, canvas_id, tenant_id):
        return get_json_result(data=False, retmsg="Only owner of canvas authorized for this operation.", retcode=RetCode.OPERATING_ERROR)

    if exp_user_id:
        sess = API4ConversationService.get_names(db, canvas_id, exp_user_id)
        return get_json_result(data={"total": len(sess), "sessions": sess})

    include_dsl = dsl

    try:
        total, sess = API4ConversationService.get_list(
            db,
            canvas_id,
            tenant_id,
            page,
            page_size,
            orderby,
            desc,
            None,
            user_id,
            include_dsl,
            keywords or "",
            from_date,
            to_date,
            exp_user_id=exp_user_id,
        )
        return get_json_result(data={"total": total, "sessions": [_normalize_agent_session(item) for item in sess]})
    except Exception as e:
        return server_error_response(e)


@router.post("/agents/{canvas_id}/sessions", summary="创建Canvas会话", response_description="成功创建会话")
def set_session(
    canvas_id: str,
    request_body: dict[str, Any],
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    """
    为指定Canvas创建一个新的会话。

    参数：
    - **canvas_id**: Canvas ID
    - **name**: 可选，会话名称

    返回：
    - dict: 新建会话的完整信息
    """
    tenant_id = user.id
    session_user_id = request_body.get("user_id") or tenant_id
    release_mode = bool(request_body.get("release", False))
    try:
        cvs, dsl = UserCanvasService.get_agent_dsl_with_release(db, canvas_id, release_mode, tenant_id)
    except LookupError:
        return get_data_error_result(retmsg="Agent not found.")
    except PermissionError as error:
        return get_data_error_result(retmsg=str(error))

    session_id = get_uuid()
    canvas = Canvas(dsl, tenant_id, canvas_id, canvas_id=cvs.id)
    canvas.reset()
    normalized_dsl = json.loads(str(canvas))
    version_title = UserCanvasVersionService.get_latest_version_title(db, cvs.id, release_mode=release_mode)
    conv = {
        "id": session_id,
        "name": request_body.get("name", ""),
        "dialog_id": cvs.id,
        "user_id": session_user_id,
        "exp_user_id": session_user_id,
        "message": [{"role": "assistant", "content": canvas.get_prologue()}],
        "source": "agent",
        "dsl": normalized_dsl,
        "reference": [],
        "version_title": version_title,
    }
    API4ConversationService.save(db, **conv)
    return get_json_result(data=_normalize_agent_session(conv))


@router.get("/agents/{canvas_id}/sessions/{session_id}", summary="获取单个Canvas会话", response_description="成功获取会话详情")
def get_session(
    canvas_id: str,
    session_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    """
    获取指定Canvas下的单个会话详情。

    参数：
    - **canvas_id**: Canvas ID
    - **session_id**: 会话 ID

    返回：
    - dict: 会话的完整信息
    """
    tenant_id = user.id
    if not UserCanvasService.accessible(db, canvas_id, tenant_id):
        return get_json_result(
            data=False,
            retmsg="Only owner of canvas authorized for this operation.",
            retcode=RetCode.OPERATING_ERROR,
        )
    conv = API4ConversationService.get_by_id(db, session_id)
    if conv is None:
        return get_data_error_result(retmsg="Session not found.")
    return get_json_result(data=_normalize_agent_session(conv.to_dict()))


@router.delete("/agents/{canvas_id}/sessions/{session_id}", summary="删除Canvas会话", response_description="成功删除会话")
def del_session(
    canvas_id: str,
    session_id: str,
    db: Session = Depends(get_db),
    user=Depends(manager),
):
    tenant_id = user.id
    if not UserCanvasService.accessible(db, canvas_id, tenant_id):
        return get_json_result(
            data=False,
            retmsg="Only owner of canvas authorized for this operation.",
            retcode=RetCode.OPERATING_ERROR,
        )
    return get_json_result(data=API4ConversationService.delete_by_id(db, session_id))


@router.get("/agents/prompts", summary="获取系统提示词", response_description="成功获取提示词")
def prompts(db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取系统预定义的提示词模板

    概要：返回Agent推理过程中使用的各种提示词模板。

    返回：
    - dict: 提示词集合，包含：
        - task_analysis: 任务分析提示词
        - plan_generation: 计划生成提示词
        - reflection: 反思提示词
        - citation_guidelines: 引用指南

    功能：
    1. 从prompts模块导入提示词
    2. 组合成字典返回

    提示词类型：
    - **task_analysis**: 分析用户任务的系统提示词
    - **plan_generation**: 生成执行计划的提示词
    - **reflection**: 反思执行结果的提示词
    - **citation_guidelines**: 引用来源的格式指南

    业务场景：
    - Agent配置界面
    - 自定义提示词参考
    - 理解Agent推理流程

    注意：
    - 这些是默认提示词，可以被覆盖
    - 提示词包含占位符，使用时需要替换
    """
    from core.prompts.generator import ANALYZE_TASK_SYSTEM, ANALYZE_TASK_USER, CITATION_PROMPT_TEMPLATE, NEXT_STEP, REFLECT

    return get_json_result(
        data={"task_analysis": ANALYZE_TASK_SYSTEM + "\n\n" + ANALYZE_TASK_USER, "plan_generation": NEXT_STEP, "reflection": REFLECT, "citation_guidelines": CITATION_PROMPT_TEMPLATE}
    )


@router.get("/agents/download", summary="下载文件", response_description="成功下载文件")
def download(id: str = Query(..., description="文件ID"), created_by: str = Query(..., description="创建者ID"), db: Session = Depends(get_db)):
    """
    下载Canvas中上传的文件

    概要：根据文件ID和创建者ID下载文件内容。

    参数：
    - **id**: 文件ID（存储位置）
    - **created_by**: 创建者ID（用户ID）

    返回：
    - 文件二进制内容

    功能：
    1. 从存储系统获取文件内容
    2. 返回二进制数据

    业务场景：
    - 下载Canvas中使用的文件
    - 预览文档内容
    - 导出处理结果

    注意：
    - 需要提供正确的创建者ID
    - 返回原始二进制内容
    - 不包含文件名等元信息
    """
    blob = FileService.get_blob(created_by, id)

    response = Response(content=blob)
    response.headers["Content-Type"] = "application/octet-stream"
    return response


@router.get("/agents/{canvas_id}", summary="获取Canvas详情", response_description="成功获取Canvas详情")
def get_agent(canvas_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    return get(canvas_id, db, user)


@router.api_route("/agents/{agent_id}/webhook", methods=["POST", "GET", "PUT", "PATCH", "DELETE", "HEAD"], summary="Webhook触发代理")
@router.api_route("/agents/{agent_id}/webhook/test", methods=["POST", "GET", "PUT", "PATCH", "DELETE", "HEAD"], summary="Webhook测试")
# async-db-ok: blocking service calls are dispatched through thread_pool_exec
async def webhook(agent_id: str, request: Request, db: Session = Depends(get_db)):
    """
    通过Webhook触发代理执行（增强版）

    特性：
    - 支持多种HTTP方法（POST/GET/PUT/PATCH/DELETE/HEAD）
    - 安全验证（max_body_size, IP白名单, 速率限制, 认证）
    - 文件上传支持
    - 统一的Content-Type处理
    - Schema-based提取和类型验证
    - 两种执行模式：立即返回/流式返回

    Args:
        agent_id: 代理ID
        request: HTTP请求对象
        db: 数据库会话

    Returns:
        根据执行模式返回响应或SSE流
    """
    is_test = request.url.path.endswith("/webhook/test")
    start_ts = time.time()

    # 1. Fetch canvas by agent_id
    cvs = await thread_pool_exec(UserCanvasService.get_by_id, db, agent_id)
    if not cvs:
        return get_data_error_result(retcode=RetCode.BAD_REQUEST, retmsg="Canvas not found.")

    # 2. Check canvas category
    if cvs.canvas_category == CanvasCategory.DataFlow:
        return get_data_error_result(retcode=RetCode.BAD_REQUEST, retmsg="Dataflow can not be triggered by webhook.")

    # 3. Load DSL from canvas
    dsl = getattr(cvs, "dsl", None)
    if not isinstance(dsl, dict):
        return get_data_error_result(retcode=RetCode.BAD_REQUEST, retmsg="Invalid DSL format.")

    # 4. Check webhook configuration in DSL
    webhook_cfg = {}
    components = dsl.get("components", {})
    for k in components:
        cpn_obj = components[k]["obj"]
        if cpn_obj["component_name"].lower() == "begin" and cpn_obj["params"].get("mode") == "Webhook":
            webhook_cfg = cpn_obj["params"]
            break

    if not webhook_cfg:
        return get_data_error_result(retcode=RetCode.BAD_REQUEST, retmsg="Webhook not configured for this agent.")

    # 5. Validate request method against webhook_cfg.methods
    allowed_methods = webhook_cfg.get("methods", [])
    request_method = request.method.upper()
    if allowed_methods and request_method not in allowed_methods:
        return get_data_error_result(retcode=RetCode.BAD_REQUEST, retmsg=f"HTTP method '{request_method}' not allowed for this webhook.")

    # 6. Validate webhook security
    async def validate_webhook_security(security_cfg: dict):
        """Validate webhook security rules based on security configuration."""
        if not security_cfg:
            return  # No security config → allowed by default

        # 1. Validate max body size
        await _validate_max_body_size(security_cfg)

        # 2. Validate IP whitelist
        _validate_ip_whitelist(security_cfg)

        # 3. Validate rate limiting
        _validate_rate_limit(security_cfg)

        # 4. Validate authentication
        auth_type = security_cfg.get("auth_type", "none")

        if auth_type == "none":
            return

        if auth_type == "token":
            _validate_token_auth(security_cfg)
        elif auth_type == "basic":
            _validate_basic_auth(security_cfg)
        elif auth_type == "jwt":
            _validate_jwt_auth(security_cfg)
        else:
            raise Exception(f"Unsupported auth_type: {auth_type}")

    async def _validate_max_body_size(security_cfg):
        """Check request size does not exceed max_body_size."""
        max_size = security_cfg.get("max_body_size")
        if not max_size:
            return

        # Convert "10MB" → bytes
        units = {"kb": 1024, "mb": 1024**2}
        size_str = max_size.lower()

        limit = None
        for suffix, factor in units.items():
            if size_str.endswith(suffix):
                limit = int(size_str.replace(suffix, "")) * factor
                break

        if limit is None:
            raise Exception("Invalid max_body_size format")

        MAX_LIMIT = 10 * 1024 * 1024  # 10MB
        if limit > MAX_LIMIT:
            raise Exception("max_body_size exceeds maximum allowed size (10MB)")

        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > limit:
            raise Exception(f"Request body too large: {content_length} > {limit}")

    def _validate_ip_whitelist(security_cfg):
        """Allow only IPs listed in ip_whitelist."""
        whitelist = security_cfg.get("ip_whitelist", [])
        if not whitelist:
            return

        client_ip = request.client.host if request.client else "unknown"

        for rule in whitelist:
            if "/" in rule:
                # CIDR notation
                if ipaddress.ip_address(client_ip) in ipaddress.ip_network(rule, strict=False):
                    return
            else:
                # Single IP
                if client_ip == rule:
                    return

        raise Exception(f"IP {client_ip} is not allowed by whitelist")

    def _validate_rate_limit(security_cfg):
        """Simple in-memory rate limiting using Redis token bucket."""
        rl = security_cfg.get("rate_limit")
        if not rl:
            return

        limit = int(rl.get("limit", 60))
        if limit <= 0:
            raise Exception("rate_limit.limit must be > 0")
        per = rl.get("per", "minute")

        window = {
            "second": 1,
            "minute": 60,
            "hour": 3600,
            "day": 86400,
        }.get(per)

        if not window:
            raise Exception(f"Invalid rate_limit.per: {per}")

        capacity = limit
        rate = limit / window
        cost = 1

        key = f"rl:tb:{agent_id}"
        now = time.time()

        try:
            res = REDIS_CONN.lua_token_bucket(
                keys=[key],
                args=[capacity, rate, now, cost],
                client=REDIS_CONN.REDIS,
            )

            allowed = int(res[0])
            if allowed != 1:
                raise Exception("Too many requests (rate limit exceeded)")

        except Exception as e:
            raise Exception(f"Rate limit error: {e}")

    def _validate_token_auth(security_cfg):
        """Validate header-based token authentication."""
        token_cfg = security_cfg.get("token", {})
        header = token_cfg.get("token_header")
        token_value = token_cfg.get("token_value")

        provided = request.headers.get(header)
        if provided != token_value:
            raise Exception("Invalid token authentication")

    def _validate_basic_auth(security_cfg):
        """Validate HTTP Basic Auth credentials."""
        auth_cfg = security_cfg.get("basic_auth", {})
        username = auth_cfg.get("username")
        password = auth_cfg.get("password")

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Basic "):
            raise Exception("Missing Basic Auth")

        try:
            encoded = auth_header[6:]
            decoded = base64.b64decode(encoded).decode("utf-8")
            provided_user, provided_pass = decoded.split(":", 1)
            if provided_user != username or provided_pass != password:
                raise Exception("Invalid Basic Auth credentials")
        except Exception:
            raise Exception("Invalid Basic Auth credentials")

    def _validate_jwt_auth(security_cfg):
        """Validate JWT token in Authorization header."""
        jwt_cfg = security_cfg.get("jwt", {})
        secret = jwt_cfg.get("secret")
        if not secret:
            raise Exception("JWT secret not configured")

        auth_header = request.headers.get("authorization", "")
        if not auth_header.startswith("Bearer "):
            raise Exception("Missing Bearer token")

        token = auth_header[len("Bearer ") :].strip()
        if not token:
            raise Exception("Empty Bearer token")

        alg = (jwt_cfg.get("algorithm") or "HS256").upper()

        decode_kwargs = {
            "key": secret,
            "algorithms": [alg],
        }
        options = {}
        if jwt_cfg.get("audience"):
            decode_kwargs["audience"] = jwt_cfg["audience"]
            options["verify_aud"] = True
        else:
            options["verify_aud"] = False

        if jwt_cfg.get("issuer"):
            decode_kwargs["issuer"] = jwt_cfg["issuer"]
            options["verify_iss"] = True
        else:
            options["verify_iss"] = False

        try:
            decoded = jwt.decode(
                token,
                options=options,
                **decode_kwargs,
            )
        except Exception as e:
            raise Exception(f"Invalid JWT: {e!s}")

        raw_required_claims = jwt_cfg.get("required_claims", [])
        if isinstance(raw_required_claims, str):
            required_claims = [raw_required_claims]
        elif isinstance(raw_required_claims, (list, tuple, set)):
            required_claims = list(raw_required_claims)
        else:
            required_claims = []

        required_claims = [c for c in required_claims if isinstance(c, str) and c.strip()]

        RESERVED_CLAIMS = {"exp", "sub", "aud", "iss", "nbf", "iat"}
        for claim in required_claims:
            if claim in RESERVED_CLAIMS:
                raise Exception(f"Reserved JWT claim cannot be required: {claim}")

        for claim in required_claims:
            if claim not in decoded:
                raise Exception(f"Missing JWT claim: {claim}")

        return decoded

    try:
        security_config = webhook_cfg.get("security", {})
        await validate_webhook_security(security_config)
    except Exception as e:
        return get_data_error_result(retcode=RetCode.BAD_REQUEST, retmsg=str(e))

    if not isinstance(cvs.dsl, str):
        dsl_str = json.dumps(cvs.dsl, ensure_ascii=False)
    else:
        dsl_str = cvs.dsl

    try:
        canvas = Canvas(dsl_str, cvs.user_id, agent_id, canvas_id=agent_id)
    except Exception as e:
        return get_data_error_result(retcode=RetCode.BAD_REQUEST, retmsg=str(e))

    # 7. Parse request body
    async def parse_webhook_request(content_type):
        """Parse request based on content-type and return structured data."""
        # 1. Query
        query_data = dict(request.query_params)

        # 2. Headers
        header_data = dict(request.headers)

        # 3. Body
        ctype = request.headers.get("content-type", "").split(";")[0].strip()
        if ctype and content_type and ctype != content_type:
            raise ValueError(f"Invalid Content-Type: expect '{content_type}', got '{ctype}'")

        body_data: dict = {}

        try:
            if ctype == "application/json":
                body_data = await request.json() or {}

            elif ctype == "multipart/form-data":
                nonlocal canvas
                form_data = await request.form()

                body_data = {}

                # Process regular form fields
                for key, value in form_data.items():
                    if not isinstance(value, UploadFile):
                        body_data[key] = value

                # Process file uploads
                files_list = []
                for key, value in form_data.items():
                    if isinstance(value, UploadFile):
                        if len(files_list) >= 10:
                            raise Exception("Too many uploaded files")

                        desc = await thread_pool_exec(FileService.upload_info, cvs.user_id, value, None)
                        file_parsed = await canvas.get_files_async([desc])
                        body_data[key] = file_parsed

            elif ctype == "application/x-www-form-urlencoded":
                form_data = await request.form()
                body_data = dict(form_data)

            else:
                # text/plain / octet-stream / empty / unknown
                raw = await request.body()
                if raw:
                    try:
                        body_data = json.loads(raw.decode("utf-8"))
                    except Exception:
                        body_data = {}
                else:
                    body_data = {}

        except Exception:
            body_data = {}

        return {
            "query": query_data,
            "headers": header_data,
            "body": body_data,
            "content_type": ctype,
        }

    def extract_by_schema(data, schema, name="section"):
        """
        Extract only fields defined in schema.
        Required fields must exist.
        Optional fields default to type-based default values.
        Type validation included.
        """
        props = schema.get("properties", {})
        required = schema.get("required", [])

        extracted = {}

        for field, field_schema in props.items():
            field_type = field_schema.get("type")

            # 1. Required field missing
            if field in required and field not in data:
                raise Exception(f"{name} missing required field: {field}")

            # 2. Optional → default value
            if field not in data:
                extracted[field] = default_for_type(field_type)
                continue

            raw_value = data[field]

            # 3. Auto convert value
            try:
                value = auto_cast_value(raw_value, field_type)
            except Exception as e:
                raise Exception(f"{name}.{field} auto-cast failed: {e!s}")

            # 4. Type validation
            if not validate_type(value, field_type):
                raise Exception(f"{name}.{field} type mismatch: expected {field_type}, got {type(value).__name__}")

            extracted[field] = value

        return extracted

    def default_for_type(t):
        """Return default value for the given schema type."""
        if t == "file":
            return []
        if t == "object":
            return {}
        if t == "boolean":
            return False
        if t == "number":
            return 0
        if t == "string":
            return ""
        if t and t.startswith("array"):
            return []
        if t == "null":
            return None
        return None

    def auto_cast_value(value, expected_type):
        """Convert string values into schema type when possible."""
        # Non-string values already good
        if not isinstance(value, str):
            return value

        v = value.strip()

        # Boolean
        if expected_type == "boolean":
            if v.lower() in ["true", "1"]:
                return True
            if v.lower() in ["false", "0"]:
                return False
            raise Exception(f"Cannot convert '{value}' to boolean")

        # Number
        if expected_type == "number":
            # integer
            if v.isdigit() or (v.startswith("-") and v[1:].isdigit()):
                return int(v)

            # float
            try:
                return float(v)
            except Exception:
                raise Exception(f"Cannot convert '{value}' to number")

        # Object
        if expected_type == "object":
            try:
                parsed = json.loads(v)
                if isinstance(parsed, dict):
                    return parsed
                else:
                    raise Exception("JSON is not an object")
            except Exception:
                raise Exception(f"Cannot convert '{value}' to object")

        # Array <T>
        if expected_type.startswith("array"):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
                else:
                    raise Exception("JSON is not an array")
            except Exception:
                raise Exception(f"Cannot convert '{value}' to array")

        # String (accept original)
        if expected_type == "string":
            return value

        # File
        if expected_type == "file":
            return value

        # Default: do nothing
        return value

    def validate_type(value, t):
        """Validate value type against schema type t."""
        if t == "file":
            return isinstance(value, list)

        if t == "string":
            return isinstance(value, str)

        if t == "number":
            return isinstance(value, (int, float))

        if t == "boolean":
            return isinstance(value, bool)

        if t == "object":
            return isinstance(value, dict)

        # array<string> / array<number> / array<object>
        if t.startswith("array"):
            if not isinstance(value, list):
                return False

            if "<" in t and ">" in t:
                inner = t[t.find("<") + 1 : t.find(">")]

                # Check each element type
                for item in value:
                    if not validate_type(item, inner):
                        return False

            return True

        return True

    parsed = await parse_webhook_request(webhook_cfg.get("content_types"))
    SCHEMA = webhook_cfg.get("schema", {"query": {}, "headers": {}, "body": {}})

    # Extract strictly by schema
    try:
        query_clean = extract_by_schema(parsed["query"], SCHEMA.get("query", {}), name="query")
        header_clean = extract_by_schema(parsed["headers"], SCHEMA.get("headers", {}), name="headers")
        body_clean = extract_by_schema(parsed["body"], SCHEMA.get("body", {}), name="body")
    except Exception as e:
        return get_data_error_result(retcode=RetCode.BAD_REQUEST, retmsg=str(e))

    clean_request = {"query": query_clean, "headers": header_clean, "body": body_clean, "input": parsed}

    execution_mode = webhook_cfg.get("execution_mode", "Immediately")
    response_cfg = webhook_cfg.get("response", {})

    def append_webhook_trace(agent_id: str, start_ts: float, event: dict, ttl=600):
        """Append event to webhook trace log in Redis."""
        key = f"webhook-trace-{agent_id}-logs"

        raw = REDIS_CONN.get(key)
        obj = json.loads(raw) if raw else {"webhooks": {}}

        ws = obj["webhooks"].setdefault(str(start_ts), {"start_ts": start_ts, "events": []})

        ws["events"].append({"ts": time.time(), **event})

        REDIS_CONN.set_obj(key, obj, ttl)

    if execution_mode == "Immediately":
        status = response_cfg.get("status", 200)
        try:
            status = int(status)
        except (TypeError, ValueError):
            return get_data_error_result(retcode=RetCode.BAD_REQUEST, retmsg=f"Invalid response status code: {status}")

        if not (200 <= status <= 399):
            return get_data_error_result(retcode=RetCode.BAD_REQUEST, retmsg=f"Invalid response status code: {status}, must be between 200 and 399")

        body_tpl = response_cfg.get("body_template", "")

        def parse_body(body: str):
            if not body:
                return None, "application/json"

            try:
                parsed_body = json.loads(body)
                return parsed_body, "application/json"
            except (json.JSONDecodeError, TypeError):
                return body, "text/plain"

        body, content_type = parse_body(body_tpl)

        async def background_run():
            try:
                async for ans in canvas.run(query="", user_id=cvs.user_id, webhook_payload=clean_request):
                    if is_test:
                        append_webhook_trace(agent_id, start_ts, ans)

                if is_test:
                    append_webhook_trace(
                        agent_id,
                        start_ts,
                        {
                            "event": "finished",
                            "elapsed_time": time.time() - start_ts,
                            "success": True,
                        },
                    )

                cvs.dsl = json.loads(str(canvas))
                await thread_pool_exec(UserCanvasService.update_by_id, db, agent_id, cvs.to_dict())

            except Exception as e:
                logging.exception("Webhook background run failed")
                if is_test:
                    try:
                        append_webhook_trace(
                            agent_id,
                            start_ts,
                            {
                                "event": "error",
                                "message": str(e),
                                "error_type": type(e).__name__,
                            },
                        )
                        append_webhook_trace(
                            agent_id,
                            start_ts,
                            {
                                "event": "finished",
                                "elapsed_time": time.time() - start_ts,
                                "success": False,
                            },
                        )
                    except Exception:
                        logging.exception("Failed to append webhook trace")

        asyncio.create_task(background_run())

        if content_type == "application/json":
            return JSONResponse(content=body, status_code=status)
        else:
            return Response(content=body, status_code=status, media_type=content_type)

    else:  # Streaming mode

        async def sse():
            nonlocal canvas
            contents: list[str] = []
            status = 200
            try:
                async for ans in canvas.run(
                    query="",
                    user_id=cvs.user_id,
                    webhook_payload=clean_request,
                ):
                    if ans["event"] == "message":
                        content = ans["data"]["content"]
                        if ans["data"].get("start_to_think", False):
                            content = "<think>"
                        elif ans["data"].get("end_to_think", False):
                            content = "</think>"
                        if content:
                            contents.append(content)
                    if ans["event"] == "message_end":
                        status = int(ans["data"].get("status", status))
                    if is_test:
                        append_webhook_trace(agent_id, start_ts, ans)

                if is_test:
                    append_webhook_trace(
                        agent_id,
                        start_ts,
                        {
                            "event": "finished",
                            "elapsed_time": time.time() - start_ts,
                            "success": True,
                        },
                    )

                final_content = "".join(contents)
                return {
                    "message": final_content,
                    "success": True,
                    "code": status,
                }

            except Exception as e:
                if is_test:
                    append_webhook_trace(
                        agent_id,
                        start_ts,
                        {
                            "event": "error",
                            "message": str(e),
                            "error_type": type(e).__name__,
                        },
                    )
                    append_webhook_trace(
                        agent_id,
                        start_ts,
                        {
                            "event": "finished",
                            "elapsed_time": time.time() - start_ts,
                            "success": False,
                        },
                    )
                return {"code": 400, "message": str(e), "success": False}

        result = await sse()
        return Response(
            json.dumps(result),
            status_code=result["code"],
            media_type="application/json",
        )


@router.get("/agents/{agent_id}/webhook/logs", summary="获取Webhook跟踪日志")
async def webhook_trace(agent_id: str, request: Request):
    """
    获取Webhook执行的跟踪日志

    Args:
        agent_id: 代理ID
        request: HTTP请求对象（用于获取查询参数）

    Returns:
        包含webhook执行事件的JSON响应
    """

    def encode_webhook_id(start_ts: str) -> str:
        WEBHOOK_ID_SECRET = "webhook_id_secret"
        sig = hmac.new(
            WEBHOOK_ID_SECRET.encode("utf-8"),
            start_ts.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(sig).decode("utf-8").rstrip("=")

    def decode_webhook_id(enc_id: str, webhooks: dict) -> str | None:
        for ts in webhooks.keys():
            if encode_webhook_id(ts) == enc_id:
                return ts
        return None

    since_ts = request.query_params.get("since_ts")
    if since_ts:
        since_ts = float(since_ts)
    webhook_id = request.query_params.get("webhook_id")

    key = f"webhook-trace-{agent_id}-logs"
    raw = REDIS_CONN.get(key)

    if since_ts is None:
        now = time.time()
        return get_json_result(
            data={
                "webhook_id": None,
                "events": [],
                "next_since_ts": now,
                "finished": False,
            }
        )

    if not raw:
        return get_json_result(
            data={
                "webhook_id": None,
                "events": [],
                "next_since_ts": since_ts,
                "finished": False,
            }
        )

    obj = json.loads(raw)
    webhooks = obj.get("webhooks", {})

    if webhook_id is None:
        candidates = [float(k) for k in webhooks.keys() if float(k) > since_ts]

        if not candidates:
            return get_json_result(
                data={
                    "webhook_id": None,
                    "events": [],
                    "next_since_ts": since_ts,
                    "finished": False,
                }
            )

        start_ts_found = min(candidates)
        real_id = str(start_ts_found)
        webhook_id = encode_webhook_id(real_id)

        return get_json_result(
            data={
                "webhook_id": webhook_id,
                "events": [],
                "next_since_ts": start_ts_found,
                "finished": False,
            }
        )

    real_id = decode_webhook_id(webhook_id, webhooks)

    if not real_id:
        return get_json_result(
            data={
                "webhook_id": webhook_id,
                "events": [],
                "next_since_ts": since_ts,
                "finished": True,
            }
        )

    ws = webhooks.get(str(real_id))
    events = ws.get("events", [])
    new_events = [e for e in events if e.get("ts", 0) > since_ts]

    next_ts = since_ts
    for e in new_events:
        next_ts = max(next_ts, e["ts"])

    finished = any(e.get("event") == "finished" for e in new_events)

    return get_json_result(
        data={
            "webhook_id": webhook_id,
            "events": new_events,
            "next_since_ts": next_ts,
            "finished": finished,
        }
    )
