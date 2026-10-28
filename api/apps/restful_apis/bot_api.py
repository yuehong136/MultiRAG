import asyncio
import copy
import json
import logging
import os
import re
import tempfile
from collections.abc import AsyncGenerator
from contextlib import aclosing
from io import BytesIO
from typing import Any

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.concurrency import iterate_in_threadpool

from api.db.db_models import TenantLLM, db_connection, get_async_db, get_db
from api.db.joint_services.tenant_model_service import (
    build_bundle_by_id_async,
    build_default_bundle_async,
    build_named_bundle_async,
    get_model_config_by_type_and_name,
    get_tenant_default_model_by_type,
)
from api.db.services.api_service import API4ConversationService
from api.db.services.canvas_service import UserCanvasService, agent_event_error, completion_openai
from api.db.services.canvas_service import completion as agent_completion
from api.db.services.conversation_service import ConversationService, async_completion, async_iframe_completion, validate_iframe_access
from api.db.services.dialog_service import DialogService, async_ask, gen_mindmap
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.knowledgebase_service import EmbeddingModelMismatchError, KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.db.services.search_service import SearchService
from api.db.services.user_service import UserTenantService
from api.utils.agent_completion import AgentCompletionAccumulator, normalize_agent_event, parse_agent_completion_chunk
from api.utils.agent_streaming import AgentStreamingResponse
from api.utils.api_utils import (
    async_beta_token_required,
    async_token_required,
    beta_token_required,
    get_data_openai,
    get_error_data_result,
    get_json_result,
    get_result,
    server_error_response,
    token_required,
)
from api.utils.reference_metadata import ReferenceMetadata, enrich_reference_metadata_async, resolve_reference_metadata_preferences
from api.utils.web_utils import CONTENT_TYPE_MAP, apply_safe_file_response_headers
from common import settings
from common.constants import LLMType, RetCode, StatusEnum
from common.metadata_utils import apply_meta_data_filter, convert_conditions, meta_filter
from common.misc_utils import thread_pool_exec
from common.token_utils import num_tokens_from_string
from core.app.tag import label_question
from core.prompts.generator import cross_languages, keyword_extraction
from core.prompts.template import load_prompt

router = APIRouter()


def _label_question_with_conn(question: str, kbs: list) -> Any:
    """label_question 的自开连接包装：内部还有 tag 缓存与 doc-store 查询等同步 IO，
    只桥 DB 会把它们留在事件循环上——整体交给线程池调用（AGENTS.md 规约）。"""
    with db_connection() as s:
        return label_question(s, question, kbs)


def _shared_search_detail(db: Session, search_id: str, tenant_id: str) -> dict[str, Any]:
    """Resolve a shared search only through a live, permitted membership."""
    detail = SearchService.get_detail(db, search_id)
    if not detail:
        raise PermissionError("No authorization for the search app.")
    membership = UserTenantService.get_membership(db, tenant_id=detail["tenant_id"], user_id=tenant_id)
    if membership is None or not UserTenantService.can_access_tenant_resources(membership.role):
        raise PermissionError("No authorization for the search app.")
    return detail


async def _accessible_shared_kbs(db: AsyncSession, kb_ids: Any, tenant_id: str) -> bool:
    """Check the complete selection before any shared provider or index work."""
    if not isinstance(kb_ids, list) or not kb_ids or any(not isinstance(kb_id, str) or not kb_id for kb_id in kb_ids):
        return False
    return await db.run_sync(lambda s: all(KnowledgebaseService.accessible(s, kb_id, tenant_id) for kb_id in kb_ids))  # TODO(async-phase4)


class ChatCompletionRequest(BaseModel):
    reference_metadata: ReferenceMetadata | None = None
    question: str | None = ""
    session_id: str | None = None
    stream: bool | None = True
    internet: bool | None = None
    metadata_condition: dict[str, Any] | None = None


class ChatCompletionOpenAIRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    stream: bool | None = True
    internet: bool | None = None
    reference: bool | None = False
    extra_body: dict[str, Any] | None = None


class AgentCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    question: str | None = ""
    query: str | None = ""
    stream: bool | None = True
    return_trace: bool | None = False
    session_id: str | None = None
    inputs: dict[str, Any] | None = None
    a2ui: list[dict[str, Any]] | None = None
    metadata: dict[str, Any] | None = None
    files: list[Any] | None = None
    release: bool | str | None = None
    user_id: str | None = ""
    custom_header: str | None = ""


class AskRequest(BaseModel):
    reference_metadata: ReferenceMetadata | None = None
    question: str
    dataset_ids: list[str]


class RelatedQuestionsRequest(BaseModel):
    question: str
    industry: str | None = ""


class ChatbotCompletionRequest(BaseModel):
    reference_metadata: ReferenceMetadata | None = None
    question: str | None = ""
    session_id: str | None = None
    stream: bool | None = True
    internet: bool | None = None
    quote: bool | None = False


class SearchBotAskRequest(BaseModel):
    reference_metadata: ReferenceMetadata | None = None
    question: str
    kb_ids: list[str]
    search_id: str | None = ""


