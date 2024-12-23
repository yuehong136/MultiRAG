# coding=utf-8
"""
@project: multirag
@Author：龙
@file： kb_app.py
@date：2024/8/5 9:22
@desc:
"""
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api.db.db_models import File
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
from api.db.database import get_db
from api.apps import manager
from core.utils.milvus_conn import MILVUS_CONNECTION
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
    parser_id: str | None = None


class RemoveKnowledgebaseRequest(BaseModel):
    kb_id: str


@router.post('/create', summary="创建知识库", response_description="成功创建知识库")
def create(request: CreateKnowledgebaseRequest, db: Session = Depends(get_db), user=Depends(manager)):
    req_data = request.model_dump()
    dataset_name = req_data["name"]
    if not isinstance(dataset_name, str):
        return get_data_error_result(retmsg="Dataset name must be string.")
    if dataset_name == "":
        return get_data_error_result(retmsg="Dataset name can't be empty.")
    if len(dataset_name) >= DATASET_NAME_LIMIT:
        return get_data_error_result(
            retmsg=f"Dataset name length is {len(dataset_name)} which is large than {DATASET_NAME_LIMIT}")
    # 验证 Milvus 集合名逻辑
    if not re.match(MILVUS_NAME_PATTERN, dataset_name):
        return get_data_error_result(
            retmsg="Dataset name must start with a letter and contain only letters, numbers, and underscores."
        )

    dataset_name = dataset_name.strip()
    req_data["name"] = duplicate_name(
        KnowledgebaseService.query,
        db=db,
        name=dataset_name,
        tenant_id=user.id,
        status=StatusEnum.VALID.value
    )
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

        if req_data["name"].lower() != kb.name.lower() \
                and len(KnowledgebaseService.query(db, name=req_data["name"], tenant_id=user.id,
                                                   status=StatusEnum.VALID.value)) > 1:
            return get_data_error_result(retmsg="Duplicated knowledgebase name.")

        del req_data["kb_id"]
        if not KnowledgebaseService.update_by_id(db, kb.id, req_data):
            return get_data_error_result()

        # if kb.pagerank != req_data.get("pagerank", 0):
        #     if req_data.get("pagerank", 0) > 0:
        #         settings.docStoreConn.update({"kb_id": kb.id}, {"pagerank_fea": req_data["pagerank"]},
        #                                  search.index_name_one(kb.tenant_id, kb.name), kb.id)
        #     else:
        #         settings.docStoreConn.update({"exist": "pagerank_fea"}, {"remove": "pagerank_fea"},
        #                                  search.index_name_one(kb.tenant_id, kb.name), kb.id)

        kb = KnowledgebaseService.get_by_id(db, kb.id)
        if not kb:
            return get_data_error_result(retmsg="Database error (Knowledgebase rename)!")

        return get_json_result(data=kb.to_dict())
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
        return get_json_result(data=kb)
    except Exception as e:
        return server_error_response(e)


@router.get('/list', summary="列出知识库", response_description="成功列出知识库")
def list_kbs(page: int = 1, page_size: int = 150, orderby: str = "create_time", desc: bool = True, keywords: str = "",
             db: Session = Depends(get_db), user=Depends(manager)):
    page_number = int(page)
    items_per_page = int(page_size)
    try:
        tenants = TenantService.get_joined_tenants_by_user_id(db, user.id)
        kbs, total = KnowledgebaseService.get_by_tenant_ids(
            db, [m["tenant_id"] for m in tenants], user.id, page_number, items_per_page, orderby, desc, keywords)
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
            FileService.filter_delete(
                db,
                [File.source_type == FileSource.KNOWLEDGEBASE, File.id == f2d[0].file_id]
            )
            # 删除文档与文件的关联记录
            File2DocumentService.delete_by_document_id(db, doc_id)
            STORAGE_IMPL.rm(b, n)
        FileService.filter_delete(
            db, [File.source_type == FileSource.KNOWLEDGEBASE, File.type == "folder", File.name == kb_name])

        # 删除 MinIO 存储桶
        STORAGE_IMPL.delete_bucket(kb_id)

        # 删除知识库本身，如果失败则返回错误信息
        if not KnowledgebaseService.delete_by_id(db, req_data["kb_id"]):
            return get_data_error_result(retmsg="Database error (Knowledgebase removal)!")
        tenants = UserTenantService.query(db, user_id=user.id)
        for tenant in tenants:
            MILVUS_CONNECTION.deleteIdx(search.index_name_one(tenant.tenant_id, kb_name), req_data["kb_id"])
        # 知识库删除成功，返回成功标志
        return get_json_result(data=True)
    except Exception as e:
        # 捕获异常，返回服务器错误响应
        return server_error_response(e)
