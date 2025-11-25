import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.exc import OperationalError
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api import settings
from api.db import FileSource, StatusEnum
from api.db.db_models import File, get_db
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import TenantService
from common.misc_utils import get_uuid
from api.utils.api_utils import (
    deep_merge,
    get_error_data_result,
    get_parser_config,
    get_result,
    remap_dictionary_keys,
    token_required,
    verify_embedding_availability,
)
from core.nlp import search
from core.settings import PAGERANK_FLD

router = APIRouter()


class CreateDatasetRequest(BaseModel):
    name: str
    avatar: str | None = ""
    description: str | None = ""
    embedding_model: str | None = None
    permission: str | None = "me"  # 'me' or 'team'
    chunk_method: str | None = "naive"  # chunking method
    parser_config: dict[str, Any] | None = None


class UpdateDatasetRequest(BaseModel):
    name: str | None = None
    avatar: str | None = None
    description: str | None = None
    embedding_model: str | None = None
    permission: str | None = None
    chunk_method: str | None = None
    pagerank: int | None = None
    parser_config: dict[str, Any] | None = None


class DeleteDatasetRequest(BaseModel):
    ids: list[str] | None = None  # If None, delete all; if empty array, delete none


@router.post("/datasets", summary="创建数据集")
def create_dataset(
    request: CreateDatasetRequest, 
    db: Session = Depends(get_db), 
    tenant_id: str = Depends(token_required)
):
    """
    创建新的数据集
    
    Args:
        request: 数据集创建参数
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        创建的数据集信息
    """
    req = request.model_dump()
    
    # Field name transformations
    if req.get("embedding_model"):
        req["embd_id"] = req.pop("embedding_model")
    if req.get("chunk_method"):
        req["parser_id"] = req.pop("chunk_method")
    
    try:
        if KnowledgebaseService.get_or_none(db, name=req["name"], tenant_id=tenant_id, status=StatusEnum.VALID.value):
            return get_error_data_result(retmsg=f"Dataset name '{req['name']}' already exists")

        req["parser_config"] = get_parser_config(req.get("parser_id", "naive"), req.get("parser_config"))
        req["id"] = get_uuid()
        req["tenant_id"] = tenant_id
        req["created_by"] = tenant_id

        ok, t = TenantService.get_by_id(db, tenant_id)
        if not ok:
            return get_error_data_result(retmsg="Tenant not found")

        if not req.get("embd_id"):
            req["embd_id"] = t.embd_id
        else:
            ok, err = verify_embedding_availability(req["embd_id"], tenant_id)
            if not ok:
                return err

        if not KnowledgebaseService.save(db, **req):
            return get_error_data_result(retmsg="Create dataset error.(Database error)")

        ok, k = KnowledgebaseService.get_by_id(db, req["id"])
        if not ok:
            return get_error_data_result(retmsg="Dataset created failed")

        response_data = remap_dictionary_keys(k.to_dict())
        return get_result(data=response_data)
    except OperationalError as e:
        logging.exception(e)
        return get_error_data_result(retmsg="Database operation failed")


@router.delete("/datasets", summary="删除数据集")
def delete_datasets(
    request: DeleteDatasetRequest, 
    db: Session = Depends(get_db), 
    tenant_id: str = Depends(token_required)
):
    """
    删除数据集
    
    Args:
        request: 删除请求参数，包含要删除的数据集ID列表
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        删除结果
    """
    req = request.model_dump()

    try:
        kb_id_instance_pairs = []
        if req["ids"] is None:
            kbs = KnowledgebaseService.query(db, tenant_id=tenant_id)
            for kb in kbs:
                kb_id_instance_pairs.append((kb.id, kb))
        else:
            error_kb_ids = []
            for kb_id in req["ids"]:
                kb = KnowledgebaseService.get_or_none(db, id=kb_id, tenant_id=tenant_id)
                if kb is None:
                    error_kb_ids.append(kb_id)
                    continue
                kb_id_instance_pairs.append((kb_id, kb))
            if len(error_kb_ids) > 0:
                return get_error_data_result(retmsg=f"""User '{tenant_id}' lacks permission for datasets: '{", ".join(error_kb_ids)}'""")

        errors = []
        success_count = 0
        for kb_id, kb in kb_id_instance_pairs:
            for doc in DocumentService.query(db, kb_id=kb_id):
                if not DocumentService.remove_document(db, doc, tenant_id):
                    errors.append(f"Remove document '{doc.id}' error for dataset '{kb_id}'")
                    continue
                f2d = File2DocumentService.get_by_document_id(db, doc.id)
                FileService.filter_delete(
                    db,
                    [
                        File.source_type == FileSource.KNOWLEDGEBASE,
                        File.id == f2d[0].file_id,
                    ]
                )
                File2DocumentService.delete_by_document_id(db, doc.id)
            FileService.filter_delete(
                db, 
                [File.source_type == FileSource.KNOWLEDGEBASE, File.type == "folder", File.name == kb.name]
            )
            if not KnowledgebaseService.delete_by_id(db, kb_id):
                errors.append(f"Delete dataset error for {kb_id}")
                continue
            success_count += 1

        if not errors:
            return get_result()

        error_message = f"Successfully deleted {success_count} datasets, {len(errors)} failed. Details: {'; '.join(errors)[:128]}..."
        if success_count == 0:
            return get_error_data_result(retmsg=error_message)

        return get_result(data={"success_count": success_count, "errors": errors[:5]}, retmsg=error_message)
    except OperationalError as e:
        logging.exception(e)
        return get_error_data_result(retmsg="Database operation failed")