class SearchBotRetrievalTestRequest(BaseModel):
    reference_metadata: ReferenceMetadata | None = None
    kb_id: str | list[str]
    question: str
    page: int | None = 1
    size: int | None = 30
    similarity_threshold: float | None = 0.0
    vector_similarity_weight: float | None = 0.3
    use_kg: bool | None = False
    top_k: int = Field(default=1024, ge=1)
    cross_languages: list[str] | None = []
    search_id: str | None = ""
    doc_ids: list[str] | None = None
    rerank_id: str | None = None
    tenant_rerank_id: int | None = None
    keyword: bool | None = False
    highlight: bool | None = False


class SearchBotRelatedQuestionsRequest(BaseModel):
    question: str
    search_id: str | None = ""


class SearchBotMindmapRequest(BaseModel):
    question: str
    kb_ids: list[str]
    search_id: str | None = ""


def build_sse_error_payload(error: Exception) -> str:
    message = str(error) or "Unknown error"
    error_result = {
        "code": RetCode.EXCEPTION_ERROR,
        "message": message,
    }
    return (
        "data:"
        + json.dumps(
            {
                "event": "message",
                "data": {
                    "content": f"Error {error_result['code']}: {message}\n\n",
                },
                **error_result,
            },
            ensure_ascii=False,
        )
        + "\n\n"
    )


