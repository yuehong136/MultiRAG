"""RESTful endpoint for linking managed files to datasets."""

from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from api.apps.services.file_convert_service import convert_files_with_new_session
from api.common.check_team_permission import check_file_team_permission, check_kb_team_permission
from api.db import FileType
from api.db.db_models import get_async_db
from api.db.services.file_service import FileService
from api.db.services.knowledgebase_service import KnowledgebaseService
from api.utils.api_utils import async_current_tenant_id, get_error_data_result, get_json_result, server_error_response
from common.constants import RetCode

router = APIRouter()


@router.post("/files/link-to-datasets", summary="关联文件到知识库")
async def link_to_datasets(
    kb_ids: list[str],
    file_ids: list[str],
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_async_db),
    tenant_id: str = Depends(async_current_tenant_id),
):
    """把文件（含文件夹内最内层文件）转换/关联为指定知识库的文档。"""
    try:

        def _collect(session: Session) -> tuple[list[str] | None, JSONResponse | None]:
            files = FileService.get_by_ids(session, file_ids)
            files_by_id = {file.id: file for file in files}
            for file_id in file_ids:
                if file_id not in files_by_id:
                    return None, get_json_result(retmsg="File not found!", retcode=RetCode.NOT_FOUND)

            knowledgebases = {}
            for kb_id in kb_ids:
                knowledgebase = KnowledgebaseService.get_by_id(session, kb_id)
                if not knowledgebase:
                    return None, get_json_result(retmsg="Can't find this dataset!", retcode=RetCode.NOT_FOUND)
                knowledgebases[kb_id] = knowledgebase

            expanded_file_ids: list[str] = []
            for file_id in file_ids:
                file = files_by_id[file_id]
                if file.type == FileType.FOLDER.value:
                    expanded_file_ids.extend(FileService.get_all_innermost_file_ids(session, file_id, []))
                else:
                    expanded_file_ids.append(file_id)

            expanded_files = FileService.get_by_ids(session, expanded_file_ids)
            expanded_files_by_id = {file.id: file for file in expanded_files}
            for file_id in expanded_file_ids:
                file = expanded_files_by_id.get(file_id)
                if not file:
                    return None, get_json_result(retmsg="File not found!", retcode=RetCode.NOT_FOUND)
                if not check_file_team_permission(session, file, tenant_id):
                    return None, get_error_data_result(retmsg="No authorization.")
            for knowledgebase in knowledgebases.values():
                if not check_kb_team_permission(session, knowledgebase, tenant_id):
                    return None, get_error_data_result(retmsg="No authorization.")
            return expanded_file_ids, None

        expanded_file_ids, error = await db.run_sync(_collect)  # TODO(async-phase4)
        if error is not None:
            return error

        background_tasks.add_task(convert_files_with_new_session, expanded_file_ids, kb_ids, tenant_id)
        return get_json_result(data=True)
    except Exception as exc:
        return server_error_response(exc)
