from __future__ import annotations

import asyncio
import base64
import datetime
import re
from typing import Any

import xxhash
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import db_connection, get_async_db
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.utils.api_utils import async_current_tenant_id, check_duplicate_ids, get_error_data_result, get_result, server_error_response
from api.utils.image_utils import store_chunk_image
from common import settings
from common.constants import LLMType, ParserType, RetCode
from common.string_utils import is_content_empty, remove_redundant_spaces
from common.tag_feature_utils import validate_tag_features
from core.app.qa import beAdoc, rmPrefix
from core.nlp import rag_tokenizer, search

router = APIRouter()


class Chunk(BaseModel):
    id: str = ""
    content: str = ""
    document_id: str = ""
    docnm_kwd: str = ""
    important_keywords: list[str] = Field(default_factory=list)
    tag_kwd: list[str] = Field(default_factory=list)
    tag_feas: Any = Field(default_factory=dict)
    questions: list[str] = Field(default_factory=list)
    question_tks: str = ""
    image_id: str = ""
    available: bool = True
    positions: list[list[int]] = Field(default_factory=list)

    @field_validator("positions")
    @classmethod
    def validate_positions(cls, value: list[list[int]]) -> list[list[int]]:
        if any(len(position) != 5 for position in value):
            raise ValueError("Each sublist in positions must have a length of 5")
        return value


class AddChunkRequest(BaseModel):
    content: str
    important_keywords: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    tag_kwd: list[str] = Field(default_factory=list)
    tag_feas: Any = Field(default_factory=dict)
    image_base64: str | None = None


class UpdateChunkRequest(BaseModel):
    content: str | None = None
    important_keywords: list[str] | None = None
    questions: list[str] | None = None
    available: bool | None = None
    positions: list[list[int]] | None = None
    tag_kwd: list[str] | None = None
    tag_feas: Any | None = None
    image_base64: str | None = None


class DeleteChunksRequest(BaseModel):
    chunk_ids: list[str] | None = None
    delete_all: bool = False


class SwitchChunksRequest(BaseModel):
    chunk_ids: list[str]
    available_int: int | None = None
    available: bool | None = None


def _map_doc(doc: dict[str, Any]) -> dict[str, Any]:
    key_mapping = {
        "chunk_num": "chunk_count",
        "kb_id": "dataset_id",
        "token_num": "token_count",
        "parser_id": "chunk_method",
    }
    run_mapping = {"0": "UNSTART", "1": "RUNNING", "2": "CANCEL", "3": "DONE", "4": "FAIL"}
    renamed = {key_mapping.get(key, key): value for key, value in doc.items()}
    if "run" in doc:
        renamed["run"] = run_mapping.get(str(doc["run"]))
    return renamed


def _strip_chunk_runtime_fields(chunk: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in chunk.items() if not re.search(r"(_vec$|_sm_|_tks|_ltks)", name)}


def _chunk_payload(chunk_id: str, chunk: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "id": chunk_id,
        "content": chunk.get("content_with_weight", ""),
        "document_id": chunk.get("doc_id", chunk.get("document_id", "")),
        "docnm_kwd": chunk.get("docnm_kwd", ""),
        "important_keywords": chunk.get("important_kwd", []),
        "questions": chunk.get("question_kwd", []),
        "dataset_id": chunk.get("kb_id", chunk.get("dataset_id")),
        "image_id": chunk.get("img_id", ""),
        "available": bool(int(chunk.get("available_int", 1))),
        "positions": chunk.get("position_int", []),
        "tag_kwd": chunk.get("tag_kwd", []),
        "tag_feas": chunk.get("tag_feas", {}),
    }
    return Chunk(**payload).model_dump()


def _read_context(db: Session, user_id: str, dataset_id: str, document_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not KnowledgebaseService.accessible(db, dataset_id, user_id):
        return None
    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    docs = DocumentService.query(db, id=document_id, kb_id=dataset_id)
    if not kb or not docs:
        return None
    return kb.to_dict(), DocumentService.serialize_document(db, docs[0])


def _write_context(db: Session, user_id: str, dataset_id: str, document_id: str) -> tuple[Any, Any] | None:
    if not KnowledgebaseService.accessible(db, dataset_id, user_id):
        return None
    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    docs = DocumentService.query(db, id=document_id, kb_id=dataset_id)
    if not kb or not docs:
        return None
    return kb, docs[0]


def _index_name(kb: Any) -> list[str]:
    return search.index_name(kb.tenant_id, [kb.name])


def _embedding_model(db: Session, kb: Any) -> LLMBundle:
    if kb.tenant_embd_id:
        config = get_model_config_by_id(db, kb.tenant_embd_id)
    else:
        config = get_model_config_by_type_and_name(db, kb.tenant_id, LLMType.EMBEDDING.value, kb.embd_id)
    return LLMBundle(db, kb.tenant_id, config)


@router.get("/datasets/{dataset_id}/documents/{document_id}/chunks", summary="获取文档分块列表")
async def list_chunks(
    dataset_id: str,
    document_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    keywords: str | None = Query(None),
    id: str | None = Query(None, description="Chunk ID to retrieve a specific chunk"),
    available: bool | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(async_current_tenant_id),
) -> JSONResponse:
    context = await db.run_sync(lambda session: _read_context(session, user_id, dataset_id, document_id))  # TODO(async-phase4)
    if context is None:
        return get_error_data_result(retmsg=f"You don't own the dataset or document {dataset_id}/{document_id}.")
    kb, doc = context
    index_name = search.index_name(str(kb["tenant_id"]), [str(kb["name"])])
    result: dict[str, Any] = {"total": 0, "chunks": [], "doc": _map_doc(doc)}
    if id:
        chunk = await asyncio.to_thread(settings.docStoreConn.get, id, index_name, [dataset_id])
        if not chunk or str(chunk.get("doc_id", chunk.get("document_id"))) != document_id:
            return get_result(retcode=RetCode.DATA_ERROR, retmsg=f"Chunk not found: {dataset_id}/{id}")
        result["total"] = 1
        result["chunks"] = [_chunk_payload(id, chunk)]
        return get_result(data=result)

    exists = await asyncio.to_thread(settings.docStoreConn.index_exist, index_name, dataset_id)
    if not exists:
        return get_result(data=result)
    query: dict[str, Any] = {
        "doc_ids": [document_id],
        "page": page,
        "size": page_size,
        "question": keywords or "",
        "sort": True,
    }
    if available is not None:
        query["available_int"] = int(available)
    search_result = await settings.retriever.search(query, index_name, [dataset_id], emb_mdl=None, highlight=True)
    result["total"] = search_result.total
    chunks = []
    for chunk_id in search_result.ids:
        source = search_result.field[chunk_id]
        if keywords and chunk_id in search_result.highlight:
            source = {**source, "content_with_weight": remove_redundant_spaces(search_result.highlight[chunk_id])}
        chunks.append(_chunk_payload(chunk_id, source))
    result["chunks"] = chunks
    return get_result(data=result)


@router.get("/datasets/{dataset_id}/documents/{document_id}/chunks/{chunk_id}", summary="获取文档分块")
async def get_chunk(
    dataset_id: str,
    document_id: str,
    chunk_id: str,
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(async_current_tenant_id),
) -> JSONResponse:
    context = await db.run_sync(lambda session: _read_context(session, user_id, dataset_id, document_id))  # TODO(async-phase4)
    if context is None:
        return get_error_data_result(retmsg=f"You don't own the dataset or document {dataset_id}/{document_id}.")
    kb, _ = context
    index_name = search.index_name(str(kb["tenant_id"]), [str(kb["name"])])
    chunk = await asyncio.to_thread(settings.docStoreConn.get, chunk_id, index_name, [dataset_id])
    if not chunk or str(chunk.get("doc_id", chunk.get("document_id"))) != document_id:
        return get_result(data=False, retcode=RetCode.DATA_ERROR, retmsg="Chunk not found!")
    return get_result(data=_strip_chunk_runtime_fields(chunk))


def _add_chunk(user_id: str, dataset_id: str, document_id: str, request: AddChunkRequest) -> JSONResponse:
    with db_connection() as db:
        context = _write_context(db, user_id, dataset_id, document_id)
        if context is None:
            return get_error_data_result(retmsg="You don't own the dataset or document.")
        kb, doc = context
        req = request.model_dump()
        if is_content_empty(req["content"]):
            return get_error_data_result(retmsg="`content` is required")
        chunk_id = xxhash.xxh64((req["content"] + document_id).encode()).hexdigest()
        tokenized = rag_tokenizer.tokenize(req["content"])
        chunk = {
            "id": chunk_id,
            "content_ltks": tokenized,
            "content_sm_ltks": rag_tokenizer.fine_grained_tokenize(tokenized),
            "content_with_weight": req["content"],
            "important_kwd": req["important_keywords"],
            "important_tks": rag_tokenizer.tokenize(" ".join(req["important_keywords"])),
            "question_kwd": [str(question).strip() for question in req["questions"] if str(question).strip()],
            "question_tks": rag_tokenizer.tokenize("\n".join(req["questions"])),
            "tag_kwd": req["tag_kwd"],
            "tag_feas": validate_tag_features(req["tag_feas"]),
            "create_time": str(datetime.datetime.now()).replace("T", " ")[:19],
            "create_timestamp_flt": datetime.datetime.now().timestamp(),
            "kb_id": dataset_id,
            "docnm_kwd": doc.name,
            "doc_id": document_id,
            "available_int": 1,
        }
        if req["image_base64"]:
            chunk.update({"img_id": f"{dataset_id}-{chunk_id}", "doc_type_kwd": "image"})
        model = _embedding_model(db, kb)
        vectors, token_count = model.encode([doc.name, "\n".join(chunk["question_kwd"]) or req["content"]])
        vector = 0.1 * vectors[0] + 0.9 * vectors[1]
        chunk[f"q_{len(vector)}_vec"] = vector.tolist()
        settings.docStoreConn.insert([chunk], _index_name(kb), dataset_id)
        if req["image_base64"]:
            store_chunk_image(dataset_id, chunk_id, base64.b64decode(req["image_base64"]))
        DocumentService.increment_chunk_num(db, document_id, dataset_id, token_count, 1, 0)
        return get_result(data={"chunk": _chunk_payload(chunk_id, chunk)})


@router.post("/datasets/{dataset_id}/documents/{document_id}/chunks", summary="添加文档分块")
async def add_chunk(
    dataset_id: str,
    document_id: str,
    request: AddChunkRequest,
    user_id: str = Depends(async_current_tenant_id),
) -> JSONResponse:
    try:
        return await asyncio.to_thread(_add_chunk, user_id, dataset_id, document_id, request)
    except Exception as exc:
        return server_error_response(exc)


def _remove_chunks(user_id: str, dataset_id: str, document_id: str, request: DeleteChunksRequest) -> JSONResponse:
    with db_connection() as db:
        context = _write_context(db, user_id, dataset_id, document_id)
        if context is None:
            return get_error_data_result(retmsg="You don't own the dataset or document.")
        kb, doc = context
        index_name = _index_name(kb)
        if not request.chunk_ids:
            if not request.delete_all:
                return get_result()
            DocumentService.delete_chunk_images(doc, index_name)
            deleted = settings.docStoreConn.delete({"doc_id": document_id}, index_name, dataset_id)
            if deleted:
                DocumentService.decrement_chunk_num(db, document_id, dataset_id, 1, deleted, 0)
            return get_result(retmsg=f"deleted {deleted} chunks")
        unique_ids, duplicate_messages = check_duplicate_ids(request.chunk_ids, "chunk")
        deleted = settings.docStoreConn.delete({"doc_id": document_id, "id": unique_ids}, index_name, dataset_id)
        if deleted:
            DocumentService.decrement_chunk_num(db, document_id, dataset_id, 1, deleted, 0)
        if deleted != len(unique_ids):
            return get_error_data_result(retmsg=f"rm_chunk deleted chunks {deleted}, expect {len(unique_ids)}")
        if duplicate_messages:
            return get_result(
                retmsg=f"Partially deleted {deleted} chunks with {len(duplicate_messages)} errors",
                data={"success_count": deleted, "errors": duplicate_messages},
            )
        return get_result(retmsg=f"deleted {deleted} chunks")


@router.delete("/datasets/{dataset_id}/documents/{document_id}/chunks", summary="批量删除文档分块")
async def remove_chunks(
    dataset_id: str,
    document_id: str,
    request: DeleteChunksRequest,
    user_id: str = Depends(async_current_tenant_id),
) -> JSONResponse:
    try:
        return await asyncio.to_thread(_remove_chunks, user_id, dataset_id, document_id, request)
    except Exception as exc:
        return server_error_response(exc)


def _update_chunk(user_id: str, dataset_id: str, document_id: str, chunk_id: str, request: UpdateChunkRequest) -> JSONResponse:
    with db_connection() as db:
        context = _write_context(db, user_id, dataset_id, document_id)
        if context is None:
            return get_error_data_result(retmsg="You don't own the dataset or document.")
        kb, doc = context
        index_name = _index_name(kb)
        current = settings.docStoreConn.get(chunk_id, index_name, [dataset_id])
        if not current or str(current.get("doc_id", current.get("document_id"))) != document_id:
            return get_error_data_result(retmsg=f"Can't find this chunk {chunk_id}")
        req = request.model_dump(exclude_unset=True)
        content = req.get("content", current.get("content_with_weight", ""))
        if is_content_empty(content):
            return get_error_data_result(retmsg="`content` is required")
        tokenized = rag_tokenizer.tokenize(content)
        patch: dict[str, Any] = {
            "id": chunk_id,
            "content_with_weight": content,
            "content_ltks": tokenized,
            "content_sm_ltks": rag_tokenizer.fine_grained_tokenize(tokenized),
            "update_time": str(datetime.datetime.now()).replace("T", " ")[:19],
            "update_timestamp_flt": datetime.datetime.now().timestamp(),
        }
        if "important_keywords" in req:
            patch["important_kwd"] = req["important_keywords"]
            patch["important_tks"] = rag_tokenizer.tokenize(" ".join(req["important_keywords"]))
        if "questions" in req:
            patch["question_kwd"] = [str(question).strip() for question in req["questions"] if str(question).strip()]
            patch["question_tks"] = rag_tokenizer.tokenize("\n".join(req["questions"]))
        if "available" in req:
            patch["available_int"] = int(req["available"])
        if "positions" in req:
            patch["position_int"] = req["positions"]
        if "tag_kwd" in req:
            patch["tag_kwd"] = req["tag_kwd"]
        if "tag_feas" in req:
            patch["tag_feas"] = validate_tag_features(req["tag_feas"])
        if req.get("image_base64"):
            patch.update({"img_id": f"{dataset_id}-{chunk_id}", "doc_type_kwd": "image"})
        if doc.parser_id == ParserType.QA:
            parts = [part for part in re.split(r"[\n\t]", content) if len(part) > 1]
            if len(parts) != 2:
                return get_error_data_result(retmsg="Q&A must be separated by TAB/ENTER key.")
            question, answer = rmPrefix(parts[0]), rmPrefix(parts[1])
            patch = beAdoc(patch, parts[0], parts[1], not any(rag_tokenizer.is_chinese(text) for text in question + answer))
        model = _embedding_model(db, kb)
        questions = patch.get("question_kwd", current.get("question_kwd", []))
        vectors, _ = model.encode([doc.name, "\n".join(questions) or content])
        vector = vectors[1] if doc.parser_id == ParserType.QA else 0.1 * vectors[0] + 0.9 * vectors[1]
        patch[f"q_{len(vector)}_vec"] = vector.tolist()
        if not settings.docStoreConn.update({"id": chunk_id}, patch, index_name, dataset_id):
            return get_error_data_result(retmsg="Index updating failure")
        if req.get("image_base64"):
            store_chunk_image(dataset_id, chunk_id, base64.b64decode(req["image_base64"]))
        return get_result()


@router.patch("/datasets/{dataset_id}/documents/{document_id}/chunks/{chunk_id}", summary="更新文档分块")
async def update_chunk(
    dataset_id: str,
    document_id: str,
    chunk_id: str,
    request: UpdateChunkRequest,
    user_id: str = Depends(async_current_tenant_id),
) -> JSONResponse:
    try:
        return await asyncio.to_thread(_update_chunk, user_id, dataset_id, document_id, chunk_id, request)
    except Exception as exc:
        return server_error_response(exc)


def _switch_chunks(user_id: str, dataset_id: str, document_id: str, request: SwitchChunksRequest) -> JSONResponse:
    with db_connection() as db:
        context = _write_context(db, user_id, dataset_id, document_id)
        if context is None:
            return get_error_data_result(retmsg="You don't own the dataset or document.")
        kb, _ = context
        if not request.chunk_ids:
            return get_error_data_result(retmsg="`chunk_ids` is required.")
        if request.available_int is None and request.available is None:
            return get_error_data_result(retmsg="`available_int` or `available` is required.")
        available = request.available_int if request.available_int is not None else int(bool(request.available))
        for chunk_id in request.chunk_ids:
            if not settings.docStoreConn.update({"id": chunk_id}, {"available_int": available}, _index_name(kb), dataset_id):
                return get_error_data_result(retmsg="Index updating failure")
        return get_result(data=True)


@router.patch("/datasets/{dataset_id}/documents/{document_id}/chunks", summary="切换分块可用状态")
async def switch_chunks(
    dataset_id: str,
    document_id: str,
    request: SwitchChunksRequest,
    user_id: str = Depends(async_current_tenant_id),
) -> JSONResponse:
    try:
        return await asyncio.to_thread(_switch_chunks, user_id, dataset_id, document_id, request)
    except Exception as exc:
        return server_error_response(exc)