@router.post("/chats/{chat_id}/completions", summary="聊天补全")
async def chat_completion(chat_id: str, request: ChatCompletionRequest, db: AsyncSession = Depends(get_async_db), tenant_id: str = Depends(async_token_required)):
    req = request.model_dump()
    if not req:
        req = {"question": ""}
    if not req.get("session_id"):
        req["question"] = ""

    dia = await db.run_sync(lambda s: DialogService.query(s, tenant_id=tenant_id, id=chat_id, status=StatusEnum.VALID.value))  # TODO(async-phase4)
    if not dia:
        return get_error_data_result(retmsg=f"You don't own the chat {chat_id}")
    dia = dia[0]
    if req.get("session_id"):
        if not await db.run_sync(lambda s: ConversationService.query(s, id=req["session_id"], dialog_id=chat_id)):  # TODO(async-phase4)
            return get_error_data_result(retmsg=f"You don't own the session {req['session_id']}")

    metadata_condition = req.get("metadata_condition") or {}
    if metadata_condition and not isinstance(metadata_condition, dict):
        return get_error_data_result(retmsg="metadata_condition must be an object.")

    if metadata_condition and req.get("question"):
        metas = await db.run_sync(lambda s: DocMetadataService.get_flatted_meta_by_kbs(s, dia.kb_ids or []))  # TODO(async-phase4)
        filtered_doc_ids = meta_filter(
            metas,
            convert_conditions(metadata_condition),
            metadata_condition.get("logic", "and"),
        )
        if metadata_condition.get("conditions") and not filtered_doc_ids:
            filtered_doc_ids = ["-999"]

        if filtered_doc_ids:
            req["doc_ids"] = ",".join(filtered_doc_ids)
        else:
            req.pop("doc_ids", None)

    if req.get("stream", True):
        resp = StreamingResponse(async_completion(db, tenant_id, chat_id, **req), media_type="text/event-stream")
        resp.headers["Cache-control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        return resp
    else:
        answer = None
        async for ans in async_completion(db, tenant_id, chat_id, **req):
            answer = ans
            break
        return get_result(data=answer)


async def agents_completion_openai_compatibility(agent_id: str, request: ChatCompletionOpenAIRequest, db: AsyncSession = Depends(get_async_db), tenant_id: str = Depends(async_token_required)):
    req = request.model_dump()
    messages = req.get("messages", [])
    if not messages:
        return get_error_data_result(retmsg="You must provide at least one message.")
    if not await db.run_sync(lambda s: UserCanvasService.query(s, user_id=tenant_id, id=agent_id)):  # TODO(async-phase4)
        return get_error_data_result(retmsg=f"You don't own the agent {agent_id}")

    filtered_messages = [m for m in messages if m["role"] in ["user", "assistant"]]
    prompt_tokens = sum(num_tokens_from_string(m["content"]) for m in filtered_messages)
    if not filtered_messages:
        return get_data_openai(
            id=agent_id,
            content="No valid messages found (user or assistant).",
            finish_reason="stop",
            model=req.get("model", ""),
            completion_tokens=num_tokens_from_string("No valid messages found (user or assistant)."),
            prompt_tokens=prompt_tokens,
        )

    question = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")

    stream = req.pop("stream", False)
    if stream:
        resp = StreamingResponse(
            completion_openai(
                db,
                tenant_id,
                agent_id,
                question,
                session_id=req.pop("session_id", req.get("id", "") or req.get("metadata", {}).get("id", "")),
                stream=True,
                **req,
            ),
            media_type="text/event-stream",
        )
        resp.headers["Cache-control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        return resp
    else:
        # For non-streaming, just return the response directly
        async for response in completion_openai(
            db,
            tenant_id,
            agent_id,
            question,
            session_id=req.pop("session_id", req.get("id", "") or req.get("metadata", {}).get("id", "")),
            stream=False,
            **req,
        ):
            return response


async def agent_completions(agent_id: str, request: AgentCompletionRequest, db: AsyncSession = Depends(get_async_db), tenant_id: str = Depends(async_token_required)):
    req = request.model_dump()
    return_trace = bool(req.get("return_trace", False))

    if req.get("stream", True):

        async def generate():
            trace_items = []
            async for answer in agent_completion(db=db, tenant_id=tenant_id, agent_id=agent_id, **req):
                if isinstance(answer, str):
                    try:
                        ans = json.loads(answer[5:])  # remove "data:"
                    except Exception:
                        continue

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

                if event not in ["message", "message_end", "a2ui_command"]:
                    continue

                yield answer

            yield "data:[DONE]\n\n"

        resp = StreamingResponse(generate(), media_type="text/event-stream")
        resp.headers["Cache-control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        return resp

    full_content = ""
    reference = {}
    final_ans = ""
    trace_items = []
    structured_output = {}
    async for answer in agent_completion(db=db, tenant_id=tenant_id, agent_id=agent_id, **req):
        try:
            ans = json.loads(answer[5:])

            if ans["event"] == "message":
                full_content += ans["data"]["content"]

            if ans.get("data", {}).get("reference", None):
                reference.update(ans["data"]["reference"])

            if ans.get("event") == "node_finished":
                data = ans.get("data", {})
                node_out = data.get("outputs", {})
                component_id = data.get("component_id")
                if component_id is not None and "structured" in node_out:
                    structured_output[component_id] = copy.deepcopy(node_out["structured"])
                if return_trace:
                    trace_items.append(
                        {
                            "component_id": data.get("component_id"),
                            "trace": [copy.deepcopy(data)],
                        }
                    )

            final_ans = ans
        except Exception as e:
            return get_result(data=f"**ERROR**: {e!s}")
    final_ans["data"]["content"] = full_content
    final_ans["data"]["reference"] = reference
    if structured_output:
        final_ans["data"]["structured"] = structured_output
    if return_trace and final_ans:
        final_ans["data"]["trace"] = trace_items
    return get_result(data=final_ans)


def list_agent_sessions(
    agent_id: str,
    id: str | None = Query(None),
    user_id: str | None = Query(None),
    page: int = Query(1),
    page_size: int = Query(30),
    orderby: str = Query("update_time"),
    desc: bool = Query(True),
    dsl: bool = Query(True),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required),
):
    if not UserCanvasService.query(db, user_id=tenant_id, id=agent_id):
        return get_error_data_result(retmsg=f"You don't own the agent {agent_id}.")

    page_number = int(page)
    items_per_page = int(page_size)
    include_dsl = dsl

    total, convs = API4ConversationService.get_list(db, agent_id, tenant_id, page_number, items_per_page, orderby, desc, id, user_id, include_dsl)
    if not convs:
        return get_result(data=[])

    for conv in convs:
        conv["messages"] = conv.pop("message")
        infos = conv["messages"]
        for info in infos:
            if "prompt" in info:
                info.pop("prompt")
        conv["agent_id"] = conv.pop("dialog_id")
        # Fix for session listing endpoint
        if conv["reference"]:
            messages = conv["messages"]
            message_num = 0
            chunk_num = 0
            # Ensure reference is a list type to prevent KeyError
            if not isinstance(conv["reference"], list):
                conv["reference"] = []
            while message_num < len(messages):
                if message_num != 0 and messages[message_num]["role"] != "user":
                    chunk_list = []
                    # Add boundary and type checks to prevent KeyError
                    if chunk_num < len(conv["reference"]) and conv["reference"][chunk_num] is not None and isinstance(conv["reference"][chunk_num], dict) and "chunks" in conv["reference"][chunk_num]:
                        chunks = conv["reference"][chunk_num]["chunks"]
                        for chunk in chunks:
                            # Ensure chunk is a dictionary before calling get method
                            if not isinstance(chunk, dict):
                                continue
                            new_chunk = {
                                "id": chunk.get("chunk_id", chunk.get("id")),
                                "content": chunk.get("content_with_weight", chunk.get("content")),
                                "document_id": chunk.get("doc_id", chunk.get("document_id")),
                                "document_name": chunk.get("docnm_kwd", chunk.get("document_name")),
                                "dataset_id": chunk.get("kb_id", chunk.get("dataset_id")),
                                "image_id": chunk.get("image_id", chunk.get("img_id")),
                                "positions": chunk.get("positions", chunk.get("position_int")),
                            }
                            chunk_list.append(new_chunk)
                    chunk_num += 1
                    messages[message_num]["reference"] = chunk_list
                message_num += 1
        del conv["reference"]
    return get_result(data=convs)


@router.post("/sessions/ask", summary="询问知识库")
async def ask_about(request: AskRequest, db: AsyncSession = Depends(get_async_db), tenant_id: str = Depends(async_token_required)):
    req = request.model_dump()
    if not req.get("question"):
        return get_error_data_result(retmsg="`question` is required.")
    if not req.get("dataset_ids"):
        return get_error_data_result(retmsg="`dataset_ids` is required.")
    if not isinstance(req.get("dataset_ids"), list):
        return get_error_data_result(retmsg="`dataset_ids` should be a list.")

    req["kb_ids"] = req.pop("dataset_ids")
    for kb_id in req["kb_ids"]:
        if not await db.run_sync(lambda s: KnowledgebaseService.accessible(s, kb_id, tenant_id)):  # TODO(async-phase4)
            return get_error_data_result(retmsg=f"You don't own the dataset {kb_id}.")
        kbs = await db.run_sync(lambda s: KnowledgebaseService.query(s, id=kb_id))  # TODO(async-phase4)
        kb = kbs[0]
        if kb.chunk_num == 0:
            return get_error_data_result(retmsg=f"The dataset {kb_id} doesn't own parsed file")

    uid = tenant_id

    async def stream():
        nonlocal req, uid, db
        try:
            async for ans in async_ask(db, req["question"], req["kb_ids"], uid, search_config={"reference_metadata": req.get("reference_metadata")}):
                yield "data:" + json.dumps({"code": 0, "message": "", "data": ans}, ensure_ascii=False) + "\n\n"
        except Exception as e:
            yield "data:" + json.dumps({"code": 500, "message": str(e), "data": {"answer": "**ERROR**: " + str(e), "reference": []}}, ensure_ascii=False) + "\n\n"
        yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"

    resp = StreamingResponse(stream(), media_type="text/event-stream")
    resp.headers["Cache-control"] = "no-cache"
    resp.headers["Connection"] = "keep-alive"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
    return resp


@router.post("/sessions/related_questions", summary="获取相关问题")
async def related_questions(request: RelatedQuestionsRequest, db: AsyncSession = Depends(get_async_db), tenant_id: str = Depends(async_token_required)):
    req = request.model_dump()
    if not req.get("question"):
        return get_error_data_result(retmsg="`question` is required.")

    question = req["question"]
    industry = req.get("industry", "")
    chat_config = await db.run_sync(lambda s: get_tenant_default_model_by_type(s, tenant_id, LLMType.CHAT))  # TODO(async-phase4)
    chat_mdl = await db.run_sync(lambda s: LLMBundle(s, tenant_id, chat_config))  # TODO(async-phase4)
    chat_mdl.db = None  # run_sync 的 facade 不得逸出 greenlet（AGENTS.md 规约）
    prompt = """
Objective: To generate search terms related to the user's search keywords, helping users find more valuable information.
Instructions:
 - Based on the keywords provided by the user, generate 5-10 related search terms.
 - Each search term should be directly or indirectly related to the keyword, guiding the user to find more valuable information.
 - Use common, general terms as much as possible, avoiding obscure words or technical jargon.
 - Keep the term length between 2-4 words, concise and clear.
 - DO NOT translate, use the language of the original keywords.
"""
    if industry:
        prompt += f" - Ensure all search terms are relevant to the industry: {industry}.\n"
    prompt += """
### Example:
Keywords: Chinese football
Related search terms:
1. Current status of Chinese football
2. Reform of Chinese football
3. Youth training of Chinese football
4. Chinese football in the Asian Cup
5. Chinese football in the World Cup

Reason:
 - When searching, users often only use one or two keywords, making it difficult to fully express their information needs.
 - Generating related search terms can help users dig deeper into relevant information and improve search efficiency.
 - At the same time, related terms can also help search engines better understand user needs and return more accurate search results.

"""
    ans = await chat_mdl.async_chat(
        prompt,
        [
            {
                "role": "user",
                "content": f"""
Keywords: {question}
Related search terms:
    """,
            }
        ],
        {"temperature": 0.9},
    )
    return get_result(data=[re.sub(r"^[0-9]\. ", "", a) for a in ans.split("\n") if re.match(r"^[0-9]\. ", a)])


@router.post("/chatbots/{dialog_id}/completions", summary="聊天机器人补全")
async def chatbot_completions(
    dialog_id: str,
    body: ChatbotCompletionRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_beta_token_required),
) -> Response:
    req = body.model_dump()

    try:
        await db.run_sync(lambda s: validate_iframe_access(s, dialog_id, tenant_id, body.session_id))  # TODO(async-phase4)
    except PermissionError as error:
        return get_error_data_result(retcode=RetCode.AUTHENTICATION_ERROR, retmsg=str(error))

    if "quote" not in req:
        req["quote"] = False

    if req.get("stream", True):
        resp = StreamingResponse(async_iframe_completion(db, dialog_id, tenant_id=tenant_id, **req), media_type="text/event-stream")
        resp.headers["Cache-control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        return resp

    try:
        async for answer in async_iframe_completion(db, dialog_id, tenant_id=tenant_id, **req):
            return get_result(data=answer)
    except PermissionError as error:
        return get_error_data_result(retcode=RetCode.AUTHENTICATION_ERROR, retmsg=str(error))
    return get_error_data_result(retmsg="No answer returned.")


@router.get("/chatbots/{dialog_id}/info", summary="获取聊天机器人信息")
def chatbots_inputs(
    dialog_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(beta_token_required),
) -> JSONResponse:
    try:
        dialog, _ = validate_iframe_access(db, dialog_id, tenant_id)
    except PermissionError as error:
        return get_error_data_result(retcode=RetCode.AUTHENTICATION_ERROR, retmsg=str(error))

    return get_result(
        data={
            "title": dialog.name,
            "avatar": dialog.icon,
            "prologue": dialog.prompt_config.get("prologue", ""),
            "has_tavily_key": bool(dialog.prompt_config.get("tavily_api_key", "").strip()),
        }
    )


@router.post("/agentbots/{agent_id}/completions", summary="代理机器人补全")
async def agent_bot_completions(
    agent_id: str,
    body: AgentCompletionRequest,
    release: str | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_beta_token_required),
) -> Response:
    req = body.model_dump()
    if release is not None and req.get("release") is None:
        req["release"] = release

    if req.get("stream", True):

        async def stream() -> AsyncGenerator[str, None]:
            try:
                async with aclosing(agent_completion(db=db, tenant_id=tenant_id, agent_id=agent_id, **req)) as answers:
                    async for answer in answers:
                        yield answer
            except Exception as e:
                logging.exception(e)
                yield build_sse_error_payload(e)

        resp = AgentStreamingResponse(stream(), media_type="text/event-stream")
        resp.headers["Cache-control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        return resp

    try:
        aggregate = AgentCompletionAccumulator(return_trace=bool(req.get("return_trace", False)))
        async with aclosing(agent_completion(db=db, tenant_id=tenant_id, agent_id=agent_id, **req)) as answers:
            async for answer in answers:
                for event in parse_agent_completion_chunk(answer):
                    failure = agent_event_error(event)
                    if failure:
                        return get_error_data_result(retmsg=failure)
                    aggregate.add(normalize_agent_event(event))
        return get_result(data=aggregate.finish(require_terminal=True))
    except Exception as e:
        logging.exception(e)
        return get_error_data_result(retmsg=str(e) or "Unknown error")


@router.get("/agentbots/{agent_id}/inputs", summary="获取代理机器人输入表单")
def begin_inputs(
    agent_id: str,
    release: str | None = Query(None),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(beta_token_required),
) -> JSONResponse:
    release_mode = str(release or "").strip().lower() == "true"
    try:
        cvs, dsl = UserCanvasService.get_agent_dsl_with_release(
            db,
            agent_id,
            release_mode=release_mode,
            tenant_id=tenant_id,
        )
    except LookupError:
        return get_error_data_result(retmsg=f"Can't find agent by ID: {agent_id}")
    except PermissionError as e:
        return get_error_data_result(retmsg=str(e))

    from agent.canvas import Canvas

    canvas = Canvas(dsl, tenant_id, canvas_id=cvs.id)
    return get_result(
        data={
            "title": cvs.title,
            "avatar": cvs.avatar,
            "inputs": canvas.get_component_input_form("begin"),
            "prologue": canvas.get_prologue(),
            "mode": canvas.get_mode(),
        }
    )


@router.get("/agentbots/{agent_id}/persondataList", summary="获取代理机器人 inputPlugin 人员数据列表")
def agentbot_persondata_list(
    agent_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(beta_token_required),
) -> JSONResponse:
    """
    公开 (share/widget beta-token) 入口，按 agent_id (即 datav workflow_id) 读取
    inputPlugin persondataList。与登录态 `/v1/datav/persondataList/{id}` 同源数据，
    供 Begin 节点 persondata 输入类型在分享页拉取候选项。
    """
    cvs = UserCanvasService.get_by_id(db, agent_id)
    if not cvs:
        return get_error_data_result(retmsg=f"Can't find agent by ID: {agent_id}")
    if cvs.user_id != tenant_id:
        return get_error_data_result(retmsg="You cannot access the agent.")

    try:
        from api.apps.datav_app import fetch_persondata_list

        return get_result(data=fetch_persondata_list(agent_id))
    except ValueError as e:
        return get_error_data_result(retmsg=str(e))
    except Exception as e:
        return server_error_response(e)


@router.get("/agentbots/{agent_id}/attachments/{attachment_id}", summary="下载公开 Agent 附件")
async def download_agentbot_attachment(
    agent_id: str,
    attachment_id: str,
    ext: str = Query("markdown"),
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_beta_token_required),
):
    """
    Public agentbot artifact download for share/widget beta-token flows.

    This is intentionally separate from generic document/file download endpoints
    so embedded agents do not expand the internal document or SDK file auth
    boundary.
    """
    owner_id = await db.run_sync(lambda s: getattr(UserCanvasService.get_by_id(s, agent_id), "user_id", None))  # TODO(async-phase4)
    if owner_id is None:
        return get_error_data_result(retmsg=f"Can't find agent by ID: {agent_id}")
    if owner_id != tenant_id:
        return get_error_data_result(retmsg="You cannot access the agent.")

    try:
        normalized_ext = (ext or "markdown").lstrip(".").lower()
        data = await thread_pool_exec(settings.STORAGE_IMPL.get, tenant_id, attachment_id)
        content_type = CONTENT_TYPE_MAP.get(normalized_ext, f"application/{normalized_ext}")
        response = StreamingResponse(BytesIO(data), media_type=content_type)
        apply_safe_file_response_headers(response, content_type, normalized_ext)
        return response
    except Exception as e:
        return server_error_response(e)


@router.post("/searchbots/ask", summary="搜索机器人询问")
async def ask_about_embedded(
    body: SearchBotAskRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_beta_token_required),
) -> Response:
    req = body.model_dump()
    uid = tenant_id

    search_id = req.get("search_id", "")
    search_config = {}
    if search_id:
        try:
            search_app = await db.run_sync(lambda s: _shared_search_detail(s, search_id, tenant_id))  # TODO(async-phase4)
        except PermissionError as error:
            return get_error_data_result(retcode=RetCode.AUTHENTICATION_ERROR, retmsg=str(error))
        search_config = search_app.get("search_config") or {}
    if not await _accessible_shared_kbs(db, search_config.get("kb_ids", req["kb_ids"]), tenant_id):
        return get_error_data_result(retcode=RetCode.AUTHENTICATION_ERROR, retmsg="No authorization for the selected dataset.")

    async def stream() -> AsyncGenerator[str, None]:
        nonlocal req, uid, db
        try:
            include, fields = resolve_reference_metadata_preferences(req, search_config)
            effective_config = {**search_config, "reference_metadata": {"include": include, "fields": None if fields is None else sorted(fields)}}
            async for ans in async_ask(db, req["question"], req["kb_ids"], uid, search_config=effective_config):
                yield "data:" + json.dumps({"code": 0, "message": "", "data": ans}, ensure_ascii=False) + "\n\n"
        except Exception as e:
            yield "data:" + json.dumps({"code": 500, "message": str(e), "data": {"answer": "**ERROR**: " + str(e), "reference": []}}, ensure_ascii=False) + "\n\n"
        yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"

    resp = StreamingResponse(stream(), media_type="text/event-stream")
    resp.headers["Cache-control"] = "no-cache"
    resp.headers["Connection"] = "keep-alive"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
    return resp


