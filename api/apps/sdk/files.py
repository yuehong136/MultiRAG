import pathlib
import re

from fastapi import APIRouter, Depends, File, Form, Path, Query, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from pathlib import Path

from api.db import FileType
from api.db.db_models import get_db
from api.db.services import duplicate_name
from api.db.services.document_service import DocumentService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from common.misc_utils import get_uuid
from api.utils.api_utils import get_error_data_result, get_result, server_error_response, token_required, \
    get_json_result
from api.utils.file_utils import filename_type
from common import settings
from common.constants import RetCode

router = APIRouter()


class CreateFileRequest(BaseModel):
    name: str
    parent_id: str | None = None
    type: str = "FOLDER"  # FOLDER or VIRTUAL


class DeleteFilesRequest(BaseModel):
    file_ids: list[str]


class RenameFileRequest(BaseModel):
    file_id: str
    name: str


class MoveFilesRequest(BaseModel):
    src_file_ids: list[str]
    dest_file_id: str

@router.post("/files/upload", summary="上传文件")
def upload_files(
    files: list[UploadFile] = File(...),
    parent_id: str | None = Form(None),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    上传文件到系统
    
    Args:
        files: 要上传的文件列表
        parent_id: 父文件夹ID，可选
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        上传成功的文件列表
    """
    pf_id = parent_id

    if not pf_id:
        root_folder = FileService.get_root_folder(db, tenant_id)
        pf_id = root_folder["id"]

    if not files:
        return get_error_data_result(retmsg='No file provided!')
    
    for file_obj in files:
        if not file_obj.filename:
            return get_error_data_result(retmsg='No selected file!')

    file_res = []

    try:
        e, pf_folder = FileService.get_by_id(db, pf_id)
        if not e:
            return get_error_data_result(retmsg="Can't find this folder!")

        for file_obj in files:
            # 文件路径处理
            full_path = '/' + file_obj.filename
            file_obj_names = full_path.split('/')
            file_len = len(file_obj_names)

            # 获取文件夹路径ID
            file_id_list = FileService.get_id_list_by_id(db, pf_id, file_obj_names, 1, [pf_id])
            len_id_list = len(file_id_list)

            # 创建文件夹结构
            if file_len != len_id_list:
                file = FileService.get_by_id(db, file_id_list[len_id_list - 1])
                if not file:
                    return get_error_data_result(retmsg="Folder not found!")
                last_folder = FileService.create_folder(db, file, file_id_list[len_id_list - 1], file_obj_names, len_id_list)
            else:
                file = FileService.get_by_id(db, file_id_list[len_id_list - 2])
                if not file:
                    return get_error_data_result(retmsg="Folder not found!")
                last_folder = FileService.create_folder(db, file, file_id_list[len_id_list - 2], file_obj_names, len_id_list)

            filetype = filename_type(file_obj_names[file_len - 1])
            location = file_obj_names[file_len - 1]
            while settings.STORAGE_IMPL.obj_exist(last_folder.id, location):
                location += "_"
            blob = file_obj.file.read()
            filename = duplicate_name(FileService.query, name=file_obj_names[file_len - 1], parent_id=last_folder.id)

            file_data = {
                "id": get_uuid(),
                "parent_id": last_folder.id,
                "tenant_id": tenant_id,
                "created_by": tenant_id,
                "type": filetype,
                "name": filename,
                "location": location,
                "size": len(blob),
            }
            file = FileService.insert(db, file_data)
            settings.STORAGE_IMPL.put(last_folder.id, location, blob)
            file_res.append(file.to_json())
        return get_result(data=file_res)
    except Exception as e:
        return server_error_response(e)


@router.post("/files", summary="创建文件或文件夹")
def create_file(
    request: CreateFileRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    创建新的文件或文件夹
    
    Args:
        request: 文件创建参数
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        创建的文件信息
    """
    req = request.model_dump()
    pf_id = req.get("parent_id")
    input_file_type = req.get("type")
    
    if not pf_id:
        root_folder = FileService.get_root_folder(tenant_id)
        pf_id = root_folder["id"]

    try:
        if not FileService.is_parent_folder_exist(pf_id):
            return get_error_data_result(retmsg="Parent Folder Doesn't Exist!")
        if FileService.query(name=req["name"], parent_id=pf_id):
            return get_error_data_result(retmsg="Duplicated folder name in the same folder.")

        if input_file_type == FileType.FOLDER.value:
            file_type = FileType.FOLDER.value
        else:
            file_type = FileType.VIRTUAL.value

        file = FileService.insert({
            "id": get_uuid(),
            "parent_id": pf_id,
            "tenant_id": tenant_id,
            "created_by": tenant_id,
            "name": req["name"],
            "location": "",
            "size": 0,
            "type": file_type
        })

        return get_result(data=file.to_json())
    except Exception as e:
        return server_error_response(e)


@router.get("/files", summary="获取文件列表")
def list_files(
    parent_id: str | None = Query(None),
    keywords: str | None = Query(""),
    page: int = Query(1),
    page_size: int = Query(15),
    orderby: str = Query("create_time"),
    desc: bool = Query(True),
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    列出指定文件夹下的文件
    
    Args:
        parent_id: 父文件夹ID
        keywords: 搜索关键词
        page: 页码
        page_size: 每页数量
        orderby: 排序字段
        desc: 是否降序
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        文件列表
    """
    pf_id = parent_id

    if not pf_id:
        root_folder = FileService.get_root_folder(tenant_id)
        pf_id = root_folder["id"]
        FileService.init_knowledgebase_docs(pf_id, tenant_id)

    try:
        e, file = FileService.get_by_id(pf_id)
        if not e:
            return get_error_data_result(retmsg="Folder not found!")

        files, total = FileService.get_by_pf_id(tenant_id, pf_id, page, page_size, orderby, desc, keywords)

        parent_folder = FileService.get_parent_folder(pf_id)
        if not parent_folder:
            return get_error_data_result(retmsg="File not found!")

        return get_result(data={"total": total, "files": files, "parent_folder": parent_folder.to_json()})
    except Exception as e:
        return server_error_response(e)


@router.get("/files/root", summary="获取根文件夹")
def get_root_folder(
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    获取用户的根文件夹
    
    Args:
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        根文件夹信息
    """
    try:
        root_folder = FileService.get_root_folder(tenant_id)
        return get_result(data={"root_folder": root_folder})
    except Exception as e:
        return server_error_response(e)


@router.get("/files/{file_id}/parent", summary="获取文件的父文件夹")
def get_parent_folder(
    file_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    获取文件的父文件夹信息
    
    Args:
        file_id: 文件ID
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        父文件夹信息
    """
    try:
        e, file = FileService.get_by_id(file_id)
        if not e:
            return get_error_data_result(retmsg="Folder not found!")

        parent_folder = FileService.get_parent_folder(file_id)
        return get_result(data={"parent_folder": parent_folder.to_json()})
    except Exception as e:
        return server_error_response(e)


@router.get("/files/{file_id}/parents", summary="获取文件的所有父文件夹")
def get_all_parent_folders(
    file_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    获取文件的所有父文件夹
    
    Args:
        file_id: 文件ID
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        所有父文件夹列表
    """
    try:
        e, file = FileService.get_by_id(file_id)
        if not e:
            return get_error_data_result(retmsg="Folder not found!")

        parent_folders = FileService.get_all_parent_folders(file_id)
        parent_folders_res = [folder.to_json() for folder in parent_folders]
        return get_result(data={"parent_folders": parent_folders_res})
    except Exception as e:
        return server_error_response(e)


@router.delete("/files/delete", summary="删除文件或文件夹")
def delete_files(
    request: DeleteFilesRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    删除一个或多个文件/文件夹
    
    Args:
        request: 删除请求参数
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        删除结果
    """
    req = request.model_dump()
    file_ids = req["file_ids"]
    
    try:
        for file_id in file_ids:
            e, file = FileService.get_by_id(file_id)
            if not e:
                return get_error_data_result(retmsg="File or Folder not found!")
            if not file.tenant_id:
                return get_error_data_result(retmsg="Tenant not found!")

            if file.type == FileType.FOLDER.value:
                file_id_list = FileService.get_all_innermost_file_ids(file_id, [])
                for inner_file_id in file_id_list:
                    e, file = FileService.get_by_id(inner_file_id)
                    if not e:
                        return get_error_data_result(retmsg="File not found!")
                    settings.STORAGE_IMPL.rm(file.parent_id, file.location)
                FileService.delete_folder_by_pf_id(tenant_id, file_id)
            else:
                settings.STORAGE_IMPL.rm(file.parent_id, file.location)
                if not FileService.delete(file):
                    return get_error_data_result(retmsg="Database error (File removal)!")

            informs = File2DocumentService.get_by_file_id(file_id)
            for inform in informs:
                doc_id = inform.document_id
                e, doc = DocumentService.get_by_id(doc_id)
                if not e:
                    return get_error_data_result(retmsg="Document not found!")
                doc_tenant_id = DocumentService.get_tenant_id(doc_id)
                if not doc_tenant_id:
                    return get_error_data_result(retmsg="Tenant not found!")
                if not DocumentService.remove_document(doc, doc_tenant_id):
                    return get_error_data_result(retmsg="Database error (Document removal)!")
            File2DocumentService.delete_by_file_id(file_id)

        return get_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.put("/files/{file_id}/name", summary="重命名文件")
def rename_file(
    file_id: str,
    request: RenameFileRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    重命名文件
    
    Args:
        file_id: 文件ID
        request: 重命名请求参数
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        重命名结果
    """
    req = request.model_dump()
    
    try:
        e, file = FileService.get_by_id(req["file_id"])
        if not e:
            return get_error_data_result(retmsg="File not found!")

        if file.type != FileType.FOLDER.value and pathlib.Path(req["name"].lower()).suffix != pathlib.Path(file.name.lower()).suffix:
            return get_error_data_result(retmsg="The extension of file can't be changed")

        for existing_file in FileService.query(name=req["name"], pf_id=file.parent_id):
            if existing_file.name == req["name"]:
                return get_error_data_result(retmsg="Duplicated file name in the same folder.")

        if not FileService.update_by_id(req["file_id"], {"name": req["name"]}):
            return get_error_data_result(retmsg="Database error (File rename)!")

        informs = File2DocumentService.get_by_file_id(req["file_id"])
        if informs:
            if not DocumentService.update_by_id(informs[0].document_id, {"name": req["name"]}):
                return get_error_data_result(retmsg="Database error (Document rename)!")

        return get_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.get("/files/{file_id}/download", summary="下载文件")
def download_file(
    file_id: str,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    下载文件
    
    Args:
        file_id: 文件ID
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        文件流
    """
    try:
        e, file = FileService.get_by_id(file_id)
        if not e:
            return get_error_data_result(retmsg="Document not found!")

        blob = settings.STORAGE_IMPL.get(file.parent_id, file.location)
        if not blob:
            b, n = File2DocumentService.get_storage_address(file_id=file_id)
            blob = settings.STORAGE_IMPL.get(b, n)

        # 确定文件的MIME类型
        ext = re.search(r"\.([^.]+)$", file.name)
        content_type = "application/octet-stream"  # 默认类型
        
        if ext:
            if file.type == FileType.VISUAL.value:
                content_type = f'image/{ext.group(1)}'
            else:
                content_type = f'application/{ext.group(1)}'

        # 使用StreamingResponse返回文件
        from io import BytesIO
        return StreamingResponse(
            BytesIO(blob),
            media_type=content_type,
            headers={
                "Content-Disposition": f"attachment; filename={file.name}"
            }
        )
    except Exception as e:
        return server_error_response(e)


@router.put("/files/move", summary="移动文件")
def move_files(
    request: MoveFilesRequest,
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    """
    移动一个或多个文件到另一个文件夹
    
    Args:
        request: 移动请求参数
        db: 数据库会话
        tenant_id: 租户ID
    
    Returns:
        移动结果
    """
    req = request.model_dump()
    
    try:
        file_ids = req["src_file_ids"]
        parent_id = req["dest_file_id"]
        files = FileService.get_by_ids(db, file_ids)
        files_dict = {f.id: f for f in files}

        for file_id in file_ids:
            file = files_dict[file_id]
            if not file:
                return get_error_data_result(retmsg="File or Folder not found!")
            if not file.tenant_id:
                return get_error_data_result(retmsg="Tenant not found!")

        fe, _ = FileService.get_by_id(db, parent_id)
        if not fe:
            return get_error_data_result(retmsg="Parent Folder not found!")

        FileService.move_file(db, file_ids, parent_id)
        return get_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post('/file/convert', summary="文件转换")
def convert(
    kb_ids: list[str],
    file_ids: list[str],
    db: Session = Depends(get_db),
    tenant_id: str = Depends(token_required)
):
    file2documents = []

    try:
        files = FileService.get_by_ids(db, file_ids)
        files_set = dict({file.id: file for file in files})
        for file_id in file_ids:
            file = files_set[file_id]
            if not file:
                return get_json_result(retmsg="File not found!", retcode=RetCode.NOT_FOUND)
            file_ids_list = [file_id]
            if file.type == FileType.FOLDER.value:
                file_ids_list = FileService.get_all_innermost_file_ids(db, file_id, [])
            for id in file_ids_list:
                informs = File2DocumentService.get_by_file_id(db, id)
                # delete
                for inform in informs:
                    doc_id = inform.document_id
                    doc = DocumentService.get_by_id(db, doc_id)
                    if not doc:
                        return get_json_result(retmsg="Document not found!", retcode=RetCode.NOT_FOUND)
                    tenant_id = DocumentService.get_tenant_id(db, doc_id)
                    if not tenant_id:
                        return get_json_result(retmsg="Tenant not found!", retcode=RetCode.NOT_FOUND)
                    if not DocumentService.remove_document(db, doc, tenant_id):
                        return get_json_result(retmsg="Database error (Document removal)!", retcode=RetCode.SERVER_ERROR)
                File2DocumentService.delete_by_file_id(db, id)

                # insert
                for kb_id in kb_ids:
                    kb = KnowledgebaseService.get_by_id(db, kb_id)
                    if not kb:
                        return get_json_result(retmsg="Can't find this knowledgebase!", retcode=RetCode.NOT_FOUND)
                    file = FileService.get_by_id(db, id)
                    if not file:
                        return get_json_result(retmsg="Can't find this file!", retcode=RetCode.NOT_FOUND)

                    doc = DocumentService.insert(
                        db,
                        {
                            "id": get_uuid(),
                            "kb_id": kb.id,
                            "parser_id": FileService.get_parser(file.type, file.name, kb.parser_id),
                            "parser_config": kb.parser_config,
                            "created_by": tenant_id,
                            "type": file.type,
                            "name": file.name,
                            "suffix": Path(file.name).suffix.lstrip("."),
                            "location": file.location,
                            "size": file.size
                        }
                    )
                    file2document = File2DocumentService.insert(
                        db, {"id": get_uuid(),"file_id": id,"document_id": doc.id,}
                    )

                    file2documents.append(file2document.to_json())
        return get_json_result(data=file2documents)
    except Exception as e:
        return server_error_response(e)