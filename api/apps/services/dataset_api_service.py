"""
@project: multirag
@Author：龙
@file： dataset_api_service.py
@date：2026/06/04
@desc: Dataset API 业务逻辑层 - 从 gateway 层解耦的业务处理。

约定：所有函数统一返回 (success: bool, result | error_message)：
    - success=True  -> result 为数据载荷（dict / list / bool / None）
    - success=False -> result 为错误信息字符串
  本层不返回 HTTP 响应对象（HTTP 包装交由 restful_apis/dataset_api.py 网关层完成）。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from sqlalchemy.orm import Session

from api.db.db_models import File
from api.db.services.connector_service import Connector2KbService
from api.db.services.document_service import DocumentService, queue_raptor_o_graphrag_tasks
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.task_service import GRAPH_RAPTOR_FAKE_DOC_ID, TaskService
from api.db.services.user_service import TenantService, UserService
from api.utils.api_utils import deep_merge, flatten_parent_child_config, get_parser_config, remap_dictionary_keys, verify_embedding_availability
from api.utils.tenant_utils import ensure_tenant_model_id_for_params
from common import settings
from common.constants import PAGERANK_FLD, FileSource, StatusEnum
from core.nlp import search

logger = logging.getLogger(__name__)


def _tag_chunk_method_guard(parser_id: str | None) -> str | None:
    """`tag` chunking 在 Infinity/Milvus 暂不支持（承接旧 web 守卫）。返回错误信息或 None。"""
    if not parser_id or str(parser_id).lower() != "tag":
        return None
    if settings.DOC_ENGINE_INFINITY:
        return "The chunking method Tag has not been supported by Infinity yet."
    if os.environ.get("DOC_ENGINE", "milvus") == "milvus":
        return "The chunking method Tag has not been supported by milvus yet."
    return None


def _normalize_metadata_fields(raw_fields: list, *, skip_non_dict: bool = False) -> list[dict]:
    """将原始元数据字段列表规范化为统一结构。"""
    fields = []
    for f in raw_fields:
        if skip_non_dict and not isinstance(f, dict):
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
    return fields


def create_dataset(db: Session, tenant_id: str, req: dict) -> tuple[bool, Any]:
    """创建数据集。"""
    # 承接 ext：把前端塞进 ext 的旧 web 参数合并回 req
    ext_fields = req.pop("ext", None) or {}

    # 字段名转换
    embd_id = req.pop("embedding_model", None) or None
    parser_id = req.pop("chunk_method", None) or None
    req.pop("parse_type", None)

    # auto_metadata_config -> parser_config.metadata
    auto_meta = req.pop("auto_metadata_config", None)
    if auto_meta:
        parser_cfg = req.get("parser_config") or {}
        parser_cfg["metadata"] = _normalize_metadata_fields(auto_meta.get("fields", []))
        parser_cfg["enable_metadata"] = auto_meta.get("enabled", True)
        req["parser_config"] = parser_cfg

    req.update(ext_fields)

    final_parser_id = parser_id or "naive"
    if err := _tag_chunk_method_guard(final_parser_id):
        return False, err

    if KnowledgebaseService.get_or_none(db, name=req["name"], tenant_id=tenant_id, status=StatusEnum.VALID.value):
        return False, f"Dataset name '{req['name']}' already exists"

    try:
        parser_config = get_parser_config(final_parser_id, req.pop("parser_config", None))

        # create_with_name 会自动处理 embd_id 默认值；剩余 req（avatar/description/permission + ext 透传字段）作为 kwargs
        e, payload = KnowledgebaseService.create_with_name(
            db=db,
            name=req.pop("name"),
            tenant_id=tenant_id,
            parser_id=final_parser_id,
            embd_id=embd_id,
            parser_config=parser_config,
            **req,
        )
        # 注意：create_with_name 失败时返回的是已构造好的 HTTP 响应（遗留实现），
        # 这里原样透传，由网关层识别 Response 后直接返回。
        if not e:
            return False, payload

        # 用户显式指定了 embd_id 时校验可用性
        if embd_id:
            ok, err = verify_embedding_availability(db, payload["embd_id"], tenant_id)
            if not ok:
                return False, err

        payload = ensure_tenant_model_id_for_params(db, tenant_id, payload)
        if not KnowledgebaseService.save(db, **payload):
            return False, "Create dataset error.(Database error)"

        k = KnowledgebaseService.get_by_id(db, payload["id"])
        if not k:
            return False, "Dataset created failed"

        return True, remap_dictionary_keys(k.to_dict())
    except ValueError as e:
        return False, str(e)


def delete_datasets(db: Session, tenant_id: str, ids: list | None = None, delete_all: bool = False) -> tuple[bool, Any]:
    """删除数据集。"""
    kb_id_instance_pairs = []
    if not ids:
        if delete_all:
            ids = [kb.id for kb in KnowledgebaseService.query(db, tenant_id=tenant_id)]
            if not ids:
                return True, None
        else:
            return True, None

    error_kb_ids = []
    for kb_id in ids:
        kb = KnowledgebaseService.get_or_none(db, id=kb_id, tenant_id=tenant_id)
        if kb is None:
            error_kb_ids.append(kb_id)
            continue
        kb_id_instance_pairs.append((kb_id, kb))
    if error_kb_ids:
        return False, f"""User '{tenant_id}' lacks permission for datasets: '{", ".join(error_kb_ids)}'"""

    errors = []
    success_count = 0
    db_type = settings.docStoreConn.db_type()
    is_tenant_scoped = db_type in {"elasticsearch", "opensearch"}
    for kb_id, kb in kb_id_instance_pairs:
        for doc in DocumentService.query(db, kb_id=kb_id):
            if not DocumentService.remove_document(db, doc, tenant_id):
                errors.append(f"Remove document '{doc.id}' error for dataset '{kb_id}'")
                continue
        FileService.filter_delete(
            db,
            [File.source_type == FileSource.KNOWLEDGEBASE, File.type == "folder", File.name == kb.name],
        )

        # 承接旧 web：删除数据集对应的存储桶/目录（部分后端支持）
        if hasattr(settings.STORAGE_IMPL, "remove_bucket"):
            try:
                settings.STORAGE_IMPL.remove_bucket(kb_id)
            except Exception as e:
                logger.warning(f"Failed to remove bucket for dataset {kb_id}: {e}")

        try:
            if is_tenant_scoped:
                # 共享租户索引（ES/OpenSearch）：先按 kb_id 删内容，再 drop 索引，避免残留/误删其它 KB
                tenant_index_name = search.index_name(kb.tenant_id)[0]
                settings.docStoreConn.delete({"kb_id": kb_id}, tenant_index_name, kb_id)
                settings.docStoreConn.delete_idx(tenant_index_name, kb_id)
            else:
                settings.docStoreConn.delete_idx(search.index_name_one(kb.tenant_id, kb.name), kb_id)
        except Exception as e:
            logger.warning(f"Failed to drop index for dataset {kb_id}: {e}")

        if not KnowledgebaseService.delete_by_id(db, kb_id):
            errors.append(f"Delete dataset error for {kb_id}")
            continue
        success_count += 1

    if not errors:
        return True, None

    if success_count == 0:
        error_message = f"Successfully deleted {success_count} datasets, {len(errors)} failed. Details: {'; '.join(errors)[:128]}..."
        return False, error_message

    return True, {"success_count": success_count, "errors": errors[:5]}


def update_dataset(db: Session, tenant_id: str, dataset_id: str, req: dict) -> tuple[bool, Any]:
    """更新数据集。"""
    # 字段名转换
    if "embedding_model" in req:
        req["embd_id"] = req.pop("embedding_model")
    if "chunk_method" in req:
        req["parser_id"] = req.pop("chunk_method")

    # 承接 ext：合并前端塞进 ext 的旧 web 参数
    ext_fields = req.pop("ext", None) or {}

    # auto_metadata_config -> parser_config
    auto_meta = req.pop("auto_metadata_config", None)
    if auto_meta:
        parser_cfg = req.get("parser_config") or {}
        parser_cfg["metadata"] = _normalize_metadata_fields(auto_meta.get("fields", []))
        parser_cfg["enable_metadata"] = auto_meta.get("enabled", True)
        req["parser_config"] = parser_cfg

    req.update(ext_fields)

    if not req:
        return False, "No properties were modified"

    kb = KnowledgebaseService.get_or_none(db, id=dataset_id, tenant_id=tenant_id)
    if kb is None:
        return False, f"User '{tenant_id}' lacks permission for dataset '{dataset_id}'"

    # tag 守卫（parser_id 可能来自 chunk_method 或 ext）
    if (pid := req.get("parser_id")) and (err := _tag_chunk_method_guard(pid)):
        return False, err

    # 抽出 connectors，不写入 KB 表（仅在显式传入时绑定，避免误清空已有关联）
    connectors_provided = "connectors" in req
    connectors = req.pop("connectors", None) or []

    if req.get("parser_config"):
        parser_config = req["parser_config"]
        parser_config.update(parser_config.pop("ext", {}) or {})
        req["parser_config"] = flatten_parent_child_config(deep_merge(kb.parser_config, parser_config))

    if (chunk_method := req.get("parser_id")) and chunk_method != kb.parser_id:
        if not req.get("parser_config"):
            req["parser_config"] = get_parser_config(chunk_method, None)
    elif "parser_config" in req and not req["parser_config"]:
        del req["parser_config"]

    # 从 pipeline dataset 切回普通 parser 时清空旧 pipeline_id；
    # 我们 KB 模型无 parse_type 列，故无需处理 parse_type
    if kb.pipeline_id and req.get("parser_id") and not req.get("pipeline_id"):
        req["pipeline_id"] = ""

    if "name" in req and req["name"].lower() != kb.name.lower():
        if KnowledgebaseService.get_or_none(db, name=req["name"], tenant_id=tenant_id, status=StatusEnum.VALID.value):
            return False, f"Dataset name '{req['name']}' already exists"
        # 承接旧 web：知识库改名时同步重命名 File folder
        FileService.filter_update(
            db,
            [
                File.tenant_id == kb.tenant_id,
                File.source_type == FileSource.KNOWLEDGEBASE,
                File.type == "folder",
                File.name == kb.name,
            ],
            {"name": req["name"]},
        )

    if "embd_id" in req:
        if not req["embd_id"]:
            req["embd_id"] = kb.embd_id
        if kb.chunk_num != 0 and req["embd_id"] != kb.embd_id:
            return False, f"When chunk_num ({kb.chunk_num}) > 0, embedding_model must remain {kb.embd_id}"
        ok, err = verify_embedding_availability(db, req["embd_id"], tenant_id)
        if not ok:
            return False, err

    if "pagerank" in req and req["pagerank"] != kb.pagerank:
        if os.environ.get("DOC_ENGINE", "elasticsearch") == "infinity":
            return False, "'pagerank' can only be set when doc_engine is elasticsearch"

        if req["pagerank"] > 0:
            settings.docStoreConn.update({"kb_id": kb.id}, {PAGERANK_FLD: req["pagerank"]}, search.index_name(kb.tenant_id), kb.id)
        else:
            # Elasticsearch requires PAGERANK_FLD be non-zero!
            settings.docStoreConn.update({"exists": PAGERANK_FLD}, {"remove": PAGERANK_FLD}, search.index_name(kb.tenant_id), kb.id)

    req = ensure_tenant_model_id_for_params(db, tenant_id, req)
    if not KnowledgebaseService.update_by_id(db, kb.id, req):
        return False, "Update dataset error.(Database error)"

    # 绑定 connectors（不写入 KB 表；仅在显式传入时操作）
    if connectors_provided:
        errors = Connector2KbService.link_connectors(db, kb.id, list(connectors), tenant_id)
        if errors:
            logger.error("Link KB connectors errors: %s", errors)

    k = KnowledgebaseService.get_by_id(db, kb.id)
    if not k:
        return False, "Dataset updated failed"

    response_data = remap_dictionary_keys(k.to_dict())
    response_data["connectors"] = connectors
    return True, response_data


def list_datasets(db: Session, tenant_id: str, args: dict) -> tuple[bool, Any]:
    """获取数据集列表，返回 {"data": [...], "total": int}。"""
    kb_id = args.get("id")
    name = args.get("name")
    page = args.get("page", 1)
    page_size = args.get("page_size", 30)
    orderby = args.get("orderby", "create_time")
    desc = args.get("desc", True)
    keywords = args.get("keywords") or ""
    parser_id = args.get("parser_id")
    owner_ids = args.get("owner_ids") or []
    include_parsing_status = args.get("include_parsing_status", False)

    if kb_id and not KnowledgebaseService.get_kb_by_id(db, kb_id, tenant_id):
        return False, f"User '{tenant_id}' lacks permission for dataset '{kb_id}'"
    if name and not KnowledgebaseService.get_kb_by_name(db, name, tenant_id):
        return False, f"User '{tenant_id}' lacks permission for dataset '{name}'"

    # owner_ids 承接：指定时按其过滤；get_list 内部仍强制 TEAM 可见或本人，安全
    if owner_ids:
        tenant_ids = owner_ids
    else:
        tenants = TenantService.get_joined_tenants_by_user_id(db, tenant_id)
        tenant_ids = [m.tenant_id for m in tenants]

    kbs, total = KnowledgebaseService.get_list(
        db,
        tenant_ids,
        tenant_id,
        page,
        page_size,
        orderby,
        desc,
        kb_id,
        name,
        keywords,
        parser_id,
    )

    # 补 nickname / tenant_avatar（对标 ragflow）
    user_map = {}
    owner_id_set = {kb.get("tenant_id") for kb in kbs if kb.get("tenant_id")}
    if owner_id_set:
        users = UserService.get_by_ids(db, list(owner_id_set))
        user_map = {u.id: u.to_dict() for u in users}

    parsing_status_map = {}
    if include_parsing_status and kbs:
        kb_ids = [kb["id"] for kb in kbs]
        parsing_status_map = DocumentService.get_parsing_status_by_kb_ids(db, kb_ids)

    response_data_list = []
    for kb in kbs:
        user_dict = user_map.get(kb.get("tenant_id"), {})
        kb["nickname"] = user_dict.get("nickname", "")
        kb["tenant_avatar"] = user_dict.get("avatar", "")
        data = remap_dictionary_keys(kb)
        if include_parsing_status:
            data.update(parsing_status_map.get(kb["id"], {}))
        response_data_list.append(data)
    return True, {"data": response_data_list, "total": total}


def get_auto_metadata(db: Session, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    """获取数据集的自动元数据配置。"""
    kb = KnowledgebaseService.get_or_none(db, id=dataset_id, tenant_id=tenant_id)
    if kb is None:
        return False, f"User '{tenant_id}' lacks permission for dataset '{dataset_id}'"

    parser_cfg = kb.parser_config or {}
    metadata = parser_cfg.get("metadata") or []
    enabled = parser_cfg.get("enable_metadata", bool(metadata))
    fields = _normalize_metadata_fields(metadata, skip_non_dict=True)
    return True, {"enabled": enabled, "fields": fields}


def update_auto_metadata(db: Session, tenant_id: str, dataset_id: str, cfg: dict) -> tuple[bool, Any]:
    """更新数据集的自动元数据配置。"""
    kb = KnowledgebaseService.get_or_none(db, id=dataset_id, tenant_id=tenant_id)
    if kb is None:
        return False, f"User '{tenant_id}' lacks permission for dataset '{dataset_id}'"

    parser_cfg = kb.parser_config or {}
    fields = _normalize_metadata_fields(cfg.get("fields", []))
    parser_cfg["metadata"] = fields
    parser_cfg["enable_metadata"] = cfg.get("enabled", True)

    if not KnowledgebaseService.update_by_id(db, kb.id, {"parser_config": parser_cfg}):
        return False, "Update auto-metadata error.(Database error)"

    return True, {"enabled": parser_cfg["enable_metadata"], "fields": fields}


async def get_knowledge_graph(db: Session, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    """获取数据集的知识图谱。失败时返回 (False, "No authorization.")。"""
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return False, "No authorization."

    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    req = {"kb_id": [dataset_id], "knowledge_graph_kwd": ["graph"]}

    obj = {"graph": {}, "mind_map": {}}
    if not settings.docStoreConn.index_exist(search.index_name_one(kb.tenant_id, kb.name), dataset_id):
        return True, obj

    sres = await settings.retriever.search(req, search.index_name_one(kb.tenant_id, kb.name), [dataset_id])
    if not len(sres.ids):
        return True, obj

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

    return True, obj


def delete_knowledge_graph(db: Session, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    """删除数据集的知识图谱。失败时返回 (False, "No authorization.")。"""
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return False, "No authorization."

    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    settings.docStoreConn.delete(
        {"knowledge_graph_kwd": ["graph", "subgraph", "entity", "relation"]},
        search.index_name_one(kb.tenant_id, kb.name),
        dataset_id,
    )
    return True, True


def run_graphrag(db: Session, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    """运行 GraphRAG 任务生成知识图谱。"""
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return False, "No authorization."

    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not kb:
        return False, "Invalid Dataset ID"

    task_id = kb.graphrag_task_id
    if task_id:
        task = TaskService.get_by_id(db, task_id)
        if not task:
            logger.warning(f"A valid GraphRAG task id is expected for Dataset {dataset_id}")
        if task and task.progress not in [-1, 1]:
            return False, f"Task {task_id} in progress with status {task.progress}. A Graph Task is already running."

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
        return False, f"No documents in Dataset {dataset_id}"

    sample_document = documents[0]
    document_ids = [document["id"] for document in documents]

    task_id = queue_raptor_o_graphrag_tasks(
        db,
        sample_doc_id=sample_document,
        ty="graphrag",
        priority=0,
        fake_doc_id=GRAPH_RAPTOR_FAKE_DOC_ID,
        doc_ids=list(document_ids),
    )

    if not KnowledgebaseService.update_by_id(db, kb.id, {"graphrag_task_id": task_id}):
        logger.warning(f"Cannot save graphrag_task_id for Dataset {dataset_id}")

    return True, {"graphrag_task_id": task_id}


def trace_graphrag(db: Session, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    """追踪 GraphRAG 任务状态。"""
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return False, "No authorization."

    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not kb:
        return False, "Invalid Dataset ID"

    task_id = kb.graphrag_task_id
    if not task_id:
        return True, {}

    task = TaskService.get_by_id(db, task_id)
    if not task:
        return True, {}

    return True, task.to_dict()


def run_raptor(db: Session, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    """运行 RAPTOR 任务。"""
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return False, "No authorization."

    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not kb:
        return False, "Invalid Dataset ID"

    task_id = kb.raptor_task_id
    if task_id:
        task = TaskService.get_by_id(db, task_id)
        if not task:
            logger.warning(f"A valid RAPTOR task id is expected for Dataset {dataset_id}")
        if task and task.progress not in [-1, 1]:
            return False, f"Task {task_id} in progress with status {task.progress}. A RAPTOR Task is already running."

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
        return False, f"No documents in Dataset {dataset_id}"

    sample_document = documents[0]
    document_ids = [document["id"] for document in documents]

    task_id = queue_raptor_o_graphrag_tasks(
        db,
        sample_doc_id=sample_document,
        ty="raptor",
        priority=0,
        fake_doc_id=GRAPH_RAPTOR_FAKE_DOC_ID,
        doc_ids=list(document_ids),
    )

    if not KnowledgebaseService.update_by_id(db, kb.id, {"raptor_task_id": task_id}):
        logger.warning(f"Cannot save raptor_task_id for Dataset {dataset_id}")

    return True, {"raptor_task_id": task_id}


def trace_raptor(db: Session, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    """追踪 RAPTOR 任务状态。"""
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return False, "No authorization."

    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not kb:
        return False, "Invalid Dataset ID"

    task_id = kb.raptor_task_id
    if not task_id:
        return True, {}

    task = TaskService.get_by_id(db, task_id)
    if not task:
        return False, "RAPTOR Task Not Found or Error Occurred"

    return True, task.to_dict()
