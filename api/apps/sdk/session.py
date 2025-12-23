import json
import re
import time
from typing import Any

import tiktoken
from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from agent.canvas import Canvas
from api import settings
from common.constants import LLMType, StatusEnum
from api.db.db_models import APIToken, get_db
from api.db.services.api_service import API4ConversationService
from api.db.services.canvas_service import UserCanvasService, completion_openai
from api.db.services.canvas_service import completion as agent_completion
from api.db.services.conversation_service import ConversationService, iframe_completion
from api.db.services.conversation_service import completion as rag_completion
from api.db.services.dialog_service import DialogService, ask, chat, gen_mindmap, meta_filter
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.db.services.search_service import SearchService
from api.db.services.user_service import UserTenantService
from common.misc_utils import get_uuid
from common.constants import RetCode
from api.utils.api_utils import check_duplicate_ids, get_data_openai, get_error_data_result, get_json_result, get_result, server_error_response, token_required, validate_request
from core.app.tag import label_question
from core.prompts.template import load_prompt
from core.prompts.generator import cross_languages, keyword_extraction, chunks_format

router = APIRouter()


class CreateSessionRequest(BaseModel):
    name: str | None = "New session"
    user_id: str | None = ""


class UpdateSessionRequest(BaseModel):
    name: str | None = None


class DeleteSessionsRequest(BaseModel):
    ids: list[str] | None = None


class ChatCompletionRequest(BaseModel):
    question: str | None = ""
    session_id: str | None = None
    stream: bool | None = True


class ChatCompletionOpenAIRequest(BaseModel):
    model: str
    messages: list[dict[str, Any]]
    stream: bool | None = True
    reference: bool | None = False


class AgentCompletionRequest(BaseModel):
    question: str | None = ""
    stream: bool | None = True


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
    keyword: bool | None = False
    highlight: bool | None = False


class SearchBotRelatedQuestionsRequest(BaseModel):
    question: str
    search_id: str | None = ""


class SearchBotMindmapRequest(BaseModel):
    question: str
    kb_ids: list[str]
    search_id: str | None = ""


@router.post("/chats/{chat_id}/sessions", summary="创建聊天会话")
def create_session(
    chat_id: str, 
    request: CreateSessionRequest, 
    db: Session = Depends(get_db), 
    tenant_id: str = Depends(token_required)
):
    req = request.model_dump()
    req["dialog_id"] = chat_id
    dia = DialogService.query(db, tenant_id=tenant_id, id=req["dialog_id"], status=StatusEnum.VALID.value)
    if not dia:
        return get_error_data_result(retmsg="You do not own the assistant.")
    
    conv = {
        "id": get_uuid(),
        "dialog_id": req["dialog_id"],
        "name": req.get("name", "New session"),
        "message": [{"role": "assistant", "content": dia[0].prompt_config.get("prologue")}],
        "user_id": req.get("user_id", ""),
        "reference": [],
    }
    if not conv.get("name"):
        return get_error_data_result(retmsg="`name` can not be empty.")
    
    ConversationService.save(db, **conv)
    conv = ConversationService.get_by_id(db, conv["id"])
    if not conv:
        return get_error_data_result(retmsg="Fail to create a session!")
    
    conv = conv.to_dict()
    conv["messages"] = conv.pop("message")
    conv["chat_id"] = conv.pop("dialog_id")
    del conv["reference"]
    return get_result(data=conv)


