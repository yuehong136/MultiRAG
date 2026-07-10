"""RESTful chat management API.

Routes are mounted under ``/api/v1`` by ``api.apps.register_page``.
The legacy ``/v1/dialog/*`` endpoints stay in ``api/apps/dialog_app.py``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import tempfile
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Depends, File, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import get_async_db, get_db
from api.db.joint_services.tenant_model_service import get_model_config_by_type_and_name, get_tenant_default_model_by_type
from api.db.services.chunk_feedback_service import ChunkFeedbackService
from api.db.services.conversation_service import ConversationService, structure_answer
from api.db.services.dialog_service import DialogService, async_ask, async_chat, gen_mindmap
from api.db.services.knowledgebase_service import EmbeddingModelMismatchError, KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.db.services.search_service import SearchService
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import TenantService, UserTenantService
from api.utils.api_utils import async_current_tenant_id, check_duplicate_ids, current_tenant_id, get_error_data_result, get_result
from api.utils.tenant_utils import ensure_tenant_model_id_for_params
from common.constants import LLMType, RetCode, StatusEnum
from common.misc_utils import get_uuid
from core.prompts.generator import chunks_format
from core.prompts.template import load_prompt

router = APIRouter()
logger = logging.getLogger(__name__)


_DEFAULT_PROMPT_CONFIG = {
    "system": (
        "You are an intelligent assistant. Please summarize the content of the dataset to answer the question. "
        "Please list the data in the dataset and answer in detail. When all dataset content is irrelevant to the "
        'question, your answer must include the sentence "The answer you are looking for is not found in the dataset!" '
        "Answers need to consider chat history.\n"
        "      Here is the knowledge base:\n"
        "      {knowledge}\n"
        "      The above is the knowledge base."
    ),
    "prologue": "Hi! I'm your assistant. What can I do for you?",
    "parameters": [{"key": "knowledge", "optional": False}],
    "empty_response": "Sorry! No relevant content was found in the knowledge base!",
    "quote": True,
    "tts": False,
    "refine_multiturn": True,
}
_DEFAULT_RERANK_MODELS = {"BAAI/bge-reranker-v2-m3", "maidalun1020/bce-reranker-base_v1"}
_READONLY_FIELDS = {"id", "tenant_id", "created_by", "create_time", "create_date", "update_time", "update_date"}
_PERSISTED_FIELDS = {column.name for column in DialogService.model.__table__.columns}
_LEGACY_PAYLOAD_FIELDS = {"avatar", "llm", "prompt", "show_quotation", "model_type"}


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")


class DeleteChatsRequest(BaseModel):
    ids: list[str] | None = None
    delete_all: bool = False


class CreateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = "New session"
    user_id: str | None = None


class UpdateSessionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str | None = None


class DeleteSessionsRequest(BaseModel):
    ids: list[str] | None = None
    delete_all: bool = False


class TTSRequest(BaseModel):
    text: str


class AskRequest(BaseModel):
    question: str
    kb_ids: list[str]
    search_id: str | None = ""


class MindmapRequest(BaseModel):
    question: str
    kb_ids: list[str]
    search_id: str | None = ""


class SessionCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    messages: list[dict[str, Any]]
    stream: bool | None = True


class RelatedQuestionsRequest(BaseModel):
    question: str
    search_id: str | None = ""


def _error(message: str, retcode: RetCode = RetCode.DATA_ERROR):
    if retcode == RetCode.AUTHENTICATION_ERROR:
        return get_result(data=False, retcode=retcode, retmsg=message)
    return get_error_data_result(retcode=retcode, retmsg=message)


def _model_to_dict(chat: Any) -> dict[str, Any]:
    if hasattr(chat, "to_dict"):
        return chat.to_dict()
    if isinstance(chat, dict):
        return dict(chat)
    return {k: v for k, v in vars(chat).items() if not k.startswith("_")}


def _resolve_kb_names(db: Session, kb_ids: list[str] | None) -> tuple[list[str], list[str]]:
    ids: list[str] = []
    names: list[str] = []
    for kb_id in kb_ids or []:
        kb = KnowledgebaseService.get_by_id(db, kb_id)
        if not kb or kb.status != StatusEnum.VALID.value:
            continue
        ids.append(kb_id)
        names.append(kb.name)
    return ids, names


def build_chat_response(db: Session, chat: Any) -> dict[str, Any]:
    data = _model_to_dict(chat)
    kb_ids, kb_names = _resolve_kb_names(db, data.get("kb_ids", []))
    data["dataset_ids"] = kb_ids
    data["kb_names"] = kb_names
    data.pop("kb_ids", None)
    return data


def build_session_response(session: Any) -> dict[str, Any]:
    data = _model_to_dict(session)
    data.pop("_sa_instance_state", None)
    data["chat_id"] = data.pop("dialog_id", data.get("chat_id"))
    data["messages"] = data.pop("message", data.get("messages") or [])
    return data


def apply_feedback_to_session_payload(
    tenant_id: str,
    payload: dict[str, Any],
    msg_id: str,
    thumbup: bool,
    feedback: str = "",
) -> bool:
    """Update message feedback and apply chunk feedback when the state changes."""
    messages = payload.get("message") or payload.get("messages") or []
    if not isinstance(messages, list):
        return False

    for message_index, msg in enumerate(messages):
        if msg_id != msg.get("id", "") or msg.get("role", "") != "assistant":
            continue

        prior_thumb = msg.get("thumbup")
        if thumbup is True:
            msg["thumbup"] = True
            msg.pop("feedback", None)
            apply_chunk_feedback = prior_thumb is not True
        else:
            msg["thumbup"] = False
            if feedback:
                msg["feedback"] = feedback
            apply_chunk_feedback = prior_thumb is not False

        if apply_chunk_feedback:
            references = payload.get("reference") or []
            ref_index = (message_index - 1) // 2
            if isinstance(references, list) and 0 <= ref_index < len(references):
                reference = references[ref_index]
                if isinstance(reference, dict) and reference:
                    try:
                        if isinstance(prior_thumb, bool) and prior_thumb != thumbup:
                            ChunkFeedbackService.apply_feedback(
                                tenant_id=tenant_id,
                                reference=reference,
                                is_positive=not prior_thumb,
                            )
                        result = ChunkFeedbackService.apply_feedback(
                            tenant_id=tenant_id,
                            reference=reference,
                            is_positive=thumbup is True,
                        )
                        logger.debug(
                            "Chunk feedback applied: %s succeeded, %s failed",
                            result.get("success_count", 0),
                            result.get("fail_count", 0),
                        )
                    except Exception:
                        logger.warning("Failed to apply chunk feedback", exc_info=True)
        return True
    return False


def _has_knowledge_placeholder(prompt_config: dict[str, Any] | None) -> bool:
    return "{knowledge}" in (prompt_config or {}).get("system", "")


def _normalize_name(name: Any, *, required: bool) -> tuple[str | None, str | None]:
    if name is None:
        if required:
            return None, "`name` is required."
        return None, None
    if not isinstance(name, str):
        return None, "Chat name must be a string."
    name = name.strip()
    if not name:
        return None, "`name` is required." if required else "`name` cannot be empty."
    if len(name.encode("utf-8")) > 255:
        return None, f"Chat name length is {len(name.encode('utf-8'))} which is larger than 255."
    return name, None


def _reject_legacy_payload(req: dict[str, Any]) -> str | None:
    legacy_fields = sorted(_LEGACY_PAYLOAD_FIELDS.intersection(req))
    if not legacy_fields:
        return None
    fields = ", ".join(legacy_fields)
    return f"Unsupported legacy chat payload fields: {fields}. Use icon, llm_id, llm_setting, prompt_config, and do_refer."


def _normalize_do_refer(req: dict[str, Any]) -> None:
    if isinstance(req.get("do_refer"), bool):
        req["do_refer"] = "1" if req["do_refer"] else "0"


def _validate_llm_id(db: Session, tenant_id: str, llm_id: str | None, llm_setting: dict[str, Any] | None) -> str | None:
    if not llm_id:
        return None
    model_type = (llm_setting or {}).get("model_type")
    if model_type not in {"chat", "image2text"}:
        model_type = "chat"
    if TenantLLMService.get_api_key(db, tenant_id, llm_id, model_type):
        return None
    return f"`llm_id` {llm_id} doesn't exist"


def _validate_rerank_id(db: Session, tenant_id: str, rerank_id: str | None) -> str | None:
    if not rerank_id:
        return None
    llm_name, _ = TenantLLMService.split_model_name_and_factory(rerank_id)
    if llm_name in _DEFAULT_RERANK_MODELS:
        return None
    if TenantLLMService.get_api_key(db, tenant_id, rerank_id, "rerank"):
        return None
    return f"`rerank_id` {rerank_id} doesn't exist"


def _validate_dataset_ids(db: Session, tenant_id: str, dataset_ids: list[str] | None) -> list[str] | str:
    if dataset_ids is None:
        return []
    if not isinstance(dataset_ids, list):
        return "`dataset_ids` should be a list."

    normalized_ids = [dataset_id for dataset_id in dataset_ids if dataset_id]
    kbs = []
    for dataset_id in normalized_ids:
        if not KnowledgebaseService.accessible(db, kb_id=dataset_id, user_id=tenant_id):
            return f"You don't own the dataset {dataset_id}"
        matches = KnowledgebaseService.query(db, id=dataset_id)
        if not matches:
            return f"You don't own the dataset {dataset_id}"
        kb = matches[0]
        if kb.chunk_num == 0:
            return f"The dataset {dataset_id} doesn't own parsed file"
        kbs.append(kb)

    try:
        KnowledgebaseService.ensure_same_embedding_model(kbs)
    except EmbeddingModelMismatchError as e:
        return str(e)
    return normalized_ids


def _apply_prompt_defaults(req: dict[str, Any]) -> None:
    prompt_config = req.setdefault("prompt_config", {})
    for key, value in _DEFAULT_PROMPT_CONFIG.items():
        if key == "system" and not prompt_config.get(key):
            prompt_config[key] = deepcopy(value)
        elif key not in prompt_config:
            prompt_config[key] = deepcopy(value)

    if req.get("kb_ids") and not prompt_config.get("parameters") and _has_knowledge_placeholder(prompt_config):
        prompt_config["parameters"] = [{"key": "knowledge", "optional": False}]


def _filter_persisted(req: dict[str, Any]) -> dict[str, Any]:
    return {field: value for field, value in req.items() if field in _PERSISTED_FIELDS and field not in _READONLY_FIELDS}


def _prepare_create_payload(db: Session, tenant_id: str, req: dict[str, Any]) -> tuple[bool, dict[str, Any] | str]:
    if err := _reject_legacy_payload(req):
        return False, err

    if req.get("tenant_id"):
        return False, "`tenant_id` must not be provided."

    name, err = _normalize_name(req.get("name"), required=True)
    if err:
        return False, err
    req["name"] = name

    tenant = TenantService.get_by_id(db, tenant_id)
    if not tenant:
        return False, "Tenant not found!"

    dataset_ids = req.pop("dataset_ids", req.get("kb_ids", []))
    kb_ids = _validate_dataset_ids(db, tenant_id, dataset_ids)
    if isinstance(kb_ids, str):
        return False, kb_ids
    req["kb_ids"] = kb_ids

    llm_setting = req.get("llm_setting")
    if llm_setting is None:
        req["llm_setting"] = {}
    elif not isinstance(llm_setting, dict):
        return False, "`llm_setting` should be an object."

    llm_id_provided = bool(req.get("llm_id"))
    if not req.get("llm_id"):
        req["llm_id"] = tenant.llm_id
    if llm_id_provided:
        err = _validate_llm_id(db, tenant_id, req.get("llm_id"), req.get("llm_setting"))
        if err:
            return False, err

    req.setdefault("description", "A helpful Assistant")
    req.setdefault("icon", "")
    req.setdefault("language", "English")
    req.setdefault("top_n", 6)
    req.setdefault("top_k", 1024)
    req.setdefault("rerank_id", "")
    req.setdefault("similarity_threshold", 0.1)
    req.setdefault("vector_similarity_weight", 0.3)

    if req.get("rerank_id"):
        err = _validate_rerank_id(db, tenant_id, req.get("rerank_id"))
        if err:
            return False, err

    if req.get("prompt_config") is not None and not isinstance(req["prompt_config"], dict):
        return False, "`prompt_config` should be an object."
    _apply_prompt_defaults(req)

    if DialogService.query(db, name=req["name"], tenant_id=tenant_id, status=StatusEnum.VALID.value):
        return False, "Duplicated chat name in creating chat."

    _normalize_do_refer(req)
    req = ensure_tenant_model_id_for_params(db, tenant_id, req)
    req = _filter_persisted(req)
    req["id"] = get_uuid()
    req["tenant_id"] = tenant_id
    return True, req


def _prepare_update_payload(
    db: Session,
    tenant_id: str,
    chat_id: str,
    req: dict[str, Any],
    *,
    merge_nested: bool,
) -> tuple[bool, dict[str, Any] | str]:
    if err := _reject_legacy_payload(req):
        return False, err

    if req.get("tenant_id"):
        return False, "`tenant_id` must not be provided."

    current_chat = DialogService.get_by_id(db, chat_id)
    if not current_chat:
        return False, "Chat not found!"
    current = current_chat.to_dict()

    if "name" in req:
        name, err = _normalize_name(req.get("name"), required=not merge_nested)
        if err:
            return False, err
        if name is not None:
            req["name"] = name

    if "dataset_ids" in req:
        kb_ids = _validate_dataset_ids(db, tenant_id, req.pop("dataset_ids"))
        if isinstance(kb_ids, str):
            return False, kb_ids
        req["kb_ids"] = kb_ids
    elif "kb_ids" in req:
        kb_ids = _validate_dataset_ids(db, tenant_id, req.get("kb_ids"))
        if isinstance(kb_ids, str):
            return False, kb_ids
        req["kb_ids"] = kb_ids

    if "llm_setting" in req and req["llm_setting"] is not None and not isinstance(req["llm_setting"], dict):
        return False, "`llm_setting` should be an object."
    if "llm_id" in req:
        err = _validate_llm_id(db, tenant_id, req.get("llm_id"), req.get("llm_setting"))
        if err:
            return False, err

    if "rerank_id" in req:
        err = _validate_rerank_id(db, tenant_id, req.get("rerank_id"))
        if err:
            return False, err

    if "prompt_config" in req:
        if req["prompt_config"] is not None and not isinstance(req["prompt_config"], dict):
            return False, "`prompt_config` should be an object."
        if merge_nested:
            prompt_config = deepcopy(current.get("prompt_config") or {})
            prompt_config.update(req.get("prompt_config") or {})
            req["prompt_config"] = prompt_config

    if "llm_setting" in req and merge_nested:
        llm_setting = deepcopy(current.get("llm_setting") or {})
        llm_setting.update(req.get("llm_setting") or {})
        req["llm_setting"] = llm_setting

    if (
        "name" in req
        and req.get("name")
        and req["name"].lower() != (current.get("name") or "").lower()
        and DialogService.query(db, name=req["name"], tenant_id=tenant_id, status=StatusEnum.VALID.value)
    ):
        return False, "Duplicated chat name."

    _normalize_do_refer(req)
    req = ensure_tenant_model_id_for_params(db, tenant_id, req)
    return True, _filter_persisted(req)


def _owned_chat_exists(db: Session, tenant_id: str, chat_id: str) -> bool:
    return bool(DialogService.query(db, tenant_id=tenant_id, id=chat_id, status=StatusEnum.VALID.value))


@router.post("/chats", summary="Create chat")
def create_chat(
    request: ChatRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    try:
        ok, payload = _prepare_create_payload(db, tenant_id, request.model_dump(exclude_unset=True))
        if not ok:
            return _error(payload)  # type: ignore[arg-type]
        if not DialogService.save(db, **payload):  # type: ignore[arg-type]
            return _error("Failed to create chat.")
        chat = DialogService.get_by_id(db, payload["id"])  # type: ignore[index]
        if not chat:
            return _error("Failed to retrieve created chat.")
        return get_result(data=build_chat_response(db, chat))
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.get("/chats", summary="List chats")
def list_chats(
    id: str | None = Query(None, description="Chat ID filter"),
    name: str | None = Query(None, description="Chat name filter"),
    keywords: str | None = Query("", description="Keyword filter"),
    page: int = Query(0, description="Page number; 0 disables pagination"),
    page_size: int = Query(0, description="Items per page; 0 disables pagination"),
    orderby: str = Query("create_time", description="Sort field"),
    desc: bool = Query(True, description="Sort descending"),
    owner_ids: list[str] | None = Query(None, description="Owner tenant IDs"),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    try:
        owner_ids = owner_ids or []
        exact_keywords = "" if id or name else (keywords or "")
        if owner_ids:
            chats, _ = DialogService.get_by_tenant_ids(db, owner_ids, tenant_id, 0, 0, orderby, desc, exact_keywords, id=id, name=name)
            chats = [chat for chat in chats if chat.get("tenant_id") in owner_ids]
            total = len(chats)
            if page and page_size:
                start = (page - 1) * page_size
                chats = chats[start : start + page_size]
        else:
            chats, total = DialogService.get_by_tenant_ids(db, [], tenant_id, page, page_size, orderby, desc, exact_keywords, id=id, name=name)
        return get_result(data={"chats": [build_chat_response(db, chat) for chat in chats], "total": total})
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.get("/chats/{chat_id}", summary="Get chat")
def get_chat(
    chat_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    try:
        tenants = UserTenantService.query(db, user_id=tenant_id)
        for tenant in tenants:
            if DialogService.query(db, tenant_id=tenant.tenant_id, id=chat_id, status=StatusEnum.VALID.value):
                break
        else:
            if not _owned_chat_exists(db, tenant_id, chat_id):
                return _error("No authorization.", RetCode.AUTHENTICATION_ERROR)

        chat = DialogService.get_by_id(db, chat_id)
        if not chat:
            return _error("Chat not found!")
        return get_result(data=build_chat_response(db, chat))
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.put("/chats/{chat_id}", summary="Update chat")
def update_chat(
    chat_id: str,
    request: ChatRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    if not _owned_chat_exists(db, tenant_id, chat_id):
        return _error("No authorization.", RetCode.AUTHENTICATION_ERROR)
    try:
        ok, payload = _prepare_update_payload(db, tenant_id, chat_id, request.model_dump(exclude_unset=True), merge_nested=False)
        if not ok:
            return _error(payload)  # type: ignore[arg-type]
        if not DialogService.update_by_id(db, chat_id, payload):  # type: ignore[arg-type]
            return _error("Chat not found!")
        chat = DialogService.get_by_id(db, chat_id)
        if not chat:
            return _error("Failed to retrieve updated chat.")
        return get_result(data=build_chat_response(db, chat))
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.patch("/chats/{chat_id}", summary="Patch chat")
def patch_chat(
    chat_id: str,
    request: ChatRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    if not _owned_chat_exists(db, tenant_id, chat_id):
        return _error("No authorization.", RetCode.AUTHENTICATION_ERROR)
    try:
        ok, payload = _prepare_update_payload(db, tenant_id, chat_id, request.model_dump(exclude_unset=True), merge_nested=True)
        if not ok:
            return _error(payload)  # type: ignore[arg-type]
        if not DialogService.update_by_id(db, chat_id, payload):  # type: ignore[arg-type]
            return _error("Failed to update chat.")
        chat = DialogService.get_by_id(db, chat_id)
        if not chat:
            return _error("Failed to retrieve updated chat.")
        return get_result(data=build_chat_response(db, chat))
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.delete("/chats/{chat_id}", summary="Delete chat")
def delete_chat(
    chat_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    if not _owned_chat_exists(db, tenant_id, chat_id):
        return _error("No authorization.", RetCode.AUTHENTICATION_ERROR)
    try:
        if not DialogService.update_by_id(db, chat_id, {"status": StatusEnum.INVALID.value}):
            return _error(f"Failed to delete chat {chat_id}")
        return get_result(data=True)
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.delete("/chats", summary="Bulk delete chats")
def bulk_delete_chats(
    request: DeleteChatsRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    try:
        req = request.model_dump()
        ids = req.get("ids")
        if not ids:
            if req.get("delete_all") is True:
                ids = [chat.id for chat in DialogService.query(db, tenant_id=tenant_id, status=StatusEnum.VALID.value)]
                if not ids:
                    return get_result(data={})
            else:
                return get_result(data={})

        errors: list[str] = []
        success_count = 0
        unique_ids, duplicate_messages = check_duplicate_ids(ids, "chat")
        for chat_id in unique_ids:
            if not _owned_chat_exists(db, tenant_id, chat_id):
                errors.append(f"Chat({chat_id}) not found.")
                continue
            success_count += DialogService.update_by_id(db, chat_id, {"status": StatusEnum.INVALID.value})

        all_errors = errors + duplicate_messages
        if all_errors:
            if success_count > 0:
                return get_result(
                    data={"success_count": success_count, "errors": all_errors},
                    retmsg=f"Partially deleted {success_count} chats with {len(all_errors)} errors",
                )
            return _error("; ".join(all_errors))
        return get_result(data={"success_count": success_count})
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.post("/chats/tts", summary="Text to speech")
def tts(
    request: TTSRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    try:
        tts_config = get_tenant_default_model_by_type(db, tenant_id, LLMType.TTS)
    except Exception as e:
        return _error(str(e))

    tts_mdl = LLMBundle(db, tenant_id, tts_config)

    def stream_audio():
        try:
            for txt in re.split(r"[，。/《》？；：！\n\r:;]+", request.text):
                if not txt.strip():
                    continue
                yield from tts_mdl.tts(txt)
        except Exception as e:
            yield (
                "data:"
                + json.dumps(
                    {"code": 500, "message": str(e), "data": {"answer": "**ERROR**: " + str(e)}},
                    ensure_ascii=False,
                )
            ).encode("utf-8")

    resp = StreamingResponse(stream_audio(), media_type="audio/mpeg")
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["Connection"] = "keep-alive"
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


@router.post("/chats/transcriptions", summary="Transcribe audio")
async def transcriptions(
    file: UploadFile = File(...),
    stream: str = "false",
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    stream_mode = stream.lower() == "true"
    allowed_exts = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".webm", ".opus", ".wma"}

    filename = file.filename or ""
    suffix = os.path.splitext(filename)[-1].lower()
    if suffix not in allowed_exts:
        return _error(f"Unsupported audio format: {suffix}. Allowed: {', '.join(sorted(allowed_exts))}")

    fd, temp_audio_path = tempfile.mkstemp(suffix=suffix)
    os.close(fd)
    try:
        content = await file.read()
        with open(temp_audio_path, "wb") as temp_file:
            temp_file.write(content)

        try:
            asr_config = get_tenant_default_model_by_type(db, tenant_id, LLMType.SPEECH2TEXT)
        except Exception as e:
            return _error(str(e))

        asr_mdl = LLMBundle(db, tenant_id, asr_config)
        if not stream_mode:
            text = asr_mdl.transcription(temp_audio_path)
            return get_result(data={"text": text})

        async def event_stream():
            try:
                for evt in asr_mdl.stream_transcription(temp_audio_path):
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
            except Exception as e:
                yield f"data: {json.dumps({'event': 'error', 'text': str(e)}, ensure_ascii=False)}\n\n"
            finally:
                try:
                    os.remove(temp_audio_path)
                except Exception as e:
                    logging.error("Failed to remove temp audio file: %s", str(e))

        return StreamingResponse(event_stream(), media_type="text/event-stream")
    finally:
        if not stream_mode:
            try:
                os.remove(temp_audio_path)
            except FileNotFoundError:
                pass
            except Exception as e:
                logging.error("Failed to remove temp audio file: %s", str(e))


@router.post("/chats/mindmap", summary="Generate mindmap")
async def mindmap(
    request: MindmapRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    try:
        req = request.model_dump()
        search_id = req.get("search_id", "")
        search_app = SearchService.get_detail(db, search_id) if search_id else {}
        search_config = search_app.get("search_config", {}) if search_app else {}
        kb_ids = list(search_config.get("kb_ids", []))
        kb_ids.extend(req["kb_ids"])
        kb_ids = list(set(kb_ids))

        mind_map = await gen_mindmap(
            db,
            req["question"],
            kb_ids,
            search_app.get("tenant_id", tenant_id) if search_app else tenant_id,
            search_config,
        )
        if "error" in mind_map:
            return _error(mind_map["error"])
        return get_result(data=mind_map)
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.post("/chats/related_questions", summary="Generate related questions")
async def related_questions(
    request: RelatedQuestionsRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        req = request.model_dump()
        question = req.get("question")
        if not question:
            return _error("`question` is required.")

        search_id = req.get("search_id", "")
        search_config = {}
        if search_id:
            if search_app := await db.run_sync(lambda s: SearchService.get_detail(s, search_id)):  # TODO(async-phase4)
                search_config = search_app.get("search_config", {})

        chat_id = search_config.get("chat_id", "")
        if chat_id:
            chat_config = await db.run_sync(lambda s: get_model_config_by_type_and_name(s, tenant_id, LLMType.CHAT.value, chat_id))  # TODO(async-phase4)
        else:
            chat_config = await db.run_sync(lambda s: get_tenant_default_model_by_type(s, tenant_id, LLMType.CHAT))  # TODO(async-phase4)
        chat_mdl = await db.run_sync(lambda s: LLMBundle(s, tenant_id, chat_config))  # TODO(async-phase4)
        chat_mdl.db = None  # run_sync 的 facade 不得逸出 greenlet（AGENTS.md 规约）

        gen_conf = search_config.get("llm_setting", {"temperature": 0.9})
        if "parameter" in gen_conf:
            del gen_conf["parameter"]
        prompt = load_prompt("related_question")
        ans = await chat_mdl.async_chat(
            prompt,
            [
                {
                    "role": "user",
                    "content": f"\nKeywords: {question}\nRelated search terms:\n    ",
                }
            ],
            gen_conf,
        )
        related_terms = [re.sub(r"^[0-9]\. ", "", item) for item in ans.split("\n") if re.match(r"^[0-9]\. ", item)]
        return get_result(data=related_terms)
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.post("/chats/ask", summary="Ask over datasets")
async def ask(
    request: AskRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    req = request.model_dump()
    search_id = req.get("search_id", "")
    search_config = {}
    if search_id:
        if search_app := SearchService.get_detail(db, search_id):
            search_config = search_app.get("search_config", {})

    async def stream_response():
        try:
            async for ans in async_ask(db, req["question"], req["kb_ids"], tenant_id, search_config=search_config):
                yield "data:" + json.dumps({"code": 0, "message": "", "data": ans}, ensure_ascii=False) + "\n\n"
        except Exception as e:
            yield (
                "data:"
                + json.dumps(
                    {"code": 500, "message": str(e), "data": {"answer": "**ERROR**: " + str(e), "reference": []}},
                    ensure_ascii=False,
                )
                + "\n\n"
            )
        yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"

    resp = StreamingResponse(stream_response(), media_type="text/event-stream")
    resp.headers["Cache-control"] = "no-cache"
    resp.headers["Connection"] = "keep-alive"
    resp.headers["X-Accel-Buffering"] = "no"
    resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
    return resp


@router.post("/chats/{chat_id}/sessions", summary="Create chat session")
def create_session(
    chat_id: str,
    request: CreateSessionRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    if not _owned_chat_exists(db, tenant_id, chat_id):
        return _error("No authorization.", RetCode.AUTHENTICATION_ERROR)

    try:
        req = request.model_dump(exclude_unset=True)
        dialog = DialogService.get_by_id(db, chat_id)
        if not dialog:
            return _error("Chat not found!")

        name = req.get("name", "New session")
        if not isinstance(name, str) or not name.strip():
            return _error("`name` can not be empty.")
        name = name.strip()[:255]

        conv = {
            "id": get_uuid(),
            "dialog_id": chat_id,
            "name": name,
            "message": [{"role": "assistant", "content": (dialog.prompt_config or {}).get("prologue", "")}],
            "user_id": req.get("user_id") or tenant_id,
            "reference": [],
        }
        ConversationService.save(db, **conv)
        saved = ConversationService.get_by_id(db, conv["id"])
        if not saved:
            return _error("Fail to create a session!")
        return get_result(data=build_session_response(saved))
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.get("/chats/{chat_id}/sessions", summary="List chat sessions")
def list_sessions(
    chat_id: str,
    id: str | None = Query(None, description="Session ID filter"),
    name: str | None = Query(None, description="Session name filter"),
    page: int = Query(1, description="Page number"),
    page_size: int = Query(30, description="Items per page; 0 disables pagination"),
    orderby: str = Query("create_time", description="Sort field"),
    desc: bool = Query(True, description="Sort descending"),
    user_id: str | None = Query(None, description="Session user ID filter"),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    if not _owned_chat_exists(db, tenant_id, chat_id):
        return _error("No authorization.", RetCode.AUTHENTICATION_ERROR)

    try:
        items_per_page = int(page_size)
        sessions = ConversationService.get_list(db, chat_id, int(page), items_per_page, orderby, desc, id, name, user_id)
        if items_per_page == 0 and not sessions:
            return get_result(data=[])
        return get_result(data=[build_session_response(session) for session in sessions])
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.get("/chats/{chat_id}/sessions/{session_id}", summary="Get chat session")
def get_session(
    chat_id: str,
    session_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    if not _owned_chat_exists(db, tenant_id, chat_id):
        return _error("No authorization.", RetCode.AUTHENTICATION_ERROR)

    try:
        conv = ConversationService.get_by_id(db, session_id)
        if not conv:
            return _error("Session not found!")
        if conv.dialog_id != chat_id:
            return _error("Session does not belong to this chat!")

        dialog = DialogService.get_by_id(db, chat_id)
        result = build_session_response(conv)
        result["avatar"] = dialog.icon if dialog else ""
        references = result.get("reference") or []
        if isinstance(references, list):
            for ref in references:
                if isinstance(ref, dict):
                    ref["chunks"] = chunks_format(ref)
        return get_result(data=result)
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.put("/chats/{chat_id}/sessions/{session_id}", summary="Update chat session")
def update_session(
    chat_id: str,
    session_id: str,
    request: UpdateSessionRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    if not _owned_chat_exists(db, tenant_id, chat_id):
        return _error("No authorization.", RetCode.AUTHENTICATION_ERROR)

    try:
        req = request.model_dump(exclude_unset=True)
        if not ConversationService.query(db, id=session_id, dialog_id=chat_id):
            return _error("Session not found!")
        if "message" in req or "messages" in req:
            return _error("`messages` cannot be changed.")
        if "reference" in req:
            return _error("`reference` cannot be changed.")

        name = req.get("name")
        if name is not None:
            if not isinstance(name, str) or not name.strip():
                return _error("`name` can not be empty.")
            req["name"] = name.strip()[:255]

        update_fields = {k: v for k, v in req.items() if k not in {"id", "dialog_id", "chat_id", "user_id"}}
        if not ConversationService.update_by_id(db, session_id, update_fields):
            return _error("Session not found!")
        conv = ConversationService.get_by_id(db, session_id)
        if not conv:
            return _error("Fail to update a session!")
        return get_result(data=build_session_response(conv))
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.delete("/chats/{chat_id}/sessions", summary="Delete chat sessions")
def delete_sessions(
    chat_id: str,
    request: DeleteSessionsRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    if not _owned_chat_exists(db, tenant_id, chat_id):
        return _error("No authorization.", RetCode.AUTHENTICATION_ERROR)

    try:
        req = request.model_dump()
        session_ids = req.get("ids")
        if not session_ids:
            if req.get("delete_all") is True:
                session_ids = [conv.id for conv in ConversationService.query(db, dialog_id=chat_id)]
                if not session_ids:
                    return get_result(data={})
            else:
                return get_result(data={})

        unique_ids, duplicate_messages = check_duplicate_ids(session_ids, "session")
        errors: list[str] = []
        success_count = 0
        for sid in unique_ids:
            if not ConversationService.query(db, id=sid, dialog_id=chat_id):
                errors.append(f"The chat doesn't own the session {sid}")
                continue
            success_count += ConversationService.delete_by_id(db, sid)

        all_errors = errors + duplicate_messages
        if all_errors:
            if success_count > 0:
                return get_result(
                    data={"success_count": success_count, "errors": all_errors},
                    retmsg=f"Partially deleted {success_count} sessions with {len(all_errors)} errors",
                )
            return _error("; ".join(all_errors))
        return get_result(data=True)
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.delete(
    "/chats/{chat_id}/sessions/{session_id}/messages/{msg_id}",
    summary="Delete chat session message",
)
def delete_session_message(
    chat_id: str,
    session_id: str,
    msg_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    if not _owned_chat_exists(db, tenant_id, chat_id):
        return _error("No authorization.", RetCode.AUTHENTICATION_ERROR)

    try:
        conv = ConversationService.get_by_id(db, session_id)
        if not conv or conv.dialog_id != chat_id:
            return _error("Session not found!")

        payload = conv.to_dict()
        messages = payload.get("message") or []
        references = payload.get("reference") or []
        for index, msg in enumerate(messages):
            if msg_id != msg.get("id", ""):
                continue
            if index + 1 < len(messages) and messages[index + 1].get("id") == msg_id:
                messages.pop(index + 1)
            messages.pop(index)
            ref_index = max(0, index // 2 - 1)
            if ref_index < len(references):
                references.pop(ref_index)
            break

        ConversationService.update_by_id(db, payload["id"], payload)
        return get_result(data=build_session_response(payload))
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.put(
    "/chats/{chat_id}/sessions/{session_id}/messages/{msg_id}/feedback",
    summary="Update chat session message feedback",
)
def update_message_feedback(
    chat_id: str,
    session_id: str,
    msg_id: str,
    request: ChatRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(current_tenant_id),
):
    if not _owned_chat_exists(db, tenant_id, chat_id):
        return _error("No authorization.", RetCode.AUTHENTICATION_ERROR)

    try:
        req = request.model_dump(exclude_unset=True)
        conv = ConversationService.get_by_id(db, session_id)
        if not conv or conv.dialog_id != chat_id:
            return _error("Session not found!")

        payload = conv.to_dict()
        thumbup = req.get("thumbup")
        if not isinstance(thumbup, bool):
            return _error("thumbup must be a boolean")
        feedback = req.get("feedback", "")
        apply_feedback_to_session_payload(tenant_id, payload, msg_id, thumbup, feedback)

        ConversationService.update_by_id(db, payload["id"], payload)
        return get_result(data=build_session_response(payload))
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")


@router.post(
    "/chats/{chat_id}/sessions/{session_id}/completions",
    summary="Complete chat session",
)
async def session_completion(
    chat_id: str,
    session_id: str,
    request: SessionCompletionRequest,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    if not await db.run_sync(lambda s: _owned_chat_exists(s, tenant_id, chat_id)):  # TODO(async-phase4)
        return _error("No authorization.", RetCode.AUTHENTICATION_ERROR)

    req = request.model_dump(exclude_unset=True)
    raw_messages = req.pop("messages", [])
    messages = []
    for message in raw_messages:
        if message.get("role") == "system":
            continue
        if message.get("role") == "assistant" and not messages:
            continue
        messages.append(message)
    if not messages:
        return _error("No valid messages found!")
    if messages[-1].get("role") != "user":
        return _error("The last content of this conversation is not from user.")
    if not messages[-1].get("id"):
        messages[-1]["id"] = get_uuid()

    message_id = messages[-1].get("id")
    chat_model_id = req.pop("llm_id", "")
    stream_mode = req.pop("stream", True)

    chat_model_config = {}
    for model_config in ["temperature", "top_p", "frequency_penalty", "presence_penalty", "max_tokens"]:
        config = req.get(model_config)
        if config:
            chat_model_config[model_config] = config

    try:
        conv = await db.run_sync(lambda s: ConversationService.get_by_id(s, session_id))  # TODO(async-phase4)
        if not conv:
            return _error("Session not found!")
        if conv.dialog_id != chat_id:
            return _error("Session does not belong to this chat!")

        dialog = await db.run_sync(lambda s: DialogService.get_by_id(s, chat_id))  # TODO(async-phase4)
        if not dialog:
            return _error("Chat not found!")

        conv.message = deepcopy(raw_messages)
        if not conv.reference:
            conv.reference = []
        conv.reference = [reference for reference in conv.reference if reference]
        conv.reference.append({"chunks": [], "doc_aggs": []})

        if chat_model_id:
            try:
                override_model_type = await asyncio.to_thread(TenantLLMService.llm_id2llm_type, chat_model_id)  # TODO(async-phase4): DB 兜底自开同步连接
                model_type = LLMType.IMAGE2TEXT.value if override_model_type == "image2text" else LLMType.CHAT.value
                override_model_config = await db.run_sync(lambda s: get_model_config_by_type_and_name(s, dialog.tenant_id, model_type, chat_model_id))  # TODO(async-phase4)
            except Exception:
                return _error(f"Cannot use specified model {chat_model_id}.")
            dialog.llm_id = chat_model_id
            dialog.tenant_llm_id = override_model_config.get("id")
            dialog.llm_setting = chat_model_config

        is_embedded = bool(chat_model_id)

        async def stream_response():
            try:
                async for ans in async_chat(dialog, messages, db, True, **req):
                    ans = structure_answer(conv, ans, message_id, conv.id)
                    yield "data:" + json.dumps({"code": 0, "message": "", "data": ans}, ensure_ascii=False) + "\n\n"
                if not is_embedded:
                    await db.run_sync(lambda s: ConversationService.update_by_id(s, conv.id, conv.to_dict()))  # TODO(async-phase4)
            except Exception as e:
                logger.exception(e)
                yield (
                    "data:"
                    + json.dumps(
                        {
                            "code": 500,
                            "message": str(e),
                            "data": {"answer": "**ERROR**: " + str(e), "reference": []},
                        },
                        ensure_ascii=False,
                    )
                    + "\n\n"
                )
            yield "data:" + json.dumps({"code": 0, "message": "", "data": True}, ensure_ascii=False) + "\n\n"

        if stream_mode:
            resp = StreamingResponse(stream_response(), media_type="text/event-stream")
            resp.headers["Cache-control"] = "no-cache"
            resp.headers["Connection"] = "keep-alive"
            resp.headers["X-Accel-Buffering"] = "no"
            resp.headers["Content-Type"] = "text/event-stream; charset=utf-8"
            return resp

        answer = None
        async for ans in async_chat(dialog, messages, db, False, **req):
            answer = structure_answer(conv, ans, message_id, conv.id)
            if not is_embedded:
                await db.run_sync(lambda s: ConversationService.update_by_id(s, conv.id, conv.to_dict()))  # TODO(async-phase4)
            break
        return get_result(data=answer)
    except Exception as e:
        logger.exception(e)
        return _error("Internal server error")
