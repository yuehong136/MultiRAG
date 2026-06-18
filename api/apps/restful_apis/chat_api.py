# coding=utf-8
"""RESTful chat management API.

Routes are mounted under ``/api/v1`` by ``api.apps.register_page``.
The legacy ``/v1/dialog/*`` endpoints stay in ``api/apps/dialog_app.py``.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from api.db.db_models import get_db
from api.db.services.dialog_service import DialogService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.user_service import TenantService, UserTenantService
from api.utils.api_utils import check_duplicate_ids, current_tenant_id, get_error_data_result, get_result
from api.utils.tenant_utils import ensure_tenant_model_id_for_params
from common.constants import RetCode, StatusEnum
from common.misc_utils import get_uuid

router = APIRouter()
logger = logging.getLogger(__name__)


_DEFAULT_PROMPT_CONFIG = {
    "system": (
        'You are an intelligent assistant. Please summarize the content of the dataset to answer the question. '
        'Please list the data in the dataset and answer in detail. When all dataset content is irrelevant to the '
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
    return (
        f"Unsupported legacy chat payload fields: {fields}. "
        "Use icon, llm_id, llm_setting, prompt_config, and do_refer."
    )


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

    embd_ids = [
        kb.tenant_embd_id or TenantLLMService.split_model_name_and_factory(kb.embd_id)[0]
        for kb in kbs
    ]
    if len(set(embd_ids)) > 1:
        return f'Datasets use different embedding models: {[kb.embd_id for kb in kbs]}'
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
            chats, _ = DialogService.get_by_tenant_ids(
                db, owner_ids, tenant_id, 0, 0, orderby, desc, exact_keywords, id=id, name=name
            )
            chats = [chat for chat in chats if chat.get("tenant_id") in owner_ids]
            total = len(chats)
            if page and page_size:
                start = (page - 1) * page_size
                chats = chats[start:start + page_size]
        else:
            chats, total = DialogService.get_by_tenant_ids(
                db, [], tenant_id, page, page_size, orderby, desc, exact_keywords, id=id, name=name
            )
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
        ok, payload = _prepare_update_payload(
            db, tenant_id, chat_id, request.model_dump(exclude_unset=True), merge_nested=False
        )
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
        ok, payload = _prepare_update_payload(
            db, tenant_id, chat_id, request.model_dump(exclude_unset=True), merge_nested=True
        )
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
                ids = [
                    chat.id
                    for chat in DialogService.query(db, tenant_id=tenant_id, status=StatusEnum.VALID.value)
                ]
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