@router.post("/searchbots/retrieval_test", summary="搜索机器人检索测试")
async def retrieval_test_embedded(
    body: SearchBotRetrievalTestRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_beta_token_required),
) -> JSONResponse:
    req = body.model_dump()

    page = int(req.get("page", 1))
    size = int(req.get("size", 30))
    question = req["question"]
    kb_ids = req["kb_id"]
    if isinstance(kb_ids, str):
        kb_ids = [kb_ids]
    if not kb_ids:
        return get_json_result(data=False, retmsg="Please specify dataset firstly.", retcode=RetCode.DATA_ERROR)
    doc_ids = req.get("doc_ids", [])
    similarity_threshold = float(req.get("similarity_threshold", 0.0))
    vector_similarity_weight = float(req.get("vector_similarity_weight", 0.3))
    use_kg = req.get("use_kg", False)
    top = int(req.get("top_k", 1024))
    langs = req.get("cross_languages", [])
    rerank_id = req.get("rerank_id", "")
    tenant_ids = []

    search_config = {}

    try:
        if not await _accessible_shared_kbs(db, kb_ids, tenant_id):
            return get_json_result(data=False, retmsg="Only owner of dataset authorized for this operation.", retcode=RetCode.OPERATING_ERROR)

        kbs = await db.run_sync(lambda s: KnowledgebaseService.get_by_ids(s, kb_ids))  # TODO(async-phase4)
        if {kb.id for kb in kbs} != set(kb_ids):
            return get_error_data_result(retmsg="Knowledgebase not found!")
        tenant_ids = [dataset.tenant_id for dataset in kbs]
        KnowledgebaseService.ensure_same_embedding_model(kbs)
        kb = kbs[0]
        allowed_rerank_tenant_ids = {tenant_id, *[dataset.tenant_id for dataset in kbs]}
        if body.tenant_rerank_id is not None:
            allowed_model = await db.scalar(select(TenantLLM.id).where(TenantLLM.id == body.tenant_rerank_id, TenantLLM.tenant_id.in_(allowed_rerank_tenant_ids)))
            if allowed_model is None:
                return get_json_result(data=False, retmsg="No authorization for rerank model.", retcode=RetCode.AUTHENTICATION_ERROR)

        if req.get("search_id", ""):
            detail = await db.run_sync(lambda s: _shared_search_detail(s, req["search_id"], tenant_id))  # TODO(async-phase4)
            search_config = detail.get("search_config") or {}
            meta_data_filter = search_config.get("meta_data_filter", {})
            if meta_data_filter:

                async def load_metadata() -> dict[str, Any]:
                    return await db.run_sync(lambda s: DocMetadataService.get_flatted_meta_by_kbs(s, kb_ids))  # TODO(async-phase4)

                chat_mdl = None
                if meta_data_filter.get("method") in ["auto", "semi_auto"]:
                    chat_mdl = await build_named_bundle_async(tenant_id, LLMType.CHAT.value, search_config.get("chat_id", ""))
                doc_ids = await apply_meta_data_filter(
                    meta_data_filter,
                    None,
                    question,
                    chat_mdl,
                    doc_ids,
                    metas_loader=load_metadata,
                    metadata_filterer=lambda conditions, logic: DocMetadataService.filter_doc_ids_by_meta_pushdown_async(db, kb_ids, conditions, logic),
                )
            # Apply search_config settings if not explicitly provided in request
            if not req.get("similarity_threshold"):
                similarity_threshold = float(search_config.get("similarity_threshold", similarity_threshold))
            if not req.get("vector_similarity_weight"):
                vector_similarity_weight = float(search_config.get("vector_similarity_weight", vector_similarity_weight))
            if not req.get("top_k"):
                top = int(search_config.get("top_k", top))
            if not req.get("rerank_id"):
                rerank_id = search_config.get("rerank_id", "")

        if langs:
            question = await cross_languages(kb.tenant_id, None, question, langs)

        if kb.tenant_embd_id:
            embd_mdl = await build_bundle_by_id_async(kb.tenant_id, kb.tenant_embd_id, model_type="embedding")
        else:
            embd_mdl = await build_named_bundle_async(kb.tenant_id, LLMType.EMBEDDING.value, kb.embd_id)

        rerank_mdl = None
        if body.tenant_rerank_id is not None:
            rerank_mdl = await build_bundle_by_id_async(tenant_id, body.tenant_rerank_id, db=db, allowed_tenant_ids=allowed_rerank_tenant_ids, model_type="rerank")
        elif rerank_id:
            rerank_mdl = await build_named_bundle_async(kb.tenant_id, LLMType.RERANK.value, rerank_id)

        if req.get("keyword", False):
            chat_mdl = await build_default_bundle_async(kb.tenant_id, LLMType.CHAT)
            question += await keyword_extraction(chat_mdl, question)

        # tag 特征：同步（tag 缓存 + doc-store 查询）且自开连接——入线程池
        labels = await asyncio.to_thread(_label_question_with_conn, question, kbs)
        # 位置参数必须对齐 Dealer.retrieval(question, filter_exp, embd_mdl, tenant_id, kb_names, page, page_size, ...)：
        # kb_names 构建索引名，kb_ids 走 kwarg 做过滤（对齐非 embedded 孪生 dataset_search_service.search_dataset）
        ranks = await settings.retriever.retrieval(
            question,
            "",
            embd_mdl,
            [k.tenant_id for k in kbs],
            [k.name for k in kbs],
            page,
            size,
            similarity_threshold,
            vector_similarity_weight,
            top,
            doc_ids,
            rerank_mdl=rerank_mdl,
            highlight=req.get("highlight"),
            rank_feature=labels,
            kb_ids=[k.id for k in kbs],
        )
        if use_kg:
            kg_chat_mdl = await build_default_bundle_async(kb.tenant_id, LLMType.CHAT)
            ck = await settings.kg_retriever.retrieval(question, tenant_ids, kb_ids, embd_mdl, kg_chat_mdl)
            if ck["content_with_weight"]:
                ranks["chunks"].insert(0, ck)

        for c in ranks["chunks"]:
            c.pop("vector", None)
        await enrich_reference_metadata_async(db, ranks["chunks"], resolve_reference_metadata_preferences(req, search_config))
        ranks["labels"] = labels

        return get_json_result(data=ranks)
    except EmbeddingModelMismatchError as e:
        return get_json_result(data=False, retmsg=str(e), retcode=RetCode.DATA_ERROR)
    except PermissionError as error:
        return get_error_data_result(retcode=RetCode.AUTHENTICATION_ERROR, retmsg=str(error))
    except Exception as e:
        if "not_found" in str(e):
            return get_json_result(data=False, retmsg="No chunk found! Check the chunk status please!", retcode=RetCode.DATA_ERROR)
        return server_error_response(e)