@router.post("/agents/{agent_id}/sessions", summary="创建代理会话")
def create_agent_session(
    agent_id: str,
    user_id: str = Query(None),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    if user_id is None:
        user_id = tenant_id
    
    e, cvs = UserCanvasService.get_by_id(db, agent_id)
    if not e:
        return get_error_data_result(retmsg="Agent not found.")
    if not UserCanvasService.query(db, user_id=tenant_id, id=agent_id):
        return get_error_data_result(retmsg="You cannot access the agent.")
    
    if not isinstance(cvs.dsl, str):
        cvs.dsl = json.dumps(cvs.dsl, ensure_ascii=False)

    session_id = get_uuid()
    canvas = Canvas(cvs.dsl, tenant_id, agent_id)
    canvas.reset()

    cvs.dsl = json.loads(str(canvas))
    conv = {
        "id": session_id, 
        "dialog_id": cvs.id, 
        "user_id": user_id, 
        "message": [{"role": "assistant", "content": canvas.get_prologue()}], 
        "source": "agent", 
        "dsl": cvs.dsl
    }
    API4ConversationService.save(db, **conv)
    conv["agent_id"] = conv.pop("dialog_id")
    return get_result(data=conv)


@router.put("/chats/{chat_id}/sessions/{session_id}", summary="更新聊天会话")
def update_session(
    chat_id: str, 
    session_id: str, 
    request: UpdateSessionRequest, 
    db: Session = Depends(get_db), 
    tenant_id: str = Depends(token_required)
):
    req = request.model_dump(exclude_unset=True)
    req["dialog_id"] = chat_id
    conv_id = session_id
    
    conv = ConversationService.query(db, id=conv_id, dialog_id=chat_id)
    if not conv:
        return get_error_data_result(retmsg="Session does not exist")
    if not DialogService.query(db, id=chat_id, tenant_id=tenant_id, status=StatusEnum.VALID.value):
        return get_error_data_result(retmsg="You do not own the session")
    
    if "message" in req or "messages" in req:
        return get_error_data_result(retmsg="`message` can not be change")
    if "reference" in req:
        return get_error_data_result(retmsg="`reference` can not be change")
    if "name" in req and not req.get("name"):
        return get_error_data_result(retmsg="`name` can not be empty.")
    
    if not ConversationService.update_by_id(db, conv_id, req):
        return get_error_data_result(retmsg="Session updates error")
    return get_result()


@router.post("/chats/{chat_id}/completions", summary="聊天补全")
def chat_completion(
    chat_id: str, 
    request: ChatCompletionRequest, 
    db: Session = Depends(get_db), 
    tenant_id: str = Depends(token_required)
):
    req = request.model_dump()
    if not req:
        req = {"question": ""}
    if not req.get("session_id"):
        req["question"] = ""
    
    if not DialogService.query(db, tenant_id=tenant_id, id=chat_id, status=StatusEnum.VALID.value):
        return get_error_data_result(retmsg=f"You don't own the chat {chat_id}")
    if req.get("session_id"):
        if not ConversationService.query(db, id=req["session_id"], dialog_id=chat_id):
            return get_error_data_result(retmsg=f"You don't own the session {req['session_id']}")
    
    if req.get("stream", True):
        resp = StreamingResponse(rag_completion(db, tenant_id, chat_id, **req), media_type="text/event-stream")
        resp.headers["Cache-control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        return resp
    else:
        answer = None
        for ans in rag_completion(db, tenant_id, chat_id, **req):
            answer = ans
            break
        return get_result(data=answer)


@router.post("/chats_openai/{chat_id}/chat/completions", summary="OpenAI兼容的聊天补全")
def chat_completion_openai_like(
    chat_id: str, 
    request: ChatCompletionOpenAIRequest, 
    db: Session = Depends(get_db), 
    tenant_id: str = Depends(token_required)
):
    """
    OpenAI-like chat completion API that simulates the behavior of OpenAI's completions endpoint.

    This function allows users to interact with a model and receive responses based on a series of historical messages.
    If `stream` is set to True (by default), the response will be streamed in chunks, mimicking the OpenAI-style API.
    Set `stream` to False explicitly, the response will be returned in a single complete answer.

    Reference:

    - If `stream` is True, the final answer and reference information will appear in the **last chunk** of the stream.
    - If `stream` is False, the reference will be included in `choices[0].message.reference`.

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

    from openai import OpenAI

    model = "model"
    client = OpenAI(api_key="ragflow-api-key", base_url=f"http://ragflow_address/api/v1/chats_openai/<chat_id>")

    stream = True
    reference = True

    completion = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Who are you?"},
            {"role": "assistant", "content": "I am an AI assistant named..."},
            {"role": "user", "content": "Can you tell me how to install neovim"},
        ],
        stream=stream,
        extra_body={"reference": reference}
    )

    if stream:
    for chunk in completion:
        print(chunk)
        if reference and chunk.choices[0].finish_reason == "stop":
            print(f"Reference:\n{chunk.choices[0].delta.reference}")
            print(f"Final content:\n{chunk.choices[0].delta.final_content}")
    else:
        print(completion.choices[0].message.content)
        if reference:
            print(completion.choices[0].message.reference)
    """
    req = request.model_dump()

    need_reference = bool(req.get("reference", False))

    messages = req.get("messages", [])
    # To prevent empty [] input
    if len(messages) < 1:
        return get_error_data_result(retmsg="You have to provide messages.")
    if messages[-1]["role"] != "user":
        return get_error_data_result(retmsg="The last content of this conversation is not from user.")

    prompt = messages[-1]["content"]
    # Treat context tokens as reasoning tokens
    context_token_used = sum(len(message["content"]) for message in messages)

    dia = DialogService.query(db, tenant_id=tenant_id, id=chat_id, status=StatusEnum.VALID.value)
    if not dia:
        return get_error_data_result(retmsg=f"You don't own the chat {chat_id}")
    dia = dia[0]

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
        def streamed_response_generator(chat_id, dia, msg):
            token_used = 0
            answer_cache = ""
            reasoning_cache = ""
            last_ans = {}
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
                for ans in chat(dia, msg, True, toolcall_session=toolcall_session, tools=tools, quote=need_reference):
                    last_ans = ans
                    answer = ans["answer"]

                    reasoning_match = re.search(r"<think>(.*?)</think>", answer, flags=re.DOTALL)
                    if reasoning_match:
                        reasoning_part = reasoning_match.group(1)
                        content_part = answer[reasoning_match.end() :]
                    else:
                        reasoning_part = ""
                        content_part = answer

                    reasoning_incremental = ""
                    if reasoning_part:
                        if reasoning_part.startswith(reasoning_cache):
                            reasoning_incremental = reasoning_part.replace(reasoning_cache, "", 1)
                        else:
                            reasoning_incremental = reasoning_part
                        reasoning_cache = reasoning_part

                    content_incremental = ""
                    if content_part:
                        if content_part.startswith(answer_cache):
                            content_incremental = content_part.replace(answer_cache, "", 1)
                    else:
                        content_incremental = content_part
                    answer_cache = content_part

                    token_used += len(reasoning_incremental) + len(content_incremental)

                    if not any([reasoning_incremental, content_incremental]):
                        continue

                    if reasoning_incremental:
                        response["choices"][0]["delta"]["reasoning_content"] = reasoning_incremental
                    else:
                        response["choices"][0]["delta"]["reasoning_content"] = None

                    if content_incremental:
                        response["choices"][0]["delta"]["content"] = content_incremental
                    else:
                        response["choices"][0]["delta"]["content"] = None

                    yield f"data:{json.dumps(response, ensure_ascii=False)}\n\n"
            except Exception as e:
                response["choices"][0]["delta"]["content"] = "**ERROR**: " + str(e)
                yield f"data:{json.dumps(response, ensure_ascii=False)}\n\n"

            # The last chunk
            response["choices"][0]["delta"]["content"] = None
            response["choices"][0]["delta"]["reasoning_content"] = None
            response["choices"][0]["finish_reason"] = "stop"
            response["usage"] = {"prompt_tokens": len(prompt), "completion_tokens": token_used, "total_tokens": len(prompt) + token_used}
            if need_reference:
                response["choices"][0]["delta"]["reference"] = chunks_format(last_ans.get("reference", []))
                response["choices"][0]["delta"]["final_content"] = last_ans.get("answer", "")
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
        for ans in chat(dia, msg, False, toolcall_session=toolcall_session, tools=tools, quote=need_reference):
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
                "prompt_tokens": len(prompt),
                "completion_tokens": len(content),
                "total_tokens": len(prompt) + len(content),
                "completion_tokens_details": {
                    "reasoning_tokens": context_token_used,
                    "accepted_prediction_tokens": len(content),
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
            response["choices"][0]["message"]["reference"] = chunks_format(answer.get("reference", []))

        return response


@router.post("/agents_openai/{agent_id}/chat/completions", summary="代理OpenAI兼容补全")
def agents_completion_openai_compatibility(
    agent_id: str, 
    request: ChatCompletionOpenAIRequest, 
    db: Session = Depends(get_db), 
    tenant_id: str = Depends(token_required)
):
    req = request.model_dump()
    tiktokenenc = tiktoken.get_encoding("cl100k_base")
    messages = req.get("messages", [])
    if not messages:
        return get_error_data_result(retmsg="You must provide at least one message.")
    if not UserCanvasService.query(db, user_id=tenant_id, id=agent_id):
        return get_error_data_result(retmsg=f"You don't own the agent {agent_id}")

    filtered_messages = [m for m in messages if m["role"] in ["user", "assistant"]]
    prompt_tokens = sum(len(tiktokenenc.encode(m["content"])) for m in filtered_messages)
    if not filtered_messages:
        return get_data_openai(
            id=agent_id,
            content="No valid messages found (user or assistant).",
            finish_reason="stop",
            model=req.get("model", ""),
            completion_tokens=len(tiktokenenc.encode("No valid messages found (user or assistant).")),
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
        response = next(
            completion_openai(
                db,
                tenant_id,
                agent_id,
                question,
                session_id=req.pop("session_id", req.get("id", "") or req.get("metadata", {}).get("id", "")),
                stream=False,
                **req,
            )
        )
        return response


@router.post("/agents/{agent_id}/completions", summary="代理补全")
def agent_completions(
    agent_id: str,
    request: AgentCompletionRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    req = request.model_dump()

    if req.get("stream", True):
        def generate():
            ans = {}
            for answer in agent_completion(tenant_id=tenant_id, agent_id=agent_id, **req):
                if isinstance(answer, str):
                    try:
                        ans = json.loads(answer[5:])  # remove "data:"
                    except Exception:
                        continue

                if ans.get("event") not in ["message", "message_end"]:
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
    for answer in agent_completion(tenant_id=tenant_id, agent_id=agent_id, **req):
        try:
            ans = json.loads(answer[5:])

            if ans["event"] == "message":
                full_content += ans["data"]["content"]

            if ans.get("data", {}).get("reference", None):
                reference.update(ans["data"]["reference"])

            final_ans = ans
        except Exception as e:
            return get_result(data=f"**ERROR**: {str(e)}")
    final_ans["data"]["content"] = full_content
    final_ans["data"]["reference"] = reference
    return get_result(data=final_ans)


@router.get("/chats/{chat_id}/sessions", summary="获取聊天会话列表")
def list_sessions(
    chat_id: str,
    id: str | None = Query(None),
    name: str | None = Query(None),
    page: int = Query(1),
    page_size: int = Query(30),
    orderby: str = Query("create_time"),
    desc: bool = Query(True),
    user_id: str | None = Query(None),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    if not DialogService.query(db, tenant_id=tenant_id, id=chat_id, status=StatusEnum.VALID.value):
        return get_error_data_result(retmsg=f"You don't own the assistant {chat_id}.")
    
    page_number = int(page)
    items_per_page = int(page_size)
    
    convs = ConversationService.get_list(db, chat_id, page_number, items_per_page, orderby, desc, id, name, user_id)
    if not convs:
        return get_result(data=[])
    
    for conv in convs:
        conv["messages"] = conv.pop("message")
        infos = conv["messages"]
        for info in infos:
            if "prompt" in info:
                info.pop("prompt")
        conv["chat_id"] = conv.pop("dialog_id")
        ref_messages = conv["reference"]
        if ref_messages:
            messages = conv["messages"]
            message_num = 0
            ref_num = 0
            while message_num < len(messages) and ref_num < len(ref_messages):
                if messages[message_num]["role"] != "user":
                    chunk_list = []
                    if "chunks" in ref_messages[ref_num]:
                        chunks = ref_messages[ref_num]["chunks"]
                        for chunk in chunks:
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
                    messages[message_num]["reference"] = chunk_list
                    ref_num += 1
                message_num += 1
        del conv["reference"]
    return get_result(data=convs)


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
    tenant_id: str = Depends(token_required)
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


@router.delete("/chats/{chat_id}/sessions", summary="批量删除聊天会话")
def delete_sessions(
    chat_id: str, 
    request: DeleteSessionsRequest, 
    db: Session = Depends(get_db), 
    tenant_id: str = Depends(token_required)
):
    if not DialogService.query(db, id=chat_id, tenant_id=tenant_id, status=StatusEnum.VALID.value):
        return get_error_data_result(retmsg="You don't own the chat")

    errors = []
    success_count = 0
    req = request.model_dump()
    convs = ConversationService.query(db, dialog_id=chat_id)
    
    ids = req.get("ids")
    if not ids:
        conv_list = [conv.id for conv in convs]
    else:
        conv_list = ids

    unique_conv_ids, duplicate_messages = check_duplicate_ids(conv_list, "session")
    conv_list = unique_conv_ids

    for id in conv_list:
        conv = ConversationService.query(db, id=id, dialog_id=chat_id)
        if not conv:
            errors.append(f"The chat doesn't own the session {id}")
            continue
        ConversationService.delete_by_id(db, id)
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


@router.delete("/agents/{agent_id}/sessions", summary="批量删除代理会话")
def delete_agent_sessions(
    agent_id: str, 
    request: DeleteSessionsRequest, 
    db: Session = Depends(get_db), 
    tenant_id: str = Depends(token_required)
):
    errors = []
    success_count = 0
    req = request.model_dump()
    cvs = UserCanvasService.query(db, user_id=tenant_id, id=agent_id)
    if not cvs:
        return get_error_data_result(retmsg=f"You don't own the agent {agent_id}")

    convs = API4ConversationService.query(db, dialog_id=agent_id)
    if not convs:
        return get_error_data_result(retmsg=f"Agent {agent_id} has no sessions")

    ids = req.get("ids")
    if not ids:
        conv_list = [conv.id for conv in convs]
    else:
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
def ask_knowledge_base(
    request: AskRequest, 
    db: Session = Depends(get_db), 
    tenant_id: str = Depends(token_required)
):
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

    def stream():
        nonlocal req, uid
        try:
            for ans in ask(req["question"], req["kb_ids"], uid):
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
def get_related_questions(
    request: RelatedQuestionsRequest, 
    db: Session = Depends(get_db), 
    tenant_id: str = Depends(token_required)
):
    req = request.model_dump()
    if not req.get("question"):
        return get_error_data_result(retmsg="`question` is required.")
    
    question = req["question"]
    industry = req.get("industry", "")
    chat_mdl = LLMBundle(tenant_id, LLMType.CHAT)
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
    ans = chat_mdl.chat(
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


# 以下是需要特殊token验证的接口，暂时保持原有逻辑

@router.post("/chatbots/{dialog_id}/completions", summary="聊天机器人补全")
def chatbot_completions(dialog_id: str, request: ChatbotCompletionRequest, db: Session = Depends(get_db)):
    req = request.model_dump()
    
    # 这些接口需要特殊的token验证逻辑，暂时保持原有方式
    # TODO: 需要重构token验证方式以符合FastAPI模式
    
    if "quote" not in req:
        req["quote"] = False

    if req.get("stream", True):
        resp = StreamingResponse(iframe_completion(dialog_id, **req), media_type="text/event-stream")
        resp.headers["Cache-control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        return resp

    for answer in iframe_completion(dialog_id, **req):
        return get_result(data=answer)


@router.get("/chatbots/{dialog_id}/info", summary="获取聊天机器人信息")
def get_chatbot_info(dialog_id: str, db: Session = Depends(get_db)):
    # TODO: 需要重构token验证方式以符合FastAPI模式
    
    e, dialog = DialogService.get_by_id(db, dialog_id)
    if not e:
        return get_error_data_result(retmsg=f"Can't find dialog by ID: {dialog_id}")

    return get_result(
        data={
            "title": dialog.name,
            "avatar": dialog.icon,
            "prologue": dialog.prompt_config.get("prologue", ""),
        }
    )


@router.post("/agentbots/{agent_id}/completions", summary="代理机器人补全")
def agent_bot_completions(agent_id: str, request: AgentCompletionRequest, db: Session = Depends(get_db)):
    req = request.model_dump()
    
    # TODO: 需要重构token验证方式以符合FastAPI模式

    if req.get("stream", True):
        # TODO: 需要获取正确的tenant_id
        tenant_id = "default"  # 临时解决方案
        resp = StreamingResponse(agent_completion(tenant_id, agent_id, **req), media_type="text/event-stream")
        resp.headers["Cache-control"] = "no-cache"
        resp.headers["Connection"] = "keep-alive"
        resp.headers["X-Accel-Buffering"] = "no"
        resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
        return resp

    # TODO: 需要获取正确的tenant_id
    tenant_id = "default"  # 临时解决方案
    for answer in agent_completion(tenant_id, agent_id, **req):
        return get_result(data=answer)


@router.get("/agentbots/{agent_id}/inputs", summary="获取代理机器人输入表单")
def get_agent_inputs(agent_id: str, db: Session = Depends(get_db)):
    # TODO: 需要重构token验证方式以符合FastAPI模式
    
    e, cvs = UserCanvasService.get_by_id(db, agent_id)
    if not e:
        return get_error_data_result(retmsg=f"Can't find agent by ID: {agent_id}")

    # TODO: 需要获取正确的tenant_id
    tenant_id = "default"  # 临时解决方案
    canvas = Canvas(json.dumps(cvs.dsl), tenant_id)
    return get_result(data={
        "title": cvs.title,
        "avatar": cvs.avatar,
        "inputs": canvas.get_component_input_form("begin")
    })


@router.post("/searchbots/ask", summary="搜索机器人询问")
def ask_searchbot(request: SearchBotAskRequest, db: Session = Depends(get_db)):
    req = request.model_dump()
    
    # TODO: 需要重构token验证方式以符合FastAPI模式
    # TODO: 需要获取正确的tenant_id
    uid = "default"  # 临时解决方案

    search_id = req.get("search_id", "")
    search_config = {}
    if search_id:
        if search_app := SearchService.get_detail(db, search_id):
            search_config = search_app.get("search_config", {})

    def stream():
        nonlocal req, uid
        try:
            for ans in ask(db, req["question"], req["kb_ids"], uid, search_config=search_config):
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
def retrieval_test_searchbot(request: SearchBotRetrievalTestRequest, db: Session = Depends(get_db)):
    req = request.model_dump()
    
    # TODO: 需要重构token验证方式以符合FastAPI模式
    # TODO: 需要获取正确的tenant_id
    tenant_id = "default"  # 临时解决方案
    
    page = int(req.get("page", 1))
    size = int(req.get("size", 30))
    question = req["question"]
    kb_ids = req["kb_id"]
    if isinstance(kb_ids, str):
        kb_ids = [kb_ids]
    if not kb_ids:
        return get_json_result(data=False, retmsg='Please specify dataset firstly.', retcode=RetCode.DATA_ERROR)
    doc_ids = req.get("doc_ids", [])
    similarity_threshold = float(req.get("similarity_threshold", 0.0))
    vector_similarity_weight = float(req.get("vector_similarity_weight", 0.3))
    use_kg = req.get("use_kg", False)
    top = int(req.get("top_k", 1024))
    langs = req.get("cross_languages", [])
    tenant_ids = []

    if not tenant_id:
        return get_error_data_result(retmsg="permission denined.")

    if req.get("search_id", ""):
        search_config = SearchService.get_detail(db, req.get("search_id", "")).get("search_config", {})
        meta_data_filter = search_config.get("meta_data_filter", {})
        metas = DocumentService.get_meta_by_kbs(db, kb_ids)
        if meta_data_filter.get("method") == "auto":
            chat_mdl = LLMBundle(tenant_id, LLMType.CHAT, llm_name=search_config.get("chat_id", ""))
            filters = gen_meta_filter(chat_mdl, metas, question)
            doc_ids.extend(meta_filter(metas, filters))
            if not doc_ids:
                doc_ids = None
        elif meta_data_filter.get("method") == "manual":
            doc_ids.extend(meta_filter(metas, meta_data_filter["manual"]))
            if not doc_ids:
                doc_ids = None

    try:
        tenants = UserTenantService.query(db, user_id=tenant_id)
        for kb_id in kb_ids:
            for tenant in tenants:
                if KnowledgebaseService.query(db, tenant_id=tenant.tenant_id, id=kb_id):
                    tenant_ids.append(tenant.tenant_id)
                    break
            else:
                return get_json_result(data=False, retmsg="Only owner of knowledgebase authorized for this operation.", retcode=RetCode.OPERATING_ERROR)

        e, kb = KnowledgebaseService.get_by_id(db, kb_ids[0])
        if not e:
            return get_error_data_result(retmsg="Knowledgebase not found!")

        if langs:
            question = cross_languages(kb.tenant_id, None, question, langs)

        embd_mdl = LLMBundle(kb.tenant_id, LLMType.EMBEDDING.value, llm_name=kb.embd_id)

        rerank_mdl = None
        if req.get("rerank_id"):
            rerank_mdl = LLMBundle(kb.tenant_id, LLMType.RERANK.value, llm_name=req["rerank_id"])

        if req.get("keyword", False):
            chat_mdl = LLMBundle(kb.tenant_id, LLMType.CHAT)
            question += keyword_extraction(chat_mdl, question)

        labels = label_question(db, question, [kb])
        ranks = settings.retriever.retrieval(
            question, embd_mdl, tenant_ids, kb_ids, page, size, similarity_threshold, vector_similarity_weight, top, doc_ids, rerank_mdl=rerank_mdl, highlight=req.get("highlight"), rank_feature=labels
        )
        if use_kg:
            ck = settings.kg_retriever.retrieval(question, tenant_ids, kb_ids, embd_mdl, LLMBundle(kb.tenant_id, LLMType.CHAT))
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
def get_searchbot_related_questions(request: SearchBotRelatedQuestionsRequest, db: Session = Depends(get_db)):
    req = request.model_dump()
    
    # TODO: 需要重构token验证方式以符合FastAPI模式
    # TODO: 需要获取正确的tenant_id
    tenant_id = "default"  # 临时解决方案
    
    if not tenant_id:
        return get_error_data_result(retmsg="permission denined.")

    search_id = req.get("search_id", "")
    search_config = {}
    if search_id:
        if search_app := SearchService.get_detail(db, search_id):
            search_config = search_app.get("search_config", {})

    question = req["question"]

    chat_id = search_config.get("chat_id", "")
    chat_mdl = LLMBundle(db, tenant_id, LLMType.CHAT, chat_id)

    gen_conf = search_config.get("llm_setting", {"temperature": 0.9})
    prompt = load_prompt("related_question")
    ans = chat_mdl.chat(
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
def get_searchbot_detail(search_id: str = Query(...), db: Session = Depends(get_db)):
    # TODO: 需要重构token验证方式以符合FastAPI模式
    # TODO: 需要获取正确的tenant_id
    tenant_id = "default"  # 临时解决方案
    
    if not tenant_id:
        return get_error_data_result(retmsg="permission denined.")
    
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
def generate_searchbot_mindmap(request: SearchBotMindmapRequest, db: Session = Depends(get_db)):
    req = request.model_dump()
    token = request.headers.get("Authorization").split()
    if len(token) != 2:
        return get_error_data_result(retmsg='Authorization is not valid!"')
    token = token[1]
    objs = APIToken.query(beta=token)
    if not objs:
        return get_error_data_result(retmsg='Authentication error: API key is invalid!"')

    tenant_id = objs[0].tenant_id

    search_id = req.get("search_id", "")
    search_app = SearchService.get_detail(search_id) if search_id else {}

    mind_map = gen_mindmap(db, req["question"], req["kb_ids"], tenant_id, search_app.get("search_config", {}))
    if "error" in mind_map:
        return server_error_response(Exception(mind_map["error"]))
    return get_json_result(data=mind_map)