import base64
import datetime
import logging
import re
from io import BytesIO
from typing import Annotated, Any, Literal
from urllib.parse import quote

import xxhash
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Discriminator, Field, field_validator, model_validator
from sqlalchemy.orm import Session

from api.db.db_models import APIToken, Document, Task, get_db
from api.db.joint_services.tenant_model_service import get_model_config_by_id, get_model_config_by_type_and_name, get_tenant_default_model_by_type
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.knowledgebase_service import EmbeddingModelMismatchError, KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.db.services.task_service import TaskService, cancel_all_task_of, queue_tasks
from api.utils.api_utils import check_duplicate_ids, construct_json_result, get_error_data_result, get_result, server_error_response, token_required
from api.utils.image_utils import store_chunk_image
from common import settings
from common.constants import LLMType, RetCode, TaskStatus
from common.metadata_utils import convert_conditions, meta_filter
from common.string_utils import is_content_empty, remove_redundant_spaces
from common.tag_feature_utils import validate_tag_features
from core.app.tag import label_question
from core.nlp import rag_tokenizer, search
from core.prompts.generator import cross_languages, keyword_extraction

router = APIRouter()


class SparseSearchMode(BaseModel):
    type: Literal["sparse"] = "sparse"


class DenseSearchMode(BaseModel):
    type: Literal["dense"] = "dense"


class HybridSearchMode(BaseModel):
    type: Literal["hybrid"] = "hybrid"
    weight_dense: float = Field(default=0.7, ge=0.0, le=1.0)
    weight_sparse: float = Field(default=0.3, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def validate_weights(self) -> "HybridSearchMode":
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
    def validate_weights_format(self) -> "FusionSearchMode":
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


class ChunkModel(BaseModel):
    id: str = ""
    content: str = ""
    document_id: str = ""
    docnm_kwd: str = ""
    important_keywords: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    question_tks: str = ""
    image_id: str = ""
    available: bool = True
    positions: list[list[int]] = Field(default_factory=list)
    tag_kwd: list[str] = Field(default_factory=list)
    tag_feas: Any = Field(default_factory=dict)

    @field_validator("positions")
    @classmethod
    def validate_positions(cls, value):
        for sublist in value:
            if len(sublist) != 5:
                raise ValueError("Each sublist in positions must have a length of 5")
        return value


class ParseDocumentRequest(BaseModel):
    document_ids: list[str]


class StopParsingRequest(BaseModel):
    document_ids: list[str]


class AddChunkRequest(BaseModel):
    content: str
    important_keywords: list[str] = Field(default_factory=list)
    tag_kwd: list[str] = Field(default_factory=list)
    tag_feas: Any = Field(default_factory=dict)
    image_base64: str | None = None


class UpdateChunkRequest(BaseModel):
    content: str | None = None
    important_keywords: list[str] | None = None
    available: bool | None = None
    tag_kwd: list[str] | None = None
    tag_feas: Any | None = None


class DeleteChunksRequest(BaseModel):
    chunk_ids: list[str] | None = None
    delete_all: bool = False


class SwitchChunksRequest(BaseModel):
    chunk_ids: list[str]
    available_int: int | None = None
    available: bool | None = None


class RetrievalTestRequest(BaseModel):
    question: str
    dataset_ids: list[str]
    document_ids: list[str] = Field(default_factory=list)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=30, ge=1, le=100)
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