@router.post("/searchbots/related_questions", summary="搜索机器人相关问题")
async def related_questions_embedded(
    body: SearchBotRelatedQuestionsRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_beta_token_required),
) -> JSONResponse:
    req = body.model_dump()

    search_id = req.get("search_id", "")
    search_config = {}
    if search_id:
        try:
            search_app = await db.run_sync(lambda s: _shared_search_detail(s, search_id, tenant_id))  # TODO(async-phase4)
        except PermissionError as error:
            return get_error_data_result(retcode=RetCode.AUTHENTICATION_ERROR, retmsg=str(error))
        search_config = search_app.get("search_config") or {}

    question = req["question"]

    chat_id = search_config.get("chat_id", "")
    if chat_id:
        chat_config = await db.run_sync(lambda s: get_model_config_by_type_and_name(s, tenant_id, LLMType.CHAT.value, chat_id))  # TODO(async-phase4)
    else:
        chat_config = await db.run_sync(lambda s: get_tenant_default_model_by_type(s, tenant_id, LLMType.CHAT))  # TODO(async-phase4)
    chat_mdl = await db.run_sync(lambda s: LLMBundle(s, tenant_id, chat_config))  # TODO(async-phase4)
    chat_mdl.db = None  # run_sync 的 facade 不得逸出 greenlet（AGENTS.md 规约）

    gen_conf = search_config.get("llm_setting", {"temperature": 0.9})
    prompt = load_prompt("related_question")
    ans = await chat_mdl.async_chat(
        prompt,
        [
            {
                "role": "user",
                "content": f"""
Keywords: {question}
Related search terms:
    """,
            }
        ],
        gen_conf,
    )
    return get_json_result(data=[re.sub(r"^[0-9]\. ", "", a) for a in ans.split("\n") if re.match(r"^[0-9]\. ", a)])


