import copy
import json
import logging
import os
import re
import tempfile
import time
from io import BytesIO
from typing import Any

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from api.db.db_models import get_db
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name, get_tenant_default_model_by_type
from api.db.services.api_service import API4ConversationService
from api.db.services.canvas_service import UserCanvasService, completion_openai
from api.db.services.canvas_service import completion as agent_completion
from api.db.services.conversation_service import ConversationService, iframe_completion
from api.db.services.conversation_service import completion as rag_completion
from api.db.services.dialog_service import DialogService, async_ask, async_chat, gen_mindmap
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.db.services.search_service import SearchService
from api.db.services.user_canvas_version import UserCanvasVersionService
from api.db.services.user_service import UserTenantService
from api.utils.api_utils import beta_token_required, check_duplicate_ids, get_data_openai, get_error_data_result, get_json_result, get_result, server_error_response, token_required
from api.utils.web_utils import CONTENT_TYPE_MAP, apply_safe_file_response_headers
from common import settings
from common.constants import LLMType, RetCode, StatusEnum
from common.metadata_utils import apply_meta_data_filter, convert_conditions, meta_filter
from common.misc_utils import get_uuid, thread_pool_exec
from common.token_utils import num_tokens_from_string
from core.app.tag import label_question
from core.prompts.generator import chunks_format, cross_languages, keyword_extraction
from core.prompts.template import load_prompt

router = APIRouter()


class DeleteSessionsRequest(BaseModel):
    ids: list[str] | None = None
    delete_all: bool = False


class ChatCompletionRequest(BaseModel):
    question: str | None = ""
    session_id: str | None = None
    stream: bool | None = True
    metadata_condition: dict[str, Any] | None = None


class ChatCompletionOpenAIRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    stream: bool | None = True
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
    question: str
    dataset_ids: list[str]


class RelatedQuestionsRequest(BaseModel):
    question: str
    industry: str | None = ""


class ChatbotCompletionRequest(BaseModel):
    question: str | None = ""
    stream: bool | None = True
    quote: bool | None = False


class SearchBotAskRequest(BaseModel):
    question: str
    kb_ids: list[str]
    search_id: str | None = ""


class SearchBotRetrievalTestRequest(BaseModel):
    kb_id: str | list[str]
    question: str
    page: int | None = 1
    size: int | None = 30
    similarity_threshold: float | None = 0.0
    vector_similarity_weight: float | None = 0.3
    use_kg: bool | None = False
    top_k: int | None = 1024
    cross_languages: list[str] | None = []
    search_id: str | None = ""
    doc_ids: list[str] | None = []
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


class CreateAgentSessionRequest(BaseModel):
    user_id: str | None = None
    release: bool = False


