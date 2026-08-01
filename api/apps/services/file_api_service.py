"""
@project: multirag
@file: file_api_service.py
@desc: File API 业务逻辑层 - 从 gateway 层解耦的文件管理业务处理。

对标 ragflow `api/apps/services/file_api_service.py`（#13741），但映射到 multirag 的
框架范式：ragflow 用 peewee + Quart async + thread_pool_exec，multirag 用 SQLAlchemy
显式 `db: Session` 穿参（与 dataset_api_service.py 一致）。

约定：所有函数统一返回 (success: bool, result | error_message)：
    - success=True  -> result 为数据载荷（dict / list / File 对象 / bool / None）
    - success=False -> result 为错误信息字符串
  本层不返回 HTTP 响应对象（HTTP 包装交由 restful_apis/file_api.py 网关层完成）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import pathlib
from typing import Any

from sqlalchemy.orm import Session

from api.common.check_team_permission import check_file_team_permission
from api.db import FileType
from api.db.db_models import db_connection
from api.db.services import duplicate_name
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.utils.file_utils import filename_type
from common import settings
from common.constants import FileSource
from common.misc_utils import get_uuid

logger = logging.getLogger(__name__)


def upload_file(db: Session, tenant_id: str, pf_id: str | None, file_contents: list[tuple[bytes, str]]) -> tuple[bool, Any]:
    """上传文件到指定文件夹。

    :param db: 数据库会话
    :param tenant_id: 租户 ID
    :param pf_id: 父文件夹 ID（为空则落到根目录）
    :param file_contents: [(blob, filename), ...]，blob 由网关层异步读出
    :return: (True, [file_dict, ...]) 或 (False, error_message)
    """
    if not pf_id:
        root_folder = FileService.get_root_folder(db, tenant_id)
        pf_id = root_folder["id"]

    pf_folder = FileService.get_by_id(db, pf_id)
    if not pf_folder:
        return False, "Can't find this folder!"

    file_res = []
    for blob, filename in file_contents:
        max_file_num_per_user = int(os.environ.get("MAX_FILE_NUM_PER_USER", 0))
        if 0 < max_file_num_per_user <= DocumentService.get_doc_count(db, tenant_id):
            return False, "Exceed the maximum file number of a free user!"

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
                return False, "Folder not found!"
            last_folder = FileService.create_folder(db, file, file_id_list[len_id_list - 1], file_obj_names, len_id_list)
        else:
            file = FileService.get_by_id(db, file_id_list[len_id_list - 2])
            if not file:
                return False, "Folder not found!"
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
            "tenant_id": tenant_id,
            "created_by": tenant_id,
            "type": filetype,
            "name": final_filename,
            "location": location,
            "size": len(blob),
        }
        inserted = FileService.insert(db, file_data)
        file_res.append(inserted.to_dict())

    return True, file_res


async def upload_file_async(tenant_id: str, pf_id: str | None, file_contents: list[tuple[bytes, str]]) -> tuple[bool, Any]:
    """upload_file 的异步入口：DB 与存储写逐文件交错（obj_exist/put），run_sync 只桥
    session 自身的 IO，故整块进工作线程 + 自开短会话（§11.12 混轨块判例的合法形态）。"""

    def _run() -> tuple[bool, Any]:
        with db_connection() as s:
            return upload_file(s, tenant_id, pf_id, file_contents)

    return await asyncio.to_thread(_run)


def create_folder(db: Session, tenant_id: str, name: str, pf_id: str | None = None, file_type: str | None = None) -> tuple[bool, Any]:
    """创建文件夹或虚拟文件。

    :return: (True, file_dict) 或 (False, error_message)
    """
    if not pf_id:
        root_folder = FileService.get_root_folder(db, tenant_id)
        pf_id = root_folder["id"]

    if not FileService.is_parent_folder_exist(db, pf_id):
        return False, "Parent Folder Doesn't Exist!"
    if FileService.query(db, name=name, parent_id=pf_id):
        return False, "Duplicated folder name in the same folder."

    if (file_type or "").lower() == FileType.FOLDER.value:
        ft = FileType.FOLDER.value
    else:
        ft = FileType.VIRTUAL.value

    file = FileService.insert(
        db,
        {
            "id": get_uuid(),
            "parent_id": pf_id,
            "tenant_id": tenant_id,
            "created_by": tenant_id,
            "name": name,
            "location": "",
            "size": 0,
            "type": ft,
        },
    )
    return True, file.to_dict()


def list_files(db: Session, tenant_id: str, args: dict) -> tuple[bool, Any]:
    """列出文件夹下的文件。

    :param args: parent_id / keywords / page / page_size / orderby / desc
    :return: (True, {total, files, parent_folder}) 或 (False, error_message)
    """
    pf_id = args.get("parent_id")
    keywords = args.get("keywords", "")
    page_number = int(args.get("page", 1))
    items_per_page = int(args.get("page_size", 15))
    orderby = args.get("orderby", "create_time")
    desc = args.get("desc", True)

    if not pf_id:
        root_folder = FileService.get_root_folder(db, tenant_id)
        pf_id = root_folder["id"]
        FileService.init_knowledgebase_docs(db, pf_id, tenant_id)

    file = FileService.get_by_id(db, pf_id)
    if not file:
        return False, "Folder not found!"

    files, total = FileService.get_by_pf_id(db, tenant_id, pf_id, page_number, items_per_page, orderby, desc, keywords)

    parent_folder = FileService.get_parent_folder(db, pf_id)
    if not parent_folder:
        return False, "File not found!"

    return True, {"total": total, "files": files, "parent_folder": parent_folder.to_dict()}


def get_parent_folder(db: Session, file_id: str, user_id: str | None = None) -> tuple[bool, Any]:
    """获取某个文件的父文件夹（带团队权限校验）。"""
    file = FileService.get_by_id(db, file_id)
    if not file:
        return False, "Folder not found!"

    if user_id and not check_file_team_permission(db, file, user_id):
        return False, "No authorization."

    parent_folder = FileService.get_parent_folder(db, file_id)
    return True, {"parent_folder": parent_folder.to_dict()}


def get_all_parent_folders(db: Session, file_id: str, user_id: str | None = None) -> tuple[bool, Any]:
    """获取某个文件的全部祖先文件夹（带团队权限校验）。"""
    file = FileService.get_by_id(db, file_id)
    if not file:
        return False, "Folder not found!"

    if user_id and not check_file_team_permission(db, file, user_id):
        return False, "No authorization."

    parent_folders = FileService.get_all_parent_folders(db, file_id)
    return True, {"parent_folders": [pf.to_dict() for pf in parent_folders]}


def delete_files(db: Session, uid: str, file_ids: list[str]) -> tuple[bool, Any]:
    """删除文件/文件夹（带团队权限校验与递归删除）。

    :return: (True, True) 或 (False, error_message)
    """

    def _delete_single_file(file):
        try:
            if file.location:
                settings.STORAGE_IMPL.rm(file.parent_id, file.location)
        except Exception as e:
            logging.exception(f"Fail to remove object: {file.parent_id}/{file.location}, error: {e}")

        informs = File2DocumentService.get_by_file_id(db, file.id)
        for inform in informs:
            doc_id = inform.document_id
            doc = DocumentService.get_by_id(db, doc_id)
            if doc:
                tenant_id = DocumentService.get_tenant_id(db, doc_id)
                if tenant_id:
                    DocumentService.remove_document(db, doc, tenant_id)
            File2DocumentService.delete_by_file_id(db, file.id)

        FileService.delete(db, file)

    def _delete_folder_recursive(folder, tenant_id):
        sub_files = FileService.list_all_files_by_parent_id(db, folder.id)
        for sub_file in sub_files:
            if sub_file.type == FileType.FOLDER.value:
                _delete_folder_recursive(sub_file, tenant_id)
            else:
                _delete_single_file(sub_file)
        FileService.delete(db, folder)

    for file_id in file_ids:
        file = FileService.get_by_id(db, file_id)
        if not file:
            return False, "File or Folder not found!"
        if not file.tenant_id:
            return False, "Tenant not found!"
        if not check_file_team_permission(db, file, uid):
            return False, "No authorization."

        if file.source_type == FileSource.KNOWLEDGEBASE:
            continue

        if file.type == FileType.FOLDER.value:
            _delete_folder_recursive(file, uid)
            continue

        _delete_single_file(file)

    return True, True


async def delete_files_async(uid: str, file_ids: list[str]) -> tuple[bool, Any]:
    """delete_files 的异步入口：存储 rm 与 remove_document（内混 Redis 取消/存储/doc-store）
    逐文件交错在共享 helper 内，整块进工作线程 + 自开短会话。"""

    def _run() -> tuple[bool, Any]:
        with db_connection() as s:
            return delete_files(s, uid, file_ids)

    return await asyncio.to_thread(_run)


def move_files(db: Session, uid: str, src_file_ids: list[str], dest_file_id: str | None = None, new_name: str | None = None) -> tuple[bool, Any]:
    """移动并/或重命名文件，遵循 Linux mv 语义：
    - 仅 new_name：原地重命名（不动存储）
    - 仅 dest_file_id：移动到新文件夹（保持文件名）
    - 两者都给：同时移动并重命名

    :return: (True, True) 或 (False, error_message)
    """
    files = FileService.get_by_ids(db, src_file_ids)
    if not files:
        return False, "Source files not found!"

    files_dict = {f.id: f for f in files}

    for file_id in src_file_ids:
        file = files_dict.get(file_id)
        if not file:
            return False, "File or folder not found!"
        if not file.tenant_id:
            return False, "Tenant not found!"
        if not check_file_team_permission(db, file, uid):
            return False, "No authorization."

    dest_folder = None
    if dest_file_id:
        dest_folder = FileService.get_by_id(db, dest_file_id)
        if not dest_folder:
            return False, "Parent folder not found!"

    if new_name:
        file = files_dict[src_file_ids[0]]
        if file.type != FileType.FOLDER.value and pathlib.Path(new_name.lower()).suffix != pathlib.Path(file.name.lower()).suffix:
            return False, "The extension of file can't be changed"
        target_parent_id = dest_folder.id if dest_folder else file.parent_id
        for f in FileService.query(db, name=new_name, parent_id=target_parent_id):
            if f.name == new_name:
                return False, "Duplicated file name in the same folder."

    def _move_entry_recursive(source_file_entry, dest_folder_entry, override_name=None):
        effective_name = override_name or source_file_entry.name

        if source_file_entry.type == FileType.FOLDER.value:
            existing_folder = FileService.query(db, name=effective_name, parent_id=dest_folder_entry.id)
            if existing_folder:
                new_folder = existing_folder[0]
            else:
                new_folder = FileService.insert(
                    db,
                    {
                        "id": get_uuid(),
                        "parent_id": dest_folder_entry.id,
                        "tenant_id": source_file_entry.tenant_id,
                        "created_by": source_file_entry.tenant_id,
                        "name": effective_name,
                        "location": "",
                        "size": 0,
                        "type": FileType.FOLDER.value,
                    },
                )

            sub_files = FileService.list_all_files_by_parent_id(db, source_file_entry.id)
            for sub_file in sub_files:
                _move_entry_recursive(sub_file, new_folder)

            FileService.delete_by_id(db, source_file_entry.id)
            return

        # 普通文件
        need_storage_move = dest_folder_entry.id != source_file_entry.parent_id
        updates = {}

        if need_storage_move:
            new_location = effective_name
            while settings.STORAGE_IMPL.obj_exist(dest_folder_entry.id, new_location):
                new_location += "_"
            try:
                settings.STORAGE_IMPL.move(
                    source_file_entry.parent_id,
                    source_file_entry.location,
                    dest_folder_entry.id,
                    new_location,
                )
            except Exception as storage_err:
                raise RuntimeError(f"Move file failed at storage layer: {storage_err!s}")
            updates["parent_id"] = dest_folder_entry.id
            updates["location"] = new_location

        if override_name:
            updates["name"] = override_name

        if updates:
            FileService.update_by_id(db, source_file_entry.id, updates)

        if override_name:
            informs = File2DocumentService.get_by_file_id(db, source_file_entry.id)
            if informs:
                if not DocumentService.update_by_id(db, informs[0].document_id, {"name": override_name}):
                    raise RuntimeError("Database error (Document rename)!")

    if dest_folder:
        for file in files:
            _move_entry_recursive(file, dest_folder, override_name=new_name)
    else:
        # 纯重命名：无需动存储
        file = files[0]
        if not FileService.update_by_id(db, file.id, {"name": new_name}):
            return False, "Database error (File rename)!"
        informs = File2DocumentService.get_by_file_id(db, file.id)
        if informs:
            if not DocumentService.update_by_id(db, informs[0].document_id, {"name": new_name}):
                return False, "Database error (Document rename)!"

    return True, True


async def move_files_async(uid: str, src_file_ids: list[str], dest_file_id: str | None = None, new_name: str | None = None) -> tuple[bool, Any]:
    """move_files 的异步入口：跨文件夹移动时存储 obj_exist/move 与 DB 更新在递归内交错，
    整块进工作线程 + 自开短会话。"""

    def _run() -> tuple[bool, Any]:
        with db_connection() as s:
            return move_files(s, uid, src_file_ids, dest_file_id, new_name)

    return await asyncio.to_thread(_run)


def get_file_content(db: Session, uid: str, file_id: str) -> tuple[bool, Any]:
    """获取文件元信息用于下载（blob 由网关层从存储读取）。

    :return: (True, File 对象) 或 (False, error_message)
    """
    file = FileService.get_by_id(db, file_id)
    if not file:
        return False, "Document not found!"
    if not check_file_team_permission(db, file, uid):
        return False, "No authorization."
    return True, file