@router.get("/searchbots/detail", summary="获取搜索机器人详情")
def detail_share_embedded(
    search_id: str = Query(...),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(beta_token_required),
) -> JSONResponse:
    try:
        return get_json_result(data=_shared_search_detail(db, search_id, tenant_id))
    except PermissionError as error:
        return get_error_data_result(retcode=RetCode.AUTHENTICATION_ERROR, retmsg=str(error))
    except Exception as e:
        return server_error_response(e)


@router.post("/searchbots/mindmap", summary="生成搜索机器人思维导图")
async def mindmap(
    body: SearchBotMindmapRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_beta_token_required),
) -> JSONResponse:
    req = body.model_dump()

    search_id = req.get("search_id", "")
    try:
        search_app = await db.run_sync(lambda s: _shared_search_detail(s, search_id, tenant_id)) if search_id else {}  # TODO(async-phase4)
    except PermissionError as error:
        return get_error_data_result(retcode=RetCode.AUTHENTICATION_ERROR, retmsg=str(error))
    if not await _accessible_shared_kbs(db, req["kb_ids"], tenant_id):
        return get_error_data_result(retcode=RetCode.AUTHENTICATION_ERROR, retmsg="No authorization for the selected dataset.")

    mind_map = await gen_mindmap(db, req["question"], req["kb_ids"], tenant_id, search_app.get("search_config", {}))
    if "error" in mind_map:
        return server_error_response(Exception(mind_map["error"]))
    return get_json_result(data=mind_map)


