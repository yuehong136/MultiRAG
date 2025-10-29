# coding=utf-8
"""
@project: multirag
@Author：龙
@file： file_app.py
@date：2025/7/17 13:50
@desc:
"""
import os
import pathlib
import re
from io import BytesIO
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from sqlalchemy.orm import Session

from starlette.responses import StreamingResponse

from api.common.check_team_permission import check_file_team_permission
from api.db import FileType, FileSource
from api.db.db_models import get_db
from api.db.services import duplicate_name
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api import settings
from api.utils.api_utils import get_json_result, construct_error_response, get_data_error_result
from api.utils import get_uuid
from api.utils.file_utils import filename_type
from api.utils.web_utils import CONTENT_TYPE_MAP
from core.utils.storage_factory import STORAGE_IMPL
from api.apps import manager
from pydantic import BaseModel, Field

router = APIRouter()


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


@router.post("/upload", summary="上传文件", response_description="成功上传文件")
async def upload(
        parent_id: str,
        files: list[UploadFile] = File(...),
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    上传文件。

    参数:
    - parent_id: 父文件夹ID。
    - files: 上传的文件列表。

    返回:
    - JSON: 上传文件结果的JSON响应。
    """

    pf_id = parent_id

    if not pf_id:
        root_folder = FileService.get_root_folder(db, user.id)
        pf_id = root_folder["id"]

    if not files:
        return get_json_result(data=False, retmsg='No file part!', retcode=settings.RetCode.ARGUMENT_ERROR)

    for file_obj in files:
        if file_obj.filename == '':
            return get_json_result(data=False, retmsg='No file selected!', retcode=settings.RetCode.ARGUMENT_ERROR)

    try:
        pf_folder = FileService.get_by_id(db, pf_id)
        if not pf_folder:
            return get_data_error_result(retmsg="Can't find this folder!")
        for file_obj in files:
            MAX_FILE_NUM_PER_USER = int(os.environ.get('MAX_FILE_NUM_PER_USER', 0))
            if 0 < MAX_FILE_NUM_PER_USER <= DocumentService.get_doc_count(db, user.id):
                return get_data_error_result(retmsg="Exceed the maximum file number of a free user!")

            if not file_obj.filename:
                file_obj_names = [pf_folder.name, file_obj.filename]
            else:
                full_path = '/' + file_obj.filename
                file_obj_names = full_path.split('/')
            file_len = len(file_obj_names)

            file_id_list = FileService.get_id_list_by_id(db, pf_id, file_obj_names, 1, [pf_id])
            len_id_list = len(file_id_list)

            if file_len != len_id_list:
                file = FileService.get_by_id(db, file_id_list[len_id_list - 1])
                if not file:
                    return get_data_error_result(retmsg="Folder not found!")
                last_folder = FileService.create_folder(db, file, file_id_list[len_id_list - 1], file_obj_names,
                                                        len_id_list)
            else:
                file = FileService.get_by_id(db, file_id_list[len_id_list - 2])
                if not file:
                    return get_data_error_result(retmsg="Folder not found!")
                last_folder = FileService.create_folder(db, file, file_id_list[len_id_list - 2], file_obj_names,
                                                        len_id_list)

            filetype = filename_type(file_obj_names[file_len - 1])
            location = file_obj_names[file_len - 1]
            while STORAGE_IMPL.obj_exist(last_folder.id, location):
                location += "_"
            blob = await file_obj.read()
            filename = duplicate_name(FileService.query, db=db, name=file_obj_names[file_len - 1],
                                      parent_id=last_folder.id)
            STORAGE_IMPL.put(last_folder.id, location, blob)
            file_data = {
                "id": get_uuid(),
                "parent_id": last_folder.id,
                "tenant_id": user.id,
                "created_by": user.id,
                "type": filetype,
                "name": filename,
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
                "type": file.type
            }
        return get_json_result(data=file_dict)
    except Exception as e:
        return construct_error_response(e)


@router.post("/create", summary="创建文件或文件夹", response_description="成功创建文件或文件夹")
async def create(
        request_body: CreateRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
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
            return get_json_result(data=False, retmsg="Parent Folder Doesn't Exist!", retcode=settings.RetCode.OPERATING_ERROR)
        if FileService.query(db, name=req["name"], parent_id=pf_id):
            return get_data_error_result(retmsg="Duplicated folder name in the same folder.")

        if input_file_type == FileType.FOLDER.value:
            file_type = FileType.FOLDER.value
        else:
            file_type = FileType.VIRTUAL.value

        file = FileService.insert(db, {
            "id": get_uuid(),
            "parent_id": pf_id,
            "tenant_id": user.id,
            "created_by": user.id,
            "name": req["name"],
            "location": "",
            "size": 0,
            "type": file_type
        })
        # 手动转换对象为字典格式
        file_dict = {
            "id": file.id,
            "parent_id": file.parent_id,
            "tenant_id": file.tenant_id,
            "created_by": file.created_by,
            "name": file.name,
            "location": file.location,
            "size": file.size,
            "type": file.type
        }

        return get_json_result(data=file_dict)
    except Exception as e:
        return construct_error_response(e)


@router.get("/list", summary="列出文件或文件夹", response_description="成功列出文件或文件夹")
async def list_files(
        parent_id: str | None = None,
        keywords: str = "",
        page: int = 1,
        page_size: int = 15,
        orderby: str = "create_time",
        desc: bool = True,
        db: Session = Depends(get_db),
        user=Depends(manager)
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

        if not check_file_team_permission(db, file, user.id):
            return get_json_result(data=False, retmsg='No authorization.', retcode=settings.RetCode.AUTHENTICATION_ERROR)

        files, total = FileService.get_by_pf_id(db, user.id, parent_id, page, page_size, orderby, desc, keywords)

        parent_folder = FileService.get_parent_folder(db, parent_id)
        if not parent_folder:
            return get_json_result(retmsg="File not found!")

        return get_json_result(data={"total": total, "files": files, "parent_folder": parent_folder.to_dict()})
    except Exception as e:
        return construct_error_response(e)


@router.get("/root_folder", summary="获取根文件夹", response_description="成功获取根文件夹")
async def get_root_folder(
        db: Session = Depends(get_db),
        user=Depends(manager)
):
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


@router.get("/parent_folder", summary="获取父文件夹", response_description="成功获取父文件夹")
async def get_parent_folder(
        file_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
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

        if not check_file_team_permission(db, file, user.id):
            return get_json_result(data=False, retmsg='No authorization.', retcode=settings.RetCode.AUTHENTICATION_ERROR)

        parent_folder = FileService.get_parent_folder(db, file_id)
        return get_json_result(data={"parent_folder": parent_folder.to_dict()})
    except Exception as e:
        return construct_error_response(e)


@router.get("/all_parent_folder", summary="获取所有父文件夹", response_description="成功获取所有父文件夹")
async def get_all_parent_folders(
        file_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
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

        if not check_file_team_permission(db, file, user.id):
            return get_json_result(data=False, retmsg='No authorization.', retcode=settings.RetCode.AUTHENTICATION_ERROR)

        parent_folders = FileService.get_all_parent_folders(db, file_id)
        parent_folders_res = [parent_folder.to_dict() for parent_folder in parent_folders]
        return get_json_result(data={"parent_folders": parent_folders_res})
    except Exception as e:
        return construct_error_response(e)


@router.post("/rm", summary="删除文件或文件夹", response_description="成功删除文件或文件夹")
async def rm(
        request_body: RemoveRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    删除文件或文件夹。

    参数:
    - request_body: 请求体，包含删除文件或文件夹的信息。

    返回:
    - JSON: 删除结果的JSON响应。
    """
    req = request_body.model_dump()
    file_ids = req["file_ids"]
    try:
        for file_id in file_ids:
            file = FileService.get_by_id(db, file_id)
            if not file:
                return get_data_error_result(retmsg="File or Folder not found!")
            if not file.tenant_id:
                return get_data_error_result(retmsg="Tenant not found!")
            if not check_file_team_permission(db, file, user.id):
                return get_json_result(data=False, retmsg='No authorization.', retcode=settings.RetCode.AUTHENTICATION_ERROR)
            if file.source_type == FileSource.KNOWLEDGEBASE:
                continue

            if file.type == FileType.FOLDER.value:
                file_id_list = FileService.get_all_innermost_file_ids(db, file_id, [])
                for inner_file_id in file_id_list:
                    file = FileService.get_by_id(db, inner_file_id)
                    if not file:
                        return get_data_error_result(retmsg="File not found!")
                    STORAGE_IMPL.rm(file.parent_id, file.location)
                FileService.delete_folder_by_pf_id(db, user.id, file_id)
            else:
                STORAGE_IMPL.rm(file.parent_id, file.location)
                if not FileService.delete(db, file):
                    return get_data_error_result(retmsg="Database error (File removal)!")

            # delete file2document
            informs = File2DocumentService.get_by_file_id(db, file_id)
            for inform in informs:
                doc_id = inform.document_id
                doc = DocumentService.get_by_id(db, doc_id)
                if not doc:
                    return get_data_error_result(retmsg="Document not found!")
                tenant_id = DocumentService.get_tenant_id(db, doc_id)
                if not tenant_id:
                    return get_data_error_result(retmsg="Tenant not found!")
                if not DocumentService.remove_document(db, doc, tenant_id):
                    return get_data_error_result(retmsg="Database error (Document removal)!")
            File2DocumentService.delete_by_file_id(db, file_id)

        return get_json_result(data=True)
    except Exception as e:
        return construct_error_response(e)


@router.post("/rename", summary="重命名文件或文件夹", response_description="成功重命名文件或文件夹")
async def rename(
        request_body: RenameRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
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
            return get_json_result(data=False, retmsg='No authorization.', retcode=settings.RetCode.AUTHENTICATION_ERROR)
        if file.type != FileType.FOLDER.value \
                and pathlib.Path(req["name"].lower()).suffix != pathlib.Path(file.name.lower()).suffix:
            return get_json_result(data=False, retmsg="The extension of file can't be changed",
                                         retcode=settings.RetCode.ARGUMENT_ERROR)
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


@router.get("/get/{file_id}", summary="获取文件", response_description="成功获取文件")
async def get_file(
        file_id: str,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
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
            return get_json_result(data=False, retmsg='No authorization.', retcode=settings.RetCode.AUTHENTICATION_ERROR)

        b, n = File2DocumentService.get_storage_address(db, file_id=file_id)
        file_content = STORAGE_IMPL.get(b, n)
        if not file_content:
            raise HTTPException(status_code=404, detail="File not found in storage")

        # 将文件内容包装成 BytesIO 对象
        file_stream = BytesIO(file_content)
        ext = re.search(r"\.([^.]+)$", file.name.lower())
        ext = ext.group(1) if ext else None
        media_type = "application/octet-stream"
        if ext:
            if file.type == FileType.VISUAL.value:
                media_type = CONTENT_TYPE_MAP.get(ext, f"image/{ext}")
            else:
                media_type = CONTENT_TYPE_MAP.get(ext, f"application/{ext}")
        encoded_filename = quote(file.name)

        response = StreamingResponse(file_stream, media_type=media_type)
        response.headers["Content-Disposition"] = f"attachment; filename={encoded_filename}"
        return response
    except Exception as e:
        return construct_error_response(e)


@router.post("/mv", summary="移动文件或文件夹", response_description="成功移动文件或文件夹")
async def move(
        request_body: MoveRequest,
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    移动文件或文件夹。

    参数:
    - request_body: 请求体，包含移动文件或文件夹的信息。

    返回:
    - JSON: 移动结果的JSON响应。
    """
    req = request_body.dict()
    try:
        file_ids = req["src_file_ids"]
        parent_id = req["dest_file_id"]

        files = FileService.get_by_ids(db, file_ids)
        files_dict = {}
        for file in files:
            files_dict[file.id] = file

        for file_id in file_ids:
            file = files_dict[file_id]
            if not file:
                return get_data_error_result(retmsg="File or Folder not found!")
            if not file.tenant_id:
                return get_data_error_result(retmsg="Tenant not found!")
            if not check_file_team_permission(db, file, user.id):
                return get_json_result(data=False, retmsg='No authorization.', retcode=settings.RetCode.AUTHENTICATION_ERROR)
        fe = FileService.get_by_id(db, parent_id)
        if not fe:
            return get_data_error_result(retmsg="Parent Folder not found!")
        FileService.move_file(db, file_ids, parent_id)
        return get_json_result(data=True)
    except Exception as e:
        return construct_error_response(e)
