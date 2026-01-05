import datetime
import logging
import pathlib
import re
from io import BytesIO
from typing import Any, Literal, Annotated

import xxhash
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator, Discriminator, model_validator
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from urllib.parse import quote

from api.constants import FILE_NAME_LEN_LIMIT
from api.db import FileType
from common.constants import FileSource, LLMType, ParserType, TaskStatus
from api.db.db_models import File as FileModel, Task, get_db
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.db.services.tenant_llm_service import TenantLLMService
from api.db.services.dialog_service import meta_filter, convert_conditions
from common.constants import RetCode

from api.db.services.task_service import TaskService, queue_tasks
from api.utils.api_utils import check_duplicate_ids, construct_json_result, get_error_data_result, get_parser_config, get_result, server_error_response, token_required
from core.app.qa import beAdoc, rmPrefix
from core.app.tag import label_question
from core.nlp import rag_tokenizer, search
from core.prompts.generator import cross_languages, keyword_extraction
from common import settings
from common.string_utils import remove_redundant_spaces

MAXIMUM_OF_UPLOADING_FILES = 256

router = APIRouter()


class SparseSearchMode(BaseModel):
    type: Literal["sparse"] = "sparse"


class DenseSearchMode(BaseModel):
    type: Literal["dense"] = "dense"


class HybridSearchMode(BaseModel):
    type: Literal["hybrid"] = "hybrid"
    weight_dense: float = Field(default=0.7, ge=0.0, le=1.0)
    weight_sparse: float = Field(default=0.3, ge=0.0, le=1.0)

    @model_validator(mode='after')
    def validate_weights(self) -> 'HybridSearchMode':
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

    @model_validator(mode='after')
    def validate_weights_format(self) -> 'FusionSearchMode':
        """验证weights格式"""
        try:
            parts = self.weights.split(',')
            if len(parts) != 2:
                raise ValueError("weights must contain exactly two comma-separated values")
            float(parts[0].strip())
            float(parts[1].strip())
        except (ValueError, AttributeError):
            raise ValueError("weights must be in format 'float,float' (e.g., '0.05,0.95')")
        return self


# 使用 Discriminator 的高效版本
SearchModeType = Annotated[
    SparseSearchMode | DenseSearchMode | HybridSearchMode | FusionSearchMode,
    Discriminator('type')
]

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

    @field_validator("positions")
    @classmethod
    def validate_positions(cls, value):
        for sublist in value:
            if len(sublist) != 5:
                raise ValueError("Each sublist in positions must have a length of 5")
        return value


class UpdateDocumentRequest(BaseModel):
    name: str | None = None
    parser_config: dict[str, Any] | None = None
    chunk_method: str | None = None
    enabled: bool | None = None
    meta_fields: dict[str, Any] | None = None


class DeleteDocumentsRequest(BaseModel):
    ids: list[str] | None = None


class ParseDocumentRequest(BaseModel):
    document_ids: list[str]


class StopParsingRequest(BaseModel):
    document_ids: list[str]


class AddChunkRequest(BaseModel):
    content: str
    important_keywords: list[str] = Field(default_factory=list)


class UpdateChunkRequest(BaseModel):
    content: str | None = None
    important_keywords: list[str] | None = None
    available: bool | None = None


class DeleteChunksRequest(BaseModel):
    chunk_ids: list[str]


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
        mode_type = mode_data.pop('type')
        return {mode_type: mode_data}


