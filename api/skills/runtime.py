"""Application lifecycle composition for the skill worker."""

import asyncio
from typing import Any

from api.skills.search import SkillSearchRuntime
from api.skills.storage import SkillStorage
from api.skills.worker import SkillWorker

_worker: SkillWorker | None = None
_task: asyncio.Task[None] | None = None
_search: SkillSearchRuntime | None = None


def get_search_runtime() -> SkillSearchRuntime:
    global _search
    if _search is None:
        _search = SkillSearchRuntime()
    return _search


async def cleanup_files(tenant_id: str, ids: list[str]) -> Any:
    from api.apps.services.file_api_service import delete_files_async

    return await delete_files_async(tenant_id, ids, allow_skill_assets=True)


async def start_skill_worker() -> None:
    global _worker, _task
    if _task is not None and not _task.done():
        return
    from api.db.db_models import async_session_factory
    from common.resources import storage

    if async_session_factory is None:
        return
    _worker = SkillWorker(async_session_factory, SkillStorage(storage()), get_search_runtime(), cleanup_files)
    _task = asyncio.create_task(_worker.run(), name="skill-operations")


async def stop_skill_worker() -> None:
    global _worker, _task
    if _worker is not None:
        _worker.stop_event.set()
    if _task is not None:
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
    _worker = None
    _task = None
