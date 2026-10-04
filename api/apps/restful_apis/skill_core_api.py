"""Python-native RAGFlow Skills capability protocol, with explicit version alias."""

import logging
from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select

from api.apps.deps import get_storage
from api.apps.restful_apis.skill_api import DB, SkillRoute, Tenant, failure, success
from api.db.db_models import PythonSkillCoreSpace
from api.skills.core_runtime import get_core_search
from api.skills.core_schemas import CoreConfigRequest, CoreCreate, CoreIndexRequest, CoreReindexRequest, CoreSearchRequest, CoreUpdate
from api.skills.core_service import SkillCoreService
from api.skills.protocol import ASSETS_PROTOCOL, CORE_PROTOCOL, PROTOCOL
from api.skills.schemas import SkillError
from api.skills.search import SkillSearchRuntime
from api.skills.storage import SkillStorage


class CoreRoute(SkillRoute):
    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def safe_handle(request: Request) -> Response:
            try:
                return await handler(request)
            except Exception as exc:
                logging.getLogger(__name__).warning("Python core request failed: %s", type(exc).__name__)
                response = failure(503, "SERVICE_UNAVAILABLE", "Skill service is temporarily unavailable")
                response.headers["X-Skills-Protocol"] = CORE_PROTOCOL
                return response

        return safe_handle


core_router = APIRouter(route_class=CoreRoute)


def service(db: DB, storage: Annotated[Any, Depends(get_storage)], search: Annotated[SkillSearchRuntime, Depends(get_core_search)]) -> SkillCoreService:
    return SkillCoreService(db, SkillStorage(storage), search)


Service = Annotated[SkillCoreService, Depends(service)]


@core_router.get("/models")
async def models(tenant: Tenant, svc: Service) -> JSONResponse:
    return success({"models": await svc.search.models(tenant)})


@core_router.get("/spaces")
async def spaces(tenant: Tenant, svc: Service) -> JSONResponse:
    rows = list(
        await svc.db.scalars(select(PythonSkillCoreSpace).where(PythonSkillCoreSpace.tenant_id == tenant, PythonSkillCoreSpace.state == "active").order_by(PythonSkillCoreSpace.create_time.desc()))
    )
    return success({"spaces": [await svc.space_dto(row) for row in rows], "total": len(rows)})


@core_router.post("/spaces")
async def create(request: CoreCreate, tenant: Tenant, svc: Service) -> JSONResponse:
    return success(await svc.create(tenant, request))


@core_router.get("/spaces/{space_id}")
async def get(space_id: str, tenant: Tenant, svc: Service) -> JSONResponse:
    return success(await svc.space_dto(await svc.space(tenant, space_id, active=False)))


@core_router.put("/spaces/{space_id}")
async def update(space_id: str, request: CoreUpdate, tenant: Tenant, svc: Service) -> JSONResponse:
    return success(await svc.update(tenant, space_id, request))


@core_router.delete("/spaces/{space_id}")
async def delete(space_id: str, tenant: Tenant, svc: Service) -> JSONResponse:
    space = await svc.space(tenant, space_id, active=False)
    await svc.plan_delete(tenant, space_id, space.folder_id, entire=True)
    return success({"deleting": True, "space_id": space_id}, 202)


@core_router.get("/space/by-folder")
async def by_folder(folder_id: str, tenant: Tenant, svc: Service) -> JSONResponse:
    row = await svc.db.scalar(select(PythonSkillCoreSpace).where(PythonSkillCoreSpace.tenant_id == tenant, PythonSkillCoreSpace.folder_id == folder_id, PythonSkillCoreSpace.state != "deleted"))
    if row is None:
        raise SkillError(404, "NOT_FOUND", "Skill space not found")
    return success(await svc.space_dto(row))


@core_router.get("/config")
async def config(tenant: Tenant, svc: Service, space_id: str = "") -> JSONResponse:
    return success(await svc.get_config(tenant, space_id))


@core_router.post("/config")
async def configure(request: CoreConfigRequest, tenant: Tenant, svc: Service) -> JSONResponse:
    return success(await svc.update_config(tenant, request))


@core_router.post("/search")
async def search(request: CoreSearchRequest, tenant: Tenant, svc: Service) -> JSONResponse:
    return success(await svc.search_skills(tenant, request))


@core_router.post("/index")
async def index(request: CoreIndexRequest, tenant: Tenant, svc: Service) -> JSONResponse:
    return success(await svc.index(tenant, request))


@core_router.delete("/index")
async def remove_index(skill_id: str, tenant: Tenant, svc: Service, space_id: str = "") -> JSONResponse:
    await svc.remove_index(tenant, space_id, skill_id)
    return success(True)


@core_router.post("/reindex")
async def reindex(request: CoreReindexRequest, tenant: Tenant, svc: Service) -> JSONResponse:
    return success(await svc.index(tenant, CoreIndexRequest(space_id=request.space_id, embd_id=request.embd_id, skills=[]), rebuild=True))


router = APIRouter()
router.include_router(core_router, prefix="/skill-core")
if PROTOCOL == CORE_PROTOCOL:
    router.include_router(core_router, prefix="/skills")


@router.get("/skill-protocols")
async def protocols(tenant: Tenant) -> JSONResponse:
    return success(
        {
            "default_protocol": PROTOCOL,
            "backend": "python",
            "protocols": [
                {"protocol": CORE_PROTOCOL, "base_path": "/api/v1/skill-core", "writable": True, "capabilities": {"rerank": False, "operations": False, "writable": True}},
                {"protocol": ASSETS_PROTOCOL, "base_path": "/api/v1/skill-assets", "writable": True, "capabilities": {"rerank": True, "operations": True, "writable": True}},
            ],
        }
    )
