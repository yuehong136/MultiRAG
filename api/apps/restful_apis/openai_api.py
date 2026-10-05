"""OpenAI-compatible chat completions for an existing chat assistant."""

from __future__ import annotations

import copy
import json
import logging
import time
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import get_async_db
from api.db.services.dialog_service import DialogService, async_chat
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.tenant_llm_service import TenantLLMService
from api.utils.api_utils import async_token_required, get_error_data_result
from api.utils.reference_metadata import enrich_reference_metadata_async, resolve_reference_metadata_preferences
from common.constants import RetCode, StatusEnum
from common.metadata_utils import convert_conditions, meta_filter
from common.token_utils import num_tokens_from_string
from core.prompts.generator import chunks_format

router = APIRouter()
logger = logging.getLogger(__name__)


class OpenAIChatCompletionRequest(BaseModel):
    model: str
    messages: Any
    stream: bool | None = None
    internet: bool | None = None
    extra_body: Any = None
    reference: bool | None = None
    reference_metadata: Any = None
    metadata_condition: Any = None


def _argument_error(message: str) -> Any:
    return get_error_data_result(retcode=RetCode.ARGUMENT_ERROR, retmsg=message)


def _normalize_message_content(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict) or part.get("type") != "text" or not isinstance(part.get("text"), str):
                return None
            parts.append(part["text"])
        return "\n".join(parts)
    return None


def _normalize_openai_messages(messages: Any) -> tuple[list[dict[str, Any]] | None, str | None]:
    if not isinstance(messages, list):
        return None, "messages must be an array."
    if not messages:
        return None, "You have to provide messages."
    normalized: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "user", "assistant"}:
            return None, "Each message must have a supported role."
        content = _normalize_message_content(message.get("content"))
        if content is None:
            return None, "messages[].content must be a string or an array of text parts."
        normalized.append({**message, "content": content})
    if normalized[-1]["role"] != "user":
        return None, "The last content of this conversation is not from user."
    return normalized, None


async def _build_reference_chunks(
    db: AsyncSession,
    reference: Any,
    *,
    include_metadata: bool,
    metadata_fields: list[str] | None,
) -> list[dict[str, Any]]:
    chunks = chunks_format(reference)
    await enrich_reference_metadata_async(db, chunks, (include_metadata, None if metadata_fields is None else set(metadata_fields)), kb_field="dataset_id", doc_field="document_id")
    return chunks


def _chunk(completion_id: str, model: str, created: int, delta: dict[str, Any], finish_reason: str | None = None, usage: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason, "logprobs": None}],
        "usage": usage,
    }


def _sse(payload: dict[str, Any] | str) -> str:
    return f"data:{json.dumps(payload, ensure_ascii=False) if isinstance(payload, dict) else payload}\n\n"


async def _stream_chat_completion_sse(
    db: AsyncSession,
    dia: Any,
    messages: list[dict[str, Any]],
    chat_kwargs: dict[str, Any],
    *,
    completion_id: str,
    requested_model: str,
    prompt: str,
    need_reference: bool,
    include_reference_metadata: bool,
    metadata_fields: list[str] | None,
) -> AsyncGenerator[str, None]:
    created = int(time.time())
    prompt_tokens = num_tokens_from_string(prompt)
    completion_tokens = 0
    full_content = ""
    final_answer: str | None = None
    final_reference: Any = None
    in_think = False
    yield _sse(_chunk(completion_id, requested_model, created, {"role": "assistant", "content": ""}))
    try:
        async for ans in async_chat(dia, messages, db, True, **chat_kwargs):
            if ans.get("final"):
                final_answer = ans.get("answer") or full_content
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
            completion_tokens += num_tokens_from_string(delta)
            if in_think:
                yield _sse(_chunk(completion_id, requested_model, created, {"reasoning_content": delta}))
            else:
                full_content += delta
                yield _sse(_chunk(completion_id, requested_model, created, {"content": delta}))
        # Some chat paths yield only a final event. Preserve that answer once.
        if final_answer and not full_content:
            full_content = final_answer
            completion_tokens += num_tokens_from_string(full_content)
            yield _sse(_chunk(completion_id, requested_model, created, {"content": full_content}))
        if final_answer is None and not full_content:
            raise RuntimeError("Chat completion returned no answer.")
        delta: dict[str, Any] = {}
        if need_reference:
            delta["reference"] = await _build_reference_chunks(db, final_reference, include_metadata=include_reference_metadata, metadata_fields=metadata_fields)
            delta["final_content"] = final_answer or full_content
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": prompt_tokens + completion_tokens}
        yield _sse(_chunk(completion_id, requested_model, created, delta, finish_reason="stop", usage=usage))
    except Exception:
        logger.exception("OpenAI-compatible chat stream failed")
        yield _sse({"error": {"message": "Chat completion failed.", "type": "server_error"}})
    yield _sse("[DONE]")


