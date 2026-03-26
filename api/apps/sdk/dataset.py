import json
import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.exc import OperationalError
from pydantic import BaseModel
from sqlalchemy.orm import Session

from common import settings
from common.constants import FileSource, StatusEnum
from api.db.db_models import File, get_db
from api.db.services.document_service import DocumentService, queue_raptor_o_graphrag_tasks
from api.db.services.task_service import GRAPH_RAPTOR_FAKE_DOC_ID, TaskService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.user_service import TenantService
from common.constants import RetCode
from api.utils.api_utils import (
    deep_merge,
    get_error_data_result,
    get_parser_config,
    get_result,
    remap_dictionary_keys,
    token_required,
    verify_embedding_availability,
)
from api.utils.tenant_utils import ensure_tenant_model_id_for_params
from core.nlp import search
from common.constants import PAGERANK_FLD

router = APIRouter()


class CreateDatasetRequest(BaseModel):
    name: str
    avatar: str | None = ""
    description: str | None = ""
    embedding_model: str | None = None
    permission: str | None = "me"  # 'me' or 'team'
    chunk_method: str | None = "naive"  # chunking method
    parser_config: dict[str, Any] | None = None
    auto_metadata_config: dict[str, Any] | None = None


class UpdateDatasetRequest(BaseModel):
    name: str | None = None
    avatar: str | None = None
    description: str | None = None
    embedding_model: str | None = None
    permission: str | None = None
    chunk_method: str | None = None
    pagerank: int | None = None
    parser_config: dict[str, Any] | None = None
    auto_metadata_config: dict[str, Any] | None = None


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
    embd_id = None
    parser_id = None
    if req.get("embedding_model"):
        embd_id = req.pop("embedding_model")
    if req.get("chunk_method"):
        parser_id = req.pop("chunk_method")
    
    # Map auto_metadata_config (if provided) into parser_config structure
    auto_meta = req.pop("auto_metadata_config", None)
    if auto_meta:
        parser_cfg = req.get("parser_config") or {}
        fields = []
        for f in auto_meta.get("fields", []):
            fields.append(
                {
                    "name": f.get("name", ""),
                    "type": f.get("type", ""),
                    "description": f.get("description"),
                    "examples": f.get("examples"),
                    "restrict_values": f.get("restrict_values", False),
                }
            )
        parser_cfg["metadata"] = fields
        parser_cfg["enable_metadata"] = auto_meta.get("enabled", True)
        req["parser_config"] = parser_cfg

    # 检查数据集名称是否已存在
    if KnowledgebaseService.get_or_none(db, name=req["name"], tenant_id=tenant_id, status=StatusEnum.VALID.value):
        return get_error_data_result(retmsg=f"Dataset name '{req['name']}' already exists")
    
    try:
        # 生成parser_config
        final_parser_id = parser_id or "naive"
        parser_config = get_parser_config(final_parser_id, req.get("parser_config"))
        
        # 使用封装的方法创建payload（会自动处理embd_id默认值）
        e, payload = KnowledgebaseService.create_with_name(
            db=db,
            name=req.pop("name"),
            tenant_id=tenant_id,
            parser_id=final_parser_id,
            embd_id=embd_id,
            parser_config=parser_config,
            avatar=req.get("avatar"),
            description=req.get("description"),
            permission=req.get("permission")
        )

        # 检查创建是否失败
        if not e:
            return payload  # 直接返回错误响应

        # 验证embedding model的可用性
        if embd_id:  # 如果用户指定了embd_id，需要验证
            ok, err = verify_embedding_availability(db, payload["embd_id"], tenant_id)
            if not ok:
                return err

        payload = ensure_tenant_model_id_for_params(db, tenant_id, payload)
        if not KnowledgebaseService.save(db, **payload):
            return get_error_data_result(retmsg="Create dataset error.(Database error)")

        ok, k = KnowledgebaseService.get_by_id(db, payload["id"])
        if not ok:
            return get_error_data_result(retmsg="Dataset created failed")

        response_data = remap_dictionary_keys(k.to_dict())
        return get_result(data=response_data)
    except ValueError as e:
        return get_error_data_result(retmsg=str(e))
    except Exception as e:
        logging.exception(e)
        return get_error_data_result(retmsg="Database operation failed")


