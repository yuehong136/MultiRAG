from __future__ import annotations

import asyncio
import base64
import datetime
import logging
import re
from typing import Annotated, Any, Literal

import xxhash
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Discriminator, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import Document, Task, db_connection, get_async_db, get_db
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name, get_tenant_default_model_by_type
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_image_lock import image_reference_key, image_write_locks
from api.db.services.document_image_service import _raster
from api.db.services.document_service import DocumentService
from api.db.services.document_status_service import insert_source_chunks
from api.db.services.document_task_service import reconcile_task_chunk_count
from api.db.services.file2document_service import File2DocumentService
from api.db.services.knowledgebase_service import EmbeddingModelMismatchError, KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.db.services.task_service import TaskService, cancel_all_task_of, queue_tasks
from api.utils.api_utils import async_current_tenant_id, check_duplicate_ids, construct_json_result, get_error_data_result, get_result, server_error_response, token_required
from api.utils.image_utils import read_chunk_image, replace_chunk_image, store_chunk_image
from api.utils.pagination_utils import REST_API_MAX_PAGE_SIZE, validate_rest_api_pagination
from api.utils.reference_metadata import ReferenceMetadata, enrich_reference_metadata, resolve_reference_metadata_preferences
from common import settings
from common.constants import LLMType, ParserType, RetCode, TaskStatus
from common.metadata_utils import apply_meta_data_filter, convert_conditions
from common.string_utils import is_content_empty, remove_redundant_spaces
from common.tag_feature_utils import validate_tag_features
from core.app.tag import label_question
from core.nlp import search
from core.prompts.generator import cross_languages, keyword_extraction

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
    image_base64: str | None = Field(
        default=None,
        description="Pure Base64 of a raster image. Omitted, null or empty preserves the image unless image_update_mode is explicitly supplied.",
    )
    image_update_mode: Literal["append", "replace"] = Field(
        default="append", description="append (default) stacks images vertically; replace stores only the new image. Both require image_base64 when explicit."
    )

    @field_validator("image_update_mode", mode="before")
    @classmethod
    def normalize_image_update_mode(cls, value: Any) -> Any:
        return value.strip().lower() if isinstance(value, str) else value


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
    return {name: float(value) if name.endswith("_flt") and value is not None else value for name, value in chunk.items() if name != "vector" and not re.search(r"(_vec$|_sm_|_tks|_ltks)", name)}


def _chunk_payload(chunk_id: str, chunk: dict[str, Any]) -> dict[str, Any]:
    available = chunk.get("available_int")
    payload = {
        "id": chunk_id,
        "content": chunk.get("content_with_weight", ""),
        "document_id": chunk.get("doc_id", chunk.get("document_id", "")),
        "docnm_kwd": chunk.get("docnm_kwd", ""),
        "important_keywords": chunk.get("important_kwd", []),
        "questions": chunk.get("question_kwd", []),
        "dataset_id": chunk.get("kb_id", chunk.get("dataset_id")),
        "image_id": chunk.get("img_id", ""),
        "available": True if available is None else bool(int(available)),
        "positions": chunk.get("position_int", []),
        "tag_kwd": chunk.get("tag_kwd", []),
        "tag_feas": chunk.get("tag_feas", {}),
    }
    # Declared nullable Milvus fields represent the same unset values as
    # omitted dynamic fields. Keep explicit zero/empty values for validation.
    return Chunk(**{name: value for name, value in payload.items() if value is not None}).model_dump()


def _read_context(db: Session, user_id: str, dataset_id: str, document_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    if not KnowledgebaseService.accessible(db, dataset_id, user_id):
        return None
    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    docs = DocumentService.query(db, id=document_id, kb_id=dataset_id)
    if not kb or not docs:
        return None
    return kb.to_dict(), DocumentService.serialize_document(db, docs[0])


def _write_context(db: Session, user_id: str, dataset_id: str, document_id: str, *, lock_document: bool = False) -> tuple[Any, Any] | None:
    if not KnowledgebaseService.accessible(db, dataset_id, user_id):
        return None
    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    doc = (
        db.scalar(select(Document).where(Document.id == document_id, Document.kb_id == dataset_id).with_for_update().execution_options(populate_existing=True))
        if lock_document
        else next(iter(DocumentService.query(db, id=document_id, kb_id=dataset_id)), None)
    )
    if not kb or doc is None:
        return None
    return kb, doc


def _index_name(kb: Any) -> list[str]:
    from core.nlp import search

    return search.index_name(kb.tenant_id, [kb.name])


def _embedding_model(db: Session, kb: Any) -> LLMBundle:
    if kb.tenant_embd_id:
        config = get_model_config_by_id(db, kb.tenant_embd_id, tenant_id=kb.tenant_id, model_type="embedding")
    else:
        config = get_model_config_by_type_and_name(db, kb.tenant_id, LLMType.EMBEDDING.value, kb.embd_id)
    return LLMBundle(db, kb.tenant_id, config)


@router.get("/datasets/{dataset_id}/documents/{document_id}/chunks", summary="获取文档分块列表", dependencies=[Depends(validate_rest_api_pagination)])
async def list_chunks(
    dataset_id: str,
    document_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=REST_API_MAX_PAGE_SIZE),
    keywords: str | None = Query(None),
    id: str | None = Query(None, description="Chunk ID to retrieve a specific chunk"),
    available: bool | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
    user_id: str = Depends(async_current_tenant_id),
) -> JSONResponse:
    context = await db.run_sync(lambda session: _read_context(session, user_id, dataset_id, document_id))  # TODO(async-phase4)
    if context is None:
        return get_error_data_result(retmsg=f"You don't own the dataset or document {dataset_id}/{document_id}.")
    from core.nlp import search

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
    from core.nlp import search

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
        from core.nlp import rag_tokenizer

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
        # Provider I/O can release the read transaction. Revalidate and hold
        # the document through native upsert and SQL accounting, including
        # concurrent requests that derive the same content-based chunk ID.
        context = _write_context(db, user_id, dataset_id, document_id, lock_document=True)
        if context is None:
            return get_error_data_result(retmsg="You don't own the dataset or document.")
        kb, doc = context
        index_name = _index_name(kb)
        # Bind the insertion helper to this transaction: its internal Session
        # must not open a second connection waiting on our document lock.
        insert_source_chunks(db.connection(), [chunk], index_name, dataset_id)
        if req["image_base64"]:
            store_chunk_image(dataset_id, chunk_id, base64.b64decode(req["image_base64"]))
        # Use the producer's logical source accounting (excluding parents),
        # so a retry repairs a previous native success followed by SQL failure.
        reconcile_task_chunk_count(db, document_id, dataset_id, index_name[0])
        DocumentService.increment_chunk_num(db, document_id, dataset_id, token_count, 0, 0)
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
        context = _write_context(db, user_id, dataset_id, document_id, lock_document=True)
        if context is None:
            return get_error_data_result(retmsg="You don't own the dataset or document.")
        kb, doc = context
        index_name = _index_name(kb)
        if not request.chunk_ids:
            if not request.delete_all:
                return get_result()
            DocumentService.delete_chunk_images(doc, index_name[0])
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
        context = _write_context(db, user_id, dataset_id, document_id, lock_document=True)
        if context is None:
            return get_error_data_result(retmsg="You don't own the dataset or document.")
        kb, doc = context
        index_name = _index_name(kb)
        current = settings.docStoreConn.get(chunk_id, index_name, [dataset_id])
        if not current or str(current.get("doc_id", current.get("document_id"))) != document_id:
            return get_error_data_result(retmsg=f"Can't find this chunk {chunk_id}")
        req = request.model_dump(exclude_unset=True)
        image_binary = None
        if "image_update_mode" in req and not req.get("image_base64"):
            return get_error_data_result(retmsg="`image_base64` is required when `image_update_mode` is supplied")
        if req.get("image_base64"):
            try:
                # Use the protected read's raster contract before any mutation,
                # so an acknowledged replacement remains readable there.
                image_binary = _raster(base64.b64decode(req["image_base64"], validate=True)).data
            except ValueError:
                return get_error_data_result(retmsg="`image_base64` must be pure Base64 of a valid raster image")
        content = req.get("content", current.get("content_with_weight", ""))
        if is_content_empty(content):
            return get_error_data_result(retmsg="`content` is required")
        from core.nlp import rag_tokenizer

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
            from core.app.qa import beAdoc, rmPrefix

            question, answer = rmPrefix(parts[0]), rmPrefix(parts[1])
            patch = beAdoc(patch, parts[0], parts[1], not any(rag_tokenizer.is_chinese(text) for text in question + answer))
        model = _embedding_model(db, kb)
        questions = patch.get("question_kwd", current.get("question_kwd", []))
        # LLMBundle releases read-only sessions before provider I/O. This
        # session owns the native write's Document lock through completion.
        model.db = None
        vectors, _ = model.encode([doc.name, "\n".join(questions) or content])
        vector = vectors[1] if doc.parser_id == ParserType.QA else 0.1 * vectors[0] + 0.9 * vectors[1]
        patch[f"q_{len(vector)}_vec"] = vector.tolist()
        keys = {key for row in [current, patch] if (key := image_reference_key(row.get("img_id"))) is not None}
        with image_write_locks(db.get_bind(), keys):
            if image_binary is None:
                if not settings.docStoreConn.update({"id": chunk_id}, patch, index_name, dataset_id):
                    return get_error_data_result(retmsg="Index updating failure")
                return get_result()
            try:
                previous_image = read_chunk_image(dataset_id, chunk_id)
            except Exception:
                return _chunk_image_update_failure(dataset_id, chunk_id, stage="image_read", index="not_attempted", image="not_attempted", outcome="unchanged")
            try:
                indexed = settings.docStoreConn.update({"id": chunk_id}, patch, index_name, dataset_id)
            except Exception:
                indexed = False
            if not indexed:
                # False/exception may follow a native delete or a lost reply.
                return _chunk_image_update_failure(dataset_id, chunk_id, stage="index_write", index="unknown", image="not_attempted", outcome="unknown")
            try:
                if request.image_update_mode == "replace":
                    replace_chunk_image(dataset_id, chunk_id, image_binary)
                else:
                    store_chunk_image(dataset_id, chunk_id, image_binary, verify=True)
            except Exception:
                image_state = "unknown"
                try:
                    observed = read_chunk_image(dataset_id, chunk_id)
                    if previous_image is not None and observed == previous_image:
                        image_state = "unchanged"
                    elif observed is not None:
                        image_state = "changed"
                except Exception:
                    image_state = "unknown"  # Never claim rollback or a safe retry.
                return _chunk_image_update_failure(dataset_id, chunk_id, stage="image_write", index="acknowledged", image=image_state, outcome="partial")
        return get_result()


def _chunk_image_update_failure(dataset_id: str, chunk_id: str, *, stage: str, index: str, image: str, outcome: str) -> JSONResponse:
    """Report observed boundaries without treating native stores as a transaction."""
    messages = {
        "image_read": "图片读取未确认；本次未写入切片索引或图片。请刷新切片和图片核对后再重试。",
        "index_write": "切片索引写入未确认，内容可能已改变；尚未写入图片。请刷新切片和图片核对后再决定是否重试，不要盲目重发 append。",
        "image_write": "图片写入未确认；切片索引更新已获确认。请刷新切片和图片核对后再决定是否重试，不要盲目重发 append；需重发图片时使用 replace。",
    }
    # get_result intentionally drops data on failure; this route's envelope
    # carries recovery observations for clients that preserve error payloads.
    return JSONResponse(
        content={
            "code": int(RetCode.EXCEPTION_ERROR),
            "message": messages[stage],
            "data": {"outcome": outcome, "stage": stage, "index": index, "image": image, "image_id": f"{dataset_id}-{chunk_id}", "retry_safe": False},
        }
    )


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
        context = _write_context(db, user_id, dataset_id, document_id, lock_document=True)
        if context is None:
            return get_error_data_result(retmsg="You don't own the dataset or document.")
        kb, _ = context
        if not request.chunk_ids:
            return get_error_data_result(retmsg="`chunk_ids` is required.")
        if request.available_int is None and request.available is None:
            return get_error_data_result(retmsg="`available_int` or `available` is required.")
        available = request.available_int if request.available_int is not None else int(bool(request.available))
        index_name = _index_name(kb)
        for chunk_id in request.chunk_ids:
            current = settings.docStoreConn.get(chunk_id, index_name, [dataset_id])
            if not current or str(current.get("doc_id", current.get("document_id"))) != document_id:
                return get_error_data_result(retmsg=f"Can't find this chunk {chunk_id}")
        for chunk_id in request.chunk_ids:
            if not settings.docStoreConn.update({"id": chunk_id, "doc_id": document_id}, {"available_int": available}, index_name, dataset_id):
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


class SparseSearchMode(BaseModel):
    type: Literal["sparse"] = "sparse"


class DenseSearchMode(BaseModel):
    type: Literal["dense"] = "dense"


class HybridSearchMode(BaseModel):
    type: Literal["hybrid"] = "hybrid"
    weight_dense: float = Field(default=0.7, ge=0.0, le=1.0)
    weight_sparse: float = Field(default=0.3, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_weights(self) -> HybridSearchMode:
        """确保权重和为1，如果不是则自动调整"""
        total = self.weight_dense + self.weight_sparse
        if abs(total - 1.0) > 0.001:
            # 自动归一化权重
            self.weight_dense = self.weight_dense / total
            self.weight_sparse = self.weight_sparse / total
        return self


class FusionSearchMode(BaseModel):
    type: Literal["fusion"] = "fusion"
    weights: str = Field(default="0.05,0.95")

    @model_validator(mode="after")
    def validate_weights_format(self) -> FusionSearchMode:
        """验证weights格式"""
        try:
            parts = self.weights.split(",")
            if len(parts) != 2:
                raise ValueError("weights must contain exactly two comma-separated values")
            float(parts[0].strip())
            float(parts[1].strip())
        except (ValueError, AttributeError):
            raise ValueError("weights must be in format 'float,float' (e.g., '0.05,0.95')")
        return self


# 使用 Discriminator 的高效版本
SearchModeType = Annotated[SparseSearchMode | DenseSearchMode | HybridSearchMode | FusionSearchMode, Discriminator("type")]


class ParseDocumentRequest(BaseModel):
    document_ids: list[str]


class StopParsingRequest(BaseModel):
    document_ids: list[str]


class RetrievalTestRequest(BaseModel):
    reference_metadata: ReferenceMetadata | None = None
    question: str
    dataset_ids: list[str]
    document_ids: list[str] = Field(default_factory=list)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=30, ge=1, le=REST_API_MAX_PAGE_SIZE, strict=True)
    similarity_threshold: float = Field(default=0.2, ge=0.0, le=1.0)
    vector_similarity_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    top_k: int = Field(default=1024, ge=1)
    rerank_id: str | None = None
    tenant_rerank_id: int | None = None
    keyword: bool = Field(default=False)
    highlight: bool = Field(default=False)
    use_kg: bool = Field(default=False)
    cross_languages: list[str] = Field(default_factory=list)
    metadata_condition: dict[str, Any] = Field(default_factory=dict)
    search_mode: SearchModeType | None = None

    def get_search_mode_dict(self) -> dict[str, Any] | None:
        """将搜索模式转换为字典格式供底层函数使用"""
        if self.search_mode is None:
            return None

        mode_data = self.search_mode.model_dump()
        mode_type = mode_data.pop("type")
        return {mode_type: mode_data}


