import logging
import os
import pathlib
import re
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from starlette.responses import StreamingResponse

from api.apps import manager
from api.common.check_team_permission import check_file_team_permission
from api.db import FileType
from api.db.db_models import get_db
from api.db.services import duplicate_name
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.utils.api_utils import construct_error_response, get_data_error_result, get_json_result
from api.utils.file_utils import filename_type
from api.utils.web_utils import CONTENT_TYPE_MAP, apply_safe_file_response_headers
from common import settings
from common.constants import FileSource, RetCode
from common.misc_utils import get_uuid, thread_pool_exec

router = APIRouter()

# ============================================================================
# 【废弃通告】本文件的文件管理路由已 RESTful 化，迁移至
#   api/apps/restful_apis/file_api.py（对外 /api/v1/files，对标 ragflow #13741）。
# 下列旧路由（/v1/file/*）已标注 deprecated=True：生产环境仍在调用，暂不删除，
# 待前端迁移到 /api/v1/files 后再于后续版本移除。
# 端点映射：
#   POST /upload, POST /create        -> POST   /api/v1/files
#   GET  /list                        -> GET    /api/v1/files
#   POST /rm                          -> DELETE /api/v1/files
#   POST /rename, POST /mv            -> POST   /api/v1/files/move
#   GET  /get/{file_id}               -> GET    /api/v1/files/{file_id}
#   GET  /parent_folder               -> GET    /api/v1/files/{file_id}/parent
#   GET  /all_parent_folder           -> GET    /api/v1/files/{file_id}/ancestors
# 未迁移（保持启用）：/upload_media_redirect（multirag 专有）、/root_folder。
# ============================================================================


class UploadRequest(BaseModel):
    parent_id: str | None = Field(None, description="父文件夹ID")


class CreateRequest(BaseModel):
    name: str = Field(..., description="文件名")
    parent_id: str | None = Field(None, description="父文件夹ID")
    type: str | None = Field(None, description="文件类型")


class RemoveRequest(BaseModel):
    file_ids: list[str] = Field(..., description="文件ID列表")


class RenameRequest(BaseModel):
    file_id: str = Field(..., description="文件ID")
    name: str = Field(..., description="新的文件名")


class MoveRequest(BaseModel):
    src_file_ids: list[str] = Field(..., description="源文件ID列表")
    dest_file_id: str = Field(..., description="目标文件夹ID")


@router.post("/upload_media_redirect", summary="上传媒体并获取临时URL", response_description="成功获取临时公网URL")
async def upload_media_redirect(file: UploadFile = File(...), db: Session = Depends(get_db), user=Depends(manager)):
    """
    上传媒体文件（如视频、图片）到对象存储，并返回临时公网可访问的 URL。
    该 URL 可用于传递给火山引擎等 AI 服务进行多模态分析。

    参数:
    - file: 上传的媒体文件

    返回:
    - JSON: 包含临时 URL 和过期时间的响应
    """
    try:
        # 异步读取文件内容
        content = await file.read()
        if not content:
            return get_json_result(data=False, retmsg="No file content!", retcode=RetCode.ARGUMENT_ERROR)

        filename = file.filename

        # 将同步的存储操作放到线程池中执行
        def _upload_sync():
            # 1. 定义存储桶和文件名
            # 建议使用一个专门的临时桶，如果未配置则使用默认桶
            # 注意：MinIO/OSS 的 bucket 名称通常有格式要求
            bucket = settings.OSS.get("bucket") or settings.MINIO.get("bucket") or "multimodal-temp"

            ext = filename.split(".")[-1].lower() if "." in filename else "bin"
            unique_filename = f"volc_upload/{get_uuid()}.{ext}"

            # Get content type
            content_type = CONTENT_TYPE_MAP.get(ext, "application/octet-stream")

            # 2. 上传文件
            # STORAGE_IMPL 会自动处理 MinIO/OSS/S3 的差异
            try:
                settings.STORAGE_IMPL.put(bucket, unique_filename, content, content_type=content_type)
            except TypeError:
                # Fallback for storage backends that don't support content_type
                settings.STORAGE_IMPL.put(bucket, unique_filename, content)

            # 3. 获取预签名 URL (有效期 1小时)
            # 这是关键：这个 URL 是带签名的，AI 服务可以通过公网访问并下载
            expires = 3600
            url = settings.STORAGE_IMPL.get_presigned_url(bucket, unique_filename, expires=expires)

            if not url:
                raise Exception("Failed to generate presigned URL")

            return get_json_result(data={"url": url, "expires_in": expires, "filename": unique_filename})

        return await thread_pool_exec(_upload_sync)

    except Exception as e:
        logging.exception("Upload media redirect failed")
        return construct_error_response(e)


