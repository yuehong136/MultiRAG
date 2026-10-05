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

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.db.db_models import Document, File, Knowledgebase, Task, db_connection
from api.db.services.connector_service import Connector2KbService
from api.db.services.doc_metadata_service import DocMetadataService
from api.db.services.document_service import DocumentService, queue_raptor_o_graphrag_tasks
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.pipeline_operation_log_service import PipelineOperationLogService
from api.db.services.task_service import GRAPH_RAPTOR_FAKE_DOC_ID, TaskService
from api.db.services.user_service import TenantService, UserService, UserTenantService
from api.utils.api_utils import deep_merge, flatten_parent_child_config, get_parser_config, remap_dictionary_keys, verify_embedding_availability
from api.utils.tenant_utils import ensure_tenant_model_id_for_params
from common import settings
from common.constants import PAGERANK_FLD, FileSource, StatusEnum, TaskStatus
from common.metadata_config import apply_metadata_config, metadata_config_view
from core.nlp import search
from core.utils.redis_conn import REDIS_CONN

logger = logging.getLogger(__name__)

# 统一索引任务（graph/raptor/mindmap）的类型表。graph 对外用 "graph"，
# 内部任务类型与 KB 列名仍是历史的 "graphrag"。
VALID_INDEX_TYPES = ("graph", "raptor", "mindmap")

_INDEX_TYPE_TO_TASK_TYPE = {
    "graph": "graphrag",
    "raptor": "raptor",
    "mindmap": "mindmap",
}

_INDEX_TYPE_TO_TASK_ID_FIELD = {
    "graph": "graphrag_task_id",
    "raptor": "raptor_task_id",
    "mindmap": "mindmap_task_id",
}

_INDEX_TYPE_TO_DISPLAY_NAME = {
    "graph": "Graph",
    "raptor": "RAPTOR",
    "mindmap": "Mindmap",
}


def _tag_chunk_method_guard(parser_id: str | None) -> str | None:
    """`tag` chunking 在 Infinity/Milvus 暂不支持（承接旧 web 守卫）。返回错误信息或 None。"""
    if not parser_id or str(parser_id).lower() != "tag":
        return None
    if settings.DOC_ENGINE_INFINITY:
        return "The chunking method Tag has not been supported by Infinity yet."
    if os.environ.get("DOC_ENGINE", "milvus") == "milvus":
        return "The chunking method Tag has not been supported by milvus yet."
    return None


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
    if auto_meta is not None:
        parser_cfg = apply_metadata_config(req.get("parser_config") or {}, auto_meta)
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


async def delete_datasets_async(tenant_id: str, ids: list | None = None, delete_all: bool = False) -> tuple[bool, Any]:
    """delete_datasets 的异步入口：DB 与 doc-store/存储/Redis 深度交错（remove_document、
    remove_bucket、delete_idx），且交错点在共享 service 内部无法拆面——run_sync 只桥
    session 自身的 IO，故整块进工作线程 + 自开短会话（§11.12 混轨块判例的合法形态）。"""

    def _run() -> tuple[bool, Any]:
        with db_connection() as s:
            return delete_datasets(s, tenant_id, ids, delete_all)

    return await asyncio.to_thread(_run)


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
    if auto_meta is not None:
        parser_cfg = apply_metadata_config(req.get("parser_config") or {}, auto_meta)
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
        parser_config = flatten_parent_child_config(parser_config)
        req["parser_config"] = deep_merge(kb.parser_config, parser_config)
        if parser_config.get("parent_child") == {}:
            req["parser_config"]["parent_child"] = {}

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


async def update_dataset_async(tenant_id: str, dataset_id: str, req: dict) -> tuple[bool, Any]:
    """update_dataset 的异步入口：pagerank 分支内联 doc-store 同步 HTTP，混轨块整体
    进工作线程 + 自开短会话（同 delete_datasets_async 口径）。"""

    def _run() -> tuple[bool, Any]:
        with db_connection() as s:
            return update_dataset(s, tenant_id, dataset_id, req)

    return await asyncio.to_thread(_run)


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
    return True, {**metadata_config_view(parser_cfg), "enabled": parser_cfg.get("enable_metadata", False)}


