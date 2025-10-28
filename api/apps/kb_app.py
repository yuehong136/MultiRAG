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

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.db.db_models import File, get_db
from api.db.services import duplicate_name
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.user_service import TenantService, UserTenantService
from api import settings
from api.utils.api_utils import server_error_response, get_data_error_result
from api.utils import get_uuid
from api.db import StatusEnum, FileSource
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import get_json_result
# from api.db.database import get_db
from api.apps import manager
# from core.utils.milvus_conn import MILVUS_CONNECTION
from core.nlp import search
from api.constants import DATASET_NAME_LIMIT, MILVUS_NAME_PATTERN
from core.utils.storage_factory import STORAGE_IMPL

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


class RemoveKnowledgebaseRequest(BaseModel):
    kb_id: str


class RemoveTagsRequest(BaseModel):
    tags: list[str]


class RenameTagRequest(BaseModel):
    from_tag: str
    to_tag: str


class ListKbsRequest(BaseModel):
    owner_ids: list[str] | None = []



@router.post('/create', summary="创建知识库", response_description="成功创建知识库")
def create(request: CreateKnowledgebaseRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req_data = request.model_dump()
    dataset_name = req_data["name"]
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
    # 检查数据库中是否已存在同名知识库
    existing_kb = KnowledgebaseService.query(
        db=db,
        name=dataset_name,
        tenant_id=user.id,
        status=StatusEnum.VALID.value
    )

    if existing_kb:
        # 如果已存在同名知识库，返回错误信息
        return get_data_error_result(retmsg=f"已存在该知识库名: {existing_kb[0].name}，请调整！")

    req_data["name"] = dataset_name
    # req_data["name"] = duplicate_name(
    #     KnowledgebaseService.query,
    #     db=db,
    #     name=dataset_name,
    #     tenant_id=user.id,
    #     status=StatusEnum.VALID.value
    # )
    try:
        req_data["id"] = get_uuid()
        req_data["tenant_id"] = user.id
        req_data["created_by"] = user.id
        t = TenantService.get_by_id(db, user.id)
        if not t:
            return get_data_error_result(retmsg="Tenant not found.")
        req_data["embd_id"] = t.embd_id if req_data["embd_id"] is None else req_data["embd_id"]
        if not KnowledgebaseService.save(db, **req_data):
            return get_data_error_result()
        return get_json_result(data={"kb_id": req_data["id"]})
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
            retcode=settings.RetCode.AUTHENTICATION_ERROR
        )
    try:
        if not KnowledgebaseService.query(db, created_by=user.id, id=req_data["kb_id"]):
            return get_json_result(
                data=False, retmsg=f'Only owner of knowledgebase authorized for this operation.',
                retcode=settings.RetCode.OPERATING_ERROR)

        kb = KnowledgebaseService.get_by_id(db, req_data["kb_id"])
        if not kb:
            return get_data_error_result(retmsg="Can't find this knowledgebase!")

        if req_data["parser_id"] == "tag" and os.environ.get('DOC_ENGINE', "milvus") == "milvus":
            return get_json_result(
                data=False,
                retmsg='The chunking method Tag has not been supported by milvus yet.',
                retcode=settings.RetCode.OPERATING_ERROR
            )

        if req_data["name"].lower() != kb.name.lower() \
                and len(KnowledgebaseService.query(db, name=req_data["name"], tenant_id=user.id,
                                                   status=StatusEnum.VALID.value)) > 1:
            return get_data_error_result(retmsg="Duplicated knowledgebase name.")

        # 过滤掉None值，避免将None写入数据库
        filtered_data = {k: v for k, v in req_data.items() if v is not None and k != "kb_id"}
        old_name = kb.name
        if not KnowledgebaseService.update_by_id(db, kb.id, filtered_data):
            return get_data_error_result()

        # ===== 插入 Milvus 重命名逻辑 =====
        if "name" in req_data:
            # 1 构造 Milvus 原集合名 & 新集合名
            old_coll = search.index_name_one(kb.tenant_id, old_name)
            new_coll = search.index_name_one(kb.tenant_id, req_data["name"])

            # 2 确认原集合存在
            if settings.docStoreConn.has_collection(old_coll):
                settings.docStoreConn.rename_collection(old_coll, new_coll)
                logging.info(f"Milvus collection renamed: {old_coll} → {new_coll}")

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

        kb = KnowledgebaseService.get_by_id(db, kb.id)
        if not kb:
            return get_data_error_result(retmsg="Database error (Knowledgebase rename)!")
        kb = kb.to_dict()
        # 使用filtered_data而不是req_data，避免包含None值
        kb.update(filtered_data)

        return get_json_result(data=kb)
    except Exception as e:
        return server_error_response(e)


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
                data=False, retmsg=f'Only owner of knowledgebase authorized for this operation.',
                retcode=settings.RetCode.OPERATING_ERROR)
        kb = KnowledgebaseService.get_detail(db, kb_id)
        if not kb:
            return get_data_error_result(retmsg="Can't find this knowledgebase!")
        kb["size"] = DocumentService.get_total_size_by_kb_id(db, kb_id=kb["id"],keywords="", run_status=[], types=[])
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
            if page_number and items_per_page:
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
            retcode=settings.RetCode.AUTHENTICATION_ERROR
        )
    try:
        # 查询知识库，确保只有知识库的创建者有权限删除
        kbs = KnowledgebaseService.query(db, created_by=user.id, id=req_data["kb_id"])
        if not kbs:
            # 如果知识库不存在或用户无权限删除，返回错误信息
            return get_json_result(
                data=False, retmsg=f'Only owner of knowledgebase authorized for this operation.',
                retcode=settings.RetCode.OPERATING_ERROR)

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
            STORAGE_IMPL.rm(b, n)
        FileService.filter_delete(
            db, [File.source_type == FileSource.KNOWLEDGEBASE, File.type == "folder", File.name == kb_name])

        # 删除 MinIO 存储桶
        STORAGE_IMPL.remove_bucket(kb_id)

        # 删除知识库本身，如果失败则返回错误信息
        if not KnowledgebaseService.delete_by_id(db, req_data["kb_id"]):
            return get_data_error_result(retmsg="Database error (Knowledgebase removal)!")
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
            retcode=settings.RetCode.AUTHENTICATION_ERROR
        )
    tenants = UserTenantService.get_tenants_by_user_id(db, user.id)
    tags = []
    for tenant in tenants:
        tags += settings.retrievaler.all_tags(tenant["tenant_id"], [kb_id])
    return get_json_result(data=tags)


@router.get("/tags", summary="获取多个知识库的标签")
def list_tags_from_kbs(kb_ids: str, db: Session = Depends(get_db), user=Depends(manager)):
    kb_id_list = kb_ids.split(",")
    for kb_id in kb_id_list:
        if not KnowledgebaseService.accessible(db, kb_id, user.id):
            return get_json_result(
                data=False,
                retmsg='No authorization.',
                retcode=settings.RetCode.AUTHENTICATION_ERROR
            )
    tenants = UserTenantService.get_tenants_by_user_id(db, user.id)
    tags = []
    for tenant in tenants:
        tags += settings.retrievaler.all_tags(tenant["tenant_id"], kb_ids)
    return get_json_result(data=tags)


@router.post("/{kb_id}/rm_tags", summary="删除知识库标签")
def rm_tags(kb_id: str, request: RemoveTagsRequest, db: Session = Depends(get_db), user=Depends(manager)):
    if not KnowledgebaseService.accessible(db, kb_id, user.id):
        return get_json_result(
            data=False,
            retmsg='No authorization.',
            retcode=settings.RetCode.AUTHENTICATION_ERROR
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
            retcode=settings.RetCode.AUTHENTICATION_ERROR
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
            retcode=settings.RetCode.AUTHENTICATION_ERROR
        )
    kb = KnowledgebaseService.get_by_id(db, kb_id)
    req = {
        "kb_id": [kb_id],
        "knowledge_graph_kwd": ["graph"]
    }
    obj = {"graph": {}, "mind_map": {}}
    if not settings.docStoreConn.indexExist(search.index_name(kb.tenant_id, [kb.name]), kb_id):
        return get_json_result(data=obj)
    sres = settings.retrievaler.search(req, search.index_name(kb.tenant_id, [kb.name]), [kb_id])
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
            retcode=settings.RetCode.AUTHENTICATION_ERROR
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
                retcode=settings.RetCode.AUTHENTICATION_ERROR
            )

    try:
        meta = DocumentService.get_meta_by_kbs(db, kb_id_list)
        return get_json_result(data=meta)
    except Exception as e:
        return server_error_response(e)


@router.get("/basic_info", summary="获取知识库文档处理统计信息", response_description="返回文档处理状态统计")
async def get_basic_info(
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
                retcode=settings.RetCode.AUTHENTICATION_ERROR
            )

        # 获取统计信息
        basic_info = DocumentService.knowledgebase_basic_info(db, kb_id)

        return get_json_result(data=basic_info)
    except Exception as e:
        return server_error_response(e)