class TTSRequest(BaseModel):
    text: str


@router.post("/sequence2txt", summary="语音转文字")
async def sequence2txt(
    file: UploadFile = File(...),
    stream: str = "false",
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_token_required),
):
    stream_mode = stream.lower() == "true"

    ALLOWED_EXTS = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".webm", ".opus", ".wma"}
    filename = file.filename or ""
    suffix = os.path.splitext(filename)[-1].lower()
    if suffix not in ALLOWED_EXTS:
        return get_error_data_result(retmsg=f"Unsupported audio format: {suffix}. Allowed: {', '.join(sorted(ALLOWED_EXTS))}")

    fd, temp_audio_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    content = await file.read()
    with open(temp_audio_path, "wb") as f:
        f.write(content)

    try:
        asr_mdl = await build_default_bundle_async(tenant_id, LLMType.SPEECH2TEXT)
    except Exception as e:
        return get_error_data_result(retmsg=str(e))

    if not stream_mode:
        # ASR 同步 HTTP 不持有 Session，工作线程执行
        text = await asyncio.to_thread(asr_mdl.transcription, temp_audio_path)
        try:
            os.remove(temp_audio_path)
        except Exception as e:
            logging.error(f"Failed to remove temp audio file: {e!s}")
        return get_json_result(data={"text": text})

    async def event_stream():
        try:
            # 同步 ASR 流逐项经线程池拉取
            async for evt in iterate_in_threadpool(asr_mdl.stream_transcription(temp_audio_path)):
                yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
        except Exception as e:
            err = {"event": "error", "text": str(e)}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n"
        finally:
            try:
                os.remove(temp_audio_path)
            except Exception as e:
                logging.error(f"Failed to remove temp audio file: {e!s}")

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.post("/tts", summary="文字转语音")
def tts(
    body: TTSRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required),
):
    text = body.text

    try:
        default_tts_model_config = get_tenant_default_model_by_type(db, tenant_id, LLMType.TTS)
    except Exception as e:
        return get_error_data_result(retmsg=str(e))
    tts_mdl = LLMBundle(db, tenant_id, default_tts_model_config)

    def stream_audio():
        try:
            for txt in re.split(r"[，。/《》？；：！\n\r:;]+", text):
                yield from tts_mdl.tts(txt)
        except Exception as e:
            yield ("data:" + json.dumps({"code": 500, "message": str(e), "data": {"answer": "**ERROR**: " + str(e)}}, ensure_ascii=False)).encode("utf-8")

    resp = StreamingResponse(stream_audio(), media_type="audio/mpeg")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Connection"] = "keep-alive"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp
