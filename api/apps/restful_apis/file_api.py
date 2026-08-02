"""
@project: multirag
@file: file_api.py
@desc: File API 网关层 - RESTful 风格的文件管理接口，挂在 /api/v1 下。

对标 ragflow `api/apps/restful_apis/file_api.py`（#13741）。路由 + 鉴权 + 参数校验，
业务逻辑委托给 services/file_api_service.py。

鉴权：复用统一异步鉴权依赖 async_current_tenant_id（同时接受 web 会话 JWT 与 SDK API-key），
      与 dataset_api.py 一致；对外既服务 web 前端又服务 SDK。

与旧 file_app.py（/v1/file/*）的端点映射：
    POST   /files            <- /upload(multipart) + /create(json)（按 content-type 分发）
    GET    /files            <- /list
    DELETE /files            <- /rm（file_ids -> ids）
    POST   /files/move       <- /mv + /rename（Linux mv 语义合并）
    GET    /files/{id}       <- /get/{id}
    GET    /files/{id}/parent    <- /parent_folder
    GET    /files/{id}/ancestors <- /all_parent_folder
旧路由保留并标 deprecated（生产仍在用），见 file_app.py。
"""

from __future__ import annotations

import asyncio
import logging
import re
from io import BytesIO
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, Depends, File, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, model_validator
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from api.apps.deps import get_storage
from api.apps.services import file_api_service
from api.apps.services.file_convert_service import convert_files_with_new_session
from api.common.check_team_permission import check_file_team_permission, check_kb_team_permission
from api.db import FileType
from api.db.db_models import get_async_db
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import async_current_tenant_id, get_error_argument_result, get_error_data_result, get_json_result, get_result, server_error_response
from api.utils.web_utils import CONTENT_TYPE_MAP, apply_safe_file_response_headers
from common.constants import RetCode
from common.misc_utils import thread_pool_exec

router = APIRouter()
logger = logging.getLogger(__name__)


# ==================== Pydantic Models (V2 风格) ====================


class CreateFolderReq(BaseModel):
    name: str = Field(..., min_length=1, max_length=255, description="文件夹名")
    parent_id: str | None = Field(None, description="父文件夹ID")
    type: str | None = Field(None, description="文件类型（folder / virtual）")


class DeleteFileReq(BaseModel):
    ids: list[str] = Field(..., min_length=1, description="待删除的文件ID列表")


class MoveFileReq(BaseModel):
    src_file_ids: list[str] = Field(..., min_length=1, description="源文件ID列表")
    dest_file_id: str | None = Field(None, description="目标文件夹ID；省略表示原地重命名")
    new_name: str | None = Field(None, min_length=1, max_length=255, description="新文件名；仅对单个源文件有效")

    @model_validator(mode="after")
    def check_operation(self):
        if not self.dest_file_id and not self.new_name:
            raise ValueError("At least one of dest_file_id or new_name must be provided")
        if self.new_name and len(self.src_file_ids) > 1:
            raise ValueError("new_name can only be used with a single file")
        return self


# ==================== 响应映射 ====================


def _respond(success: bool, result: Any):
    """成功 -> get_result(data)，失败 -> get_error_data_result(retmsg)。"""
    if success:
        return get_result(data=result)
    if isinstance(result, JSONResponse):
        return result
    return get_error_data_result(retmsg=result)


# ==================== API Endpoints ====================


@router.post("/files", summary="上传文件或创建文件夹")
async def create_or_upload(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    """按 Content-Type 分发：multipart/form-data 上传文件，否则创建文件夹。"""
    content_type = request.headers.get("content-type") or ""
    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            pf_id = form.get("parent_id")
            file_objs = form.getlist("file")
            if not file_objs:
                return get_error_argument_result("No file part!")

            file_contents = []
            for file_obj in file_objs:
                if getattr(file_obj, "filename", "") == "":
                    return get_error_argument_result("No file selected!")
                blob = await file_obj.read()
                file_contents.append((blob, file_obj.filename))

            # DB 与存储写逐文件交错：整块在工作线程 + 自开短会话执行
            success, result = await file_api_service.upload_file_async(tenant_id, pf_id, file_contents)
            return _respond(success, result)

        body = await request.json()
        try:
            req = CreateFolderReq.model_validate(body)
        except ValidationError as ve:
            return get_error_argument_result(str(ve))

        success, result = await db.run_sync(lambda s: file_api_service.create_folder(s, tenant_id, req.name, req.parent_id, req.type))  # TODO(async-phase4)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/files", summary="列出文件夹下的文件")
async def list_files(
    parent_id: str | None = Query(None, description="父文件夹ID"),
    keywords: str = Query("", description="搜索关键字"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(15, ge=1, le=100, description="每页数量"),
    orderby: str = Query("create_time", description="排序字段"),
    desc: bool = Query(True, description="是否降序"),
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    args = {
        "parent_id": parent_id,
        "keywords": keywords,
        "page": page,
        "page_size": page_size,
        "orderby": orderby,
        "desc": desc,
    }
    try:
        success, result = await db.run_sync(lambda s: file_api_service.list_files(s, tenant_id, args))  # TODO(async-phase4)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


# ---------------------------------------------------------------------------
# multirag 专有端点（ragflow #13741 无对应），收编自退役的 sdk/files.py，
# 鉴权统一为 async_current_tenant_id（web 会话 + SDK key 皆可）。
# 注意：GET /files/root 必须在 GET /files/{file_id} 之前注册，否则会被路径参数吞掉。
# ---------------------------------------------------------------------------


@router.post("/files/upload_info", summary="上传运行时文件元数据（SDK 会话用）")
async def upload_info(
    files: list[UploadFile] | None = File(None),
    url: str | None = Query(None),
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    """为 SDK chat completions 上传运行时文件元数据；multipart 文件与 URL 互斥、须其一。"""
    file_objs = [f for f in files if getattr(f, "filename", "")] if files else []

    if file_objs and url:
        return get_error_argument_result("Provide either multipart file(s) or ?url=..., not both.")
    if not file_objs and not url:
        return get_error_argument_result("Missing input: provide multipart file(s) or url")

    try:
        if url and not file_objs:
            return get_result(data=await FileService.upload_info(db, tenant_id, None, url))
        if len(file_objs) == 1:
            return get_result(data=await FileService.upload_info(db, tenant_id, file_objs[0], None))
        results = [await FileService.upload_info(db, tenant_id, f, None) for f in file_objs]
        return get_result(data=results)
    except Exception as e:
        return server_error_response(e)


@router.get("/files/root", summary="获取根文件夹")
async def get_root_folder(
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    """获取用户根文件夹。"""
    try:
        root_folder = await db.run_sync(lambda s: FileService.get_root_folder(s, tenant_id))  # TODO(async-phase4)
        return get_result(data={"root_folder": root_folder})
    except Exception as e:
        return server_error_response(e)


@router.delete("/files", summary="删除文件或文件夹")
async def delete(
    request_body: DeleteFileReq,
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        # 存储 rm 与 remove_document（内混 Redis/存储/doc-store）交错：整块在工作线程 + 自开短会话执行
        success, result = await file_api_service.delete_files_async(tenant_id, request_body.ids)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.post("/files/move", summary="移动并/或重命名文件")
async def move(
    request_body: MoveFileReq,
    tenant_id: str = Depends(async_current_tenant_id),
):
    """遵循 Linux mv 语义：dest_file_id 与 new_name 至少给一个。
    - 仅 dest_file_id：移动到新文件夹（保持文件名）
    - 仅 new_name：原地重命名（不动存储）
    - 两者都给：同时移动并重命名
    """
    try:
        # 跨文件夹移动时存储 obj_exist/move 与 DB 更新交错：整块在工作线程 + 自开短会话执行
        success, result = await file_api_service.move_files_async(tenant_id, request_body.src_file_ids, request_body.dest_file_id, request_body.new_name)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/files/{file_id}", summary="下载文件")
async def download(
    file_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
    storage: Any = Depends(get_storage),
):
    try:

        def _file_meta(s: Session) -> tuple[dict | None, str]:
            success, result = file_api_service.get_file_content(s, tenant_id, file_id)
            if not success:
                return None, result
            return {"parent_id": result.parent_id, "location": result.location, "name": result.name, "type": result.type}, ""

        meta, err = await db.run_sync(_file_meta)  # TODO(async-phase4)
        if meta is None:
            return get_error_data_result(retmsg=err)

        # 存储读取是同步 IO：to_thread 外移避免阻塞事件循环
        blob = await asyncio.to_thread(storage.get, meta["parent_id"], meta["location"])
        if not blob:
            b, n = await db.run_sync(lambda s: File2DocumentService.get_storage_address(s, file_id=file_id))  # TODO(async-phase4)
            blob = await asyncio.to_thread(storage.get, b, n)
        if not blob:
            return get_error_data_result(retmsg="File not found in storage")

        ext = re.search(r"\.([^.]+)$", meta["name"].lower())
        ext = ext.group(1) if ext else None
        content_type = None
        if ext:
            fallback_prefix = "image" if meta["type"] == FileType.VISUAL.value else "application"
            content_type = CONTENT_TYPE_MAP.get(ext, f"{fallback_prefix}/{ext}")
        encoded_filename = quote(meta["name"])

        response = StreamingResponse(BytesIO(blob), media_type=content_type or "application/octet-stream")
        response.headers["Content-Disposition"] = f"attachment; filename={encoded_filename}"
        apply_safe_file_response_headers(response, content_type, ext)
        return response
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/files/{file_id}/parent", summary="获取文件的父文件夹")
async def parent_folder(
    file_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        success, result = await db.run_sync(lambda s: file_api_service.get_parent_folder(s, file_id, user_id=tenant_id))  # TODO(async-phase4)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


@router.get("/files/{file_id}/ancestors", summary="获取文件的全部祖先文件夹")
async def ancestors(
    file_id: str,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    try:
        success, result = await db.run_sync(lambda s: file_api_service.get_all_parent_folders(s, file_id, user_id=tenant_id))  # TODO(async-phase4)
        return _respond(success, result)
    except Exception as e:
        logger.exception(e)
        return get_error_data_result(retmsg="Internal server error")


# ---------------------------------------------------------------------------
# 文件关联知识库的正典入口为 /files/link-to-datasets；旧 /file/convert 路径继续
# 兼容 SDK，标记 deprecated。鉴权统一为 async_current_tenant_id。
# ---------------------------------------------------------------------------


@router.post("/files/link-to-datasets", summary="关联文件到知识库")
@router.post("/file/convert", summary="[Deprecated] 文件转换为知识库文档", deprecated=True)
async def convert(
    kb_ids: list[str],
    file_ids: list[str],
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    """把文件（含文件夹内最内层文件）转换/关联为指定知识库的文档。"""
    try:

        def _collect(s: Session) -> tuple[list[str] | None, JSONResponse | None]:
            files = FileService.get_by_ids(s, file_ids)
            files_set = {file.id: file for file in files}
            for file_id in file_ids:
                file = files_set.get(file_id)
                if not file:
                    return None, get_json_result(retmsg="File not found!", retcode=RetCode.NOT_FOUND)

            knowledgebases = {}
            for kb_id in kb_ids:
                kb = KnowledgebaseService.get_by_id(s, kb_id)
                if not kb:
                    return None, get_json_result(retmsg="Can't find this dataset!", retcode=RetCode.NOT_FOUND)
                knowledgebases[kb_id] = kb

            all_file_ids: list[str] = []
            for file_id in file_ids:
                file = files_set[file_id]
                if file.type == FileType.FOLDER.value:
                    all_file_ids.extend(FileService.get_all_innermost_file_ids(s, file_id, []))
                else:
                    all_file_ids.append(file_id)

            expanded_files = FileService.get_by_ids(s, all_file_ids)
            expanded_files_set = {file.id: file for file in expanded_files}
            for file_id in all_file_ids:
                file = expanded_files_set.get(file_id)
                if not file:
                    return None, get_json_result(retmsg="File not found!", retcode=RetCode.NOT_FOUND)
                if not check_file_team_permission(s, file, tenant_id):
                    return None, get_error_data_result(retmsg="No authorization.")
            for kb in knowledgebases.values():
                if not check_kb_team_permission(s, kb, tenant_id):
                    return None, get_error_data_result(retmsg="No authorization.")
            return all_file_ids, None

        all_file_ids, error = await db.run_sync(_collect)  # TODO(async-phase4)
        if error is not None:
            return error

        background_tasks.add_task(convert_files_with_new_session, all_file_ids, kb_ids, tenant_id)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.get("/file/download/{attachment_id}", summary="下载 attachment 文件（SDK）")
async def download_attachment(
    attachment_id: str,
    ext: str = Query("markdown"),
    tenant_id: str = Depends(async_current_tenant_id),
    storage: Any = Depends(get_storage),
):
    """下载 message 组件输出的 attachment 文件。"""
    try:
        data = await thread_pool_exec(storage.get, tenant_id, attachment_id)
        content_type = CONTENT_TYPE_MAP.get(ext, f"application/{ext}")
        response = StreamingResponse(BytesIO(data), media_type=content_type)
        apply_safe_file_response_headers(response, content_type, ext)
        return response
    except Exception as e:
        return server_error_response(e)