@router.post("/datasets/{dataset_id}/chunks", summary="解析文档")
def parse_documents(dataset_id: str, request: ParseDocumentRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    """
    解析数据集中的文档为chunks

    Args:
        dataset_id: 数据集ID
        request: 解析请求参数
        db: 数据库会话
        tenant_id: 租户ID

    Returns:
        解析任务结果
    """
    req = request.model_dump()

    if not KnowledgebaseService.query(db, id=dataset_id, tenant_id=tenant_id):
        return get_error_data_result(retmsg="You don't own the dataset.")

    document_ids = req["document_ids"]
    if not document_ids:
        return get_error_data_result(retmsg="document_ids is required")

    # 检查重复ID
    unique_doc_ids, duplicate_messages = check_duplicate_ids(document_ids, "document")

    try:
        docs = []
        for doc_id in unique_doc_ids:
            doc = DocumentService.query(db, kb_id=dataset_id, id=doc_id)
            if not doc:
                return get_error_data_result(retmsg=f"Document {doc_id} not found")
            docs.append(doc[0])

        # 队列解析任务
        for doc in docs:
            if doc.status == "0":  # 未启用的文档不解析
                continue
            info = {
                "progress": 0,
                "progress_msg": "",
                "run": TaskStatus.RUNNING.value,
                "chunk_num": 0,
                "token_num": 0,
            }
            if not DocumentService.filter_update(
                db,
                [
                    Document.id == doc.id,
                    (Document.run.is_(None) | (Document.run != TaskStatus.RUNNING.value)),
                ],
                info,
            ):
                return get_error_data_result(retmsg="Can't parse document that is currently being processed")

            settings.docStoreConn.delete({"doc_id": doc.id}, search.index_name(tenant_id), dataset_id)
            TaskService.filter_delete(db, [Task.doc_id == doc.id])
            doc = DocumentService.get_by_id(db, doc.id)
            doc_dict = doc.to_dict()
            doc_dict["tenant_id"] = tenant_id
            bucket, name = File2DocumentService.get_storage_address(db, doc_id=doc_dict["id"])
            queue_tasks(db, doc_dict, bucket, name, 0)

        message = "Documents queued for parsing"
        if duplicate_messages:
            message += f" (with {len(duplicate_messages)} duplicate IDs ignored)"

        return get_result(retmsg=message)
    except Exception as e:
        logging.exception(e)
        return get_error_data_result(retmsg=f"Failed to queue parsing: {e!s}")


DOC_STOP_PARSING_INVALID_STATE_MESSAGE = "Can't stop parsing document that has not started or already completed"
DOC_STOP_PARSING_INVALID_STATE_ERROR_CODE = "DOC_STOP_PARSING_INVALID_STATE"


@router.delete("/datasets/{dataset_id}/chunks", summary="停止解析")
def stop_parsing_documents(dataset_id: str, request: StopParsingRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    """
    停止解析数据集中的文档

    Args:
        dataset_id: 数据集ID
        request: 停止解析请求参数
        db: 数据库会话
        tenant_id: 租户ID

    Returns:
        停止解析结果
    """
    req = request.model_dump()

    if not KnowledgebaseService.query(db, id=dataset_id, tenant_id=tenant_id):
        return get_error_data_result(retmsg="You don't own the dataset.")

    document_ids = req["document_ids"]
    if not document_ids:
        return get_error_data_result(retmsg="document_ids is required")

    # 检查重复ID
    unique_doc_ids, duplicate_messages = check_duplicate_ids(document_ids, "document")

    try:
        success_count = 0
        for doc_id in unique_doc_ids:
            doc = DocumentService.query(db, kb_id=dataset_id, id=doc_id)
            if not doc:
                continue
            if doc[0].run != TaskStatus.RUNNING.value:
                return construct_json_result(
                    code=RetCode.DATA_ERROR,
                    message=DOC_STOP_PARSING_INVALID_STATE_MESSAGE,
                    data={"error_code": DOC_STOP_PARSING_INVALID_STATE_ERROR_CODE},
                )
            # Send cancellation signal via Redis to stop background task
            cancel_all_task_of(doc_id)
            # 更新文档状态为取消
            DocumentService.update_by_id(db, doc_id, {"run": TaskStatus.CANCEL.value, "progress": 0, "progress_msg": "Cancelled by user"})

            # 删除相关任务
            TaskService.filter_delete(db, [Task.doc_id == doc_id])
            success_count += 1

        message = f"Parsing stopped for {success_count} documents"
        if duplicate_messages:
            message += f" (with {len(duplicate_messages)} duplicate IDs ignored)"

        return get_result(retmsg=message)
    except Exception as e:
        logging.exception(e)
        return get_error_data_result(retmsg=f"Failed to stop parsing: {e!s}")


@router.post("/retrieval", summary="检索测试")
async def retrieval_test(request: RetrievalTestRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)) -> JSONResponse:
    """
    测试检索功能

    Args:
        request: 检索测试请求参数
        db: 数据库会话
        tenant_id: 租户ID

    Returns:
        检索结果
    """
    req = request.model_dump()

    # 验证必填参数
    if not req.get("dataset_ids"):
        return get_error_data_result(retmsg="`dataset_ids` is required.")

    kb_ids = req["dataset_ids"]
    if not isinstance(kb_ids, list):
        return get_error_data_result(retmsg="`dataset_ids` should be a list")

    # 验证数据集权限
    for kb_id in kb_ids:
        if not KnowledgebaseService.query(db, id=kb_id, tenant_id=tenant_id):
            return get_error_data_result(retmsg=f"You don't own the dataset {kb_id}.")

    # 获取所有知识库
    kbs = KnowledgebaseService.get_by_ids(db, kb_ids)
    if {kb.id for kb in kbs} != set(kb_ids):
        return get_error_data_result(retmsg="Dataset not found!")
    kb_names = [kb.name for kb in kbs]

    try:
        KnowledgebaseService.ensure_same_embedding_model(kbs)
    except EmbeddingModelMismatchError as e:
        return get_result(
            retmsg=str(e),
            retcode=RetCode.DATA_ERROR,
        )

    # Resolve only after the complete dataset selection is authorized, and
    # before metadata/model work can read credentials or call a provider.
    rerank_config = None
    if request.tenant_rerank_id is not None:
        try:
            rerank_config = get_model_config_by_id(db, request.tenant_rerank_id, allowed_tenant_ids={tenant_id, *[kb.tenant_id for kb in kbs]}, model_type="rerank")
        except LookupError:
            return get_result(retcode=RetCode.AUTHENTICATION_ERROR, retmsg="No authorization for rerank model.")

    if request.use_kg and (request.document_ids or request.metadata_condition):
        return get_result(retcode=RetCode.BAD_REQUEST, retmsg="Knowledge graph enhancement cannot be combined with document or metadata filters.")

    if "question" not in req:
        return get_error_data_result(retmsg="`question` is required.")

    page = int(req.get("page", 1))
    size = int(req.get("page_size", 30))
    question = req["question"]
    # Trim whitespace and validate question
    if isinstance(question, str):
        question = question.strip()
    # Return empty result if question is empty or whitespace-only
    if not question:
        return get_result(data={"total": 0, "chunks": [], "doc_aggs": {}})
    doc_ids = req.get("document_ids", [])
    use_kg = req.get("use_kg", False)
    toc_enhance = req.get("toc_enhance", False)
    langs = req.get("cross_languages", [])
    search_mode_dict = request.get_search_mode_dict()

    if not isinstance(doc_ids, list):
        return get_error_data_result(retmsg="`document_ids` should be a list")

    # 验证文档ID
    if doc_ids:
        doc_ids_list = KnowledgebaseService.list_documents_by_ids(db, kb_ids)
        for doc_id in doc_ids:
            if doc_id not in doc_ids_list:
                return get_error_data_result(retmsg=f"The datasets don't own the document {doc_id}")

    # Metadata conditions narrow the validated document selection, too.
    metadata_condition = req.get("metadata_condition")
    if metadata_condition:
        conditions = convert_conditions(metadata_condition)
        if conditions:

            async def load_metadata() -> dict[str, Any]:
                return DocMetadataService.get_flatted_meta_by_kbs(db, kb_ids)

            async def filter_metadata(filters: list[dict[str, Any]], logic: str) -> list[str] | None:
                return DocMetadataService.filter_doc_ids_by_meta_pushdown(db, kb_ids, filters, logic)

            doc_ids = await apply_meta_data_filter(
                {"method": "manual", "manual": conditions, "logic": metadata_condition.get("logic", "and")},
                None,
                question,
                base_doc_ids=doc_ids if doc_ids else None,
                metas_loader=load_metadata,
                metadata_filterer=filter_metadata,
            )
            if doc_ids == ["-999"]:
                return get_result(data={"total": 0, "chunks": [], "doc_aggs": {}})
        elif not doc_ids:
            doc_ids = ["-999"]
    elif not doc_ids:
        # If doc_ids is None all documents of the datasets are used.
        doc_ids = None
    similarity_threshold = float(req.get("similarity_threshold", 0.2))
    vector_similarity_weight = float(req.get("vector_similarity_weight", 0.3))
    top = int(req.get("top_k", 1024))

    # 处理highlight参数
    highlight_val = req.get("highlight", None)
    if highlight_val is None:
        highlight = False
    elif isinstance(highlight_val, bool):
        highlight = highlight_val
    elif isinstance(highlight_val, str):
        if highlight_val.lower() in ["true", "false"]:
            highlight = highlight_val.lower() == "true"
        else:
            return get_error_data_result(retmsg="`highlight` should be a boolean")
    else:
        return get_error_data_result(retmsg="`highlight` should be a boolean")

    try:
        tenant_ids = list({kb.tenant_id for kb in kbs})
        kb = KnowledgebaseService.get_by_id(db, kb_ids[0])
        if not kb:
            return get_error_data_result(retmsg="Dataset not found!")

        if kb.tenant_embd_id:
            embd_config = get_model_config_by_id(db, kb.tenant_embd_id, model_type="embedding")
        else:
            embd_config = get_model_config_by_type_and_name(db, kb.tenant_id, LLMType.EMBEDDING.value, kb.embd_id)
        embd_mdl = LLMBundle(db, kb.tenant_id, embd_config)

        rerank_mdl = None
        if rerank_config is not None:
            rerank_mdl = LLMBundle(db, rerank_config["tenant_id"], rerank_config)
        elif req.get("rerank_id"):
            rerank_config = get_model_config_by_type_and_name(db, kb.tenant_id, LLMType.RERANK.value, req["rerank_id"])
            rerank_mdl = LLMBundle(db, kb.tenant_id, rerank_config)

        # 跨语言翻译
        if langs:
            question = await cross_languages(kb.tenant_id, None, question, langs)

        # 关键词提取增强
        if req.get("keyword", False):
            chat_config = get_tenant_default_model_by_type(db, kb.tenant_id, LLMType.CHAT)
            chat_mdl = LLMBundle(db, kb.tenant_id, chat_config)
            question += await keyword_extraction(chat_mdl, question)

        # 执行检索
        filter_exp = ""
        ranks = await settings.retriever.retrieval(
            question,
            filter_exp,
            embd_mdl,
            [item.tenant_id for item in kbs],
            kb_names,
            page,
            size,
            similarity_threshold,
            vector_similarity_weight,
            top,
            doc_ids,
            rerank_mdl=rerank_mdl,
            highlight=highlight,
            rank_feature=label_question(db, question, kbs),
            search_mode=search_mode_dict,
            kb_ids=[item.id for item in kbs],
        )
        if toc_enhance:
            toc_chat_config = get_tenant_default_model_by_type(db, kb.tenant_id, LLMType.CHAT)
            chat_mdl = LLMBundle(db, kb.tenant_id, toc_chat_config)
            cks = await settings.retriever.retrieval_by_toc(question, ranks["chunks"], tenant_ids, chat_mdl, size)
            if cks:
                ranks["chunks"] = cks
        ranks["chunks"] = settings.retriever.retrieval_by_children(ranks["chunks"], tenant_ids)
        # 知识图谱增强
        if use_kg:
            kg_chat_config = get_tenant_default_model_by_type(db, kb.tenant_id, LLMType.CHAT)
            ck = await settings.kg_retriever.retrieval(question, [k.tenant_id for k in kbs], kb_ids, embd_mdl, LLMBundle(db, kb.tenant_id, kg_chat_config))
            if ck["content_with_weight"]:
                ranks["chunks"].insert(0, ck)

        # 移除向量数据
        for c in ranks["chunks"]:
            c.pop("vector", None)

        enrich_reference_metadata(db, ranks["chunks"], resolve_reference_metadata_preferences(req))

        # 重命名键名
        renamed_chunks = []
        for chunk in ranks["chunks"]:
            key_mapping = {
                "chunk_id": "id",
                "content_with_weight": "content",
                "doc_id": "document_id",
                "important_kwd": "important_keywords",
                "question_kwd": "questions",
                "docnm_kwd": "document_keyword",
                "kb_id": "dataset_id",
            }
            rename_chunk = {}
            for key, value in chunk.items():
                new_key = key_mapping.get(key, key)
                rename_chunk[new_key] = value
            renamed_chunks.append(rename_chunk)
        ranks["chunks"] = renamed_chunks

        return get_result(data=ranks)
    except Exception as e:
        if "not_found" in str(e):
            return get_result(
                retmsg="No chunk found! Check the chunk status please!",
                retcode=RetCode.DATA_ERROR,
            )
        return server_error_response(e)
