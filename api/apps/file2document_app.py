from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session

from api.apps import manager
from api.apps.services.file_convert_service import convert_files_with_new_session
from api.db import FileType
from api.db.db_models import get_db
from api.db.services.document_service import DocumentService
from api.db.services.file2document_service import File2DocumentService
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import get_data_error_result, get_json_result, server_error_response
from common.constants import RetCode

router = APIRouter()


@router.post("/convert", summary="转换文件", response_description="成功转换文件")
def convert(kb_ids: list[str], file_ids: list[str], background_tasks: BackgroundTasks, db: Session = Depends(get_db), user=Depends(manager)):
    """
    转换文件。

    参数:
    - kb_ids: 知识库ID列表。
    - file_ids: 文件ID列表。

    返回:
    - JSON: 文件转换结果的JSON响应。
    """
    try:
        files = FileService.get_by_ids(db, file_ids)
        files_set = {file.id: file for file in files}
        for file_id in file_ids:
            file = files_set.get(file_id)
            if not file:
                return get_data_error_result(retmsg="File not found!")

        for kb_id in kb_ids:
            kb = KnowledgebaseService.get_by_id(db, kb_id)
            if not kb:
                return get_data_error_result(retmsg="Can't find this dataset!")

        all_file_ids = []
        for file_id in file_ids:
            file = files_set[file_id]
            if file.type == FileType.FOLDER.value:
                all_file_ids.extend(FileService.get_all_innermost_file_ids(db, file_id, []))
            else:
                all_file_ids.append(file_id)

        background_tasks.add_task(convert_files_with_new_session, all_file_ids, kb_ids, user.id)
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)


@router.post("/rm", summary="删除文件", response_description="成功删除文件")
def rm(file_ids: list[str], db: Session = Depends(get_db), user=Depends(manager)):
    """
    删除文件。

    参数:
    - file_ids: 文件ID列表。

    返回:
    - JSON: 文件删除结果的JSON响应。
    """
    if not file_ids:
        return get_json_result(data=False, retmsg='Lack of "Files ID"', retcode=RetCode.ARGUMENT_ERROR)
    try:
        for file_id in file_ids:
            informs = File2DocumentService.get_by_file_id(db, file_id)
            if not informs:
                return get_data_error_result(retmsg="Inform not found!")
            for inform in informs:
                if not inform:
                    return get_data_error_result(retmsg="Inform not found!")
                doc_id = inform.document_id
                doc = DocumentService.get_by_id(db, doc_id)
                if not doc:
                    return get_data_error_result(retmsg="Document not found!")
                tenant_id = DocumentService.get_tenant_id(db, doc_id)
                if not tenant_id:
                    return get_data_error_result(retmsg="Tenant not found!")
                if not DocumentService.remove_document(db, doc, tenant_id):
                    return get_data_error_result(retmsg="Database error (Document removal)!")
        return get_json_result(data=True)
    except Exception as e:
        return server_error_response(e)