@router.get("/datasets/{dataset_id}/documents/{document_id}", summary="下载文档")
def download_document(dataset_id: str, document_id: str, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    """
    从数据集下载文档

    Args:
        dataset_id: 数据集ID
        document_id: 文档ID
        db: 数据库会话
        tenant_id: 租户ID

    Returns:
        文档文件流
    """
    if not document_id:
        return get_error_data_result(retmsg="Specify document_id please.")

    if not KnowledgebaseService.query(db, id=dataset_id, tenant_id=tenant_id):
        return get_error_data_result(retmsg=f"You do not own the dataset {dataset_id}.")

    doc = DocumentService.query(db, kb_id=dataset_id, id=document_id)
    if not doc:
        return get_error_data_result(retmsg=f"The dataset not own the document {document_id}.")

    doc = doc[0]
    bucket, name = File2DocumentService.get_storage_address(db, doc_id=document_id)
    file_stream = settings.STORAGE_IMPL.get(bucket, name)
    if not file_stream:
        return construct_json_result(message="This file is empty.", code=RetCode.DATA_ERROR)

    file = BytesIO(file_stream)
    encoded_filename = quote(doc.name)
    return StreamingResponse(file, media_type="application/octet-stream", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"})


@router.get("/documents/{document_id}", summary="按文档ID下载文档")
def download_doc(
    document_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """
    仅通过文档ID下载文档（使用API Token认证）
    """
    token = request.headers.get("Authorization", "").split()
    if len(token) != 2:
        return get_error_data_result(retmsg="Authorization is not valid!")
    token = token[1]
    objs = APIToken.query(db, beta=token)
    if not objs:
        return get_error_data_result(retmsg="Authentication error: API key is invalid!")

    if not document_id:
        return get_error_data_result(retmsg="Specify document_id please.")
    doc = DocumentService.query(db, id=document_id)
    if not doc:
        return get_error_data_result(retmsg=f"The dataset not own the document {document_id}.")
    # The process of downloading
    doc_id, doc_location = File2DocumentService.get_storage_address(db, doc_id=document_id)
    file_stream = settings.STORAGE_IMPL.get(doc_id, doc_location)
    if not file_stream:
        return construct_json_result(message="This file is empty.", code=RetCode.DATA_ERROR)
    file = BytesIO(file_stream)
    encoded_filename = quote(doc[0].name)
    return StreamingResponse(file, media_type="application/octet-stream", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"})


class MetadataUpdateSelectorSDK(BaseModel):
    """元数据批量更新的选择器"""

    document_ids: list[str] | None = None
    metadata_condition: dict | None = None


class MetadataUpdateRequestSDK(BaseModel):
    """元数据批量更新请求"""

    selector: MetadataUpdateSelectorSDK | None = None
    updates: list[dict] = []
    deletes: list[dict] = []


@router.post("/datasets/{dataset_id}/metadata/update", summary="批量更新元数据")
def metadata_batch_update(dataset_id: str, request: MetadataUpdateRequestSDK, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    """
    批量更新或删除文档元数据。

    如果 selector 中的 document_ids 和 metadata_condition 都未提供，则选择数据集中的所有文档。
    如果同时提供，则取交集。

    Args:
        dataset_id: 数据集ID
        request: 更新请求参数
            - selector: 文档选择器
                - document_ids: 文档ID列表
                - metadata_condition: 元数据过滤条件
            - updates: 更新操作列表，每个包含 {"key": str, "value": any, "match": any (optional)}
            - deletes: 删除操作列表，每个包含 {"key": str, "value": any (optional)}
        db: 数据库会话
        tenant_id: 租户ID

    Returns:
        {"updated": 更新的文档数, "matched_docs": 匹配的文档数}
    """
    if not KnowledgebaseService.accessible(db, kb_id=dataset_id, user_id=tenant_id):
        return get_error_data_result(retmsg=f"You don't own the dataset {dataset_id}.")

    req = request.model_dump()
    selector = req.get("selector") or {}
    updates = req.get("updates") or []
    deletes = req.get("deletes") or []

    if not isinstance(selector, dict):
        return get_error_data_result(retmsg="selector must be an object.")
    if not isinstance(updates, list) or not isinstance(deletes, list):
        return get_error_data_result(retmsg="updates and deletes must be lists.")

    metadata_condition = selector.get("metadata_condition") or {}
    if metadata_condition and not isinstance(metadata_condition, dict):
        return get_error_data_result(retmsg="metadata_condition must be an object.")

    document_ids = selector.get("document_ids") or []
    if document_ids and not isinstance(document_ids, list):
        return get_error_data_result(retmsg="document_ids must be a list.")

    for upd in updates:
        if not isinstance(upd, dict) or not upd.get("key") or "value" not in upd:
            return get_error_data_result(retmsg="Each update requires key and value.")
    for d in deletes:
        if not isinstance(d, dict) or not d.get("key"):
            return get_error_data_result(retmsg="Each delete requires key.")

    if document_ids:
        kb_doc_ids = KnowledgebaseService.list_documents_by_ids(db, [dataset_id])
        target_doc_ids = set(kb_doc_ids)
        invalid_ids = set(document_ids) - set(kb_doc_ids)
        if invalid_ids:
            return get_error_data_result(retmsg=f"These documents do not belong to dataset {dataset_id}: {', '.join(invalid_ids)}")
        target_doc_ids = set(document_ids)

    if metadata_condition:
        metas = DocMetadataService.get_flatted_meta_by_kbs(db, [dataset_id])
        filtered_ids = set(meta_filter(metas, convert_conditions(metadata_condition), metadata_condition.get("logic", "and")))
        target_doc_ids = target_doc_ids & filtered_ids
        if metadata_condition.get("conditions") and not target_doc_ids:
            return get_result(data={"updated": 0, "matched_docs": 0})

    target_doc_ids = list(target_doc_ids)
    updated = DocMetadataService.batch_update_metadata(db, dataset_id, target_doc_ids, updates, deletes)
    return get_result(data={"updated": updated, "matched_docs": len(target_doc_ids)})


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


@router.get("/datasets/{dataset_id}/documents/{document_id}/chunks", summary="获取文档分块列表")
async def list_chunks(
    dataset_id: str,
    document_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    keywords: str | None = Query(None),
    id: str | None = Query(None, description="Chunk ID to retrieve a specific chunk"),
    available: bool | None = Query(None, description="Filter by availability status"),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required),
):
    """
    获取文档的分块列表

    Args:
        dataset_id: 数据集ID
        document_id: 文档ID
        page: 页码
        page_size: 每页数量
        keywords: 关键词搜索
        id: 可选的Chunk ID，用于获取单个chunk
        db: 数据库会话
        tenant_id: 租户ID

    Returns:
        文档分块列表，包含total、chunks和doc信息
    """
    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not kb:
        return get_error_data_result(retmsg=f"You don't own the dataset {dataset_id}.")

    doc = DocumentService.query(db, kb_id=dataset_id, id=document_id)
    if not doc:
        return get_error_data_result(retmsg=f"You don't own the document {document_id}.")

    doc = doc[0]

    # Build query parameters
    query = {
        "doc_ids": [document_id],
        "page": page,
        "size": page_size,
        "question": keywords or "",
        "sort": True,
    }

    if available is not None:
        query["available_int"] = 1 if available else 0

    # Key mapping for document fields
    key_mapping = {
        "chunk_num": "chunk_count",
        "kb_id": "dataset_id",
        "token_num": "token_count",
        "parser_id": "chunk_method",
    }
    run_mapping = {
        "0": "UNSTART",
        "1": "RUNNING",
        "2": "CANCEL",
        "3": "DONE",
        "4": "FAIL",
    }

    # Convert doc to dict with renamed keys
    doc_dict = DocumentService.serialize_document(db, doc)
    renamed_doc = {}
    for key, value in doc_dict.items():
        new_key = key_mapping.get(key, key)
        renamed_doc[new_key] = value
        if key == "run":
            renamed_doc["run"] = run_mapping.get(str(value))

    res = {"total": 0, "chunks": [], "doc": renamed_doc}

    # If specific chunk id is requested
    if id:
        chunk = settings.docStoreConn.get(id, search.index_name(tenant_id, [kb.name]), [dataset_id])
        if not chunk:
            return get_result(retmsg=f"Chunk not found: {dataset_id}/{id}", retcode=RetCode.NOT_FOUND)
        # Remove internal fields
        keys_to_remove = []
        for n in chunk.keys():
            if re.search(r"(_vec$|_sm_|_tks|_ltks)", n):
                keys_to_remove.append(n)
        for n in keys_to_remove:
            del chunk[n]
        if not chunk:
            return get_error_data_result(retmsg=f"Chunk `{id}` not found.")
        res["total"] = 1
        final_chunk = {
            "id": chunk.get("id", chunk.get("chunk_id")),
            "content": chunk.get("content_with_weight", ""),
            "document_id": chunk.get("doc_id", chunk.get("document_id")),
            "docnm_kwd": chunk.get("docnm_kwd", ""),
            "important_keywords": chunk.get("important_kwd", []),
            "questions": chunk.get("question_kwd", []),
            "dataset_id": chunk.get("kb_id", chunk.get("dataset_id")),
            "image_id": chunk.get("img_id", ""),
            "available": bool(chunk.get("available_int", 1)),
            "positions": chunk.get("position_int", []),
            "tag_kwd": chunk.get("tag_kwd", []),
            "tag_feas": chunk.get("tag_feas", {}),
        }
        res["chunks"].append(final_chunk)
        _ = ChunkModel(**final_chunk)  # validate the chunk

    elif settings.docStoreConn.index_exist(search.index_name(tenant_id, [kb.name]), dataset_id):
        sres = await settings.retriever.search(query, search.index_name(tenant_id, [kb.name]), [dataset_id], emb_mdl=None, highlight=True)
        res["total"] = sres.total
        for chunk_id in sres.ids:
            chunk_data = sres.field[chunk_id]
            d = {
                "id": chunk_id,
                "content": (remove_redundant_spaces(sres.highlight[chunk_id]) if keywords and chunk_id in sres.highlight else chunk_data.get("content_with_weight", "")),
                "document_id": chunk_data.get("doc_id", ""),
                "docnm_kwd": chunk_data.get("docnm_kwd", ""),
                "important_keywords": chunk_data.get("important_kwd", []),
                "questions": chunk_data.get("question_kwd", []),
                "dataset_id": chunk_data.get("kb_id", chunk_data.get("dataset_id")),
                "image_id": chunk_data.get("img_id", ""),
                "available": bool(int(chunk_data.get("available_int", "1"))),
                "positions": chunk_data.get("position_int", []),
                "tag_kwd": chunk_data.get("tag_kwd", []),
                "tag_feas": chunk_data.get("tag_feas", {}),
            }
            res["chunks"].append(d)
            _ = ChunkModel(**d)  # validate the chunk

    return get_result(data=res)


@router.post("/datasets/{dataset_id}/documents/{document_id}/chunks", summary="添加文档分块")
def add_chunk(dataset_id: str, document_id: str, request: AddChunkRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    """
    为文档添加新的分块

    Args:
        dataset_id: 数据集ID
        document_id: 文档ID
        request: 添加分块请求参数
        db: 数据库会话
        tenant_id: 租户ID

    Returns:
        添加的分块信息
    """
    req = request.model_dump()

    if not KnowledgebaseService.query(db, id=dataset_id, tenant_id=tenant_id):
        return get_error_data_result(retmsg="You don't own the dataset.")

    doc = DocumentService.query(db, kb_id=dataset_id, id=document_id)
    if not doc:
        return get_error_data_result(retmsg="Document not found.")

    if is_content_empty(req["content"]):
        return get_error_data_result(retmsg="`content` is required")

    try:
        doc = doc[0]
        kb = KnowledgebaseService.get_by_id(db, dataset_id)
        if not kb:
            return get_error_data_result(retmsg="Dataset not found.")

        # 生成chunk ID
        chunk_id = xxhash.xxh64(f"{document_id}-{req['content']}").hexdigest()

        # 准备chunk数据
        if kb.tenant_embd_id:
            embd_config = get_model_config_by_id(db, kb.tenant_embd_id)
        else:
            embd_config = get_model_config_by_type_and_name(db, kb.tenant_id, LLMType.EMBEDDING.value, kb.embd_id)
        embd_mdl = LLMBundle(db, kb.tenant_id, embd_config)
        tks = rag_tokenizer.tokenize(req["content"])

        chunk_data = {
            "content_with_weight": req["content"],
            "content_ltks": tks,
            "content_sm_ltks": rag_tokenizer.fine_grained_tokenize(tks),
            "doc_id": document_id,
            "docnm_kwd": doc.name,
            "kb_id": dataset_id,
            "important_keywords": req.get("important_keywords", []),
            "img_id": "",
            "available_int": 1,
            "create_time": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "create_timestamp_flt": datetime.datetime.now().timestamp(),
        }
        if "tag_kwd" in req:
            chunk_data["tag_kwd"] = req["tag_kwd"]
        if "tag_feas" in req:
            try:
                chunk_data["tag_feas"] = validate_tag_features(req["tag_feas"])
            except ValueError as exc:
                return get_error_data_result(retmsg=f"`tag_feas` {exc}")
        image_base64 = req.get("image_base64")
        if image_base64:
            chunk_data["img_id"] = f"{dataset_id}-{chunk_id}"
            chunk_data["doc_type_kwd"] = "image"

        # 生成embedding
        v, c = embd_mdl.encode([req["content"]])
        chunk_data["q_%d_vec" % len(v[0])] = v[0]

        # 保存到搜索引擎
        settings.docStoreConn.upsert([chunk_id], [chunk_data], search.index_name(kb.tenant_id), dataset_id)

        if image_base64:
            store_chunk_image(dataset_id, chunk_id, base64.b64decode(image_base64))

        # 更新文档统计
        DocumentService.increment_chunk_num(
            db,
            document_id,
            dataset_id,
            c,  # token count
            1,  # chunk count
            0,  # process duration
        )

        renamed_chunk = {
            "id": chunk_id,
            "content": req["content"],
            "document_id": document_id,
            "docnm_kwd": doc.name,
            "important_keywords": chunk_data.get("important_keywords", []),
            "questions": chunk_data.get("question_kwd", []),
            "dataset_id": dataset_id,
            "image_id": chunk_data.get("img_id", ""),
            "available": bool(chunk_data.get("available_int", 1)),
            "positions": chunk_data.get("position_int", []),
            "tag_kwd": chunk_data.get("tag_kwd", []),
            "tag_feas": chunk_data.get("tag_feas", {}),
        }
        _ = ChunkModel(**renamed_chunk)
        return get_result(data={"chunk": renamed_chunk})
    except Exception as e:
        logging.exception(e)
        return get_error_data_result(retmsg=f"Failed to add chunk: {e!s}")


@router.delete("/datasets/{dataset_id}/documents/{document_id}/chunks", summary="批量删除文档分块")
def rm_chunk(dataset_id: str, document_id: str, request: DeleteChunksRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    """批量删除文档分块"""
    req = request.model_dump()

    if not KnowledgebaseService.query(db, id=dataset_id, tenant_id=tenant_id):
        return get_error_data_result(retmsg="You don't own the dataset.")

    doc = DocumentService.query(db, kb_id=dataset_id, id=document_id)
    if not doc:
        return get_error_data_result(retmsg="Document not found.")

    chunk_ids = req.get("chunk_ids")
    if not chunk_ids:
        if req.get("delete_all") is True:
            doc_obj = doc[0]
            # Clean up storage assets while index rows still exist for discovery
            DocumentService.delete_chunk_images(doc_obj, search.index_name(tenant_id))
            condition = {"doc_id": document_id}
            chunk_number = settings.docStoreConn.delete(condition, search.index_name(tenant_id), dataset_id)
            if chunk_number != 0:
                DocumentService.decrement_chunk_num(db, document_id, dataset_id, 1, chunk_number, 0)
            return get_result(retmsg=f"deleted {chunk_number} chunks")
        else:
            return get_result()

    unique_chunk_ids, duplicate_messages = check_duplicate_ids(chunk_ids, "chunk")
    settings.docStoreConn.delete({"id": unique_chunk_ids}, search.index_name(tenant_id), dataset_id)

    DocumentService.increment_chunk_num(
        db,
        document_id,
        dataset_id,
        0,  # token count (需要重新计算)
        -len(chunk_ids),  # chunk count
        0,  # process duration
    )

    return get_result(retmsg=f"Successfully deleted {len(chunk_ids)} chunks")


@router.put("/datasets/{dataset_id}/documents/{document_id}/chunks/{chunk_id}", summary="更新文档分块")
def update_chunk(dataset_id: str, document_id: str, chunk_id: str, request: UpdateChunkRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    """更新文档分块"""
    req = request.model_dump(exclude_unset=True)

    if not KnowledgebaseService.query(db, id=dataset_id, tenant_id=tenant_id):
        return get_error_data_result(retmsg="You don't own the dataset.")

    doc = DocumentService.query(db, kb_id=dataset_id, id=document_id)
    if not doc:
        return get_error_data_result(retmsg="Document not found.")

    res = settings.docStoreConn.get(chunk_id, search.index_name(tenant_id), dataset_id)
    if not res:
        return get_error_data_result(retmsg="Chunk not found.")

    chunk_data = res[chunk_id]

    # 更新内容
    if "content" in req and req["content"] is not None:
        if is_content_empty(req["content"]):
            return get_error_data_result(retmsg="`content` is required")
        chunk_data["content_with_weight"] = req["content"]
        tks = rag_tokenizer.tokenize(req["content"])
        chunk_data["content_ltks"] = tks
        chunk_data["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(tks)

        # 重新生成embedding
        kb = KnowledgebaseService.get_by_id(db, dataset_id)
        if kb.tenant_embd_id:
            embd_config = get_model_config_by_id(db, kb.tenant_embd_id)
        else:
            embd_config = get_model_config_by_type_and_name(db, kb.tenant_id, LLMType.EMBEDDING.value, kb.embd_id)
        embd_mdl = LLMBundle(db, kb.tenant_id, embd_config)
        v, c = embd_mdl.encode([req["content"]])
        chunk_data["q_%d_vec" % len(v[0])] = v[0]

    # 更新重要关键词
    if "important_keywords" in req:
        chunk_data["important_keywords"] = req["important_keywords"]

    if "questions" in req:
        if not isinstance(req["questions"], list):
            return get_error_data_result("`questions` should be a list")
        chunk_data["question_kwd"] = [str(q).strip() for q in req.get("questions", []) if str(q).strip()]
        chunk_data["question_tks"] = rag_tokenizer.tokenize("\n".join(req["questions"]))

    # 更新可用状态
    if "available" in req:
        chunk_data["available_int"] = 1 if req["available"] else 0

    # 更新位置
    if "positions" in req:
        if not isinstance(req["positions"], list):
            return get_error_data_result("`positions` should be a list")
        chunk_data["position_int"] = req["positions"]

    if "tag_kwd" in req:
        chunk_data["tag_kwd"] = req["tag_kwd"]
    if "tag_feas" in req:
        try:
            chunk_data["tag_feas"] = validate_tag_features(req["tag_feas"])
        except ValueError as exc:
            return get_error_data_result(retmsg=f"`tag_feas` {exc}")

    # 更新修改时间
    chunk_data["update_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    chunk_data["update_timestamp_flt"] = datetime.datetime.now().timestamp()

    # 保存到搜索引擎
    settings.docStoreConn.upsert([chunk_id], [chunk_data], search.index_name(tenant_id), dataset_id)

    return get_result(data={"chunk_id": chunk_id, **req})


@router.post("/datasets/{dataset_id}/documents/{document_id}/chunks/switch", summary="切换分块可用状态")
def switch_chunks(dataset_id: str, document_id: str, request: SwitchChunksRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
    """批量切换指定文档中分块的可用状态"""
    if not KnowledgebaseService.query(db, id=dataset_id, tenant_id=tenant_id):
        return get_error_data_result(retmsg=f"You don't own the dataset {dataset_id}.")

    if not request.chunk_ids:
        return get_error_data_result(retmsg="`chunk_ids` is required.")

    if request.available_int is None and request.available is None:
        return get_error_data_result(retmsg="`available_int` or `available` is required.")

    available_int = request.available_int if request.available_int is not None else (1 if request.available else 0)

    try:
        doc = DocumentService.query(db, kb_id=dataset_id, id=document_id)
        if not doc:
            return get_error_data_result(retmsg="Document not found!")

        for cid in request.chunk_ids:
            if not settings.docStoreConn.update(
                {"id": cid},
                {"available_int": available_int},
                search.index_name(tenant_id),
                doc[0].kb_id,
            ):
                return get_error_data_result(retmsg="Index updating failure")

        return get_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post("/retrieval", summary="检索测试")
async def retrieval_test(request: RetrievalTestRequest, db: Session = Depends(get_db), tenant_id: str = Depends(token_required)):
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
    kb_names = [kb.name for kb in kbs]

    try:
        KnowledgebaseService.ensure_same_embedding_model(kbs)
    except EmbeddingModelMismatchError as e:
        return get_result(
            retmsg=str(e),
            retcode=RetCode.DATA_ERROR,
        )

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

    # 处理元数据过滤
    if not doc_ids:
        metadata_condition = req.get("metadata_condition")
        if metadata_condition:
            metas = DocMetadataService.get_flatted_meta_by_kbs(db, kb_ids)
            doc_ids = meta_filter(metas, convert_conditions(metadata_condition), metadata_condition.get("logic", "and"))
            # If metadata_condition has conditions but no docs match, return empty result
            if not doc_ids and metadata_condition.get("conditions"):
                return get_result(data={"total": 0, "chunks": [], "doc_aggs": {}})
            if metadata_condition and not doc_ids:
                doc_ids = ["-999"]
        else:
            # If doc_ids is None all documents of the datasets are used
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
            embd_config = get_model_config_by_id(db, kb.tenant_embd_id)
        else:
            embd_config = get_model_config_by_type_and_name(db, kb.tenant_id, LLMType.EMBEDDING.value, kb.embd_id)
        embd_mdl = LLMBundle(db, kb.tenant_id, embd_config)

        rerank_mdl = None
        if req.get("tenant_rerank_id"):
            rerank_config = get_model_config_by_id(db, req["tenant_rerank_id"])
            rerank_mdl = LLMBundle(db, kb.tenant_id, rerank_config)
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
            tenant_ids,
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
        if str(e).find("not_found") > 0:
            return get_result(
                retmsg="No chunk found! Check the chunk status please!",
                retcode=RetCode.DATA_ERROR,
            )
        return server_error_response(e)
