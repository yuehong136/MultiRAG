# coding=utf-8
"""
@project: multirag
@Author：龙
@file： file2document_app.py
@date：2025/7/17 9:53
@desc:
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from pathlib import Path

from api.db.db_models import get_db
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import server_error_response, get_data_error_result, get_json_result
from common.misc_utils import get_uuid
from api.db import FileType
from api.db.services.document_service import DocumentService
from common.constants import RetCode
from api.apps import manager

router = APIRouter()

@router.post("/convert", summary="转换文件", response_description="成功转换文件")
def convert(
        kb_ids: list[str],
        file_ids: list[str],
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    转换文件。

    参数:
    - kb_ids: 知识库ID列表。
    - file_ids: 文件ID列表。

    返回:
    - JSON: 文件转换结果的JSON响应。
    """
    file2documents = []

    try:
        files = FileService.get_by_ids(db, file_ids)
        files_set = dict({file.id: file for file in files})
        for file_id in file_ids:
            file = files_set[file_id]
            if not file:
                return get_data_error_result(retmsg="File not found!")
            file_ids_list = [file_id]
            if file.type == FileType.FOLDER.value:
                file_ids_list = FileService.get_all_innermost_file_ids(db, file_id, [])
            for id in file_ids_list:
                informs = File2DocumentService.get_by_file_id(db, id)
                # 删除
                for inform in informs:
                    doc_id = inform.document_id
                    doc = DocumentService.get_by_id(db, doc_id)
                    if not doc:
                        return get_data_error_result(retmsg="Document not found!")
                    tenant_id = DocumentService.get_tenant_id(db, doc_id)
                    if not tenant_id:
                        return get_data_error_result(retmsg="Tenant not found!")
                    if not DocumentService.remove_document(db, doc, tenant_id):
                        return get_data_error_result(
                            retmsg="Database error (Document removal)!")
                File2DocumentService.delete_by_file_id(db, id)

                # 插入
                for kb_id in kb_ids:
                    kb = KnowledgebaseService.get_by_id(db, kb_id)
                    if not kb:
                        return get_data_error_result(
                            retmsg="Can't find this knowledgebase!")
                    file = FileService.get_by_id(db, id)
                    if not file:
                        return get_data_error_result(
                            retmsg="Can't find this file!")

                    doc = DocumentService.insert(db, {
                        "id": get_uuid(),
                        "kb_id": kb.id,
                        "parser_id": kb.parser_id,
                        "pipeline_id": kb.pipeline_id,
                        "parser_config": kb.parser_config,
                        "created_by": user.id,
                        "type": file.type,
                        "name": file.name,
                        "suffix": Path(file.name).suffix.lstrip("."),
                        "location": file.location,
                        "size": file.size
                    })
                    file2document = File2DocumentService.insert(db, {
                        "id": get_uuid(),
                        "file_id": id,
                        "document_id": doc.id,
                    })
                    file2documents.append(file2document.to_dict())
        return get_json_result(data=file2documents)
    except Exception as e:
        return server_error_response(e)


@router.post("/rm", summary="删除文件", response_description="成功删除文件")
def rm(
        file_ids: list[str],
        db: Session = Depends(get_db),
        user=Depends(manager)
):
    """
    删除文件。

    参数:
    - file_ids: 文件ID列表。

    返回:
    - JSON: 文件删除结果的JSON响应。
    """
    if not file_ids:
        return get_json_result(
            data=False, retmsg='Lack of "Files ID"', retcode=RetCode.ARGUMENT_ERROR)
    try:
        for file_id in file_ids:
            informs = File2DocumentService.get_by_file_id(db, file_id)
            if not informs:
                return get_data_error_result(retmsg="Inform not found!")
            for inform in informs:
                if not inform:
                    return get_data_error_result(retmsg="Inform not found!")
                File2DocumentService.delete_by_file_id(db, file_id)
                doc_id = inform.document_id
                doc = DocumentService.get_by_id(db, doc_id)
                if not doc:
                    return get_data_error_result(retmsg="Document not found!")
                tenant_id = DocumentService.get_tenant_id(db, doc_id)
                if not tenant_id:
                    return get_data_error_result(retmsg="Tenant not found!")
                if not DocumentService.remove_document(db, doc, tenant_id):
                    return get_data_error_result(
                        retmsg="Database error (Document removal)!")
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)