@router.post("/agents/{agent_id}/sessions", summary="创建代理会话")
def create_agent_session(agent_id: str, request_body: CreateAgentSessionRequest = None, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    user_id = (request_body.user_id if request_body and request_body.user_id else None) or tenant_id
    release_mode = request_body.release if request_body else False

    if not UserCanvasService.query(db, user_id=tenant_id, id=agent_id):
        return get_error_data_result(retmsg="You cannot access the agent.")

    try:
        cvs, dsl = UserCanvasService.get_agent_dsl_with_release(
            db,
            agent_id,
            release_mode=release_mode,
            tenant_id=tenant_id,
        )
    except LookupError:
        return get_error_data_result(retmsg="Agent not found.")
    except PermissionError as e:
        return get_error_data_result(retmsg=str(e))

    session_id = get_uuid()
    canvas = Canvas(dsl, tenant_id, agent_id, canvas_id=cvs.id)
    canvas.reset()

    cvs.dsl = json.loads(str(canvas))
    # 记录建会话时 canvas 的版本标题（按 release_mode 取已发布/最新版本）
    version_title = UserCanvasVersionService.get_latest_version_title(db, cvs.id, release_mode=release_mode)
    conv = {
        "id": session_id,
        "dialog_id": cvs.id,
        "user_id": user_id,
        "message": [{"role": "assistant", "content": canvas.get_prologue()}],
        "source": "agent",
        "dsl": cvs.dsl,
        "version_title": version_title,
    }
    API4ConversationService.save(db, **conv)
    conv["agent_id"] = conv.pop("dialog_id")
    return get_result(data=conv)


@router.post("/chats/{chat_id}/completions", summary="聊天补全")
async def chat_completion(chat_id: str, request: ChatCompletionRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    req = request.model_dump()
    if not req:
        req = {"question": ""}
    if not req.get("session_id"):
        req["question"] = ""

    dia = DialogService.query(db, tenant_id=tenant_id, id=chat_id, status=StatusEnum.VALID.value)
    if not dia:
        return get_error_data_result(retmsg=f"You don't own the chat {chat_id}")
    dia = dia[0]
    if req.get("session_id"):
        if not ConversationService.query(db, id=req["session_id"], dialog_id=chat_id):
            return get_error_data_result(retmsg=f"You don't own the session {req['session_id']}")

    metadata_condition = req.get("metadata_condition") or {}
    if metadata_condition and not isinstance(metadata_condition, dict):
        return get_error_data_result(retmsg="metadata_condition must be an object.")

    if metadata_condition and req.get("question"):
        metas = DocMetadataService.get_flatted_meta_by_kbs(db, dia.kb_ids or [])
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
        resp = StreamingResponse(rag_completion(db, tenant_id, chat_id, **req), media_type="text/event-stream")
        resp.headers["Cache-control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        return resp
    else:
        answer = None
        async for ans in rag_completion(db, tenant_id, chat_id, **req):
            answer = ans
            break
        return get_result(data=answer)


@router.post("/chats_openai/{chat_id}/chat/completions", summary="OpenAI兼容的聊天补全")
async def chat_completion_openai_like(chat_id: str, request: ChatCompletionOpenAIRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    """
    OpenAI-like chat completion API that simulates the behavior of OpenAI's completions endpoint.

    This function allows users to interact with a model and receive responses based on a series of historical messages.
    If `stream` is set to True (by default), the response will be streamed in chunks, mimicking the OpenAI-style API.
    Set `stream` to False explicitly, the response will be returned in a single complete answer.

    Reference:

    - If `stream` is True, the final answer and reference information will appear in the **last chunk** of the stream.
    - If `stream` is False, the reference will be included in `choices[0].message.reference`.
    - If `extra_body.reference_metadata.include` is True, each reference chunk may include `document_metadata` in both streaming and non-streaming responses.

    Example usage:

    curl -X POST https://ragflow_address.com/api/v1/chats_openai/<chat_id>/chat/completions \
        -H "Content-Type: application/json" \
        -H "Authorization: Bearer $RAGFLOW_API_KEY" \
        -d '{
            "model": "model",
            "messages": [{"role": "user", "content": "Say this is a test!"}],
            "stream": true
        }'

    Alternatively, you can use Python's `OpenAI` client:

    NOTE: Streaming via `client.chat.completions.create(stream=True, ...)` does
    not return `reference` currently. The only way to return `reference` is
    non-stream mode with `with_raw_response`.

    from openai import OpenAI
    import json

    model = "model"
    client = OpenAI(api_key="ragflow-api-key", base_url=f"http://ragflow_address/api/v1/chats_openai/<chat_id>")

    stream = True
    reference = True

    request_kwargs = dict(
        model="model",
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Who are you?"},
            {"role": "assistant", "content": "I am an AI assistant named..."},
            {"role": "user", "content": "Can you tell me how to install neovim"},
        ],
        extra_body={
            "reference": reference,
            "reference_metadata": {
                "include": True,
                "fields": ["author", "year", "source"],
            },
        },
    )

    if stream:
        completion = client.chat.completions.create(stream=True, **request_kwargs)
        for chunk in completion:
            print(chunk)
    else:
        resp = client.chat.completions.with_raw_response.create(
            stream=False, **request_kwargs
        )
        print("status:", resp.http_response.status_code)
        raw_text = resp.http_response.text
        print("raw:", raw_text)

        data = json.loads(raw_text)
        print("assistant:", data["choices"][0]["message"].get("content"))
        print("reference:", data["choices"][0]["message"].get("reference"))
    """
    req = request.model_dump()

    extra_body = req.get("extra_body") or {}
    if extra_body and not isinstance(extra_body, dict):
        return get_error_data_result(retmsg="extra_body must be an object.")

    need_reference = bool(extra_body.get("reference", False))
    reference_metadata = extra_body.get("reference_metadata") or {}
    if reference_metadata and not isinstance(reference_metadata, dict):
        return get_error_data_result(retmsg="reference_metadata must be an object.")
    include_reference_metadata = bool(reference_metadata.get("include", False))
    metadata_fields = reference_metadata.get("fields")
    if metadata_fields is not None and not isinstance(metadata_fields, list):
        return get_error_data_result(retmsg="reference_metadata.fields must be an array.")

    messages = req.get("messages", [])
    # To prevent empty [] input
    if len(messages) < 1:
        return get_error_data_result(retmsg="You have to provide messages.")
    if messages[-1]["role"] != "user":
        return get_error_data_result(retmsg="The last content of this conversation is not from user.")

    prompt = messages[-1]["content"]
    # Treat context tokens as reasoning tokens
    context_token_used = sum(num_tokens_from_string(message["content"]) for message in messages)

    dia = DialogService.query(db, tenant_id=tenant_id, id=chat_id, status=StatusEnum.VALID.value)
    if not dia:
        return get_error_data_result(retmsg=f"You don't own the chat {chat_id}")
    dia = dia[0]

    metadata_condition = extra_body.get("metadata_condition") or {}
    if metadata_condition and not isinstance(metadata_condition, dict):
        return get_error_data_result(retmsg="metadata_condition must be an object.")

    doc_ids_str = None
    if metadata_condition:
        metas = DocMetadataService.get_flatted_meta_by_kbs(db, dia.kb_ids or [])
        filtered_doc_ids = meta_filter(
            metas,
            convert_conditions(metadata_condition),
            metadata_condition.get("logic", "and"),
        )
        if metadata_condition.get("conditions") and not filtered_doc_ids:
            filtered_doc_ids = ["-999"]
        doc_ids_str = ",".join(filtered_doc_ids) if filtered_doc_ids else None

    # Filter system and non-sense assistant messages
    msg = []
    for m in messages:
        if m["role"] == "system":
            continue
        if m["role"] == "assistant" and not msg:
            continue
        msg.append(m)

    # tools = get_tools()
    # toolcall_session = SimpleFunctionCallServer()
    tools = None
    toolcall_session = None

    if req.get("stream", True):
        # The value for the usage field on all chunks except for the last one will be null.
        # The usage field on the last chunk contains token usage statistics for the entire request.
        # The choices field on the last chunk will always be an empty array [].
        async def streamed_response_generator(chat_id, dia, msg):
            token_used = 0
            last_ans = {}
            full_content = ""
            full_reasoning = ""
            final_answer = None
            final_reference = None
            in_think = False
            response = {
                "id": f"chatcmpl-{chat_id}",
                "choices": [
                    {
                        "delta": {
                            "content": "",
                            "role": "assistant",
                            "function_call": None,
                            "tool_calls": None,
                            "reasoning_content": "",
                        },
                        "finish_reason": None,
                        "index": 0,
                        "logprobs": None,
                    }
                ],
                "created": int(time.time()),
                "model": "model",
                "object": "chat.completion.chunk",
                "system_fingerprint": "",
                "usage": None,
            }

            try:
                chat_kwargs = {"toolcall_session": toolcall_session, "tools": tools, "quote": need_reference}
                if doc_ids_str:
                    chat_kwargs["doc_ids"] = doc_ids_str
                async for ans in async_chat(dia, msg, db, True, **chat_kwargs):
                    last_ans = ans
                    if ans.get("final"):
                        if ans.get("answer"):
                            full_content = ans["answer"]
                            response["choices"][0]["delta"]["content"] = full_content
                            response["choices"][0]["delta"]["reasoning_content"] = None
                            yield f"data:{json.dumps(response, ensure_ascii=False)}\n\n"
                        final_answer = full_content
                        final_reference = ans.get("reference", {})
                        continue
                    if ans.get("start_to_think"):
                        in_think = True
                        continue
                    if ans.get("end_to_think"):
                        in_think = False
                        continue
                    delta = ans.get("answer") or ""
                    if not delta:
                        continue
                    token_used += num_tokens_from_string(delta)
                    if in_think:
                        full_reasoning += delta
                        response["choices"][0]["delta"]["reasoning_content"] = delta
                        response["choices"][0]["delta"]["content"] = None
                    else:
                        full_content += delta
                        response["choices"][0]["delta"]["content"] = delta
                        response["choices"][0]["delta"]["reasoning_content"] = None
                    yield f"data:{json.dumps(response, ensure_ascii=False)}\n\n"
            except Exception as e:
                response["choices"][0]["delta"]["content"] = "**ERROR**: " + str(e)
                yield f"data:{json.dumps(response, ensure_ascii=False)}\n\n"

            # The last chunk
            response["choices"][0]["delta"]["content"] = None
            response["choices"][0]["delta"]["reasoning_content"] = None
            response["choices"][0]["finish_reason"] = "stop"
            prompt_tokens = num_tokens_from_string(prompt)
            response["usage"] = {"prompt_tokens": prompt_tokens, "completion_tokens": token_used, "total_tokens": prompt_tokens + token_used}
            if need_reference:
                reference_payload = final_reference if final_reference is not None else last_ans.get("reference", [])
                response["choices"][0]["delta"]["reference"] = _build_reference_chunks(
                    reference_payload,
                    include_metadata=include_reference_metadata,
                    metadata_fields=metadata_fields,
                )
                response["choices"][0]["delta"]["final_content"] = final_answer if final_answer is not None else full_content
            yield f"data:{json.dumps(response, ensure_ascii=False)}\n\n"
            yield "data:[DONE]\n\n"

        resp = StreamingResponse(streamed_response_generator(chat_id, dia, msg), media_type="text/event-stream")
        resp.headers["Cache-control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        return resp
    else:
        answer = None
        chat_kwargs = {"toolcall_session": toolcall_session, "tools": tools, "quote": need_reference}
        if doc_ids_str:
            chat_kwargs["doc_ids"] = doc_ids_str
        async for ans in async_chat(dia, msg, db, False, **chat_kwargs):
            # focus answer content only
            answer = ans
            break
        content = answer["answer"]

        response = {
            "id": f"chatcmpl-{chat_id}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.get("model", ""),
            "usage": {
                "prompt_tokens": num_tokens_from_string(prompt),
                "completion_tokens": num_tokens_from_string(content),
                "total_tokens": num_tokens_from_string(prompt) + num_tokens_from_string(content),
                "completion_tokens_details": {
                    "reasoning_tokens": context_token_used,
                    "accepted_prediction_tokens": num_tokens_from_string(content),
                    "rejected_prediction_tokens": 0,  # 0 for simplicity
                },
            },
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "logprobs": None,
                    "finish_reason": "stop",
                    "index": 0,
                }
            ],
        }
        if need_reference:
            response["choices"][0]["message"]["reference"] = _build_reference_chunks(
                answer.get("reference", {}),
                include_metadata=include_reference_metadata,
                metadata_fields=metadata_fields,
            )

        return response


@router.post("/agents_openai/{agent_id}/chat/completions", summary="代理OpenAI兼容补全")
async def agents_completion_openai_compatibility(agent_id: str, request: ChatCompletionOpenAIRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    req = request.model_dump()
    messages = req.get("messages", [])
    if not messages:
        return get_error_data_result(retmsg="You must provide at least one message.")
    if not UserCanvasService.query(db, user_id=tenant_id, id=agent_id):
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


@router.post("/agents/{agent_id}/completions", summary="代理补全")
async def agent_completions(agent_id: str, request: AgentCompletionRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
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


@router.get("/agents/{agent_id}/sessions", summary="获取代理会话列表")
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


@router.delete("/agents/{agent_id}/sessions", summary="批量删除代理会话")
def delete_agent_sessions(agent_id: str, request: DeleteSessionsRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    errors = []
    success_count = 0
    req = request.model_dump()
    cvs = UserCanvasService.query(db, user_id=tenant_id, id=agent_id)
    if not cvs:
        return get_error_data_result(retmsg=f"You don't own the agent {agent_id}")

    ids = req.get("ids")
    if not ids:
        if req.get("delete_all") is True:
            ids = [conv.id for conv in API4ConversationService.query(db, dialog_id=agent_id)]
            if not ids:
                return get_result()
        else:
            return get_result()

    conv_list = ids

    unique_conv_ids, duplicate_messages = check_duplicate_ids(conv_list, "session")
    conv_list = unique_conv_ids

    for session_id in conv_list:
        conv = API4ConversationService.query(db, id=session_id, dialog_id=agent_id)
        if not conv:
            errors.append(f"The agent doesn't own the session {session_id}")
            continue
        API4ConversationService.delete_by_id(db, session_id)
        success_count += 1

    if errors:
        if success_count > 0:
            return get_result(data={"success_count": success_count, "errors": errors}, retmsg=f"Partially deleted {success_count} sessions with {len(errors)} errors")
        else:
            return get_error_data_result(retmsg="; ".join(errors))

    if duplicate_messages:
        if success_count > 0:
            return get_result(retmsg=f"Partially deleted {success_count} sessions with {len(duplicate_messages)} errors", data={"success_count": success_count, "errors": duplicate_messages})
        else:
            return get_error_data_result(retmsg=";".join(duplicate_messages))

    return get_result()


@router.post("/sessions/ask", summary="询问知识库")
async def ask_about(request: AskRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    req = request.model_dump()
    if not req.get("question"):
        return get_error_data_result(retmsg="`question` is required.")
    if not req.get("dataset_ids"):
        return get_error_data_result(retmsg="`dataset_ids` is required.")
    if not isinstance(req.get("dataset_ids"), list):
        return get_error_data_result(retmsg="`dataset_ids` should be a list.")

    req["kb_ids"] = req.pop("dataset_ids")
    for kb_id in req["kb_ids"]:
        if not KnowledgebaseService.accessible(db, kb_id, tenant_id):
            return get_error_data_result(retmsg=f"You don't own the dataset {kb_id}.")
        kbs = KnowledgebaseService.query(db, id=kb_id)
        kb = kbs[0]
        if kb.chunk_num == 0:
            return get_error_data_result(retmsg=f"The dataset {kb_id} doesn't own parsed file")

    uid = tenant_id

    async def stream():
        nonlocal req, uid, db
        try:
            async for ans in async_ask(db, req["question"], req["kb_ids"], uid):
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
async def related_questions(request: RelatedQuestionsRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    req = request.model_dump()
    if not req.get("question"):
        return get_error_data_result(retmsg="`question` is required.")

    question = req["question"]
    industry = req.get("industry", "")
    chat_config = get_tenant_default_model_by_type(db, tenant_id, LLMType.CHAT)
    chat_mdl = LLMBundle(db, tenant_id, chat_config)
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
    db: Session = Depends(get_db),
    _: str = Depends(beta_token_required),
):
    req = body.model_dump()

    if "quote" not in req:
        req["quote"] = False

    if req.get("stream", True):
        resp = StreamingResponse(iframe_completion(db, dialog_id, **req), media_type="text/event-stream")
        resp.headers["Cache-control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        return resp

    async for answer in iframe_completion(db, dialog_id, **req):
        return get_result(data=answer)


@router.get("/chatbots/{dialog_id}/info", summary="获取聊天机器人信息")
def chatbots_inputs(
    dialog_id: str,
    db: Session = Depends(get_db),
    _: str = Depends(beta_token_required),
):
    dialog = DialogService.get_by_id(db, dialog_id)
    if not dialog:
        return get_error_data_result(retmsg=f"Can't find dialog by ID: {dialog_id}")

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
    db: Session = Depends(get_db),
    tenant_id: str = Depends(beta_token_required),
):
    req = body.model_dump()
    if release is not None and req.get("release") is None:
        req["release"] = release

    if req.get("stream", True):

        async def stream():
            try:
                async for answer in agent_completion(db=db, tenant_id=tenant_id, agent_id=agent_id, **req):
                    yield answer
            except Exception as e:
                logging.exception(e)
                yield build_sse_error_payload(e)

        resp = StreamingResponse(stream(), media_type="text/event-stream")
        resp.headers["Cache-control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        return resp

    try:
        async for answer in agent_completion(db=db, tenant_id=tenant_id, agent_id=agent_id, **req):
            return get_result(data=answer)
    except Exception as e:
        logging.exception(e)
        return get_error_data_result(retmsg=str(e) or "Unknown error")


@router.get("/agentbots/{agent_id}/inputs", summary="获取代理机器人输入表单")
def begin_inputs(
    agent_id: str,
    release: str | None = Query(None),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(beta_token_required),
):
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
):
    """
    公开 (share/widget beta-token) 入口，按 agent_id (即 datav workflow_id) 读取
    inputPlugin persondataList。与登录态 `/v1/datav/persondataList/{id}` 同源数据，
    供 Begin 节点 persondata 输入类型在分享页拉取候选项。
    """
    cvs = UserCanvasService.get_by_id(db, agent_id)
    if not cvs:
        return get_error_data_result(retmsg=f"Can't find agent by ID: {agent_id}")

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
    db: Session = Depends(get_db),
    tenant_id: str = Depends(beta_token_required),
):
    """
    Public agentbot artifact download for share/widget beta-token flows.

    This is intentionally separate from generic document/file download endpoints
    so embedded agents do not expand the internal document or SDK file auth
    boundary.
    """
    cvs = UserCanvasService.get_by_id(db, agent_id)
    if not cvs:
        return get_error_data_result(retmsg=f"Can't find agent by ID: {agent_id}")
    if cvs.user_id != tenant_id:
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
    db: Session = Depends(get_db),
    tenant_id: str = Depends(beta_token_required),
):
    req = body.model_dump()
    uid = tenant_id

    search_id = req.get("search_id", "")
    search_config = {}
    if search_id:
        if search_app := SearchService.get_detail(db, search_id):
            search_config = search_app.get("search_config", {})

    async def stream():
        nonlocal req, uid, db
        try:
            async for ans in async_ask(db, req["question"], req["kb_ids"], uid, search_config=search_config):
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
    db: Session = Depends(get_db),
    tenant_id: str = Depends(beta_token_required),
):
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

    if req.get("search_id", ""):
        search_config = SearchService.get_detail(db, req.get("search_id", "")).get("search_config", {})
        meta_data_filter = search_config.get("meta_data_filter", {})
        if meta_data_filter:
            metas = DocMetadataService.get_flatted_meta_by_kbs(db, kb_ids)
            chat_mdl = None
            if meta_data_filter.get("method") in ["auto", "semi_auto"]:
                chat_config = get_model_config_by_type_and_name(db, tenant_id, LLMType.CHAT.value, search_config.get("chat_id", ""))
                chat_mdl = LLMBundle(db, tenant_id, chat_config)
            doc_ids = await apply_meta_data_filter(meta_data_filter, metas, question, chat_mdl, doc_ids)
        # Apply search_config settings if not explicitly provided in request
        if not req.get("similarity_threshold"):
            similarity_threshold = float(search_config.get("similarity_threshold", similarity_threshold))
        if not req.get("vector_similarity_weight"):
            vector_similarity_weight = float(search_config.get("vector_similarity_weight", vector_similarity_weight))
        if not req.get("top_k"):
            top = int(search_config.get("top_k", top))
        if not req.get("rerank_id"):
            rerank_id = search_config.get("rerank_id", "")

    try:
        tenants = UserTenantService.query(db, user_id=tenant_id)
        for kb_id in kb_ids:
            for tenant in tenants:
                if KnowledgebaseService.query(db, tenant_id=tenant.tenant_id, id=kb_id):
                    tenant_ids.append(tenant.tenant_id)
                    break
            else:
                return get_json_result(data=False, retmsg="Only owner of dataset authorized for this operation.", retcode=RetCode.OPERATING_ERROR)

        kb = KnowledgebaseService.get_by_id(db, kb_ids[0])
        if not kb:
            return get_error_data_result(retmsg="Knowledgebase not found!")

        if langs:
            question = await cross_languages(kb.tenant_id, None, question, langs)

        if kb.tenant_embd_id:
            embd_config = get_model_config_by_id(db, kb.tenant_embd_id)
        else:
            embd_config = get_model_config_by_type_and_name(db, kb.tenant_id, LLMType.EMBEDDING.value, kb.embd_id)
        embd_mdl = LLMBundle(db, kb.tenant_id, embd_config)

        rerank_mdl = None
        if req.get("tenant_rerank_id"):
            rerank_config = get_model_config_by_id(db, req["tenant_rerank_id"])
            rerank_mdl = LLMBundle(db, kb.tenant_id, rerank_config)
        elif rerank_id:
            rerank_config = get_model_config_by_type_and_name(db, kb.tenant_id, LLMType.RERANK.value, rerank_id)
            rerank_mdl = LLMBundle(db, kb.tenant_id, rerank_config)

        if req.get("keyword", False):
            chat_config = get_tenant_default_model_by_type(db, kb.tenant_id, LLMType.CHAT)
            chat_mdl = LLMBundle(db, kb.tenant_id, chat_config)
            question += await keyword_extraction(chat_mdl, question)

        labels = label_question(db, question, [kb])
        ranks = await settings.retriever.retrieval(
            question, embd_mdl, tenant_ids, kb_ids, page, size, similarity_threshold, vector_similarity_weight, top, doc_ids, rerank_mdl=rerank_mdl, highlight=req.get("highlight"), rank_feature=labels
        )
        if use_kg:
            kg_chat_config = get_tenant_default_model_by_type(db, kb.tenant_id, LLMType.CHAT)
            ck = await settings.kg_retriever.retrieval(question, tenant_ids, kb_ids, embd_mdl, LLMBundle(db, kb.tenant_id, kg_chat_config))
            if ck["content_with_weight"]:
                ranks["chunks"].insert(0, ck)

        for c in ranks["chunks"]:
            c.pop("vector", None)
        ranks["labels"] = labels

        return get_json_result(data=ranks)
    except Exception as e:
        if str(e).find("not_found") > 0:
            return get_json_result(data=False, retmsg="No chunk found! Check the chunk status please!", retcode=RetCode.DATA_ERROR)
        return server_error_response(e)


@router.post("/searchbots/related_questions", summary="搜索机器人相关问题")
async def related_questions_embedded(
    body: SearchBotRelatedQuestionsRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(beta_token_required),
):
    req = body.model_dump()

    search_id = req.get("search_id", "")
    search_config = {}
    if search_id:
        if search_app := SearchService.get_detail(db, search_id):
            search_config = search_app.get("search_config", {})

    question = req["question"]

    chat_id = search_config.get("chat_id", "")
    if chat_id:
        chat_config = get_model_config_by_type_and_name(db, tenant_id, LLMType.CHAT.value, chat_id)
    else:
        chat_config = get_tenant_default_model_by_type(db, tenant_id, LLMType.CHAT)
    chat_mdl = LLMBundle(db, tenant_id, chat_config)

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
):
    try:
        tenants = UserTenantService.query(db, user_id=tenant_id)
        for tenant in tenants:
            if SearchService.query(db, tenant_id=tenant.tenant_id, id=search_id):
                break
        else:
            return get_json_result(data=False, retmsg="Has no permission for this operation.", retcode=RetCode.OPERATING_ERROR)

        search = SearchService.get_detail(db, search_id)
        if not search:
            return get_error_data_result(retmsg="Can't find this Search App!")
        return get_json_result(data=search)
    except Exception as e:
        return server_error_response(e)


@router.post("/searchbots/mindmap", summary="生成搜索机器人思维导图")
async def mindmap(
    body: SearchBotMindmapRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(beta_token_required),
):
    req = body.model_dump()

    search_id = req.get("search_id", "")
    search_app = SearchService.get_detail(db, search_id) if search_id else {}

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
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required),
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
        default_asr_model_config = get_tenant_default_model_by_type(db, tenant_id, LLMType.SPEECH2TEXT)
    except Exception as e:
        return get_error_data_result(retmsg=str(e))
    asr_mdl = LLMBundle(db, tenant_id, default_asr_model_config)

    if not stream_mode:
        text = asr_mdl.transcription(temp_audio_path)
        try:
            os.remove(temp_audio_path)
        except Exception as e:
            logging.error(f"Failed to remove temp audio file: {e!s}")
        return get_json_result(data={"text": text})

    async def event_stream():
        try:
            for evt in asr_mdl.stream_transcription(temp_audio_path):
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


def _build_reference_chunks(reference, include_metadata=False, metadata_fields=None):
    chunks = chunks_format(reference)
    if not include_metadata:
        return chunks

    doc_ids_by_kb = {}
    for chunk in chunks:
        kb_id = chunk.get("dataset_id")
        doc_id = chunk.get("document_id")
        if not kb_id or not doc_id:
            continue
        doc_ids_by_kb.setdefault(kb_id, set()).add(doc_id)

    if not doc_ids_by_kb:
        return chunks

    meta_by_doc = {}
    for kb_id, doc_ids in doc_ids_by_kb.items():
        meta_map = DocMetadataService.get_metadata_for_documents(list(doc_ids), kb_id)
        if meta_map:
            meta_by_doc.update(meta_map)

    if metadata_fields is not None:
        metadata_fields = {f for f in metadata_fields if isinstance(f, str)}
        if not metadata_fields:
            return chunks

    for chunk in chunks:
        doc_id = chunk.get("document_id")
        if not doc_id:
            continue
        meta = meta_by_doc.get(doc_id)
        if not meta:
            continue
        if metadata_fields is not None:
            meta = {k: v for k, v in meta.items() if k in metadata_fields}
        if meta:
            chunk["document_metadata"] = meta

    return chunks
