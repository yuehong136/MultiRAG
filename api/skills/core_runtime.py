"""Independent Python core index namespace and durable cleanup lifecycle."""

import asyncio
import logging
import os
import re
import time
import traceback
from typing import Any

from sqlalchemy import select

from api.skills.core_service import SkillCoreService
from api.skills.milvus_store import MilvusSkillStore
from api.skills.search import SkillSearchRuntime
from api.skills.search_types import SkillSearchError
from api.skills.storage import SkillStorage

logger = logging.getLogger(__name__)


class PythonCoreStore(MilvusSkillStore):
    @staticmethod
    def _name(name: str) -> str:
        if not re.fullmatch(r"skill_pycore_[0-9a-f]{32}", name):
            raise SkillSearchError("INVALID_INDEX_NAME", "Invalid Python core index name", False)
        return name


class PythonCoreSearch(SkillSearchRuntime):
    def keyword_part(self, raw_score: float, weight: float, total_weight: float) -> float:
        # Core field boosts apply before normalization. Empty optional fields
        # must not dilute an exact name match below the default threshold.
        return weight * max(0.0, raw_score)

    def keyword_total(self, score: float) -> float:
        return score / (1 + score)

    def supported(self) -> bool:
        return os.environ.get("DOC_ENGINE", "milvus").strip().lower() == "milvus"


_search: SkillSearchRuntime | None = None
_task: asyncio.Task[None] | None = None


def get_core_search() -> SkillSearchRuntime:
    global _search
    if _search is None:
        _search = PythonCoreSearch(PythonCoreStore())
    return _search


async def sweep(sessions: Any, storage: SkillStorage, search: SkillSearchRuntime) -> None:
    from api.db.db_models import PythonSkillCoreSpace

    async with sessions() as db:
        identities = list((await db.execute(select(PythonSkillCoreSpace.tenant_id, PythonSkillCoreSpace.id))).all())
    for tenant, identity in identities:
        try:
            async with sessions() as db:
                svc = SkillCoreService(db, storage, search)
                space = await db.get(PythonSkillCoreSpace, identity)
                if space is None:
                    continue
                config = await svc.config_row(tenant, identity)
                retired = [item["name"] for item in config.index_data.get("retired", []) if item["after"] <= time.time()]
                # An abandoned upload is hidden until strict cleanup succeeds.
                upload = space.cleanup.get("uploads")
                if upload and upload["deadline"] <= time.time():
                    cleanup = dict(space.cleanup)
                    cleanup.pop("uploads")
                    cleanup["retired_objects"] = [*cleanup.get("retired_objects", []), *upload["objects"]]
                    space.revision += 1
                    cleanup["files"] = [*cleanup.get("files", []), {"root": upload["ids"][0], "ids": upload["ids"], "objects": upload["objects"]}]
                    space.cleanup = cleanup
                    await db.commit()
                needs_cleanup = bool(space.cleanup.get("files") or space.cleanup.get("indices")) or space.state in ("deleting", "delete_failed")
                needs_cleanup = needs_cleanup and space.cleanup.get("lease_until", 0) <= time.time() and space.cleanup.get("not_before", 0) <= time.time()
                retired_objects = list(space.cleanup.get("retired_objects", []))
                deleted = space.state == "deleted"
                await db.rollback()
                if needs_cleanup and not deleted:
                    await svc.clean(tenant, identity)
                for name in retired:
                    await search.delete(name)
                for bucket, key in retired_objects:
                    await storage.remove(bucket, key)
        except Exception as exc:
            frame = traceback.extract_tb(exc.__traceback__)[-1]
            logger.warning("Python Skills cleanup remains pending: %s at %s:%s", type(exc).__name__, frame.name, frame.lineno)


async def run(sessions: Any, storage: SkillStorage, search: SkillSearchRuntime) -> None:
    while True:
        await sweep(sessions, storage, search)
        await asyncio.sleep(2)


async def start_core_worker() -> None:
    global _task
    from api.db.db_models import async_session_factory
    from common.resources import storage

    if async_session_factory is not None and (_task is None or _task.done()):
        _task = asyncio.create_task(run(async_session_factory, SkillStorage(storage()), get_core_search()), name="python-skill-core-cleanup")


async def stop_core_worker() -> None:
    global _task
    if _task is not None:
        _task.cancel()
        await asyncio.gather(_task, return_exceptions=True)
    _task = None