@router.delete("/datasets", summary="删除数据集")
def delete(
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
        if req["ids"] is None or len(req["ids"]) == 0:
            return get_result()

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
        db_type = settings.docStoreConn.db_type()
        is_tenant_scoped = db_type in {"elasticsearch", "opensearch"}
        for kb_id, kb in kb_id_instance_pairs:
            for doc in DocumentService.query(db, kb_id=kb_id):
                if not DocumentService.remove_document(db, doc, tenant_id):
                    errors.append(f"Remove document '{doc.id}' error for dataset '{kb_id}'")
                    continue
                f2d = File2DocumentService.get_by_document_id(db, doc.id)
                if f2d:
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

            try:
                if is_tenant_scoped:
                    tenant_index_name = search.index_name(kb.tenant_id)[0]
                    settings.docStoreConn.delete_idx(tenant_index_name, kb_id)
                else:
                    settings.docStoreConn.delete_idx(search.index_name_one(kb.tenant_id, kb.name), kb_id)
            except Exception as e:
                logging.warning(f"Failed to drop index for dataset {kb_id}: {e}")

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

        # Map auto_metadata_config into parser_config if present
        auto_meta = req.pop("auto_metadata_config", None)
        if auto_meta:
            parser_cfg = req.get("parser_config") or {}
            fields = []
            for f in auto_meta.get("fields", []):
                fields.append(
                    {
                        "name": f.get("name", ""),
                        "type": f.get("type", ""),
                        "description": f.get("description"),
                        "examples": f.get("examples"),
                        "restrict_values": f.get("restrict_values", False),
                    }
                )
            parser_cfg["metadata"] = fields
            parser_cfg["enable_metadata"] = auto_meta.get("enabled", True)
            req["parser_config"] = parser_cfg

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

        req = ensure_tenant_model_id_for_params(db, tenant_id, req)
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
            [m.tenant_id for m in tenants],
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


class AutoMetadataConfigRequest(BaseModel):
    enabled: bool = True
    fields: list[dict[str, Any]] = []


@router.get("/datasets/{dataset_id}/auto_metadata", summary="获取数据集自动元数据配置")
def get_auto_metadata(
    dataset_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    获取数据集的自动元数据配置
    """
    try:
        kb = KnowledgebaseService.get_or_none(db, id=dataset_id, tenant_id=tenant_id)
        if kb is None:
            return get_error_data_result(
                retmsg=f"User '{tenant_id}' lacks permission for dataset '{dataset_id}'"
            )

        parser_cfg = kb.parser_config or {}
        metadata = parser_cfg.get("metadata") or []
        enabled = parser_cfg.get("enable_metadata", bool(metadata))
        fields = []
        for f in metadata:
            if not isinstance(f, dict):
                continue
            fields.append(
                {
                    "name": f.get("name", ""),
                    "type": f.get("type", ""),
                    "description": f.get("description"),
                    "examples": f.get("examples"),
                    "restrict_values": f.get("restrict_values", False),
                }
            )
        return get_result(data={"enabled": enabled, "fields": fields})
    except OperationalError as e:
        logging.exception(e)
        return get_error_data_result(retmsg="Database operation failed")


@router.put("/datasets/{dataset_id}/auto_metadata", summary="更新数据集自动元数据配置")
def update_auto_metadata(
    dataset_id: str,
    request: AutoMetadataConfigRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    更新数据集的自动元数据配置
    """
    cfg = request.model_dump()

    try:
        kb = KnowledgebaseService.get_or_none(db, id=dataset_id, tenant_id=tenant_id)
        if kb is None:
            return get_error_data_result(
                retmsg=f"User '{tenant_id}' lacks permission for dataset '{dataset_id}'"
            )

        parser_cfg = kb.parser_config or {}
        fields = []
        for f in cfg.get("fields", []):
            fields.append(
                {
                    "name": f.get("name", ""),
                    "type": f.get("type", ""),
                    "description": f.get("description"),
                    "examples": f.get("examples"),
                    "restrict_values": f.get("restrict_values", False),
                }
            )
        parser_cfg["metadata"] = fields
        parser_cfg["enable_metadata"] = cfg.get("enabled", True)

        if not KnowledgebaseService.update_by_id(db, kb.id, {"parser_config": parser_cfg}):
            return get_error_data_result(retmsg="Update auto-metadata error.(Database error)")

        return get_result(data={"enabled": parser_cfg["enable_metadata"], "fields": fields})
    except OperationalError as e:
        logging.exception(e)
        return get_error_data_result(retmsg="Database operation failed")


@router.get("/datasets/{dataset_id}/knowledge_graph", summary="获取数据集知识图谱")
async def get_knowledge_graph(
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
            retcode=RetCode.AUTHENTICATION_ERROR
        )
    
    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    req = {
        "kb_id": [dataset_id],
        "knowledge_graph_kwd": ["graph"]
    }

    obj = {"graph": {}, "mind_map": {}}
    if not settings.docStoreConn.index_exist(search.index_name_one(kb.tenant_id, kb.name), dataset_id):
        return get_result(data=obj)
    
    sres = await settings.retriever.search(req, search.index_name_one(kb.tenant_id, kb.name), [dataset_id])
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
            retcode=RetCode.AUTHENTICATION_ERROR
        )
    
    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    settings.docStoreConn.delete(
        {"knowledge_graph_kwd": ["graph", "subgraph", "entity", "relation"]}, 
        search.index_name_one(kb.tenant_id, kb.name),
        dataset_id
    )

    return get_result(data=True)


@router.post("/datasets/{dataset_id}/run_graphrag", summary="运行GraphRAG任务")
def run_graphrag(
    dataset_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    运行GraphRAG任务生成知识图谱

    Args:
        dataset_id: 数据集ID
        db: 数据库会话
        tenant_id: 租户ID

    Returns:
        graphrag_task_id: 任务ID
    """
    if not dataset_id:
        return get_error_data_result(retmsg='Lack of "Dataset ID"')
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return get_result(
            data=False,
            retmsg='No authorization.',
            retcode=RetCode.AUTHENTICATION_ERROR
        )

    ok, kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not ok:
        return get_error_data_result(retmsg="Invalid Dataset ID")

    task_id = kb.graphrag_task_id
    if task_id:
        task = TaskService.get_by_id(db, task_id)
        if not task:
            logging.warning(f"A valid GraphRAG task id is expected for Dataset {dataset_id}")

        if task and task.progress not in [-1, 1]:
            return get_error_data_result(retmsg=f"Task {task_id} in progress with status {task.progress}. A Graph Task is already running.")

    documents, _ = DocumentService.get_by_kb_id(
        db,
        kb_id=dataset_id,
        page_number=0,
        items_per_page=0,
        orderby="create_time",
        desc=False,
        keywords="",
        run_status=[],
        types=[],
        suffix=[],
    )
    if not documents:
        return get_error_data_result(retmsg=f"No documents in Dataset {dataset_id}")

    sample_document = documents[0]
    document_ids = [document["id"] for document in documents]

    task_id = queue_raptor_o_graphrag_tasks(
        db,
        sample_doc_id=sample_document,
        ty="graphrag",
        priority=0,
        fake_doc_id=GRAPH_RAPTOR_FAKE_DOC_ID,
        doc_ids=list(document_ids)
    )

    if not KnowledgebaseService.update_by_id(db, kb.id, {"graphrag_task_id": task_id}):
        logging.warning(f"Cannot save graphrag_task_id for Dataset {dataset_id}")

    return get_result(data={"graphrag_task_id": task_id})


@router.get("/datasets/{dataset_id}/trace_graphrag", summary="追踪GraphRAG任务状态")
def trace_graphrag(
    dataset_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    追踪GraphRAG任务状态

    Args:
        dataset_id: 数据集ID
        db: 数据库会话
        tenant_id: 租户ID

    Returns:
        任务状态信息
    """
    if not dataset_id:
        return get_error_data_result(retmsg='Lack of "Dataset ID"')
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return get_result(
            data=False,
            retmsg='No authorization.',
            retcode=RetCode.AUTHENTICATION_ERROR
        )

    ok, kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not ok:
        return get_error_data_result(retmsg="Invalid Dataset ID")

    task_id = kb.graphrag_task_id
    if not task_id:
        return get_result(data={})

    task = TaskService.get_by_id(db, task_id)
    if not task:
        return get_result(data={})

    return get_result(data=task.to_dict())


@router.post("/datasets/{dataset_id}/run_raptor", summary="运行RAPTOR任务")
def run_raptor(
    dataset_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    运行RAPTOR任务

    Args:
        dataset_id: 数据集ID
        db: 数据库会话
        tenant_id: 租户ID

    Returns:
        raptor_task_id: 任务ID
    """
    if not dataset_id:
        return get_error_data_result(retmsg='Lack of "Dataset ID"')
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return get_result(
            data=False,
            retmsg='No authorization.',
            retcode=RetCode.AUTHENTICATION_ERROR
        )

    ok, kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not ok:
        return get_error_data_result(retmsg="Invalid Dataset ID")

    task_id = kb.raptor_task_id
    if task_id:
        task = TaskService.get_by_id(db, task_id)
        if not task:
            logging.warning(f"A valid RAPTOR task id is expected for Dataset {dataset_id}")

        if task and task.progress not in [-1, 1]:
            return get_error_data_result(retmsg=f"Task {task_id} in progress with status {task.progress}. A RAPTOR Task is already running.")

    documents, _ = DocumentService.get_by_kb_id(
        db,
        kb_id=dataset_id,
        page_number=0,
        items_per_page=0,
        orderby="create_time",
        desc=False,
        keywords="",
        run_status=[],
        suffix=[],
        types=[],
    )
    if not documents:
        return get_error_data_result(retmsg=f"No documents in Dataset {dataset_id}")

    sample_document = documents[0]
    document_ids = [document["id"] for document in documents]

    task_id = queue_raptor_o_graphrag_tasks(
        db,
        sample_doc_id=sample_document,
        ty="raptor",
        priority=0,
        fake_doc_id=GRAPH_RAPTOR_FAKE_DOC_ID,
        doc_ids=list(document_ids)
    )

    if not KnowledgebaseService.update_by_id(db, kb.id, {"raptor_task_id": task_id}):
        logging.warning(f"Cannot save raptor_task_id for Dataset {dataset_id}")

    return get_result(data={"raptor_task_id": task_id})


@router.get("/datasets/{dataset_id}/trace_raptor", summary="追踪RAPTOR任务状态")
def trace_raptor(
    dataset_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    追踪RAPTOR任务状态

    Args:
        dataset_id: 数据集ID
        db: 数据库会话
        tenant_id: 租户ID

    Returns:
        任务状态信息
    """
    if not dataset_id:
        return get_error_data_result(retmsg='Lack of "Dataset ID"')
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return get_result(
            data=False,
            retmsg='No authorization.',
            retcode=RetCode.AUTHENTICATION_ERROR
        )

    ok, kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not ok:
        return get_error_data_result(retmsg="Invalid Dataset ID")

    task_id = kb.raptor_task_id
    if not task_id:
        return get_result(data={})

    task = TaskService.get_by_id(db, task_id)
    if not task:
        return get_error_data_result(retmsg="RAPTOR Task Not Found or Error Occurred")

    return get_result(data=task.to_dict())