@router.put("/datasets/{dataset_id}", summary="更新数据集")
def update_dataset(
    dataset_id: str, 
    request: UpdateDatasetRequest, 
    db: Session = Depends(get_db), 
    tenant_id: str = Depends(token_required)
):
    """
    更新数据集
    
    Args:
        dataset_id: 数据集ID
        request: 更新请求参数
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        更新后的数据集信息
    """
    req = request.model_dump(exclude_unset=True)
    
    # Field name transformations
    if "embedding_model" in req:
        req["embd_id"] = req.pop("embedding_model")
    if "chunk_method" in req:
        req["parser_id"] = req.pop("chunk_method")

    if not req:
        return get_error_data_result(retmsg="No properties were modified")

    try:
        kb = KnowledgebaseService.get_or_none(db, id=dataset_id, tenant_id=tenant_id)
        if kb is None:
            return get_error_data_result(retmsg=f"User '{tenant_id}' lacks permission for dataset '{dataset_id}'")

        if req.get("parser_config"):
            req["parser_config"] = deep_merge(kb.parser_config, req["parser_config"])

        if (chunk_method := req.get("parser_id")) and chunk_method != kb.parser_id:
            if not req.get("parser_config"):
                req["parser_config"] = get_parser_config(chunk_method, None)
        elif "parser_config" in req and not req["parser_config"]:
            del req["parser_config"]

        if "name" in req and req["name"].lower() != kb.name.lower():
            exists = KnowledgebaseService.get_or_none(db, name=req["name"], tenant_id=tenant_id, status=StatusEnum.VALID.value)
            if exists:
                return get_error_data_result(retmsg=f"Dataset name '{req['name']}' already exists")

        if "embd_id" in req:
            if not req["embd_id"]:
                req["embd_id"] = kb.embd_id
            if kb.chunk_num != 0 and req["embd_id"] != kb.embd_id:
                return get_error_data_result(retmsg=f"When chunk_num ({kb.chunk_num}) > 0, embedding_model must remain {kb.embd_id}")
            ok, err = verify_embedding_availability(req["embd_id"], tenant_id)
            if not ok:
                return err

        if "pagerank" in req and req["pagerank"] != kb.pagerank:
            if os.environ.get("DOC_ENGINE", "elasticsearch") == "infinity":
                return get_error_data_result(retmsg="'pagerank' can only be set when doc_engine is elasticsearch")

            if req["pagerank"] > 0:
                settings.docStoreConn.update({"kb_id": kb.id}, {PAGERANK_FLD: req["pagerank"]}, search.index_name(kb.tenant_id), kb.id)
            else:
                # Elasticsearch requires PAGERANK_FLD be non-zero!
                settings.docStoreConn.update({"exists": PAGERANK_FLD}, {"remove": PAGERANK_FLD}, search.index_name(kb.tenant_id), kb.id)

        if not KnowledgebaseService.update_by_id(db, kb.id, req):
            return get_error_data_result(retmsg="Update dataset error.(Database error)")

        ok, k = KnowledgebaseService.get_by_id(db, kb.id)
        if not ok:
            return get_error_data_result(retmsg="Dataset updated failed")

        response_data = remap_dictionary_keys(k.to_dict())
        return get_result(data=response_data)
    except OperationalError as e:
        logging.exception(e)
        return get_error_data_result(retmsg="Database operation failed")


