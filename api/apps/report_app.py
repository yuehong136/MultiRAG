"""
HTMLReport 设计期端点(/v1/report/*):AI 生成骨架 + 试运行填值,均走 SSE。

收口动机:报告的 prompt/parse/coerce/normalize/expand/fill 逻辑统一在后端
(report_skeleton + report_fill),前端 Designer 只当瘦客户端。试运行端点与真实工作流算子
(html_report.py)共用同一套「展开 → 填值」,故预览=生产。

SSE 不吐原始模型文本,而是后端自己编排/解析/拼装后,流式吐**结构化进度** + 末尾一个
**结构化结果对象**(沿用 /v1/llm/* 的 `data: {json}\n\n` 信封):
  进度:{"retcode":0,"retmsg":"","data":{"phase":..., "current":i, "total":n}}
  结果:{"retcode":0,"retmsg":"done","data":{...}}
  收尾:{"retcode":0,"retmsg":"Stream completed","data":true}
  错误:{"retcode":500,"retmsg":"<msg>","data":null}
"""

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from agent.component.report_fill.fill import fill_skeleton
from agent.component.report_skeleton import expand_open_regions, generate_skeleton
from api.apps import manager
from api.db.db_models import get_db
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name
from api.db.services.llm_service import LLMBundle
from api.db.services.user_service import TenantService
from common.constants import LLMType

router = APIRouter()

_SSE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
    "Content-Type": "text/event-stream; charset=utf-8",
}


class SkeletonGenRequest(BaseModel):
    """报告文本 → 可复用骨架。"""

    report_text: str
    llm_name: str
    gen_conf: dict[str, Any] = {}


class ReportFillRequest(BaseModel):
    """骨架 + 源料 → ReportSchema(先展开生成区,再填值)。variables 为变量字段的样本值。"""

    skeleton: dict[str, Any]
    source_text: str
    llm_name: str
    temperature: float = 0.1
    variables: dict[str, Any] = {}


def _resolve_chat_mdl(db: Session, user: Any, llm_name: str) -> LLMBundle:
    """租户 + 模型名 → LLMBundle(CHAT)。仿 llm_app 的 fine_prompt/chat_service 解析。"""
    tenants = TenantService.get_info_by(db, user.id)
    if not tenants:
        raise HTTPException(status_code=404, detail="Tenant not found!")
    tenant_id = tenants[0]["tenant_id"]
    try:
        mdl_config = get_model_config_by_type_and_name(db, tenant_id, LLMType.CHAT.value, llm_name)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=f"Model {llm_name} not found") from exc
    return LLMBundle(db, tenant_id, mdl_config)


def _make_call_llm(chat_mdl: LLMBundle, gen_conf: dict[str, Any]) -> Callable[[list[dict[str, str]]], Awaitable[str]]:
    """把 LLMBundle 包成 report_skeleton/report_fill 要的注入式 call_llm(同 html_report.py)。"""

    async def call_llm(messages: list[dict[str, str]]) -> str:
        has_sys = bool(messages) and messages[0].get("role") == "system"
        system = messages[0]["content"] if has_sys else ""
        history = messages[1:] if has_sys else messages
        return await chat_mdl.async_chat(system, history, gen_conf)

    return call_llm


def _frame(obj: dict[str, Any]) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


# 进度桥:编排里的同步 on_progress 回调 → 队列 → SSE 生成器穿插 yield。
# 编排跑成 task,SSE 生成器边 drain 边吐;task 完成后吐最终结果 + 收尾;客户端断开则取消 task。
async def _stream_orchestration(produce: Callable[[Callable[[dict[str, Any]], None]], Awaitable[dict[str, Any]]]) -> AsyncIterator[str]:
    queue: asyncio.Queue = asyncio.Queue()
    done_marker = object()

    def emit_progress(data: dict[str, Any]) -> None:
        queue.put_nowait(("progress", data))

    async def runner() -> None:
        try:
            result = await produce(emit_progress)
            await queue.put(("result", result))
        except Exception as exc:  # noqa: BLE001 - 任何编排失败都降级为 SSE error 帧
            logging.exception("report SSE orchestration failed")
            await queue.put(("error", str(exc)))
        finally:
            await queue.put((done_marker, None))

    task = asyncio.create_task(runner())
    try:
        while True:
            kind, payload = await queue.get()
            if kind is done_marker:
                break
            if kind == "progress":
                yield _frame({"retcode": 0, "retmsg": "", "data": payload})
            elif kind == "result":
                yield _frame({"retcode": 0, "retmsg": "done", "data": payload})
            elif kind == "error":
                yield _frame({"retcode": 500, "retmsg": payload, "data": None})
        yield _frame({"retcode": 0, "retmsg": "Stream completed", "data": True})
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


@router.post("/skeleton", summary="AI 生成报告骨架(SSE)", response_description="流式进度 + 最终 SkeletonSchema")
async def report_skeleton_sse(request: SkeletonGenRequest, db: Session = Depends(get_db), user=Depends(manager)):
    chat_mdl = _resolve_chat_mdl(db, user, request.llm_name)
    call_llm = _make_call_llm(chat_mdl, dict(request.gen_conf or {}))

    async def produce(emit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        def on_progress(phase: str, current: int, total: int) -> None:
            emit({"phase": phase, "current": current, "total": total})

        result = await generate_skeleton(request.report_text, call_llm, on_progress)
        return {"skeleton": result.skeleton, "warnings": len(result.errors), "usedFallback": result.used_fallback}

    return StreamingResponse(_stream_orchestration(produce), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post("/fill", summary="试运行填值:展开生成区 + 逐节填值(SSE)", response_description="流式进度 + 最终 ReportSchema")
async def report_fill_sse(request: ReportFillRequest, db: Session = Depends(get_db), user=Depends(manager)):
    chat_mdl = _resolve_chat_mdl(db, user, request.llm_name)
    call_llm = _make_call_llm(chat_mdl, {"temperature": float(request.temperature)})
    variables = request.variables or {}

    def resolve_ref(ref: str) -> Any:
        return variables.get(ref)

    async def produce(emit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        expanded = await expand_open_regions(
            request.skeleton,
            request.source_text,
            call_llm,
            lambda current, total: emit({"phase": "expand", "current": current, "total": total}),
        )
        fill_result = await fill_skeleton(
            expanded.skeleton,
            request.source_text,
            resolve_ref,
            call_llm,
            lambda current, total: emit({"phase": "fill", "current": current, "total": total}),
        )
        return {
            "schema": fill_result.schema,
            "failedSections": len(fill_result.errors),
            "failedRegions": len(expanded.errors),
            # 与真实算子 html_report 同一判败口径(llmSections>0 且 okSections==0 → 失败),保预览=生产
            "llmSections": fill_result.llm_sections,
            "okSections": fill_result.ok_sections,
        }

    return StreamingResponse(_stream_orchestration(produce), media_type="text/event-stream", headers=_SSE_HEADERS)