def update_auto_metadata(db: Session, tenant_id: str, dataset_id: str, cfg: dict) -> tuple[bool, Any]:
    """更新数据集的自动元数据配置。"""
    kb = KnowledgebaseService.get_or_none(db, id=dataset_id, tenant_id=tenant_id)
    if kb is None:
        return False, f"User '{tenant_id}' lacks permission for dataset '{dataset_id}'"

    parser_cfg = apply_metadata_config(kb.parser_config or {}, cfg)
    if not KnowledgebaseService.update_by_id(db, kb.id, {"parser_config": parser_cfg}):
        return False, "Update auto-metadata error.(Database error)"

    return True, {**metadata_config_view(parser_cfg), "enabled": parser_cfg.get("enable_metadata", False)}


async def get_knowledge_graph(db: AsyncSession, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    """获取数据集的知识图谱。失败时返回 (False, "No authorization.")。"""
    if not await db.run_sync(lambda s: KnowledgebaseService.accessible(s, dataset_id, tenant_id)):  # TODO(async-phase4)
        return False, "No authorization."

    kb = await db.run_sync(lambda s: KnowledgebaseService.get_by_id(s, dataset_id))  # TODO(async-phase4)
    req = {"kb_id": [dataset_id], "knowledge_graph_kwd": ["graph"]}

    obj = {"graph": {}, "mind_map": {}}
    # doc-store 探测是同步 HTTP：不持有 Session，to_thread 外移避免阻塞事件循环
    if not await asyncio.to_thread(settings.docStoreConn.index_exist, search.index_name_one(kb.tenant_id, kb.name), dataset_id):
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


async def delete_knowledge_graph(db: AsyncSession, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    """删除数据集的知识图谱。失败时返回 (False, "No authorization.")。"""
    if not await db.run_sync(lambda s: KnowledgebaseService.accessible(s, dataset_id, tenant_id)):  # TODO(async-phase4)
        return False, "No authorization."

    def _kb_index(s: Session) -> str:
        kb = KnowledgebaseService.get_by_id(s, dataset_id)
        return search.index_name_one(kb.tenant_id, kb.name)

    index_name = await db.run_sync(_kb_index)  # TODO(async-phase4)
    # doc-store 删除是同步 HTTP：不持有 Session，to_thread 外移避免阻塞事件循环
    await asyncio.to_thread(
        settings.docStoreConn.delete,
        {"knowledge_graph_kwd": ["graph", "subgraph", "entity", "relation"]},
        index_name,
        dataset_id,
    )
    return True, True


def run_graphrag(db: Session, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    """Compatibility adapter retaining the legacy task-id field."""
    success, result = run_index(db, tenant_id, dataset_id, "graph")
    if success:
        return True, {"graphrag_task_id": result["task_id"]}
    return False, result


async def run_graphrag_async(tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    def _run() -> tuple[bool, Any]:
        with db_connection() as s:
            return run_graphrag(s, tenant_id, dataset_id)

    return await asyncio.to_thread(_run)


async def trace_graphrag(db: AsyncSession, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    return await trace_index(db, tenant_id, dataset_id, "graph")


def run_raptor(db: Session, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    """Compatibility adapter retaining the legacy task-id field."""
    success, result = run_index(db, tenant_id, dataset_id, "raptor")
    if success:
        return True, {"raptor_task_id": result["task_id"]}
    return False, result


async def run_raptor_async(tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    def _run() -> tuple[bool, Any]:
        with db_connection() as s:
            return run_raptor(s, tenant_id, dataset_id)

    return await asyncio.to_thread(_run)


async def trace_raptor(db: AsyncSession, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    return await trace_index(db, tenant_id, dataset_id, "raptor", missing_task_error="RAPTOR Task Not Found or Error Occurred")


# ==================== 统一索引任务（graph / raptor / mindmap） ====================


def run_index(db: Session, tenant_id: str, dataset_id: str, index_type: str) -> tuple[bool, Any]:
    """运行索引任务（graph/raptor/mindmap），三者共用同一套排队与去重逻辑。"""
    if index_type not in VALID_INDEX_TYPES:
        return False, f"Invalid index type '{index_type}'. Must be one of {sorted(VALID_INDEX_TYPES)}"
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return False, "No authorization."

    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not kb:
        return False, "Invalid Dataset ID"

    task_type = _INDEX_TYPE_TO_TASK_TYPE[index_type]
    task_id_field = _INDEX_TYPE_TO_TASK_ID_FIELD[index_type]
    display_name = _INDEX_TYPE_TO_DISPLAY_NAME[index_type]

    existing_task_id = getattr(kb, task_id_field, None)
    if existing_task_id:
        task = TaskService.get_by_id(db, existing_task_id)
        if not task:
            logger.warning(f"A valid {display_name} task id is expected for Dataset {dataset_id}")
        if task and task.progress not in [-1, 1]:
            return False, f"Task {existing_task_id} in progress with status {task.progress}. A {display_name} Task is already running."

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
        sample_doc=sample_document,
        ty=task_type,
        priority=0,
        fake_doc_id=GRAPH_RAPTOR_FAKE_DOC_ID,
        doc_ids=list(document_ids),
    )

    if not KnowledgebaseService.update_by_id(db, kb.id, {task_id_field: task_id}):
        logger.warning(f"Cannot save {task_id_field} for Dataset {dataset_id}")

    return True, {"task_id": task_id}


async def run_index_async(tenant_id: str, dataset_id: str, index_type: str) -> tuple[bool, Any]:
    """run_index 的异步入口：同 run_graphrag_async 口径（共享 helper 内 DB+Redis 交错）。"""

    def _run() -> tuple[bool, Any]:
        with db_connection() as s:
            return run_index(s, tenant_id, dataset_id, index_type)

    return await asyncio.to_thread(_run)


async def trace_index(db: AsyncSession, tenant_id: str, dataset_id: str, index_type: str, *, missing_task_error: str | None = None) -> tuple[bool, Any]:
    """追踪索引任务（graph/raptor/mindmap）状态。任务未建立时返回空 dict。"""
    if index_type not in VALID_INDEX_TYPES:
        return False, f"Invalid index type '{index_type}'. Must be one of {sorted(VALID_INDEX_TYPES)}"
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'
    if not await KnowledgebaseService.accessible_async(db, dataset_id, tenant_id):
        return False, "No authorization."

    kb = await db.get(Knowledgebase, dataset_id)
    if not kb:
        return False, "Invalid Dataset ID"

    task_id = getattr(kb, _INDEX_TYPE_TO_TASK_ID_FIELD[index_type], None)
    if not task_id:
        return True, {}

    task = await db.get(Task, task_id)
    if not task:
        return (False, missing_task_error) if missing_task_error else (True, {})

    return True, task.to_dict()


def delete_index(db: Session, tenant_id: str, dataset_id: str, index_type: str) -> tuple[bool, Any]:
    """取消并解绑索引任务，同时清掉该任务写进 doc-store 的产物。

    RAPTOR 之外的 mindmap 没有独立产物字段，只解绑任务。
    """
    if index_type not in VALID_INDEX_TYPES:
        return False, f"Invalid index type '{index_type}'. Must be one of {sorted(VALID_INDEX_TYPES)}"
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return False, "No authorization."

    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not kb:
        return False, "Invalid Dataset ID"

    task_id_field = _INDEX_TYPE_TO_TASK_ID_FIELD[index_type]
    task_finish_at_field = task_id_field.replace("_task_id", "_task_finish_at")
    task_id = getattr(kb, task_id_field, None)

    if task_id:
        try:
            REDIS_CONN.set(f"{task_id}-cancel", "x")
        except Exception as e:
            logger.exception(e)
        TaskService.delete_by_id(db, task_id)

    index_name = search.index_name_one(kb.tenant_id, kb.name)
    if index_type == "graph":
        settings.docStoreConn.delete({"knowledge_graph_kwd": ["graph", "subgraph", "entity", "relation"]}, index_name, dataset_id)
    elif index_type == "raptor":
        settings.docStoreConn.delete({"raptor_kwd": ["raptor"]}, index_name, dataset_id)

    if not KnowledgebaseService.update_by_id(db, kb.id, {task_id_field: "", task_finish_at_field: None}):
        return False, f"Internal error: cannot delete {index_type} task"

    return True, {}


async def delete_index_async(tenant_id: str, dataset_id: str, index_type: str) -> tuple[bool, Any]:
    """delete_index 的异步入口：Redis 取消信号 + doc-store 删除 + DB 写交错，
    整块进工作线程 + 自开短会话（同 delete_datasets_async 口径）。"""

    def _run() -> tuple[bool, Any]:
        with db_connection() as s:
            return delete_index(s, tenant_id, dataset_id, index_type)

    return await asyncio.to_thread(_run)


# ==================== 数据集详情与摄取日志 ====================


async def get_dataset(db: AsyncSession, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    """获取单个数据集详情。"""
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'
    if not await KnowledgebaseService.accessible_async(db, dataset_id, tenant_id):
        return False, f"User '{tenant_id}' lacks permission for dataset '{dataset_id}'"

    kb = await db.get(Knowledgebase, dataset_id)
    if not kb:
        return False, "Invalid Dataset ID"

    return True, remap_dictionary_keys(kb.to_dict())


async def get_ingestion_summary(db: AsyncSession, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    """获取数据集的摄取概览：文档/分块/token 计数 + 各解析状态计数。"""
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'
    if not await KnowledgebaseService.accessible_async(db, dataset_id, tenant_id):
        return False, f"User '{tenant_id}' lacks permission for dataset '{dataset_id}'"

    kb = await db.get(Knowledgebase, dataset_id)
    if not kb:
        return False, "Invalid Dataset ID"

    status_fields = {
        TaskStatus.UNSTART.value: "unstart_count",
        TaskStatus.RUNNING.value: "running_count",
        TaskStatus.CANCEL.value: "cancel_count",
        TaskStatus.DONE.value: "done_count",
        TaskStatus.FAIL.value: "fail_count",
    }
    status = dict.fromkeys(status_fields.values(), 0)
    rows = await db.execute(select(Document.run, func.count(Document.id)).where(Document.kb_id == dataset_id).group_by(Document.run))
    for run, count in rows:
        if str(run) in status_fields:
            status[status_fields[str(run)]] = int(count)
    return True, {
        "doc_num": kb.doc_num,
        "chunk_num": kb.chunk_num,
        "token_num": kb.token_num,
        "status": status,
    }


async def list_ingestion_logs(
    db: AsyncSession,
    tenant_id: str,
    dataset_id: str,
    page: int = 0,
    page_size: int = 0,
    orderby: str = "create_time",
    desc: bool = True,
    operation_status: list[str] | None = None,
    create_date_from: datetime | None = None,
    create_date_to: datetime | None = None,
    log_type: str = "dataset",
    keywords: str | None = None,
    types: list[str] | None = None,
    suffix: list[str] | None = None,
) -> tuple[bool, Any]:
    """列出文件或数据集级摄取日志，保留各自字段及筛选合同。"""
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'
    if not await KnowledgebaseService.accessible_async(db, dataset_id, tenant_id):
        return False, "No authorization."

    if log_type not in {"file", "dataset"}:
        return False, 'Invalid "log_type", expected "dataset" or "file"'

    # PostgreSQL create_date is a UTC timestamp without timezone. Normalize
    # explicit offsets before comparing/filtering, including mixed inputs.
    if create_date_from and create_date_from.tzinfo is not None:
        create_date_from = create_date_from.astimezone(UTC).replace(tzinfo=None)
    if create_date_to and create_date_to.tzinfo is not None:
        create_date_to = create_date_to.astimezone(UTC).replace(tzinfo=None)
    if create_date_from and create_date_to and create_date_from > create_date_to:
        return False, "create_date_from must not be later than create_date_to"

    model = PipelineOperationLogService.model
    fields = PipelineOperationLogService.get_file_logs_fields() if log_type == "file" else PipelineOperationLogService.get_dataset_logs_fields()
    document_scope = model.document_id != GRAPH_RAPTOR_FAKE_DOC_ID if log_type == "file" else model.document_id == GRAPH_RAPTOR_FAKE_DOC_ID
    stmt = select(*fields).where(model.kb_id == dataset_id, document_scope)
    if keywords:
        stmt = stmt.where(func.lower(model.document_name).contains(keywords.lower(), autoescape=True))
    if log_type == "file":
        if types:
            stmt = stmt.where(model.document_type.in_(types))
        if suffix:
            stmt = stmt.where(model.document_suffix.in_(suffix))
    if operation_status:
        stmt = stmt.where(model.operation_status.in_(operation_status))
    if create_date_from:
        stmt = stmt.where(model.create_date >= create_date_from)
    if create_date_to:
        stmt = stmt.where(model.create_date <= create_date_to)
    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    columns = {field.key: field for field in fields}
    if orderby not in columns:
        return False, "Invalid orderby field"
    column = columns[orderby]
    stmt = stmt.order_by(column.desc() if desc else column.asc())
    if page and page_size:
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
    logs = [dict(row) for row in (await db.execute(stmt)).mappings()]
    return True, {"total": total, "logs": logs}


async def get_ingestion_log(db: AsyncSession, tenant_id: str, dataset_id: str, log_id: str) -> tuple[bool, Any]:
    """获取单条摄取日志。日志必须属于该数据集，否则视为不存在。"""
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'
    if not await KnowledgebaseService.accessible_async(db, dataset_id, tenant_id):
        return False, "No authorization."

    model = PipelineOperationLogService.model
    stmt = select(*PipelineOperationLogService.get_dataset_logs_fields()).where(model.id == log_id, model.kb_id == dataset_id)
    log = (await db.execute(stmt)).mappings().first()
    if not log:
        return False, "Log not found"

    return True, dict(log)


# ==================== 标签 ====================


def list_tags(db: Session, tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    """列出数据集的标签聚合，保留上游 [(tag, count)] 契约。"""
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return False, "No authorization."

    tenants = UserTenantService.get_tenants_by_user_id(db, tenant_id)
    tags: list[tuple[str, int]] = []
    for tenant in tenants:
        tags += settings.retriever.all_tags(tenant["tenant_id"], [dataset_id])
    return True, tags


async def list_tags_async(tenant_id: str, dataset_id: str) -> tuple[bool, Any]:
    """list_tags 的异步入口：retriever.all_tags 内部自开 db_connection() 且走 doc-store
    同步 HTTP，run_sync 桥不了——整块进工作线程 + 自开短会话。"""

    def _run() -> tuple[bool, Any]:
        with db_connection() as s:
            return list_tags(s, tenant_id, dataset_id)

    return await asyncio.to_thread(_run)


def aggregate_tags(db: Session, tenant_id: str, dataset_ids: list[str]) -> tuple[bool, Any]:
    """跨数据集合并标签计数。跨租户的数据集按各自租户分组查询后再合并。"""
    if not dataset_ids:
        return False, 'Lack of "dataset_ids"'

    for dataset_id in dataset_ids:
        if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
            return False, f"No authorization for dataset '{dataset_id}'"

    dataset_ids_by_tenant: dict[str, list[str]] = {}
    for dataset_id in dataset_ids:
        kb = KnowledgebaseService.get_by_id(db, dataset_id)
        if not kb:
            return False, f"Invalid Dataset ID '{dataset_id}'"
        dataset_ids_by_tenant.setdefault(kb.tenant_id, []).append(dataset_id)

    merged: dict[str, int] = {}
    for kb_tenant_id, kb_ids in dataset_ids_by_tenant.items():
        for tag, count in settings.retriever.all_tags(kb_tenant_id, kb_ids):
            merged[tag] = merged.get(tag, 0) + count

    return True, [{"value": tag, "count": count} for tag, count in merged.items()]


async def aggregate_tags_async(tenant_id: str, dataset_ids: list[str]) -> tuple[bool, Any]:
    """aggregate_tags 的异步入口：同 list_tags_async 口径。"""

    def _run() -> tuple[bool, Any]:
        with db_connection() as s:
            return aggregate_tags(s, tenant_id, dataset_ids)

    return await asyncio.to_thread(_run)


def delete_tags(db: Session, tenant_id: str, dataset_id: str, tags: list[str]) -> tuple[bool, Any]:
    """从数据集的所有分块上摘掉给定标签。"""
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return False, "No authorization."

    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not kb:
        return False, "Invalid Dataset ID"

    index_name = search.index_name_one(kb.tenant_id, kb.name)
    for tag in tags:
        settings.docStoreConn.update({"tag_kwd": tag, "kb_id": [dataset_id]}, {"remove": {"tag_kwd": tag}}, index_name, dataset_id)

    return True, {}


async def delete_tags_async(tenant_id: str, dataset_id: str, tags: list[str]) -> tuple[bool, Any]:
    """delete_tags 的异步入口：doc-store 写是同步 HTTP，整块进工作线程 + 自开短会话。"""

    def _run() -> tuple[bool, Any]:
        with db_connection() as s:
            return delete_tags(s, tenant_id, dataset_id, tags)

    return await asyncio.to_thread(_run)


def rename_tag(db: Session, tenant_id: str, dataset_id: str, from_tag: str, to_tag: str) -> tuple[bool, Any]:
    """把数据集内的一个标签整体改名。"""
    if not dataset_id:
        return False, 'Lack of "Dataset ID"'
    if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
        return False, "No authorization."

    kb = KnowledgebaseService.get_by_id(db, dataset_id)
    if not kb:
        return False, "Invalid Dataset ID"

    settings.docStoreConn.update(
        {"tag_kwd": from_tag, "kb_id": [dataset_id]},
        {"remove": {"tag_kwd": from_tag.strip()}, "add": {"tag_kwd": to_tag}},
        search.index_name_one(kb.tenant_id, kb.name),
        dataset_id,
    )

    return True, {"from": from_tag, "to": to_tag}


async def rename_tag_async(tenant_id: str, dataset_id: str, from_tag: str, to_tag: str) -> tuple[bool, Any]:
    """rename_tag 的异步入口：同 delete_tags_async 口径。"""

    def _run() -> tuple[bool, Any]:
        with db_connection() as s:
            return rename_tag(s, tenant_id, dataset_id, from_tag, to_tag)

    return await asyncio.to_thread(_run)


# ==================== 元数据聚合 ====================


def get_flattened_metadata(db: Session, tenant_id: str, dataset_ids: list[str]) -> tuple[bool, Any]:
    """跨数据集拉平文档元数据，形如 {field: {value: [doc_id, ...]}}。"""
    if not dataset_ids:
        return False, 'Lack of "dataset_ids"'

    for dataset_id in dataset_ids:
        if not KnowledgebaseService.accessible(db, dataset_id, tenant_id):
            return False, f"No authorization for dataset '{dataset_id}'"

    return True, DocMetadataService.get_flatted_meta_by_kbs(db, dataset_ids)


async def get_flattened_metadata_async(tenant_id: str, dataset_ids: list[str]) -> tuple[bool, Any]:
    """get_flattened_metadata 的异步入口：元数据 store 在 ES 后端下走 doc-store
    同步 HTTP，整块进工作线程 + 自开短会话。"""

    def _run() -> tuple[bool, Any]:
        with db_connection() as s:
            return get_flattened_metadata(s, tenant_id, dataset_ids)

    return await asyncio.to_thread(_run)