@router.get("/datasets", summary="获取数据集列表")
def list_datasets(
    id: str | None = Query(None, description="数据集ID过滤"),
    name: str | None = Query(None, description="数据集名称过滤"),
    page: int = Query(1, description="页码"),
    page_size: int = Query(30, description="每页数量"),
    orderby: str = Query("create_time", description="排序字段"),
    desc: bool = Query(True, description="是否降序"),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    获取数据集列表
    
    Args:
        id: 数据集ID过滤（可选）
        name: 数据集名称过滤（可选）
        page: 页码
        page_size: 每页数量
        orderby: 排序字段
        desc: 是否降序
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        数据集列表
    """
    try:
        kb_id = id
        if kb_id:
            kbs = KnowledgebaseService.get_kb_by_id(db, kb_id, tenant_id)
            if not kbs:
                return get_error_data_result(retmsg=f"User '{tenant_id}' lacks permission for dataset '{kb_id}'")
        if name:
            kbs = KnowledgebaseService.get_kb_by_name(db, name, tenant_id)
            if not kbs:
                return get_error_data_result(retmsg=f"User '{tenant_id}' lacks permission for dataset '{name}'")

        tenants = TenantService.get_joined_tenants_by_user_id(db, tenant_id)
        kbs, total = KnowledgebaseService.get_list(
            db,
            [m["tenant_id"] for m in tenants],
            tenant_id,
            page,
            page_size,
            orderby,
            desc,
            kb_id,
            name,
        )

        response_data_list = []
        for kb in kbs:
            response_data_list.append(remap_dictionary_keys(kb))
        return get_result(data=response_data_list, total=total)
    except OperationalError as e:
        logging.exception(e)
        return get_error_data_result(retmsg="Database operation failed")


@router.get("/datasets/{dataset_id}/knowledge_graph", summary="获取数据集知识图谱")
def get_knowledge_graph(
    dataset_id: str, 
    db: Session = Depends(get_db), 
    tenant_id: str = Depends(token_required)
):
    """
    获取数据集的知识图谱
    
    Args:
        dataset_id: 数据集ID
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        知识图谱数据
    """
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return get_result(
            data=False,
            retmsg='No authorization.',
            retcode=settings.RetCode.AUTHENTICATION_ERROR
        )
    
    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    req = {
        "kb_id": [dataset_id],
        "knowledge_graph_kwd": ["graph"]
    }

    obj = {"graph": {}, "mind_map": {}}
    if not settings.docStoreConn.indexExist(search.index_name_one(kb.tenant_id, kb.name), dataset_id):
        return get_result(data=obj)
    
    sres = settings.retriever.search(req, search.index_name_one(kb.tenant_id,kb.name), [dataset_id])
    if not len(sres.ids):
        return get_result(data=obj)

    for id in sres.ids[:1]:
        ty = sres.field[id]["knowledge_graph_kwd"]
        try:
            content_json = json.loads(sres.field[id]["content_with_weight"])
        except Exception:
            continue

        obj[ty] = content_json

    if "nodes" in obj["graph"]:
        obj["graph"]["nodes"] = sorted(obj["graph"]["nodes"], key=lambda x: x.get("pagerank", 0), reverse=True)[:256]
        if "edges" in obj["graph"]:
            node_id_set = {o["id"] for o in obj["graph"]["nodes"]}
            filtered_edges = [o for o in obj["graph"]["edges"] if o["source"] != o["target"] and o["source"] in node_id_set and o["target"] in node_id_set]
            obj["graph"]["edges"] = sorted(filtered_edges, key=lambda x: x.get("weight", 0), reverse=True)[:128]
    
    return get_result(data=obj)


@router.delete("/datasets/{dataset_id}/knowledge_graph", summary="删除数据集知识图谱")
def delete_knowledge_graph(
    dataset_id: str, 
    db: Session = Depends(get_db), 
    tenant_id: str = Depends(token_required)
):
    """
    删除数据集的知识图谱
    
    Args:
        dataset_id: 数据集ID
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        删除结果
    """
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return get_result(
            data=False,
            retmsg='No authorization.',
            retcode=settings.RetCode.AUTHENTICATION_ERROR
        )
    
    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    settings.docStoreConn.delete(
        {"knowledge_graph_kwd": ["graph", "subgraph", "entity", "relation"]}, 
        search.index_name_one(kb.tenant_id, kb.name),
        dataset_id
    )

    return get_result(data=True)