@router.post("/upload", summary="上传文件", response_description="成功上传文件", deprecated=True)
async def upload(parent_id: str, files: list[UploadFile] = File(...), db: Session = Depends(get_db), user=Depends(manager)):
    """
    上传文件。

    参数:
    - parent_id: 父文件夹ID。
    - files: 上传的文件列表。

    返回:
    - JSON: 上传文件结果的JSON响应。
    """
    if not files:
        return get_json_result(data=False, retmsg="No file part!", retcode=RetCode.ARGUMENT_ERROR)

    for file_obj in files:
        if file_obj.filename == "":
            return get_json_result(data=False, retmsg="No file selected!", retcode=RetCode.ARGUMENT_ERROR)

    # 异步读取所有文件内容
    file_contents = []
    for file_obj in files:
        blob = await file_obj.read()
        file_contents.append((blob, file_obj.filename))

    # 将同步的数据库和存储操作放到线程池中执行
    def _upload_sync():
        pf_id = parent_id

        if not pf_id:
            root_folder = FileService.get_root_folder(db, user.id)
            pf_id = root_folder["id"]

        pf_folder = FileService.get_by_id(db, pf_id)
        if not pf_folder:
            return get_data_error_result(retmsg="Can't find this folder!")

        file_dict = None
        for blob, filename in file_contents:
            MAX_FILE_NUM_PER_USER: int = int(os.environ.get("MAX_FILE_NUM_PER_USER", 0))
            if 0 < MAX_FILE_NUM_PER_USER <= DocumentService.get_doc_count(db, user.id):
                return get_data_error_result(retmsg="Exceed the maximum file number of a free user!")

            if not filename:
                file_obj_names = [pf_folder.name, filename]
            else:
                full_path = "/" + filename
                file_obj_names = full_path.split("/")
            file_len = len(file_obj_names)

            file_id_list = FileService.get_id_list_by_id(db, pf_id, file_obj_names, 1, [pf_id])
            len_id_list = len(file_id_list)

            if file_len != len_id_list:
                file = FileService.get_by_id(db, file_id_list[len_id_list - 1])
                if not file:
                    return get_data_error_result(retmsg="Folder not found!")
                last_folder = FileService.create_folder(db, file, file_id_list[len_id_list - 1], file_obj_names, len_id_list)
            else:
                file = FileService.get_by_id(db, file_id_list[len_id_list - 2])
                if not file:
                    return get_data_error_result(retmsg="Folder not found!")
                last_folder = FileService.create_folder(db, file, file_id_list[len_id_list - 2], file_obj_names, len_id_list)

            filetype = filename_type(file_obj_names[file_len - 1])
            location = file_obj_names[file_len - 1]
            while settings.STORAGE_IMPL.obj_exist(last_folder.id, location):
                location += "_"

            final_filename = duplicate_name(FileService.query, db=db, name=file_obj_names[file_len - 1], parent_id=last_folder.id)
            settings.STORAGE_IMPL.put(last_folder.id, location, blob)
            file_data = {
                "id": get_uuid(),
                "parent_id": last_folder.id,
                "tenant_id": user.id,
                "created_by": user.id,
                "type": filetype,
                "name": final_filename,
                "location": location,
                "size": len(blob),
            }
            file = FileService.insert(db, file_data)
            file_dict = {
                "id": file.id,
                "parent_id": file.parent_id,
                "tenant_id": file.tenant_id,
                "created_by": file.created_by,
                "name": file.name,
                "location": file.location,
                "size": file.size,
                "type": file.type,
            }
        return get_json_result(data=file_dict)

    try:
        return await thread_pool_exec(_upload_sync)
    except Exception as e:
        return construct_error_response(e)