@router.post("/datasets/{dataset_id}/documents", summary="上传文档")
def upload_documents(
    dataset_id: str,
    files: list[UploadFile] = File(...),
    parent_path: str | None = Form(None, description="Optional nested path under the parent folder. Uses '/' separators."),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    上传文档到数据集

    Args:
        dataset_id: 数据集ID
        files: 上传的文件列表
        parent_path: 可选的父文件夹路径，使用 '/' 分隔
        db: 数据库会话
        tenant_id: 租户ID

    Returns:
        上传的文档信息列表
    """
    if not files:
        return get_error_data_result(retmsg="No file part!", retcode=RetCode.ARGUMENT_ERROR)
    
    if len(files) > MAXIMUM_OF_UPLOADING_FILES:
        return get_error_data_result(retmsg=f"You try to upload {len(files)} files, which exceeds the maximum number: {MAXIMUM_OF_UPLOADING_FILES}")
    
    for file_obj in files:
        if file_obj.filename == "":
            return get_result(retmsg="No file selected!", retcode=RetCode.ARGUMENT_ERROR)
        if len(file_obj.filename.encode("utf-8")) > FILE_NAME_LEN_LIMIT:
            return get_result(retmsg=f"File name must be {FILE_NAME_LEN_LIMIT} bytes or less.", retcode=RetCode.ARGUMENT_ERROR)
    
    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not kb:
        raise HTTPException(status_code=RetCode.NOT_FOUND, detail=f"Can't find the dataset with ID {dataset_id}!")
    
    err, uploaded_files = FileService.upload_document(db, kb, files, tenant_id, parent_path=parent_path)
    if err:
        return get_result(retmsg="\n".join(err), retcode=RetCode.SERVER_ERROR)
    
    # 重命名键名
    renamed_doc_list = []
    for file in uploaded_files:
        doc = file[0]
        key_mapping = {
            "chunk_num": "chunk_count",
            "kb_id": "dataset_id",
            "token_num": "token_count",
            "parser_id": "chunk_method",
        }
        renamed_doc = {}
        for key, value in doc.items():
            new_key = key_mapping.get(key, key)
            renamed_doc[new_key] = value
        renamed_doc["run"] = "UNSTART"
        renamed_doc_list.append(renamed_doc)
    
    return get_result(data=renamed_doc_list)


@router.put("/datasets/{dataset_id}/documents/{document_id}", summary="更新文档")
def update_document(
    dataset_id: str,
    document_id: str,
    request: UpdateDocumentRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    更新数据集中的文档
    
    Args:
        dataset_id: 数据集ID
        document_id: 文档ID
        request: 更新请求参数
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        更新后的文档信息
    """
    req = request.model_dump(exclude_unset=True)
    
    if not KnowledgebaseService.query(db, id=dataset_id, tenant_id=tenant_id):
        return get_error_data_result(retmsg="You don't own the dataset.")
    
    e, kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not e:
        return get_error_data_result(retmsg="Can't find this knowledgebase!")
    
    doc = DocumentService.query(db, kb_id=dataset_id, id=document_id)
    if not doc:
        return get_error_data_result(retmsg="The dataset doesn't own the document.")
    doc = doc[0]
    
    # 检查不可更改的字段
    if "chunk_count" in req and req["chunk_count"] != doc.chunk_num:
        return get_error_data_result(retmsg="Can't change `chunk_count`.")
    if "token_count" in req and req["token_count"] != doc.token_num:
        return get_error_data_result(retmsg="Can't change `token_count`.")
    if "progress" in req and req["progress"] != doc.progress:
        return get_error_data_result(retmsg="Can't change `progress`.")
    
    # 更新元数据字段
    if "meta_fields" in req:
        if not isinstance(req["meta_fields"], dict):
            return get_error_data_result(retmsg="meta_fields must be a dictionary")
        DocumentService.update_meta_fields(db, document_id, req["meta_fields"])
    
    # 更新文档名称
    if "name" in req and req["name"] != doc.name:
        if len(req["name"].encode("utf-8")) > FILE_NAME_LEN_LIMIT:
            return get_result(
                retmsg=f"File name must be {FILE_NAME_LEN_LIMIT} bytes or less.",
                retcode=RetCode.ARGUMENT_ERROR,
            )
        if pathlib.Path(req["name"].lower()).suffix != pathlib.Path(doc.name.lower()).suffix:
            return get_result(
                retmsg="The extension of file can't be changed",
                retcode=RetCode.ARGUMENT_ERROR,
            )
        
        for d in DocumentService.query(db, name=req["name"], kb_id=doc.kb_id):
            if d.name == req["name"]:
                return get_error_data_result(retmsg="Duplicated document name in the same dataset.")
        
        if not DocumentService.update_by_id(db, document_id, {"name": req["name"]}):
            return get_error_data_result(retmsg="Database error (Document rename)!")
        
        informs = File2DocumentService.get_by_document_id(db, document_id)
        if informs:
            e, file = FileService.get_by_id(db, informs[0].file_id)
            FileService.update_by_id(db, file.id, {"name": req["name"]})
    
    # 更新解析配置
    if "parser_config" in req:
        DocumentService.update_parser_config(db, doc.id, req["parser_config"])
    
    # 更新分块方法
    if "chunk_method" in req:
        valid_chunk_method = {"naive", "manual", "qa", "table", "paper", "book", "laws", "presentation", "picture", "one", "knowledge_graph", "email", "tag"}
        if req.get("chunk_method") not in valid_chunk_method:
            return get_error_data_result(retmsg=f"`chunk_method` {req['chunk_method']} doesn't exist")
        
        if doc.type == FileType.VISUAL or re.search(r"\.(ppt|pptx|pages)$", doc.name):
            return get_error_data_result(retmsg="Not supported yet!")
        
        if doc.parser_id.lower() != req["chunk_method"].lower():
            e = DocumentService.update_by_id(
                db,
                doc.id,
                {
                    "parser_id": req["chunk_method"],
                    "progress": 0,
                    "progress_msg": "",
                    "run": TaskStatus.UNSTART.value,
                },
            )
            if not e:
                return get_error_data_result(retmsg="Document not found!")
        
        if not req.get("parser_config"):
            req["parser_config"] = get_parser_config(req["chunk_method"], req.get("parser_config"))
            DocumentService.update_parser_config(db, doc.id, req["parser_config"])
        
        if doc.token_num > 0:
            e = DocumentService.increment_chunk_num(
                db,
                doc.id,
                doc.kb_id,
                doc.token_num * -1,
                doc.chunk_num * -1,
                doc.process_duration * -1,
            )
            if not e:
                return get_error_data_result(retmsg="Document not found!")
            settings.docStoreConn.delete({"doc_id": doc.id}, search.index_name(tenant_id), dataset_id)
    
    # 更新启用状态
    if "enabled" in req:
        status = int(req["enabled"])
        if doc.status != req["enabled"]:
            try:
                if not DocumentService.update_by_id(db, doc.id, {"status": str(status)}):
                    return get_error_data_result(retmsg="Database error (Document update)!")
                
                settings.docStoreConn.update({"doc_id": doc.id}, {"available_int": status}, search.index_name(kb.tenant_id), doc.kb_id)
                return get_result(data=True)
            except Exception as e:
                return server_error_response(e)
    
    try:
        ok, doc = DocumentService.get_by_id(db, doc.id)
        if not ok:
            return get_error_data_result(retmsg="Document update failed")
    except OperationalError as e:
        logging.exception(e)
        return get_error_data_result(retmsg="Database operation failed")
    
    # 重命名键名
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
    renamed_doc = {}
    for key, value in doc.to_dict().items():
        new_key = key_mapping.get(key, key)
        if key == "run":
            renamed_doc[new_key] = run_mapping.get(str(value), str(value))
        else:
            renamed_doc[new_key] = value
    
    return get_result(data=renamed_doc)


@router.get("/datasets/{dataset_id}/documents/{document_id}", summary="下载文档")
def download_document(
    dataset_id: str,
    document_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
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
    informs = File2DocumentService.get_by_document_id(db, document_id)
    if not informs:
        return get_error_data_result(retmsg="This document has been deleted")
    
    e, file = FileService.get_by_id(db, informs[0].file_id)
    if not e:
        return get_error_data_result(retmsg="This document has been deleted")
    
    try:
        settings.STORAGE_IMPL.obj_exist(file.location)
        
        def file_generator():
            try:
                for chunk in settings.STORAGE_IMPL.get(file.location):
                    yield chunk
            except Exception:
                yield b""
        
        encoded_filename = quote(doc.name)
        return StreamingResponse(
            file_generator(),
            media_type="application/octet-stream",
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{encoded_filename}"}
        )
    except Exception:
        return get_error_data_result(retmsg="This document has been deleted")


@router.get("/datasets/{dataset_id}/documents", summary="获取文档列表")
def list_documents(
    dataset_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    orderby: str = Query("create_time"),
    desc: str | bool = Query("true"),
    keywords: str | None = Query(None),
    id: str | None = Query(None),
    name: str | None = Query(None),
    suffix: list[str] = Query(None),
    run: list[str] = Query(None),
    create_time_from: int = Query(0),
    create_time_to: int = Query(0),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    获取数据集中的文档列表
    
    Args:
        dataset_id: 数据集ID
        page: 页码
        page_size: 每页数量
        orderby: 排序字段
        desc: 是否降序
        keywords: 关键词搜索
        id: 文档ID过滤
        name: 文档名称过滤
        suffix: 文件后缀过滤 (e.g., ["pdf", "txt", "docx"])
        run: 文档运行状态过滤 (支持数字和文本格式: "0"/"UNSTART", "1"/"RUNNING", "2"/"CANCEL", "3"/"DONE", "4"/"FAIL")
        create_time_from: Unix时间戳，过滤此时间之后创建的文档（0表示无过滤）
        create_time_to: Unix时间戳，过滤此时间之前创建的文档（0表示无过滤）
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        文档列表
    """
    if not KnowledgebaseService.query(db, id=dataset_id, tenant_id=tenant_id):
        return get_error_data_result(retmsg="You don't own the dataset.")
    
    # 检查文档ID
    if id and not DocumentService.query(db, id=id, kb_id=dataset_id):
        return get_error_data_result(retmsg=f"You don't own the document {id}.")
    if name and not DocumentService.query(db, name=name, kb_id=dataset_id):
        return get_error_data_result(retmsg=f"You don't own the document {name}.")
    
    # 处理 desc 参数
    desc_bool = str(desc).strip().lower() != "false"
    
    # 映射 run 状态（接受文本或数字格式）
    run_status_text_to_numeric = {
        "UNSTART": "0",
        "RUNNING": "1", 
        "CANCEL": "2",
        "DONE": "3",
        "FAIL": "4"
    }
    run_status_converted = None
    if run:
        run_status_converted = [run_status_text_to_numeric.get(v, v) for v in run]
    
    try:
        docs, total = DocumentService.get_list(
            db, 
            dataset_id, 
            page, 
            page_size, 
            orderby, 
            desc_bool, 
            keywords=keywords, 
            id=id,
            name=name,
            suffix=suffix,
            run=run_status_converted
        )
        
        # 时间范围过滤（0表示无限制）
        if create_time_from or create_time_to:
            docs = [
                d for d in docs
                if (create_time_from == 0 or d.get("create_time", 0) >= create_time_from)
                and (create_time_to == 0 or d.get("create_time", 0) <= create_time_to)
            ]
        
        # 重命名键名 + 映射 run 状态回文本格式输出
        key_mapping = {
            "chunk_num": "chunk_count",
            "kb_id": "dataset_id", 
            "token_num": "token_count",
            "parser_id": "chunk_method",
        }
        run_status_numeric_to_text = {
            "0": "UNSTART",
            "1": "RUNNING", 
            "2": "CANCEL",
            "3": "DONE",
            "4": "FAIL",
        }
        
        output_docs = []
        for d in docs:
            renamed_doc = {key_mapping.get(k, k): v for k, v in d.items()}
            if "run" in d:
                renamed_doc["run"] = run_status_numeric_to_text.get(str(d["run"]), d["run"])
            output_docs.append(renamed_doc)
        
        return get_result(data={"total": total, "docs": output_docs})
    except Exception as e:
        logging.exception(e)
        return get_error_data_result(retmsg="Failed to retrieve documents")


@router.delete("/datasets/{dataset_id}/documents", summary="批量删除文档")
def delete_documents(
    dataset_id: str,
    request: DeleteDocumentsRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    批量删除数据集中的文档
    
    Args:
        dataset_id: 数据集ID
        request: 删除请求参数
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        删除结果
    """
    req = request.model_dump()
    
    if not KnowledgebaseService.query(db, id=dataset_id, tenant_id=tenant_id):
        return get_error_data_result(retmsg="You don't own the dataset.")
    
    ids = req.get("ids")
    if ids is None:
        # 删除所有文档
        docs = DocumentService.query(db, kb_id=dataset_id)
        doc_ids = [doc.id for doc in docs]
    else:
        doc_ids = ids
    
    unique_doc_ids, duplicate_messages = check_duplicate_ids(doc_ids, "document")
    errors = []
    success_count = 0
    
    for doc_id in unique_doc_ids:
        doc = DocumentService.query(db, kb_id=dataset_id, id=doc_id)
        if not doc:
            errors.append(f"Document {doc_id} not found")
            continue
        
        try:
            if not DocumentService.remove_document(db, doc[0], tenant_id):
                errors.append(f"Failed to remove document {doc_id}")
                continue
            
            # 删除相关文件记录
            f2d = File2DocumentService.get_by_document_id(db, doc_id)
            if f2d:
                FileService.filter_delete(
                    db,
                    [
                        FileModel.source_type == FileSource.KNOWLEDGEBASE,
                        FileModel.id == f2d[0].file_id,
                    ]
                )
            File2DocumentService.delete_by_document_id(db, doc_id)
            success_count += 1
        except Exception as e:
            errors.append(f"Error deleting document {doc_id}: {str(e)}")
    
    if errors:
        if success_count > 0:
            return get_result(
                data={"success_count": success_count, "errors": errors[:5]},
                retmsg=f"Partially deleted {success_count} documents with {len(errors)} errors"
            )
        else:
            return get_error_data_result(retmsg=f"Failed to delete documents: {'; '.join(errors)}")
    
    if duplicate_messages:
        if success_count > 0:
            return get_result(
                data={"success_count": success_count, "errors": duplicate_messages},
                retmsg=f"Partially deleted {success_count} documents with {len(duplicate_messages)} errors"
            )
        else:
            return get_error_data_result(retmsg=";".join(duplicate_messages))
    
    return get_result(retmsg=f"Successfully deleted {success_count} documents")


@router.post("/datasets/{dataset_id}/chunks", summary="解析文档")
def parse_documents(
    dataset_id: str,
    request: ParseDocumentRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
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
            
            DocumentService.update_by_id(
                db,
                doc.id, 
                {
                    "progress": 0,
                    "progress_msg": "",
                    "run": TaskStatus.UNSTART.value,
                }
            )
            
            queue_tasks(db, doc, tenant_id)
        
        message = "Documents queued for parsing"
        if duplicate_messages:
            message += f" (with {len(duplicate_messages)} duplicate IDs ignored)"
            
        return get_result(retmsg=message)
    except Exception as e:
        logging.exception(e)
        return get_error_data_result(retmsg=f"Failed to queue parsing: {str(e)}")


@router.delete("/datasets/{dataset_id}/chunks", summary="停止解析")
def stop_parsing_documents(
    dataset_id: str,
    request: StopParsingRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
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
            
            # 更新文档状态为取消
            DocumentService.update_by_id(
                db,
                doc_id,
                {
                    "run": TaskStatus.CANCEL.value,
                    "progress": 0,
                    "progress_msg": "Cancelled by user"
                }
            )
            
            # 删除相关任务
            TaskService.filter_delete(db, [Task.doc_id == doc_id])
            success_count += 1
        
        message = f"Parsing stopped for {success_count} documents"
        if duplicate_messages:
            message += f" (with {len(duplicate_messages)} duplicate IDs ignored)"
            
        return get_result(retmsg=message)
    except Exception as e:
        logging.exception(e)
        return get_error_data_result(retmsg=f"Failed to stop parsing: {str(e)}")


@router.get("/datasets/{dataset_id}/documents/{document_id}/chunks", summary="获取文档分块列表")
def list_document_chunks(
    dataset_id: str,
    document_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(30, ge=1, le=100),
    keywords: str | None = Query(None),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    获取文档的分块列表
    
    Args:
        dataset_id: 数据集ID
        document_id: 文档ID
        page: 页码
        page_size: 每页数量
        keywords: 关键词搜索
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        文档分块列表
    """
    if not KnowledgebaseService.query(db, id=dataset_id, tenant_id=tenant_id):
        return get_error_data_result(retmsg="You don't own the dataset.")
    
    doc = DocumentService.query(db, kb_id=dataset_id, id=document_id)
    if not doc:
        return get_error_data_result(retmsg="Document not found.")
    
    try:
        # 从搜索引擎获取chunks
        query_conditions = {"doc_id": document_id}
        if keywords:
            query_conditions["content"] = keywords
        
        res = settings.docStoreConn.search(
            query_conditions,
            search.index_name(tenant_id),
            dataset_id,
            page=page,
            size=page_size
        )
        
        chunks = []
        for chunk_id in res.ids:
            chunk_data = res.field[chunk_id]
            chunk = {
                "id": chunk_id,
                "content": chunk_data.get("content_with_weight", ""),
                "document_id": chunk_data.get("doc_id", ""),
                "docnm_kwd": chunk_data.get("docnm_kwd", ""),
                "important_keywords": chunk_data.get("important_keywords", []),
                "image_id": chunk_data.get("img_id", ""),
                "available": chunk_data.get("available_int", 1) == 1,
                "positions": chunk_data.get("position_int", []),
            }
            chunks.append(chunk)
        
        return get_result(data=chunks)
    except Exception as e:
        logging.exception(e)
        return get_error_data_result(retmsg=f"Failed to retrieve chunks: {str(e)}")


@router.post("/datasets/{dataset_id}/documents/{document_id}/chunks", summary="添加文档分块")
def add_document_chunk(
    dataset_id: str,
    document_id: str,
    request: AddChunkRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
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
    
    try:
        doc = doc[0]
        e, kb = KnowledgebaseService.get_by_id(db, dataset_id)
        if not e:
            return get_error_data_result(retmsg="Dataset not found.")
        
        # 生成chunk ID
        chunk_id = xxhash.xxh64(f"{document_id}-{req['content']}").hexdigest()
        
        # 准备chunk数据
        embd_mdl = LLMBundle(kb.tenant_id, LLMType.EMBEDDING, llm_name=kb.embd_id)
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
        
        # 生成embedding
        v, c = embd_mdl.encode([req["content"]])
        chunk_data["q_%d_vec" % len(v[0])] = v[0]
        
        # 保存到搜索引擎
        settings.docStoreConn.upsert([chunk_id], [chunk_data], search.index_name(kb.tenant_id), dataset_id)
        
        # 更新文档统计
        DocumentService.increment_chunk_num(
            db,
            document_id,
            dataset_id,
            c,  # token count
            1,  # chunk count
            0   # process duration
        )
        
        return get_result(data={"chunk_id": chunk_id, "content": req["content"]})
    except Exception as e:
        logging.exception(e)
        return get_error_data_result(retmsg=f"Failed to add chunk: {str(e)}")


@router.delete("/datasets/{dataset_id}/documents/{document_id}/chunks", summary="批量删除文档分块")
def delete_document_chunks(
    dataset_id: str,
    document_id: str,
    request: DeleteChunksRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """批量删除文档分块"""
    req = request.model_dump()
    
    if not KnowledgebaseService.query(db, id=dataset_id, tenant_id=tenant_id):
        return get_error_data_result(retmsg="You don't own the dataset.")
    
    doc = DocumentService.query(db, kb_id=dataset_id, id=document_id)
    if not doc:
        return get_error_data_result(retmsg="Document not found.")
    
    chunk_ids = req["chunk_ids"]
    if not chunk_ids:
        return get_error_data_result(retmsg="chunk_ids is required")
    
    try:
        # 从搜索引擎删除chunks
        settings.docStoreConn.delete({"id": chunk_ids}, search.index_name(tenant_id), dataset_id)
        
        # 更新文档统计
        DocumentService.increment_chunk_num(
            db,
            document_id,
            dataset_id,
            0,  # token count (需要重新计算)
            -len(chunk_ids),  # chunk count
            0   # process duration
        )
        
        return get_result(retmsg=f"Successfully deleted {len(chunk_ids)} chunks")
    except Exception as e:
        logging.exception(e)
        return get_error_data_result(retmsg=f"Failed to delete chunks: {str(e)}")


@router.put("/datasets/{dataset_id}/documents/{document_id}/chunks/{chunk_id}", summary="更新文档分块")
def update_document_chunk(
    dataset_id: str,
    document_id: str,
    chunk_id: str,
    request: UpdateChunkRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """更新文档分块"""
    req = request.model_dump(exclude_unset=True)
    
    if not KnowledgebaseService.query(db, id=dataset_id, tenant_id=tenant_id):
        return get_error_data_result(retmsg="You don't own the dataset.")
    
    doc = DocumentService.query(db, kb_id=dataset_id, id=document_id)
    if not doc:
        return get_error_data_result(retmsg="Document not found.")
    
    try:
        # 获取现有chunk数据
        res = settings.docStoreConn.get(chunk_id, search.index_name(tenant_id), dataset_id)
        if not res:
            return get_error_data_result(retmsg="Chunk not found.")
        
        chunk_data = res[chunk_id]
        
        # 更新内容
        if "content" in req and req["content"] is not None:
            chunk_data["content_with_weight"] = req["content"]
            tks = rag_tokenizer.tokenize(req["content"])
            chunk_data["content_ltks"] = tks
            chunk_data["content_sm_ltks"] = rag_tokenizer.fine_grained_tokenize(tks)
            
            # 重新生成embedding
            e, kb = KnowledgebaseService.get_by_id(db, dataset_id)
            embd_mdl = LLMBundle(kb.tenant_id, LLMType.EMBEDDING, llm_name=kb.embd_id)
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

        # 更新修改时间
        chunk_data["update_time"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        chunk_data["update_timestamp_flt"] = datetime.datetime.now().timestamp()
        
        # 保存到搜索引擎
        settings.docStoreConn.upsert([chunk_id], [chunk_data], search.index_name(tenant_id), dataset_id)
        
        return get_result(data={"chunk_id": chunk_id, **req})
    except Exception as e:
        logging.exception(e)
        return get_error_data_result(retmsg=f"Failed to update chunk: {str(e)}")


@router.post("/retrieval", summary="检索测试")
def retrieval_test(
    request: RetrievalTestRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
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
    kb_names = list([kb.name for kb in kbs])
    
    # 验证所有数据集使用相同的embedding模型
    embd_nms = list(set([TenantLLMService.split_model_name_and_factory(kb.embd_id)[0] for kb in kbs]))
    if len(embd_nms) != 1:
        return get_result(
            retmsg='Datasets use different embedding models.',
            retcode=RetCode.DATA_ERROR,
        )
    
    if "question" not in req:
        return get_error_data_result(retmsg="`question` is required.")
    
    page = int(req.get("page", 1))
    size = int(req.get("page_size", 30))
    question = req["question"]
    doc_ids = req.get("document_ids", [])
    use_kg = req.get("use_kg", False)
    toc_enhance = req.get("toc_enhance", False)
    langs = req.get("cross_languages", [])
    search_mode_dict = req.get_search_mode_dict()

    if not isinstance(doc_ids, list):
        return get_error_data_result(retmsg="`document_ids` should be a list")
    
    # 验证文档ID
    doc_ids_list = KnowledgebaseService.list_documents_by_ids(db, kb_ids)
    for doc_id in doc_ids:
        if doc_id not in doc_ids_list:
            return get_error_data_result(retmsg=f"The datasets don't own the document {doc_id}")
    
    # 处理元数据过滤
    if not doc_ids:
        metadata_condition = req.get("metadata_condition", {}) or {}
        if metadata_condition:
            metas = DocumentService.get_meta_by_kbs(db, kb_ids)
            doc_ids = meta_filter(metas, convert_conditions(metadata_condition), metadata_condition.get("logic", "and"))
            # If metadata_condition has conditions but no docs match, return empty result
            if not doc_ids and metadata_condition.get("conditions"):
                return get_result(data={"total": 0, "chunks": [], "doc_aggs": {}})
            if metadata_condition and not doc_ids:
                doc_ids = ["-999"]
    similarity_threshold = float(req.get("similarity_threshold", 0.2))
    vector_similarity_weight = float(req.get("vector_similarity_weight", 0.3))
    top = int(req.get("top_k", 1024))
    
    # 处理highlight参数
    if req.get("highlight") == "False" or req.get("highlight") == "false":
        highlight = False
    else:
        highlight = bool(req.get("highlight", False))
    
    try:
        tenant_ids = list(set([kb.tenant_id for kb in kbs]))
        e, kb = KnowledgebaseService.get_by_id(db, kb_ids[0])
        if not e:
            return get_error_data_result(retmsg="Dataset not found!")
        
        embd_mdl = LLMBundle(db, kb.tenant_id, LLMType.EMBEDDING, llm_name=kb.embd_id)
        
        rerank_mdl = None
        if req.get("rerank_id"):
            rerank_mdl = LLMBundle(db, kb.tenant_id, LLMType.RERANK, llm_name=req["rerank_id"])
        
        # 跨语言翻译
        if langs:
            question = cross_languages(db, kb.tenant_id, None, question, langs)
        
        # 关键词提取增强
        if req.get("keyword", False):
            chat_mdl = LLMBundle(db, kb.tenant_id, LLMType.CHAT)
            question += keyword_extraction(chat_mdl, question)
        
        # 执行检索
        ranks = settings.retriever.retrieval(
            question,
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
            search_mode=search_mode_dict
        )
        if toc_enhance:
            chat_mdl = LLMBundle(kb.tenant_id, LLMType.CHAT)
            cks = settings.retriever.retrieval_by_toc(question, ranks["chunks"], tenant_ids, chat_mdl, size)
            if cks:
                ranks["chunks"] = cks
        # 知识图谱增强
        if use_kg:
            ck = settings.kg_retriever.retrieval(
                question, 
                [k.tenant_id for k in kbs], 
                kb_ids, 
                embd_mdl, 
                LLMBundle(db, kb.tenant_id, LLMType.CHAT)
            )
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
