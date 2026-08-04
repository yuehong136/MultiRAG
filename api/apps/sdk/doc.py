import logging
from io import BytesIO
from typing import Annotated, Any, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Discriminator, Field, model_validator
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
from common import settings
from common.constants import LLMType, RetCode, TaskStatus
from common.metadata_utils import convert_conditions, meta_filter
from core.app.tag import label_question
from core.nlp import search
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


class ParseDocumentRequest(BaseModel):
    document_ids: list[str]


class StopParsingRequest(BaseModel):
    document_ids: list[str]


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