@router.post("/openai/{chat_id}/chat/completions", summary="OpenAI-compatible chat completion")
@router.post("/chats_openai/{chat_id}/chat/completions", deprecated=True, include_in_schema=True)
async def openai_chat_completions(
    chat_id: str,
    request: OpenAIChatCompletionRequest,
    http_request: Request,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_token_required),
) -> Any:
    """Complete an existing chat with an API key and an OpenAI-shaped response."""
    req = request.model_dump()
    if not req["model"].strip():
        return _argument_error("model must not be empty.")
    extra_body = req["extra_body"] or {}
    if not isinstance(extra_body, dict):
        return _argument_error("extra_body must be an object.")
    # OpenAI's Python client merges extra_body into the top-level JSON object.
    # Also accept the nested shape used by raw HTTP callers and our legacy API.
    extra_body = dict(extra_body)
    for key in ("reference", "reference_metadata", "metadata_condition"):
        if req[key] is not None:
            extra_body[key] = req[key]
    reference_metadata = extra_body.get("reference_metadata") or {}
    if not isinstance(reference_metadata, dict):
        return _argument_error("reference_metadata must be an object.")
    metadata_fields = reference_metadata.get("fields")
    if metadata_fields is not None and (not isinstance(metadata_fields, list) or any(not isinstance(field, str) for field in metadata_fields)):
        return _argument_error("reference_metadata.fields must be an array of strings.")
    metadata_condition = extra_body.get("metadata_condition") or {}
    if not isinstance(metadata_condition, dict):
        return _argument_error("metadata_condition must be an object.")
    messages, normalize_error = _normalize_openai_messages(req["messages"])
    if normalize_error:
        return _argument_error(normalize_error)
    assert messages is not None

    dia_list = await db.run_sync(lambda s: DialogService.query(s, tenant_id=tenant_id, id=chat_id, status=StatusEnum.VALID.value))  # TODO(async-phase4)
    if not dia_list:
        return get_error_data_result(retmsg=f"You don't own the chat {chat_id}")
    dia = dia_list[0]
    requested_model = req["model"]
    if requested_model == "model":
        requested_model = dia.llm_id or "model"
    else:
        tenant_llm_id = await db.run_sync(lambda s: getattr(TenantLLMService.get_api_key(s, tenant_id, requested_model, "chat"), "id", None))  # TODO(async-phase4)
        if tenant_llm_id is None:
            return _argument_error(f"`model` {requested_model} doesn't exist")
        dia = copy.deepcopy(dia)
        dia.llm_id = requested_model
        if hasattr(dia, "tenant_llm_id"):
            dia.tenant_llm_id = tenant_llm_id

    doc_ids_str: str | None = None
    if metadata_condition:
        try:
            metas = await db.run_sync(lambda s: DocMetadataService.get_flatted_meta_by_kbs(s, dia.kb_ids or []))  # TODO(async-phase4)
            filtered_doc_ids = meta_filter(metas, convert_conditions(metadata_condition), metadata_condition.get("logic", "and"))
        except (KeyError, TypeError, ValueError) as exc:
            return _argument_error(f"Invalid metadata_condition: {exc}")
        if metadata_condition.get("conditions") and not filtered_doc_ids:
            filtered_doc_ids = ["-999"]
        doc_ids_str = ",".join(filtered_doc_ids) if filtered_doc_ids else None

    filtered_messages = [m for m in messages if m["role"] != "system"]
    while filtered_messages and filtered_messages[0]["role"] == "assistant":
        filtered_messages.pop(0)
    chat_kwargs: dict[str, Any] = {"toolcall_session": None, "tools": None, "quote": bool(extra_body.get("reference", False))}
    if req["internet"] is not None:
        chat_kwargs["internet"] = req["internet"]
    if doc_ids_str:
        chat_kwargs["doc_ids"] = doc_ids_str
    prompt = messages[-1]["content"]
    completion_id = f"chatcmpl-{chat_id}"
    need_reference = bool(extra_body.get("reference", False))
    try:
        include_reference_metadata, selected_fields = resolve_reference_metadata_preferences(extra_body, dia.prompt_config)
    except ValueError as exc:
        return _argument_error(str(exc))
    metadata_fields = None if selected_fields is None else sorted(selected_fields)
    chat_kwargs["reference_metadata"] = {"include": include_reference_metadata, "fields": metadata_fields}
    legacy = "/chats_openai/" in http_request.url.path
    stream_mode = req["stream"] if req["stream"] is not None else legacy
    if stream_mode:
        resp = StreamingResponse(
            _stream_chat_completion_sse(
                db,
                dia,
                filtered_messages,
                chat_kwargs,
                completion_id=completion_id,
                requested_model=requested_model,
                prompt=prompt,
                need_reference=need_reference,
                include_reference_metadata=include_reference_metadata,
                metadata_fields=metadata_fields,
            ),
            media_type="text/event-stream",
        )
        resp.headers.update({"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})
        return resp

    try:
        answer = None
        async for ans in async_chat(dia, filtered_messages, db, False, **chat_kwargs):
            answer = ans
            break
        if not isinstance(answer, dict) or not isinstance(answer.get("answer"), str):
            raise RuntimeError("Chat completion returned no answer.")
        content = answer["answer"]
        reference = await _build_reference_chunks(db, answer.get("reference", {}), include_metadata=include_reference_metadata, metadata_fields=metadata_fields) if need_reference else None
    except Exception:
        logger.exception("OpenAI-compatible chat completion failed")
        return get_error_data_result(retcode=RetCode.SERVER_ERROR, retmsg="Chat completion failed.")
    prompt_tokens = num_tokens_from_string(prompt)
    completion_tokens = num_tokens_from_string(content)
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if need_reference:
        message["reference"] = reference
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": requested_model,
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "completion_tokens_details": {
                "reasoning_tokens": sum(num_tokens_from_string(m["content"]) for m in messages),
                "accepted_prediction_tokens": completion_tokens,
                "rejected_prediction_tokens": 0,
            },
        },
        "choices": [{"index": 0, "message": message, "finish_reason": "stop", "logprobs": None}],
    }
