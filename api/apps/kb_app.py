# coding=utf-8
"""
@project: multirag
@Author：龙
@file： kb_app.py
@date：2024/8/5 9:22
@desc:
"""
import json
import logging
import os
import re
import random

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session
import numpy as np

from api.db.db_models import File, get_db
from api.db.services.connector_service import Connector2KbService
from api.db.services.document_service import DocumentService, queue_raptor_o_graphrag_tasks
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.pipeline_operation_log_service import PipelineOperationLogService
from api.db.services.task_service import TaskService, GRAPH_RAPTOR_FAKE_DOC_ID
from api.db.services.user_service import TenantService, UserTenantService
from api.utils.api_utils import server_error_response, get_data_error_result, get_error_data_result, get_parser_config
from api.db import VALID_FILE_TYPES
from common.constants import RetCode, PipelineTaskType, StatusEnum, VALID_TASK_STATUS, FileSource, LLMType, PAGERANK_FLD
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.llm_service import LLMBundle
from api.utils.api_utils import get_json_result
from api.apps import manager
from api.constants import DATASET_NAME_LIMIT, MILVUS_NAME_PATTERN
from common.constants import RetCode
from core.nlp import search
from core.utils.redis_conn import REDIS_CONN
from common import settings
from core.utils.doc_store_conn import OrderByExpr

router = APIRouter()


class CreateKnowledgebaseRequest(BaseModel):
    name: str
    description: str | None = None
    permission: str | None = None
    parser_id: str | None = None
    embd_id: str | None = None


class UpdateKnowledgebaseRequest(BaseModel):
    kb_id: str
    name: str
    description: str | None = None
    permission: str | None = None
    avatar: str | None = None
    parser_id: str | None = None
    parser_config: dict | None = None
    embd_id: str | None = None
    pagerank: int | None = 0
    connectors: list[dict] | None = None


class RemoveKnowledgebaseRequest(BaseModel):
    kb_id: str


class RemoveTagsRequest(BaseModel):
    tags: list[str]


class RenameTagRequest(BaseModel):
    from_tag: str
    to_tag: str


class ListKbsRequest(BaseModel):
    owner_ids: list[str] | None = []


class ListPipelineLogsRequest(BaseModel):
    operation_status: list[str] | None = []
    types: list[str] | None = []
    suffix: list[str] | None = []


class DeletePipelineLogsRequest(BaseModel):
    log_ids: list[str]


class RunGraphRagRequest(BaseModel):
    kb_id: str


class RunRaptorRequest(BaseModel):
    kb_id: str


class RunMindmapRequest(BaseModel):
    kb_id: str


class CheckEmbeddingRequest(BaseModel):
    kb_id: str
    embd_id: str
    check_num: int | None = 5


class UpdateMetadataSettingRequest(BaseModel):
    kb_id: str
    metadata: dict


@router.post('/create', summary="创建知识库", response_description="成功创建知识库")
def create(request: CreateKnowledgebaseRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req_data = request.model_dump()
    dataset_name = req_data["name"]
    
    # 验证数据集名称
    if not isinstance(dataset_name, str):
        return get_data_error_result(retmsg="Dataset name must be string.")
    if dataset_name.strip() == "":
        return get_data_error_result(retmsg="Dataset name can't be empty.")
    if len(dataset_name.encode("utf-8")) > DATASET_NAME_LIMIT:
        return get_data_error_result(
            retmsg=f"Dataset name length is {len(dataset_name)} which is larger than {DATASET_NAME_LIMIT}")
    
    # 验证 Milvus 集合名逻辑
    if not re.match(MILVUS_NAME_PATTERN, dataset_name):
        return get_data_error_result(
            retmsg="Dataset name must start with a letter and contain only letters, numbers, and underscores."
        )

    dataset_name = dataset_name.strip()
    
    # 检查数据库中是否已存在同名知识库（直接报错，不自动去重）
    existing_kb = KnowledgebaseService.query(
        db=db,
        name=dataset_name,
        tenant_id=user.id,
        status=StatusEnum.VALID.value
    )
    if existing_kb:
        return get_data_error_result(retmsg=f"已存在该知识库名: {existing_kb[0].name}，请调整！")

    try:
        # 生成parser_config
        parser_id = req_data.get("parser_id") or "naive"
        parser_config = get_parser_config(parser_id, None)
        
        # 使用封装的方法创建payload
        e, res = KnowledgebaseService.create_with_name(
            db=db,
            name=dataset_name,
            tenant_id=user.id,
            parser_id=parser_id,
            embd_id=req_data.get("embd_id"),
            parser_config=parser_config,
            description=req_data.get("description"),
            permission=req_data.get("permission")
        )

        if not e:
            return res  # 直接返回错误响应
        
        if not KnowledgebaseService.save(db, **res):
            return get_data_error_result()
        return get_json_result(data={"kb_id": res["id"]})
    except Exception as e:
        return server_error_response(e)


@router.post('/update', summary="更新知识库", response_description="成功更新知识库")
def update(request: UpdateKnowledgebaseRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req_data = request.model_dump()
    if not isinstance(req_data["name"], str):
        return get_data_error_result(retmsg="Dataset name must be string.")
    if req_data["name"].strip() == "":
        return get_data_error_result(retmsg="Dataset name can't be empty.")
    if len(req_data["name"].encode("utf-8")) > DATASET_NAME_LIMIT:
        return get_data_error_result(
            retmsg=f"Dataset name length is {len(req_data['name'])} which is large than {DATASET_NAME_LIMIT}")
    req_data["name"] = req_data["name"].strip()

    if not KnowledgebaseService.accessible4deletion(db, req_data["kb_id"], user.id):
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=RetCode.AUTHENTICATION_ERROR
        )
    try:
        if not KnowledgebaseService.query(db, created_by=user.id, id=req_data["kb_id"]):
            return get_json_result(
                data=False, retmsg=f'Only owner of dataset authorized for this operation.',
                retcode=RetCode.OPERATING_ERROR)

        kb = KnowledgebaseService.get_by_id(db, req_data["kb_id"])
        if not kb:
            return get_data_error_result(retmsg="Can't find this dataset!")

        if req_data["parser_id"] == "tag" and os.environ.get('DOC_ENGINE', "milvus") == "milvus":
            return get_json_result(
                data=False,
                retmsg='The chunking method Tag has not been supported by milvus yet.',
                retcode=RetCode.OPERATING_ERROR
            )

        if req_data["name"].lower() != kb.name.lower() \
                and len(KnowledgebaseService.query(db, name=req_data["name"], tenant_id=user.id,
                                                   status=StatusEnum.VALID.value)) > 1:
            return get_data_error_result(retmsg="Duplicated dataset name.")

        # 提取 connectors 字段，不写入知识库表
        connectors = []
        if "connectors" in req_data:
            connectors = req_data.get("connectors") or []
            del req_data["connectors"]

        # 过滤掉None值，避免将None写入数据库
        filtered_data = {k: v for k, v in req_data.items() if v is not None and k != "kb_id"}
        old_name = kb.name
        if not KnowledgebaseService.update_by_id(db, kb.id, filtered_data):
            return get_data_error_result()

        # ===== 向量数据库集合重命名逻辑 =====
        # 只有当名称实际发生变化时才执行重命名
        if "name" in req_data and req_data["name"] != old_name:
            db_type = settings.docStoreConn.dbType()
            old_coll = search.index_name_one(kb.tenant_id, old_name)
            new_coll = search.index_name_one(kb.tenant_id, req_data["name"])

            # 只有 Milvus 支持原生的 rename_collection
            if db_type == "milvus":
                if settings.docStoreConn.has_collection(old_coll):
                    settings.docStoreConn.rename_collection(old_coll, new_coll)
                    logging.info(f"Milvus collection renamed: {old_coll} → {new_coll}")
            else:
                # Elasticsearch/OpenSearch/Infinity 等暂不支持集合重命名
                # 因为 ES 需要 reindex 操作，其他数据库也没有原生重命名支持
                logging.warning(
                    f"Collection rename not supported for {db_type}. "
                    f"Old collection '{old_coll}' will remain, new data will use '{new_coll}'."
                )

        if kb.pagerank != req_data.get("pagerank", 0):
            # todo 测试 milvus 能否利用 pagerank【20250715】
            if os.environ.get("DOC_ENGINE", "milvus") != "milvus":
                logging.warning("'pagerank' can only be set when doc_engine is elasticsearch")
                # return get_data_error_result(retmsg="'pagerank' can only be set when doc_engine is elasticsearch")

            if req_data.get("pagerank", 0) > 0:
                try:
                    settings.docStoreConn.update(
                        {"kb_id": kb.id},
                        {"pagerank_fea": req_data["pagerank"]},
                        search.index_name_one(kb.tenant_id, kb.name),
                        kb.id
                    )
                    logging.info(f"已更新知识库 {kb.id} 的 PageRank 值为 {req_data['pagerank']}")
                except Exception as e:
                    logging.error(f"更新知识库 {kb.id} 的 PageRank 失败: {str(e)}")
            else:
                # 移除PageRank（设置为0）
                try:
                    settings.docStoreConn.update(
                        {"kb_id": id},
                        {"remove": "pagerank_fea"},  # 使用与ES相同的格式
                        search.index_name_one(kb.tenant_id, kb.name),
                        kb.id
                    )
                    logging.info(f"已移除知识库 {kb.id} 的 PageRank 值")
                except Exception as e:
                    logging.error(f"移除知识库 {kb.id} 的 PageRank 失败: {str(e)}")

        # 处理 connectors 关联
        if connectors:
            errors = Connector2KbService.link_connectors(db, kb.id, [conn for conn in connectors], user.id)
            if errors:
                logging.error(f"Link KB errors: {errors}")

        kb = KnowledgebaseService.get_by_id(db, kb.id)
        if not kb:
            return get_data_error_result(retmsg="Database error (dataset rename)!")
        kb = kb.to_dict()
        # 使用filtered_data而不是req_data，避免包含None值
        kb.update(filtered_data)
        kb["connectors"] = connectors

        return get_json_result(data=kb)
    except Exception as e:
        return server_error_response(e)


@router.post('/update_metadata_setting', summary="更新知识库元数据配置")
def update_metadata_setting(request: UpdateMetadataSettingRequest, db: Session = Depends(get_db), user=Depends(manager)):
    kb = KnowledgebaseService.get_by_id(db, request.kb_id)
    if not kb:
        return get_data_error_result(retmsg="Database error (Knowledgebase rename)!")
    kb_dict = kb.to_dict()
    if kb_dict.get("parser_config") is None:
        kb_dict["parser_config"] = {}
    kb_dict["parser_config"]["metadata"] = request.metadata
    KnowledgebaseService.update_by_id(db, kb_dict["id"], {"parser_config": kb_dict["parser_config"]})
    return get_json_result(data=kb_dict)


@router.get('/detail', summary="获取知识库详情", response_description="成功获取知识库详情")
def detail(kb_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    try:
        tenants = UserTenantService.query(db, user_id=user.id)
        for tenant in tenants:
            if KnowledgebaseService.query(
                    db, tenant_id=tenant.tenant_id, id=kb_id):
                break
        else:
            return get_json_result(
                data=False, retmsg=f'Only owner of dataset authorized for this operation.',
                retcode=RetCode.OPERATING_ERROR)
        kb = KnowledgebaseService.get_detail(db, kb_id)
        if not kb:
            return get_data_error_result(retmsg="Can't find this dataset!")
        kb["size"] = DocumentService.get_total_size_by_kb_id(db, kb_id=kb["id"],keywords="", run_status=[], types=[])
        kb["connectors"] = Connector2KbService.list_connectors(db, kb_id)
        
        for key in ["graphrag_task_finish_at", "raptor_task_finish_at", "mindmap_task_finish_at"]:
            if finish_at := kb.get(key):
                kb[key] = finish_at.strftime("%Y-%m-%d %H:%M:%S")
        return get_json_result(data=kb)
    except Exception as e:
        return server_error_response(e)


# @router.get('/list', summary="列出知识库", response_description="成功列出知识库")
# def list_kbs(page: int = 1, page_size: int = 150, orderby: str = "create_time", desc: bool = True, keywords: str = "",
#              db: Session = Depends(get_db), user=Depends(manager)):
#     page_number = int(page)
#     items_per_page = int(page_size)
#     try:
#         tenants = TenantService.get_joined_tenants_by_user_id(db, user.id)
#         kbs, total = KnowledgebaseService.get_by_tenant_ids(
#             db, [m["tenant_id"] for m in tenants], user.id, page_number, items_per_page, orderby, desc, keywords)
#         return get_json_result(data={"kbs": kbs, "total": total})
#     except Exception as e:
#         return server_error_response(e)

@router.post('/list', summary="列出知识库", response_description="成功列出知识库")
def list_kbs(
        request_body: ListKbsRequest,
        page: int = 0,
        page_size: int = 0,
        orderby: str = "create_time",
        desc: bool = True,
        keywords: str = "",
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    page_number = int(page)
    items_per_page = int(page_size)

    # 当指定了分页大小但页码<=0时，回退到第一页，避免分页条件被判定为 False
    if items_per_page > 0 and page_number <= 0:
        page_number = 1
    owner_ids = request_body.owner_ids or []

    try:
        if not owner_ids:
            # 原有逻辑：获取用户加入的租户
            tenants = TenantService.get_joined_tenants_by_user_id(db, user.id)
            tenants = [m["tenant_id"] for m in tenants]
            kbs, total = KnowledgebaseService.get_by_tenant_ids(
                db, tenants, user.id, page_number, items_per_page, orderby, desc, keywords)
        else:
            # 新逻辑：使用指定的owner_ids
            tenants = owner_ids
            # 先获取所有数据（page=0, page_size=0）
            kbs, total = KnowledgebaseService.get_by_tenant_ids(
                db, tenants, user.id, 0, 0, orderby, desc, keywords)

            # 过滤出指定租户的知识库
            kbs = [kb for kb in kbs if kb["tenant_id"] in tenants]

            # 计算总数（在分页前）
            total = len(kbs)

            # 手动处理分页
            if items_per_page > 0:
                start_idx = (page_number - 1) * items_per_page
                end_idx = page_number * items_per_page
                kbs = kbs[start_idx:end_idx]

        return get_json_result(data={"kbs": kbs, "total": total})
    except Exception as e:
        return server_error_response(e)


@router.post('/rm', summary="删除知识库", response_description="成功删除知识库")
def rm(request: RemoveKnowledgebaseRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    删除知识库

    参数:
    - request: 请求体，包含要删除的知识库ID。

    返回:
    - 成功删除知识库时，返回包含成功标志的JSON结果。
    - 如有错误发生，返回相应的错误信息。
    """
    # 将请求体转换为字典
    req_data = request.model_dump()
    if not KnowledgebaseService.accessible4deletion(db, req_data["kb_id"], user.id):
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=RetCode.AUTHENTICATION_ERROR
        )
    try:
        # 查询知识库，确保只有知识库的创建者有权限删除
        kbs = KnowledgebaseService.query(db, created_by=user.id, id=req_data["kb_id"])
        if not kbs:
            # 如果知识库不存在或用户无权限删除，返回错误信息
            return get_json_result(
                data=False, retmsg=f'Only owner of dataset authorized for this operation.',
                retcode=RetCode.OPERATING_ERROR)

        # 提前保存知识库名称，避免访问被删除对象
        kb_name = kbs[0].name
        kb_id = kbs[0].id

        # 遍历知识库中的所有文档，进行删除
        for doc in DocumentService.query(db, kb_id=req_data["kb_id"]):
            doc_id = doc.id  # 提前保存文档 ID，避免后续访问被删除的对象

            b, n = File2DocumentService.get_storage_address(db, doc_id=doc_id)

            # 删除文档，如果失败则返回错误信息
            if not DocumentService.remove_document(db, doc, kbs[0].tenant_id):
                return get_data_error_result(retmsg="Database error (Document removal)!")

            # 查询与文档关联的文件，并删除这些文件
            f2d = File2DocumentService.get_by_document_id(db, doc_id)
            if f2d:
                FileService.filter_delete(db, [File.source_type == FileSource.KNOWLEDGEBASE, File.id == f2d[0].file_id])
            # 删除文档与文件的关联记录
            File2DocumentService.delete_by_document_id(db, doc_id)
            settings.STORAGE_IMPL.rm(b, n)
        FileService.filter_delete(
            db, [File.source_type == FileSource.KNOWLEDGEBASE, File.type == "folder", File.name == kb_name])

        # 删除 MinIO 存储桶
        settings.STORAGE_IMPL.remove_bucket(kb_id)

        # 删除知识库本身，如果失败则返回错误信息
        if not KnowledgebaseService.delete_by_id(db, req_data["kb_id"]):
            return get_data_error_result(retmsg="Database error (dataset removal)!")
        tenants = UserTenantService.query(db, user_id=user.id)
        for tenant in tenants:
            settings.docStoreConn.deleteIdx(search.index_name_one(tenant.tenant_id, kb_name), req_data["kb_id"])
        # 知识库删除成功，返回成功标志
        return get_json_result(data=True)
    except Exception as e:
        # 捕获异常，返回服务器错误响应
        return server_error_response(e)


@router.get("/{kb_id}/tags", summary="获取知识库标签")
def list_tags(kb_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    if not KnowledgebaseService.accessible(db, kb_id, user.id):
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=RetCode.AUTHENTICATION_ERROR
        )
    tenants = UserTenantService.get_tenants_by_user_id(db, user.id)
    tags = []
    for tenant in tenants:
        tags += settings.retriever.all_tags(tenant["tenant_id"], [kb_id])
    return get_json_result(data=tags)


@router.get("/tags", summary="获取多个知识库的标签")
def list_tags_from_kbs(kb_ids: str, db: Session = Depends(get_db), user=Depends(manager)):
    kb_id_list = kb_ids.split(",")
    for kb_id in kb_id_list:
        if not KnowledgebaseService.accessible(db, kb_id, user.id):
            return get_json_result(
                data=False,
                retmsg='No authorization.',
                retcode=RetCode.AUTHENTICATION_ERROR
            )
    tenants = UserTenantService.get_tenants_by_user_id(db, user.id)
    tags = []
    for tenant in tenants:
        tags += settings.retriever.all_tags(tenant["tenant_id"], kb_ids)
    return get_json_result(data=tags)


@router.post("/{kb_id}/rm_tags", summary="删除知识库标签")
def rm_tags(kb_id: str, request: RemoveTagsRequest, db: Session = Depends(get_db), user=Depends(manager)):
    if not KnowledgebaseService.accessible(db, kb_id, user.id):
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=RetCode.AUTHENTICATION_ERROR
        )
    kb = KnowledgebaseService.get_by_id(db, kb_id)

    for tag in request.tags:
        settings.docStoreConn.update(
            {"tag_kwd": tag, "kb_id": [kb_id]},
            {"remove": {"tag_kwd": tag}},
            search.index_name_one(kb.tenant_id, kb.name),
            kb_id,
        )
    return get_json_result(data=True)


@router.post("/{kb_id}/rename_tag", summary="重命名知识库标签")
def rename_tags(kb_id: str, request: RenameTagRequest, db: Session = Depends(get_db), user=Depends(manager)):
    if not KnowledgebaseService.accessible(db, kb_id, user.id):
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=RetCode.AUTHENTICATION_ERROR
        )
    kb = KnowledgebaseService.get_by_id(db, kb_id)

    settings.docStoreConn.update(
        {"tag_kwd": request.from_tag, "kb_id": [kb_id]},
        {
            "remove": {"tag_kwd": request.from_tag.strip()},
            "add": {"tag_kwd": request.to_tag},
        },
        search.index_name_one(kb.tenant_id, kb.name),
        kb_id,
    )
    return get_json_result(data=True)


@router.get("/<kb_id>/knowledge_graph'", summary="获取知识图谱")
def knowledge_graph(kb_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    if not KnowledgebaseService.accessible(db, kb_id, user.id):
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=RetCode.AUTHENTICATION_ERROR
        )
    kb = KnowledgebaseService.get_by_id(db, kb_id)
    req = {
        "kb_id": [kb_id],
        "knowledge_graph_kwd": ["graph"]
    }
    obj = {"graph": {}, "mind_map": {}}
    if not settings.docStoreConn.indexExist(search.index_name(kb.tenant_id, [kb.name]), kb_id):
        return get_json_result(data=obj)
    sres = settings.retriever.search(req, search.index_name(kb.tenant_id, [kb.name]), [kb_id])
    if not len(sres.ids):
        return get_json_result(data=obj)

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
    return get_json_result(data=obj)


@router.delete('/<kb_id>/knowledge_graph', summary="删除知识图谱")
def delete_knowledge_graph(kb_id, db: Session = Depends(get_db), user=Depends(manager)):
    if not KnowledgebaseService.accessible(db, kb_id, user.id):
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=RetCode.AUTHENTICATION_ERROR
        )
    kb = KnowledgebaseService.get_by_id(db, kb_id)

    settings.docStoreConn.delete({"knowledge_graph_kwd": ["graph", "subgraph", "entity", "relation"]}, search.index_name(kb.tenant_id, [kb.name]), kb_id)

    return get_json_result(data=True)


@router.get("/get_meta", summary="查询知识库元数据聚合", response_description="元数据聚合结果")
def get_meta(
        kb_ids: str = Query(default="", description="知识库ID列表，逗号分隔"),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """汇总指定知识库中文档元数据字段的取值分布，便于在 Apifox 等工具中查看筛选项。

    - **kb_ids**: 多个知识库ID，使用英文逗号分隔。
    - **返回值**: 形如 `{meta_key: {meta_value: [doc_id, ...]}}` 的嵌套映射。
    """
    kb_id_list = [kb_id.strip() for kb_id in kb_ids.split(",") if kb_id.strip()]

    if not kb_id_list:
        return get_json_result(data={})

    for kb_id in kb_id_list:
        if not KnowledgebaseService.accessible(db, kb_id, user.id):
            return get_json_result(
                data=False,
                retmsg='No authorization.',
                retcode=RetCode.AUTHENTICATION_ERROR
            )

    try:
        meta = DocumentService.get_meta_by_kbs(db, kb_id_list)
        return get_json_result(data=meta)
    except Exception as e:
        return server_error_response(e)


@router.get("/basic_info", summary="获取知识库文档处理统计信息", response_description="返回文档处理状态统计")
def get_basic_info(
    kb_id: str = Query(..., description="知识库ID"),
    db: Session = Depends(get_db),
    user=Depends(manager)
):
    """
    获取知识库的文档处理基本统计信息
    
    概要：返回指定知识库中文档的处理状态统计。
    响应描述：成功返回文档处理状态的统计数据，包括处理中、已完成、失败和已取消的文档数量。
    
    参数：
    - **kb_id**: 知识库ID（必填）
    
    返回：
    - dict: 包含以下字段的统计信息
        - processing: 正在处理的文档数量
        - finished: 已完成的文档数量
        - failed: 处理失败的文档数量
        - cancelled: 已取消的文档数量
    
    功能：
    1. 验证用户是否有权限访问指定知识库
    2. 查询知识库中文档的处理状态
    3. 返回各状态的统计数量
    
    权限要求：
    - 用户必须对该知识库有访问权限
    
    异常处理：
    - 如果用户无权访问，返回 AUTHENTICATION_ERROR 错误
    - 如果发生其他异常，返回服务器错误
    
    注意：
    - 统计信息基于 Document 表的 progress 和 run 字段
    """
    try:
        # 检查用户权限
        if not KnowledgebaseService.accessible(db, kb_id, user.id):
            return get_json_result(
                data=False,
                retmsg='No authorization.',
                retcode=RetCode.AUTHENTICATION_ERROR
            )

        # 获取统计信息
        basic_info = DocumentService.knowledgebase_basic_info(db, kb_id)

        return get_json_result(data=basic_info)
    except Exception as e:
        return server_error_response(e)


@router.post("/list_pipeline_logs", summary="列出Pipeline日志", response_description="成功获取Pipeline日志列表")
def list_pipeline_logs(
        request_body: ListPipelineLogsRequest,
        kb_id: str = Query(..., description="知识库ID"),
        keywords: str = Query("", description="关键词搜索"),
        page: int = Query(0, description="页码"),
        page_size: int = Query(0, description="每页数量"),
        orderby: str = Query("create_time", description="排序字段"),
        desc: bool = Query(True, description="是否降序"),
        create_date_from: str = Query("", description="创建日期起始"),
        create_date_to: str = Query("", description="创建日期结束"),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    获取指定知识库的文件级Pipeline处理日志列表
    
    概要：查询并返回知识库中文件的处理日志，支持多维度筛选、搜索、排序和分页。
    
    参数：
    - **request_body**: 请求体，包含过滤条件
        - operation_status: 操作状态列表，可选值见 VALID_TASK_STATUS
        - types: 文件类型列表，可选值见 VALID_FILE_TYPES
        - suffix: 文件后缀列表，如 ["pdf", "docx", "txt"]
    - **kb_id**: 知识库ID（必填）
    - **keywords**: 关键词搜索，支持文件名模糊匹配
    - **page**: 页码，0表示不分页
    - **page_size**: 每页数量，0表示不分页
    - **orderby**: 排序字段，默认按创建时间排序
    - **desc**: 是否降序排列，True为降序，False为升序
    - **create_date_from**: 创建日期起始时间，过滤该时间之后的日志
    - **create_date_to**: 创建日期结束时间，过滤该时间之前的日志
    
    返回：
    - dict: 包含以下字段
        - total: 符合条件的日志总数
        - logs: 日志列表，每条日志包含文件处理的详细信息
    
    功能：
    1. 验证知识库ID是否存在
    2. 验证筛选条件的有效性（操作状态、文件类型）
    3. 根据多维度条件筛选日志：
       - 按操作状态筛选（如：处理中、已完成、失败等）
       - 按文件类型筛选（如：pdf、docx、txt等）
       - 按文件后缀筛选
       - 按关键词搜索文件名
       - 按创建日期范围筛选
    4. 支持自定义排序和分页
    5. 返回日志列表和总数
    
    验证逻辑：
    - 时间范围验证：create_date_to 必须晚于或等于 create_date_from
    - 状态值验证：operation_status 中的值必须在 VALID_TASK_STATUS 中
    - 类型值验证：types 中的值必须在 VALID_FILE_TYPES 中
    
    异常处理：
    - 如果缺少知识库ID，返回 ARGUMENT_ERROR
    - 如果时间范围无效，返回数据错误
    - 如果筛选条件无效，返回数据错误并说明具体哪些条件不合法
    - 其他异常返回服务器错误
    
    注意：
    - 当 page=0 或 page_size=0 时，返回所有符合条件的记录
    - 日志记录了文件从上传到处理完成的全过程
    - 可用于追踪文件处理进度和排查处理失败的原因
    """
    if not kb_id:
        return get_json_result(data=False, retmsg='Lack of "KB ID"', retcode=RetCode.ARGUMENT_ERROR)

    page_number = int(page)
    items_per_page = int(page_size)
    
    if create_date_to and create_date_from and create_date_to > create_date_from:
        return get_data_error_result(retmsg="Create data filter is abnormal.")

    operation_status = request_body.operation_status or []
    if operation_status:
        invalid_status = {s for s in operation_status if s not in VALID_TASK_STATUS}
        if invalid_status:
            return get_data_error_result(retmsg=f"Invalid filter operation_status status conditions: {', '.join(invalid_status)}")

    types = request_body.types or []
    if types:
        invalid_types = {t for t in types if t not in VALID_FILE_TYPES}
        if invalid_types:
            return get_data_error_result(retmsg=f"Invalid filter conditions: {', '.join(invalid_types)} type{'s' if len(invalid_types) > 1 else ''}")

    suffix = request_body.suffix or []

    try:
        logs, tol = PipelineOperationLogService.get_file_logs_by_kb_id(db, kb_id, page_number, items_per_page, orderby, desc, keywords, operation_status, types, suffix, create_date_from, create_date_to)
        return get_json_result(data={"total": tol, "logs": logs})
    except Exception as e:
        return server_error_response(e)


@router.post("/list_pipeline_dataset_logs", summary="列出Pipeline数据集日志", response_description="成功获取Pipeline数据集日志列表")
def list_pipeline_dataset_logs(
        request_body: ListPipelineLogsRequest,
        kb_id: str = Query(..., description="知识库ID"),
        page: int = Query(0, description="页码"),
        page_size: int = Query(0, description="每页数量"),
        orderby: str = Query("create_time", description="排序字段"),
        desc: bool = Query(True, description="是否降序"),
        create_date_from: str = Query("", description="创建日期起始"),
        create_date_to: str = Query("", description="创建日期结束"),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    获取指定知识库的数据集级Pipeline处理日志列表
    
    概要：查询并返回知识库整体的数据集处理日志，记录知识库级别的操作（如全量重建、批量更新等）。
    
    参数：
    - **request_body**: 请求体，包含过滤条件
        - operation_status: 操作状态列表，可选值见 VALID_TASK_STATUS
    - **kb_id**: 知识库ID（必填）
    - **page**: 页码，0表示不分页
    - **page_size**: 每页数量，0表示不分页
    - **orderby**: 排序字段，默认按创建时间排序
    - **desc**: 是否降序排列，True为降序，False为升序
    - **create_date_from**: 创建日期起始时间，过滤该时间之后的日志
    - **create_date_to**: 创建日期结束时间，过滤该时间之前的日志
    
    返回：
    - dict: 包含以下字段
        - total: 符合条件的日志总数
        - logs: 日志列表，每条日志包含数据集级操作的详细信息
    
    功能：
    1. 验证知识库ID是否存在
    2. 验证操作状态筛选条件的有效性
    3. 查询数据集级别的处理日志：
       - 知识库创建日志
       - 知识库更新日志
       - 批量操作日志
       - GraphRAG/RAPTOR/Mindmap等全局任务日志
    4. 支持按操作状态筛选
    5. 支持按时间范围筛选
    6. 支持自定义排序和分页
    
    区别于文件级日志：
    - 文件级日志（list_pipeline_logs）：记录单个文件的处理过程
    - 数据集级日志（本接口）：记录整个知识库的操作记录
    
    验证逻辑：
    - 时间范围验证：create_date_to 必须晚于或等于 create_date_from
    - 状态值验证：operation_status 中的值必须在 VALID_TASK_STATUS 中
    
    异常处理：
    - 如果缺少知识库ID，返回 ARGUMENT_ERROR
    - 如果时间范围无效，返回数据错误
    - 如果状态值无效，返回数据错误并说明具体哪些状态不合法
    - 其他异常返回服务器错误
    
    注意：
    - 当 page=0 或 page_size=0 时，返回所有符合条件的记录
    - 数据集级日志通常用于追踪知识库级别的重大操作
    - 可用于审计和问题排查
    """
    if not kb_id:
        return get_json_result(data=False, retmsg='Lack of "KB ID"', retcode=RetCode.ARGUMENT_ERROR)

    page_number = int(page)
    items_per_page = int(page_size)
    
    if create_date_to and create_date_from and create_date_to > create_date_from:
        return get_data_error_result(retmsg="Create data filter is abnormal.")

    operation_status = request_body.operation_status or []
    if operation_status:
        invalid_status = {s for s in operation_status if s not in VALID_TASK_STATUS}
        if invalid_status:
            return get_data_error_result(retmsg=f"Invalid filter operation_status status conditions: {', '.join(invalid_status)}")

    try:
        logs, tol = PipelineOperationLogService.get_dataset_logs_by_kb_id(db, kb_id, page_number, items_per_page, orderby, desc, operation_status, create_date_from, create_date_to)
        return get_json_result(data={"total": tol, "logs": logs})
    except Exception as e:
        return server_error_response(e)


@router.post("/delete_pipeline_logs", summary="删除Pipeline日志", response_description="成功删除Pipeline日志")
def delete_pipeline_logs(
        request_body: DeletePipelineLogsRequest,
        kb_id: str = Query(..., description="知识库ID"),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    批量删除指定的Pipeline处理日志
    
    概要：根据提供的日志ID列表，批量删除Pipeline处理日志记录。
    
    参数：
    - **request_body**: 请求体，包含要删除的日志ID列表
        - log_ids: 日志ID列表，支持批量删除多条日志
    - **kb_id**: 知识库ID（必填），用于权限验证
    
    返回：
    - dict: 包含操作结果
        - data: True 表示删除成功
    
    功能：
    1. 验证知识库ID是否存在
    2. 验证用户是否有权限操作该知识库
    3. 根据提供的日志ID列表批量删除日志记录
    4. 删除操作是永久性的，无法恢复
    
    业务场景：
    - 清理历史日志，释放存储空间
    - 删除无用或错误的日志记录
    - 定期维护，清理过期日志
    
    权限要求：
    - 用户必须对该知识库有操作权限
    - 只能删除指定知识库下的日志
    
    异常处理：
    - 如果缺少知识库ID，返回 ARGUMENT_ERROR
    - 如果用户无权限，返回认证错误
    - 其他异常返回服务器错误
    
    注意：
    - 删除操作不可逆，请谨慎使用
    - 建议删除前确认日志ID的正确性
    - 支持批量删除，提高操作效率
    - 删除日志不影响实际的文件处理结果，只是清理记录
    """
    if not kb_id:
        return get_json_result(data=False, retmsg='Lack of "KB ID"', retcode=RetCode.ARGUMENT_ERROR)

    log_ids = request_body.log_ids

    PipelineOperationLogService.delete_by_ids(db, log_ids)

    return get_json_result(data=True)


@router.get("/pipeline_log_detail", summary="获取Pipeline日志详情", response_description="成功获取Pipeline日志详情")
def pipeline_log_detail(
        log_id: str = Query(..., description="日志ID"),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    获取单条Pipeline日志的详细信息
    
    概要：根据日志ID查询并返回该条日志的完整详细信息。
    
    参数：
    - **log_id**: 日志ID（必填），指定要查询的日志记录
    
    返回：
    - dict: 日志的完整详细信息，包含：
        - log_id: 日志唯一标识
        - kb_id: 所属知识库ID
        - file_id: 关联的文件ID（如果是文件级日志）
        - operation_type: 操作类型（如：上传、解析、向量化等）
        - operation_status: 操作状态（如：进行中、成功、失败）
        - progress: 进度百分比（0-100）
        - progress_msg: 进度消息，描述当前处理状态
        - error_msg: 错误信息（如果失败）
        - create_time: 创建时间
        - update_time: 更新时间
        - duration: 处理耗时（秒）
        - 其他相关字段
    
    功能：
    1. 根据日志ID查询数据库
    2. 返回日志的完整详细信息
    3. 可用于详细了解某次操作的全过程
    
    业务场景：
    - 查看文件处理的详细过程
    - 排查处理失败的具体原因
    - 分析处理耗时和性能瓶颈
    - 获取错误堆栈和调试信息
    
    权限要求：
    - 用户必须对日志所属的知识库有访问权限
    
    异常处理：
    - 如果缺少日志ID，返回 ARGUMENT_ERROR
    - 如果日志ID不存在，返回 "Invalid pipeline log ID"
    - 如果用户无权限，返回认证错误
    - 其他异常返回服务器错误
    
    注意：
    - 返回的信息非常详细，可用于问题诊断
    - 包含处理过程中的所有关键信息
    - 错误日志会包含详细的错误堆栈
    """
    if not log_id:
        return get_json_result(data=False, retmsg='Lack of "Pipeline log ID"', retcode=RetCode.ARGUMENT_ERROR)

    log = PipelineOperationLogService.get_by_id(db, log_id)
    if not log:
        return get_data_error_result(retmsg="Invalid pipeline log ID")

    return get_json_result(data=log.to_dict())


@router.post("/run_graphrag", summary="运行GraphRAG", response_description="成功启动GraphRAG任务")
def run_graphrag(
        request: RunGraphRagRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    为指定知识库启动GraphRAG知识图谱构建任务
    
    概要：基于知识库中的所有文档，构建知识图谱，提取实体、关系和子图，增强知识库的语义检索能力。
    
    参数：
    - **request**: 请求体
        - kb_id: 知识库ID（必填）
    
    返回：
    - dict: 包含任务信息
        - graphrag_task_id: GraphRAG任务ID，可用于追踪任务进度
    
    功能：
    1. 验证知识库ID的有效性
    2. 检查知识库中是否存在文档（至少需要一个文档）
    3. 检查是否已有GraphRAG任务在运行：
       - 如果有任务且状态为进行中（progress不为-1或1），返回错误
       - 如果任务已完成或失败，可以重新启动
    4. 获取知识库中所有文档的ID列表
    5. 创建GraphRAG任务并加入处理队列
    6. 将任务ID保存到知识库记录中
    
    GraphRAG处理流程：
    1. **实体提取**：从文档中识别并提取实体（人物、组织、地点、事件、类别等）
    2. **关系抽取**：分析实体之间的关系，构建关系网络
    3. **子图生成**：基于实体和关系生成知识子图
    4. **图谱索引**：将图谱数据存入向量数据库，支持图谱检索
    5. **PageRank计算**：计算节点重要性，优化检索排序
    
    业务价值：
    - 提升检索准确性：通过知识图谱理解文档语义关系
    - 支持图谱问答：可以回答需要推理的复杂问题
    - 增强推荐能力：基于实体关系推荐相关内容
    - 可视化展示：生成可交互的知识图谱可视化
    
    任务特点：
    - 计算密集型任务，处理时间取决于文档数量和复杂度
    - 异步执行，不阻塞当前请求
    - 支持任务追踪（通过trace_graphrag接口）
    - 任务失败后可重新运行
    
    权限要求：
    - 用户必须对该知识库有操作权限
    - 知识库必须至少包含一个文档
    
    异常处理：
    - 如果缺少知识库ID，返回 "Lack of KB ID"
    - 如果知识库不存在，返回 "Invalid dataset ID"
    - 如果已有任务在运行，返回 "A Graph Task is already running"
    - 如果知识库中没有文档，返回 "No documents in dataset"
    - 其他异常返回服务器错误
    
    注意：
    - GraphRAG任务会使用伪文档ID（GRAPH_RAPTOR_FAKE_DOC_ID）进行知识库级别的处理
    - 任务完成后，可通过knowledge_graph接口查看图谱结果
    - 建议在文档较多时，在业务低峰期运行
    - 任务执行期间不影响正常的检索功能
    - 可通过unbind_task接口清除任务绑定关系
    """
    kb_id = request.kb_id
    if not kb_id:
        return get_error_data_result(retmsg='Lack of "KB ID"')

    kb = KnowledgebaseService.get_by_id(db, kb_id)
    if not kb:
        return get_error_data_result(retmsg="Invalid dataset ID")

    task_id = kb.graphrag_task_id
    if task_id:
        task = TaskService.get_by_id(db, task_id)
        if not task:
            logging.warning(f"A valid GraphRAG task id is expected for kb {kb_id}")

        if task and task.progress not in [-1, 1]:
            return get_error_data_result(retmsg=f"Task {task_id} in progress with status {task.progress}. A Graph Task is already running.")

    documents, _ = DocumentService.get_by_kb_id(
        db=db,
        kb_id=kb_id,
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
        return get_error_data_result(retmsg=f"No documents in dataset {kb_id}")

    sample_document = documents[0]
    document_ids = [document["id"] for document in documents]

    task_id = queue_raptor_o_graphrag_tasks(db, sample_doc_id=sample_document, ty="graphrag", priority=0, fake_doc_id=GRAPH_RAPTOR_FAKE_DOC_ID, doc_ids=list(document_ids))

    if not KnowledgebaseService.update_by_id(db, kb.id, {"graphrag_task_id": task_id}):
        logging.warning(f"Cannot save graphrag_task_id for kb {kb_id}")

    return get_json_result(data={"graphrag_task_id": task_id})


@router.get("/trace_graphrag", summary="追踪GraphRAG任务", response_description="成功获取GraphRAG任务状态")
def trace_graphrag(
        kb_id: str = Query(..., description="知识库ID"),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    追踪指定知识库的GraphRAG任务执行状态
    
    概要：实时查询GraphRAG任务的处理进度、状态和详细信息，用于监控任务执行情况。
    
    参数：
    - **kb_id**: 知识库ID（必填）
    
    返回：
    - dict: 任务详细信息，包含：
        - task_id: 任务唯一标识
        - task_type: 任务类型（graphrag）
        - doc_id: 关联的文档ID（使用GRAPH_RAPTOR_FAKE_DOC_ID）
        - kb_id: 所属知识库ID
        - progress: 任务进度（-1：失败，0-0.99：进行中，1：完成）
        - progress_msg: 进度消息，描述当前处理阶段
        - status: 任务状态（RUNNING/DONE/FAIL）
        - create_time: 任务创建时间
        - begin_at: 任务开始时间
        - update_time: 最后更新时间
        - duration: 已处理时长（秒）
        - error_msg: 错误信息（如果失败）
        - priority: 任务优先级
    
    功能：
    1. 根据知识库ID查找关联的GraphRAG任务
    2. 返回任务的实时状态信息
    3. 如果知识库没有关联任务，返回空对象{}
    
    任务进度说明：
    - **progress = -1**: 任务失败，可查看error_msg了解失败原因
    - **progress = 0-0.99**: 任务正在执行中
      - 0-0.2: 实体提取阶段
      - 0.2-0.6: 关系抽取阶段
      - 0.6-0.8: 子图生成阶段
      - 0.8-1.0: 图谱索引阶段
    - **progress = 1**: 任务已完成
    
    progress_msg 示例：
    - "Extracting entities from documents..."
    - "Building relationship graph..."
    - "Generating subgraphs..."
    - "Indexing graph data..."
    - "Task completed successfully"
    
    业务场景：
    - 实时监控任务执行进度
    - 前端轮询显示进度条
    - 任务失败后查看错误原因
    - 估算剩余处理时间
    
    权限要求：
    - 用户必须对该知识库有访问权限
    
    异常处理：
    - 如果缺少知识库ID，返回 "Lack of KB ID"
    - 如果知识库不存在，返回 "Invalid dataset ID"
    - 如果任务不存在，返回 "GraphRAG Task Not Found or Error Occurred"
    - 其他异常返回服务器错误
    
    注意：
    - 如果知识库从未运行过GraphRAG，返回空对象 {}
    - 建议客户端以3-5秒间隔轮询该接口获取最新状态
    - 任务完成后，可以停止轮询
    - 任务信息会持久化保存，即使任务完成也可查询历史记录
    """
    if not kb_id:
        return get_error_data_result(retmsg='Lack of "KB ID"')

    kb = KnowledgebaseService.get_by_id(db, kb_id)
    if not kb:
        return get_error_data_result(retmsg="Invalid dataset ID")

    task_id = kb.graphrag_task_id
    if not task_id:
        return get_json_result(data={})

    task = TaskService.get_by_id(db, task_id)
    if not task:
        return get_json_result(data={})

    return get_json_result(data=task.to_dict())


@router.post("/run_raptor", summary="运行RAPTOR", response_description="成功启动RAPTOR任务")
def run_raptor(
        request: RunRaptorRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    为指定知识库启动RAPTOR递归摘要任务
    
    概要：基于知识库中的所有文档，使用递归聚类和摘要技术构建层次化的文档摘要树，提升长文档检索和摘要能力。
    
    参数：
    - **request**: 请求体
        - kb_id: 知识库ID（必填）
    
    返回：
    - dict: 包含任务信息
        - raptor_task_id: RAPTOR任务ID，可用于追踪任务进度
    
    功能：
    1. 验证知识库ID和文档存在性
    2. 检查是否已有RAPTOR任务在运行
    3. 创建RAPTOR任务并加入处理队列
    4. 保存任务ID到知识库记录
    
    RAPTOR处理流程：
    1. **文档分块**：将长文档切分成多个chunks
    2. **向量聚类**：基于语义相似度对chunks进行聚类
    3. **递归摘要**：对每个聚类生成摘要，形成树状结构
    4. **多层索引**：将不同层级的摘要和原始chunks都建立索引
    5. **检索增强**：支持从粗粒度到细粒度的多层次检索
    
    RAPTOR优势：
    - 提升长文档检索：通过层次化摘要快速定位相关内容
    - 支持多粒度摘要：可获取不同详细程度的文档总结
    - 增强上下文理解：聚类摘要保留了文档的整体结构
    - 优化Token使用：通过摘要层次减少不必要的Token消耗
    
    配置参数（来自kb的parser_config）：
    - use_raptor: 是否启用RAPTOR
    - prompt: 摘要生成的提示词模板
    - max_token: 每条摘要的最大token数
    - threshold: 聚类相似度阈值
    - max_cluster: 最大聚类数量
    - random_seed: 随机种子，用于可复现性
    
    任务特点：
    - 计算密集型，涉及大量LLM调用生成摘要
    - 异步执行，不阻塞当前请求
    - 支持任务追踪（通过trace_raptor接口）
    - 任务失败后可重新运行
    
    权限要求：
    - 用户必须对该知识库有操作权限
    - 知识库必须至少包含一个文档
    
    异常处理：
    - 如果缺少知识库ID，返回 "Lack of KB ID"
    - 如果知识库不存在，返回 "Invalid dataset ID"
    - 如果已有任务在运行，返回 "A RAPTOR Task is already running"
    - 如果知识库中没有文档，返回 "No documents in dataset"
    
    注意：
    - RAPTOR任务会消耗大量LLM tokens，建议评估成本
    - 适用于长文档较多的知识库
    - 任务完成后，检索结果会自动包含多层摘要
    - 可通过unbind_task接口清除任务绑定关系
    """
    kb_id = request.kb_id
    if not kb_id:
        return get_error_data_result(retmsg='Lack of "KB ID"')

    kb = KnowledgebaseService.get_by_id(db, kb_id)
    if not kb:
        return get_error_data_result(retmsg="Invalid dataset ID")

    task_id = kb.raptor_task_id
    if task_id:
        task = TaskService.get_by_id(db, task_id)
        if not task:
            logging.warning(f"A valid RAPTOR task id is expected for kb {kb_id}")

        if task and task.progress not in [-1, 1]:
            return get_error_data_result(retmsg=f"Task {task_id} in progress with status {task.progress}. A RAPTOR Task is already running.")

    documents, _ = DocumentService.get_by_kb_id(
        db=db,
        kb_id=kb_id,
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
        return get_error_data_result(retmsg=f"No documents in dataset {kb_id}")

    sample_document = documents[0]
    document_ids = [document["id"] for document in documents]

    task_id = queue_raptor_o_graphrag_tasks(db, sample_doc_id=sample_document, ty="raptor", priority=0, fake_doc_id=GRAPH_RAPTOR_FAKE_DOC_ID, doc_ids=list(document_ids))

    if not KnowledgebaseService.update_by_id(db, kb.id, {"raptor_task_id": task_id}):
        logging.warning(f"Cannot save raptor_task_id for kb {kb_id}")

    return get_json_result(data={"raptor_task_id": task_id})


@router.get("/trace_raptor", summary="追踪RAPTOR任务", response_description="成功获取RAPTOR任务状态")
def trace_raptor(
        kb_id: str = Query(..., description="知识库ID"),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    追踪指定知识库的RAPTOR任务执行状态
    
    概要：实时查询RAPTOR任务的处理进度、状态和详细信息，用于监控递归摘要构建过程。
    
    参数、返回值、功能与trace_graphrag类似，但针对RAPTOR任务。
    
    RAPTOR任务进度说明：
    - 0-0.3: 文档分块和向量化阶段
    - 0.3-0.7: 递归聚类阶段
    - 0.7-0.95: 摘要生成阶段
    - 0.95-1.0: 多层索引构建阶段
    
    注意：RAPTOR任务通常比GraphRAG耗时更长，因为需要多次调用LLM生成摘要。
    """
    if not kb_id:
        return get_error_data_result(retmsg='Lack of "KB ID"')

    kb = KnowledgebaseService.get_by_id(db, kb_id)
    if not kb:
        return get_error_data_result(retmsg="Invalid dataset ID")

    task_id = kb.raptor_task_id
    if not task_id:
        return get_json_result(data={})

    task = TaskService.get_by_id(db, task_id)
    if not task:
        return get_error_data_result(retmsg="RAPTOR Task Not Found or Error Occurred")

    return get_json_result(data=task.to_dict())


@router.post("/run_mindmap", summary="运行思维导图", response_description="成功启动思维导图任务")
def run_mindmap(
        request: RunMindmapRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    为指定知识库启动思维导图生成任务
    
    概要：基于知识库中的所有文档，自动构建结构化的思维导图，帮助用户快速理解文档内容的层次结构和逻辑关系。
    
    参数：
    - **request**: 请求体
        - kb_id: 知识库ID（必填）
    
    返回：
    - dict: 包含任务信息
        - mindmap_task_id: Mindmap任务ID，可用于追踪任务进度
    
    功能：
    1. 验证知识库ID和文档存在性
    2. 检查是否已有Mindmap任务在运行
    3. 创建Mindmap任务并加入处理队列
    4. 保存任务ID到知识库记录
    
    Mindmap处理流程：
    1. **内容分析**：分析文档的主题和内容结构
    2. **层次提取**：识别文档的层次结构（标题、章节等）
    3. **关键点提取**：提取每个章节的关键信息点
    4. **关系构建**：建立内容之间的逻辑关系
    5. **图谱生成**：生成可视化的思维导图数据结构
    
    Mindmap优势：
    - 快速概览：一目了然地了解文档整体结构
    - 可视化展示：支持交互式思维导图可视化
    - 辅助学习：帮助用户理解复杂文档的逻辑关系
    - 导航便利：通过思维导图快速定位到具体内容
    
    应用场景：
    - 学术论文：展示论文的研究框架和逻辑结构
    - 技术文档：呈现技术文档的模块和功能关系
    - 报告文档：展示报告的章节结构和要点
    - 知识梳理：整理和归纳多篇文档的知识体系
    
    权限要求：
    - 用户必须对该知识库有操作权限
    - 知识库必须至少包含一个文档
    
    异常处理：
    - 如果缺少知识库ID，返回 "Lack of KB ID"
    - 如果知识库不存在，返回 "Invalid dataset ID"
    - 如果已有任务在运行，返回 "A Mindmap Task is already running"
    - 如果知识库中没有文档，返回 "No documents in dataset"
    
    注意：
    - 适用于结构化程度较高的文档（如学术论文、技术文档等）
    - 任务完成后，可通过knowledge_graph接口查看思维导图
    - 思维导图数据以JSON格式存储，支持多种可视化工具
    - 可通过unbind_task接口清除任务绑定关系
    """
    kb_id = request.kb_id
    if not kb_id:
        return get_error_data_result(retmsg='Lack of "KB ID"')

    kb = KnowledgebaseService.get_by_id(db, kb_id)
    if not kb:
        return get_error_data_result(retmsg="Invalid dataset ID")

    task_id = kb.mindmap_task_id
    if task_id:
        task = TaskService.get_by_id(db, task_id)
        if not task:
            logging.warning(f"A valid Mindmap task id is expected for kb {kb_id}")

        if task and task.progress not in [-1, 1]:
            return get_error_data_result(retmsg=f"Task {task_id} in progress with status {task.progress}. A Mindmap Task is already running.")

    documents, _ = DocumentService.get_by_kb_id(
        db=db,
        kb_id=kb_id,
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
        return get_error_data_result(retmsg=f"No documents in dataset {kb_id}")

    sample_document = documents[0]
    document_ids = [document["id"] for document in documents]

    task_id = queue_raptor_o_graphrag_tasks(db, sample_doc_id=sample_document, ty="mindmap", priority=0, fake_doc_id=GRAPH_RAPTOR_FAKE_DOC_ID, doc_ids=list(document_ids))

    if not KnowledgebaseService.update_by_id(db, kb.id, {"mindmap_task_id": task_id}):
        logging.warning(f"Cannot save mindmap_task_id for kb {kb_id}")

    return get_json_result(data={"mindmap_task_id": task_id})


@router.get("/trace_mindmap", summary="追踪思维导图任务", response_description="成功获取思维导图任务状态")
def trace_mindmap(
        kb_id: str = Query(..., description="知识库ID"),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    追踪指定知识库的Mindmap任务执行状态
    
    概要：实时查询Mindmap任务的处理进度、状态和详细信息，用于监控思维导图构建过程。
    
    参数、返回值、功能与trace_graphrag类似，但针对Mindmap任务。
    
    Mindmap任务进度说明：
    - 0-0.25: 文档内容分析阶段
    - 0.25-0.5: 层次结构提取阶段
    - 0.5-0.75: 关键点提取和关系构建阶段
    - 0.75-1.0: 思维导图数据生成和存储阶段
    
    注意：Mindmap任务相对较快，适合结构化文档的快速可视化。
    """
    if not kb_id:
        return get_error_data_result(retmsg='Lack of "KB ID"')

    kb = KnowledgebaseService.get_by_id(db, kb_id)
    if not kb:
        return get_error_data_result(retmsg="Invalid dataset ID")

    task_id = kb.mindmap_task_id
    if not task_id:
        return get_json_result(data={})

    task = TaskService.get_by_id(db, task_id)
    if not task:
        return get_error_data_result(retmsg="Mindmap Task Not Found or Error Occurred")

    return get_json_result(data=task.to_dict())


@router.delete("/unbind_task", summary="解绑知识库任务", response_description="成功解绑任务")
def delete_kb_task(
        kb_id: str = Query(..., description="知识库ID"),
        pipeline_task_type: str = Query(..., description="Pipeline任务类型"),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    解绑知识库与Pipeline任务的关联关系，并清理相关数据
    
    概要：删除知识库中保存的任务ID和完成时间，对于GraphRAG任务还会删除生成的知识图谱数据，允许重新运行任务。
    
    参数：
    - **kb_id**: 知识库ID（必填）
    - **pipeline_task_type**: Pipeline任务类型（必填），可选值：
        - "GRAPH_RAG": GraphRAG知识图谱任务
        - "RAPTOR": RAPTOR递归摘要任务
        - "MINDMAP": Mindmap思维导图任务
    
    返回：
    - dict: 操作结果
        - data: True 表示解绑成功
    
    功能：
    1. 验证知识库ID和任务类型的有效性
    2. 根据任务类型执行相应的清理操作：
       - **GraphRAG**: 
         - 删除向量数据库中的知识图谱数据（实体、关系、子图等）
         - 清除知识库的graphrag_task_id
         - 清除graphrag_task_finish_at时间戳
       - **RAPTOR**:
         - 清除知识库的raptor_task_id
         - 清除raptor_task_finish_at时间戳
       - **MINDMAP**:
         - 清除知识库的mindmap_task_id
         - 清除mindmap_task_finish_at时间戳
    3. 更新知识库记录，移除任务绑定关系
    
    业务场景：
    - 任务执行失败需要重新运行
    - 文档内容更新后需要重新构建图谱/摘要/思维导图
    - 清理旧的任务数据，释放存储空间
    - 切换不同的任务配置重新执行
    
    清理范围：
    - **GraphRAG**: 删除图谱数据（graph、subgraph、entity、relation）
    - **RAPTOR**: 只清除任务绑定，摘要数据作为普通chunks保留
    - **MINDMAP**: 只清除任务绑定，思维导图数据保留在知识图谱中
    
    权限要求：
    - 用户必须对该知识库有操作权限
    - 用户必须是知识库的所有者
    
    异常处理：
    - 如果缺少知识库ID，返回 "Lack of KB ID"
    - 如果知识库不存在，返回 True（幂等操作）
    - 如果任务类型无效，返回 "Invalid task type"
    - 如果更新失败，返回 "Internal error: cannot delete task"
    
    注意：
    - 解绑操作不会删除任务记录本身，只是解除与知识库的关联
    - GraphRAG解绑会删除图谱数据，操作不可逆，请谨慎使用
    - 解绑后可以立即重新运行任务
    - 如果任务正在运行中，建议先等待任务完成或失败后再解绑
    - 解绑操作是幂等的，多次调用结果相同
    
    使用示例：
    1. 删除GraphRAG并重建：DELETE /unbind_task?kb_id=xxx&pipeline_task_type=GRAPH_RAG
    2. 清除RAPTOR任务：DELETE /unbind_task?kb_id=xxx&pipeline_task_type=RAPTOR
    3. 重置Mindmap：DELETE /unbind_task?kb_id=xxx&pipeline_task_type=MINDMAP
    """
    if not kb_id:
        return get_error_data_result(retmsg='Lack of "KB ID"')
    
    kb = KnowledgebaseService.get_by_id(db, kb_id)
    if not kb:
        return get_json_result(data=True)

    if not pipeline_task_type or pipeline_task_type not in [PipelineTaskType.GRAPH_RAG, PipelineTaskType.RAPTOR, PipelineTaskType.MINDMAP]:
        return get_error_data_result(retmsg="Invalid task type")

    def cancel_task(task_id):
        REDIS_CONN.set(f"{task_id}-cancel", "x")

    kb_task_id_field: str = ""
    kb_task_finish_at: str = ""
    match pipeline_task_type:
        case PipelineTaskType.GRAPH_RAG:
            kb_task_id_field = "graphrag_task_id"
            task_id = kb.graphrag_task_id
            kb_task_finish_at = "graphrag_task_finish_at"
            cancel_task(task_id)
            settings.docStoreConn.delete({"knowledge_graph_kwd": ["graph", "subgraph", "entity", "relation"]}, search.index_name(kb.tenant_id), kb_id)
        case PipelineTaskType.RAPTOR:
            kb_task_id_field = "raptor_task_id"
            task_id = kb.raptor_task_id
            kb_task_finish_at = "raptor_task_finish_at"
            cancel_task(task_id)
            settings.docStoreConn.delete({"raptor_kwd": ["raptor"]}, search.index_name(kb.tenant_id), kb_id)
        case PipelineTaskType.MINDMAP:
            kb_task_id_field = "mindmap_task_id"
            task_id = kb.mindmap_task_id
            kb_task_finish_at = "mindmap_task_finish_at"
            cancel_task(task_id)
        case _:
            return get_error_data_result(retmsg="Internal Error: Invalid task type")

    ok = KnowledgebaseService.update_by_id(db, kb_id, {kb_task_id_field: "", kb_task_finish_at: None})
    if not ok:
        return server_error_response(f"Internal error: cannot delete task {pipeline_task_type}")

    return get_json_result(data=True)


@router.post("/check_embedding", summary="抽样校验知识库向量一致性")
def check_embedding(
        request: CheckEmbeddingRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):

    def _guess_vec_field(src: dict) -> str | None:
        for k in src or {}:
            if k.endswith("_vec"):
                return k
        return None

    def _as_float_vec(v):
        if v is None:
            return []
        if isinstance(v, str):
            return [float(x) for x in v.split("\t") if x != ""]
        if isinstance(v, (list, tuple, np.ndarray)):
            return [float(x) for x in v]
        return []

    def _to_1d(x):
        a = np.asarray(x, dtype=np.float32)
        return a.reshape(-1)

    def _cos_sim(a, b, eps=1e-12):
        a = _to_1d(a)
        b = _to_1d(b)
        na = np.linalg.norm(a)
        nb = np.linalg.norm(b)
        if na < eps or nb < eps:
            return 0.0
        return float(np.dot(a, b) / (na * nb))

    def sample_random_chunks_with_vectors(
        doc_store_conn,
        tenant_id: str,
        kb_id: str,
        kb_name: str,
        n: int = 5,
        base_fields=("docnm_kwd", "doc_id", "content_with_weight", "page_num_int", "position_int", "top_int"),
    ):
        idx_names = search.index_name(tenant_id, [kb_name])

        res0 = doc_store_conn.search(
            selectFields=[], highlightFields=[],
            condition={"kb_id": kb_id, "available_int": 1},
            matchExprs=[], orderBy=OrderByExpr(),
            offset=0, limit=1,
            indexNames=idx_names, knowledgebaseIds=[kb_id]
        )
        total = doc_store_conn.get_total(res0)
        if total <= 0:
            return []

        n = min(n, total)
        offsets = sorted(random.sample(range(min(total, 1000)), n))
        out = []

        for off in offsets:
            res1 = doc_store_conn.search(
                selectFields=list(base_fields),
                highlightFields=[],
                condition={"kb_id": kb_id, "available_int": 1},
                matchExprs=[], orderBy=OrderByExpr(),
                offset=off, limit=1,
                indexNames=idx_names, knowledgebaseIds=[kb_id]
            )
            ids = doc_store_conn.get_chunk_ids(res1)
            if not ids:
                continue

            cid = ids[0]
            full_doc = doc_store_conn.get(cid, search.index_name_one(tenant_id, kb_name), [kb_id]) or {}
            vec_field = _guess_vec_field(full_doc)
            vec = _as_float_vec(full_doc.get(vec_field))

            out.append({
                "chunk_id": cid,
                "kb_id": kb_id,
                "doc_id": full_doc.get("doc_id"),
                "doc_name": full_doc.get("docnm_kwd"),
                "vector_field": vec_field,
                "vector_dim": len(vec),
                "vector": vec,
                "page_num_int": full_doc.get("page_num_int"),
                "position_int": full_doc.get("position_int"),
                "top_int": full_doc.get("top_int"),
                "content_with_weight": full_doc.get("content_with_weight") or "",
                "question_kwd": full_doc.get("question_kwd") or []
            })
        return out

    def _clean(s: str) -> str:
        s = re.sub(r"</?(table|td|caption|tr|th)( [^<>]{0,12})?>", " ", s or "")
        return s if s else "None"
    req = request.model_dump()
    kb_id = req.get("kb_id", "")
    embd_id = req.get("embd_id", "")
    n = int(req.get("check_num", 5) or 5)

    if not KnowledgebaseService.accessible(db, kb_id, user.id):
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=RetCode.AUTHENTICATION_ERROR
        )

    kb = KnowledgebaseService.get_by_id(db, kb_id)
    if not kb:
        return get_error_data_result(retmsg="Invalid dataset ID")

    emb_mdl = LLMBundle(db, kb.tenant_id, LLMType.EMBEDDING, embd_id)
    samples = sample_random_chunks_with_vectors(settings.docStoreConn, tenant_id=kb.tenant_id, kb_id=kb_id, kb_name=kb.name, n=n)

    results, eff_sims = [], []
    for ck in samples:
        title = ck.get("doc_name") or "Title"
        txt_in = "\n".join(ck.get("question_kwd") or []) or ck.get("content_with_weight") or ""
        txt_in = _clean(txt_in)
        if not txt_in:
            results.append({"chunk_id": ck["chunk_id"], "reason": "no_text"})
            continue

        if not ck.get("vector"):
            results.append({"chunk_id": ck["chunk_id"], "reason": "no_stored_vector"})
            continue

        try:
            v, _ = emb_mdl.encode([title, txt_in])
            assert len(v[1]) == len(ck["vector"]), f"The dimension ({len(v[1])}) of given embedding model is different from the original ({len(ck['vector'])})"
            sim_content = _cos_sim(v[1], ck["vector"])
            title_w = 0.1
            qv_mix = title_w * v[0] + (1 - title_w) * v[1]
            sim_mix = _cos_sim(qv_mix, ck["vector"])
            sim = sim_content
            mode = "content_only"
            if sim_mix > sim:
                sim = sim_mix
                mode = "title+content"
        except Exception as e:
            return get_error_data_result(retmsg=f"Embedding failure. {e}")

        eff_sims.append(sim)
        results.append({
            "chunk_id": ck["chunk_id"],
            "doc_id": ck["doc_id"],
            "doc_name": ck["doc_name"],
            "vector_field": ck["vector_field"],
            "vector_dim": ck["vector_dim"],
            "cos_sim": round(sim, 6),
        })

    summary = {
        "kb_id": kb_id,
        "model": embd_id,
        "sampled": len(samples),
        "valid": len(eff_sims),
        "avg_cos_sim": round(float(np.mean(eff_sims)) if eff_sims else 0.0, 6),
        "min_cos_sim": round(float(np.min(eff_sims)) if eff_sims else 0.0, 6),
        "max_cos_sim": round(float(np.max(eff_sims)) if eff_sims else 0.0, 6),
        "match_mode": mode,
    }
    if summary["avg_cos_sim"] > 0.9:
        return get_json_result(data={"summary": summary, "results": results})
    return get_json_result(retcode=RetCode.NOT_EFFECTIVE, retmsg="Embedding model switch failed: the average similarity between old and new vectors is below 0.9, indicating incompatible vector spaces.", data={"summary": summary, "results": results})