@router.post("/create", summary="创建文件或文件夹", response_description="成功创建文件或文件夹", deprecated=True)
def create(request_body: CreateRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    创建文件或文件夹。

    参数:
    - request_body: 请求体，包含创建文件或文件夹的信息。

    返回:
    - JSON: 创建结果的JSON响应。
    """
    req = request_body.model_dump()
    pf_id = req.get("parent_id")
    input_file_type = req.get("type")
    if not pf_id:
        root_folder = FileService.get_root_folder(db, user.id)
        pf_id = root_folder["id"]

    try:
        if not FileService.is_parent_folder_exist(db, pf_id):
            return get_json_result(data=False, retmsg="Parent Folder Doesn't Exist!", retcode=RetCode.OPERATING_ERROR)
        if FileService.query(db, name=req["name"], parent_id=pf_id):
            return get_data_error_result(retmsg="Duplicated folder name in the same folder.")

        if input_file_type == FileType.FOLDER.value:
            file_type = FileType.FOLDER.value
        else:
            file_type = FileType.VIRTUAL.value

        file = FileService.insert(db, {"id": get_uuid(), "parent_id": pf_id, "tenant_id": user.id, "created_by": user.id, "name": req["name"], "location": "", "size": 0, "type": file_type})
        # 手动转换对象为字典格式
        file_dict = {
            "id": file.id,
            "parent_id": file.parent_id,
            "tenant_id": file.tenant_id,
            "created_by": file.created_by,
            "name": file.name,
            "location": file.location,
            "size": file.size,
            "type": file.type,
        }

        return get_json_result(data=file_dict)
    except Exception as e:
        return construct_error_response(e)


@router.get("/list", summary="列出文件或文件夹", response_description="成功列出文件或文件夹", deprecated=True)
def list_files(
    parent_id: str | None = None, keywords: str = "", page: int = 1, page_size: int = 15, orderby: str = "create_time", desc: bool = True, db: Session = Depends(get_db), user=Depends(manager)
):
    """
    列出文件或文件夹。

    参数:
    - parent_id: 父文件夹ID。
    - keywords: 搜索关键字。
    - page: 页码。
    - page_size: 每页显示数量。
    - orderby: 排序字段。
    - desc: 是否降序排列。

    返回:
    - JSON: 列出结果的JSON响应。
    """
    if not parent_id:
        root_folder = FileService.get_root_folder(db, user.id)
        parent_id = root_folder["id"]
        FileService.init_knowledgebase_docs(db, parent_id, user.id)
    try:
        file = FileService.get_by_id(db, parent_id)
        if not file:
            return get_data_error_result(retmsg="Folder not found!")

        files, total = FileService.get_by_pf_id(db, user.id, parent_id, page, page_size, orderby, desc, keywords)

        parent_folder = FileService.get_parent_folder(db, parent_id)
        if not parent_folder:
            return get_json_result(retmsg="File not found!")

        return get_json_result(data={"total": total, "files": files, "parent_folder": parent_folder.to_dict()})
    except Exception as e:
        return construct_error_response(e)


@router.get("/root_folder", summary="获取根文件夹", response_description="成功获取根文件夹")
def get_root_folder(db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取根文件夹。

    返回:
    - JSON: 根文件夹的JSON响应。
    """
    try:
        root_folder = FileService.get_root_folder(db, user.id)
        return get_json_result(data={"root_folder": root_folder})
    except Exception as e:
        return construct_error_response(e)


@router.get("/parent_folder", summary="获取父文件夹", response_description="成功获取父文件夹", deprecated=True)
def get_parent_folder(file_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取父文件夹。

    参数:
    - file_id: 文件ID。

    返回:
    - JSON: 父文件夹的JSON响应。
    """
    try:
        file = FileService.get_by_id(db, file_id)
        if not file:
            return get_data_error_result(retmsg="Folder not found!")

        parent_folder = FileService.get_parent_folder(db, file_id)
        return get_json_result(data={"parent_folder": parent_folder.to_dict()})
    except Exception as e:
        return construct_error_response(e)


@router.get("/all_parent_folder", summary="获取所有父文件夹", response_description="成功获取所有父文件夹", deprecated=True)
def get_all_parent_folders(file_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取所有父文件夹。

    参数:
    - file_id: 文件ID。

    返回:
    - JSON: 所有父文件夹的JSON响应。
    """
    try:
        file = FileService.get_by_id(db, file_id)
        if not file:
            return get_data_error_result(retmsg="Folder not found!")

        parent_folders = FileService.get_all_parent_folders(db, file_id)
        parent_folders_res = [parent_folder.to_dict() for parent_folder in parent_folders]
        return get_json_result(data={"parent_folders": parent_folders_res})
    except Exception as e:
        return construct_error_response(e)


@router.post("/rm", summary="删除文件或文件夹", response_description="成功删除文件或文件夹", deprecated=True)
def rm(request_body: RemoveRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    删除文件或文件夹。

    参数:
    - request_body: 请求体，包含删除文件或文件夹的信息。

    返回:
    - JSON: 删除结果的JSON响应。
    """
    req = request_body.model_dump()
    file_ids = req["file_ids"]

    def _delete_single_file(file):
        """删除单个文件及其关联的文档"""
        try:
            if file.location:
                settings.STORAGE_IMPL.rm(file.parent_id, file.location)
        except Exception as e:
            logging.exception(f"Fail to remove object: {file.parent_id}/{file.location}, error: {e}")

        # 删除关联的文档
        informs = File2DocumentService.get_by_file_id(db, file.id)
        for inform in informs:
            doc_id = inform.document_id
            doc = DocumentService.get_by_id(db, doc_id)
            if doc:
                tenant_id = DocumentService.get_tenant_id(db, doc_id)
                if tenant_id:
                    DocumentService.remove_document(db, doc, tenant_id)
        FileService.delete(db, file)

    def _delete_folder_recursive(folder, tenant_id):
        """递归删除文件夹及其所有子文件"""
        sub_files = FileService.list_all_files_by_parent_id(db, folder.id)
        for sub_file in sub_files:
            if sub_file.type == FileType.FOLDER.value:
                _delete_folder_recursive(sub_file, tenant_id)
            else:
                _delete_single_file(sub_file)
        FileService.delete(db, folder)

    try:
        for file_id in file_ids:
            file = FileService.get_by_id(db, file_id)
            if not file:
                return get_data_error_result(retmsg="File or folder not found!")
            if not file.tenant_id:
                return get_data_error_result(retmsg="Tenant not found!")
            if not check_file_team_permission(db, file, user.id):
                return get_json_result(data=False, retmsg="No authorization.", retcode=RetCode.AUTHENTICATION_ERROR)

            if file.source_type == FileSource.KNOWLEDGEBASE:
                continue

            if file.type == FileType.FOLDER.value:
                _delete_folder_recursive(file, user.id)
                continue

            _delete_single_file(file)

        return get_json_result(data=True)

    except Exception as e:
        return construct_error_response(e)


@router.post("/rename", summary="重命名文件或文件夹", response_description="成功重命名文件或文件夹", deprecated=True)
def rename(request_body: RenameRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    重命名文件或文件夹。

    参数:
    - request_body: 请求体，包含重命名文件或文件夹的信息。

    返回:
    - JSON: 重命名结果的JSON响应。
    """
    req = request_body.model_dump()
    try:
        file = FileService.get_by_id(db, req["file_id"])
        if not file:
            return get_data_error_result(retmsg="File not found!")
        if not check_file_team_permission(db, file, user.id):
            return get_json_result(data=False, retmsg="No authorization.", retcode=RetCode.AUTHENTICATION_ERROR)
        if file.type != FileType.FOLDER.value and pathlib.Path(req["name"].lower()).suffix != pathlib.Path(file.name.lower()).suffix:
            return get_json_result(data=False, retmsg="The extension of file can't be changed", retcode=RetCode.ARGUMENT_ERROR)
        for f in FileService.query(db, name=req["name"], pf_id=file.parent_id):
            if f.name == req["name"]:
                return get_data_error_result(retmsg="Duplicated file name in the same folder.")

        if not FileService.update_by_id(db, req["file_id"], {"name": req["name"]}):
            return get_data_error_result(retmsg="Database error (File rename)!")

        informs = File2DocumentService.get_by_file_id(db, req["file_id"])
        if informs:
            if not DocumentService.update_by_id(db, informs[0].document_id, {"name": req["name"]}):
                return get_data_error_result(retmsg="Database error (Document rename)!")

        return get_json_result(data=True)
    except Exception as e:
        return construct_error_response(e)


@router.get("/get/{file_id}", summary="获取文件", response_description="成功获取文件", deprecated=True)
def get_file(file_id: str, db: Session = Depends(get_db), user=Depends(manager)):
    """
    获取文件。

    参数:
    - file_id: 文件ID。

    返回:
    - 响应对象: 文件内容的响应。
    """
    try:
        file = FileService.get_by_id(db, file_id)
        if not file:
            return get_data_error_result(retmsg="Document not found!")
        if not check_file_team_permission(db, file, user.id):
            return get_json_result(data=False, retmsg="No authorization.", retcode=RetCode.AUTHENTICATION_ERROR)

        b, n = File2DocumentService.get_storage_address(db, file_id=file_id)
        file_content = settings.STORAGE_IMPL.get(b, n)
        if not file_content:
            raise HTTPException(status_code=404, detail="File not found in storage")

        # 将文件内容包装成 BytesIO 对象
        file_stream = BytesIO(file_content)
        ext = re.search(r"\.([^.]+)$", file.name.lower())
        ext = ext.group(1) if ext else None
        content_type = None
        if ext:
            fallback_prefix = "image" if file.type == FileType.VISUAL.value else "application"
            content_type = CONTENT_TYPE_MAP.get(ext, f"{fallback_prefix}/{ext}")
        encoded_filename = quote(file.name)

        response = StreamingResponse(file_stream, media_type=content_type or "application/octet-stream")
        response.headers["Content-Disposition"] = f"attachment; filename={encoded_filename}"
        apply_safe_file_response_headers(response, content_type, ext)
        return response
    except Exception as e:
        return construct_error_response(e)


@router.post("/mv", summary="移动文件或文件夹", response_description="成功移动文件或文件夹", deprecated=True)
def move(request_body: MoveRequest, db: Session = Depends(get_db), user=Depends(manager)):
    """
    移动文件或文件夹。

    参数:
    - request_body: 请求体，包含移动文件或文件夹的信息。

    返回:
    - JSON: 移动结果的JSON响应。
    """
    req = request_body.model_dump()

    try:
        file_ids = req["src_file_ids"]
        dest_parent_id = req["dest_file_id"]

        # 先检查目标文件夹是否存在
        dest_folder = FileService.get_by_id(db, dest_parent_id)
        if not dest_folder:
            return get_data_error_result(retmsg="Parent folder not found!")

        # 检查源文件是否存在
        files = FileService.get_by_ids(db, file_ids)
        if not files:
            return get_data_error_result(retmsg="Source files not found!")

        # 使用字典推导式简化代码
        files_dict = {f.id: f for f in files}

        # 权限检查
        for file_id in file_ids:
            file = files_dict.get(file_id)
            if not file:
                return get_data_error_result(retmsg="File or folder not found!")
            if not file.tenant_id:
                return get_data_error_result(retmsg="Tenant not found!")
            if not check_file_team_permission(db, file, user.id):
                return get_json_result(data=False, retmsg="No authorization.", retcode=RetCode.AUTHENTICATION_ERROR)

        def _move_entry_recursive(source_file_entry, dest_folder):
            """递归移动文件或文件夹"""
            # 如果是文件夹，递归处理
            if source_file_entry.type == FileType.FOLDER.value:
                # 检查目标位置是否已存在同名文件夹
                existing_folder = FileService.query(db, name=source_file_entry.name, parent_id=dest_folder.id)
                if existing_folder:
                    new_folder = existing_folder[0]
                else:
                    # 在目标位置创建新文件夹
                    new_folder = FileService.insert(
                        db,
                        {
                            "id": get_uuid(),
                            "parent_id": dest_folder.id,
                            "tenant_id": source_file_entry.tenant_id,
                            "created_by": user.id,
                            "name": source_file_entry.name,
                            "location": "",
                            "size": 0,
                            "type": FileType.FOLDER.value,
                        },
                    )

                # 递归移动所有子文件
                sub_files = FileService.list_all_files_by_parent_id(db, source_file_entry.id)
                for sub_file in sub_files:
                    _move_entry_recursive(sub_file, new_folder)

                # 删除源文件夹
                FileService.delete_by_id(db, source_file_entry.id)
                return

            # 处理普通文件
            old_parent_id = source_file_entry.parent_id
            old_location = source_file_entry.location
            filename = source_file_entry.name

            new_location = filename
            # 处理文件名冲突
            while settings.STORAGE_IMPL.obj_exist(dest_folder.id, new_location):
                new_location += "_"

            try:
                # 移动存储层的文件
                settings.STORAGE_IMPL.move(old_parent_id, old_location, dest_folder.id, new_location)
            except Exception as storage_err:
                raise RuntimeError(f"Move file failed at storage layer: {storage_err!s}")

            # 更新数据库记录
            FileService.update_by_id(
                db,
                source_file_entry.id,
                {
                    "parent_id": dest_folder.id,
                    "location": new_location,
                },
            )

        # 移动所有选中的文件/文件夹
        for file in files:
            _move_entry_recursive(file, dest_folder)

        return get_json_result(data=True)

    except Exception as e:
        return construct_error_response(e)
