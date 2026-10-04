"""Native FastAPI Skill Assets v1. Package contents are never executed."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.apps.deps import get_storage
from api.db.db_models import Skill, SkillIndexGeneration, SkillVersion, get_async_db
from api.skills.package import MAX_FILE_SIZE, MAX_TOTAL_SIZE, read_archive, validate_package
from api.skills.runtime import get_search_runtime
from api.skills.schemas import ActivateVersion, CreateSpace, DeleteMany, SearchRequest, SkillError, UpdateConfig, UpdateSpace, UploadManifest
from api.skills.search import SkillSearchRuntime
from api.skills.search_types import SkillSearchError
from api.skills.service import SkillService, public
from api.skills.storage import SkillStorage
from api.utils.api_utils import SDKAuthError, async_current_tenant_id


def failure(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"code": status, "message": message, "data": {"error_code": code}}, status_code=status)


class SkillRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Response]]:
        original = super().get_route_handler()

        async def handle(request: Request) -> Response:
            # Bound raw multipart input before Starlette spools file parts to disk.
            if request.method == "POST" and request.url.path.endswith("/versions"):
                receive = request._receive
                received = 0

                async def bounded_receive() -> Any:
                    nonlocal received
                    message = await receive()
                    received += len(message.get("body", b""))
                    if received > MAX_TOTAL_SIZE + 2 * 1024 * 1024:
                        raise SkillError(413, "PACKAGE_TOO_LARGE", "Multipart upload exceeds limit")
                    return message

                request._receive = bounded_receive
            try:
                return await original(request)
            except SkillError as exc:
                return failure(exc.status, exc.code, exc.message)
            except SkillSearchError as exc:
                return failure(503 if exc.retryable or exc.error_code == "SEARCH_BACKEND_UNSUPPORTED" else 422, exc.error_code, exc.message)
            except SDKAuthError:
                return failure(401, "UNAUTHENTICATED", "Valid authentication is required")
            except (RequestValidationError, ValidationError, json.JSONDecodeError):
                return failure(422, "INVALID_REQUEST", "Request fields are invalid")
            except StarletteHTTPException as exc:
                return failure(exc.status_code, "INVALID_REQUEST", "Invalid HTTP request")
            except IntegrityError:
                return failure(409, "RESOURCE_CONFLICT", "Resource name, version or request conflicts with existing state")

        return handle


router = APIRouter(prefix="/skills", route_class=SkillRoute)
Tenant = Annotated[str, Depends(async_current_tenant_id)]
DB = Annotated[AsyncSession, Depends(get_async_db)]
Key = Annotated[str | None, Header(alias="Idempotency-Key", pattern=r"^[!-~]{1,128}$")]


def service(db: DB, storage: Annotated[Any, Depends(get_storage)], search: Annotated[SkillSearchRuntime, Depends(get_search_runtime)]) -> SkillService:
    return SkillService(db, SkillStorage(storage), search)


Service = Annotated[SkillService, Depends(service)]


def success(data: Any, status: int = 200) -> JSONResponse:
    return JSONResponse({"code": 0, "message": "success", "data": data}, status_code=status)


@router.get("/capabilities")
async def capabilities(tenant: Tenant, svc: Service) -> JSONResponse:
    supported = svc.search.supported()
    return success(
        {
            "backend": "python",
            "schema_version": 1,
            "sources": ["local"],
            "search_modes": ["keyword", "vector", "hybrid"] if supported else [],
            "search_available": supported,
            "storage_available": svc.storage.supported(),
        }
    )


@router.get("/models")
async def models(tenant: Tenant, svc: Service) -> JSONResponse:
    return success({"models": await svc.search.models(tenant)})


@router.get("/spaces")
async def list_spaces(tenant: Tenant, svc: Service, page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), keywords: str = "") -> JSONResponse:
    return success(await svc.list_spaces(tenant, page, page_size, keywords))


@router.post("/spaces")
async def create_space(request: CreateSpace, tenant: Tenant, svc: Service, key: Key = None) -> JSONResponse:
    return success(await svc.create_space(tenant, request))


@router.post("/spaces/delete")
async def delete_spaces(request: DeleteMany, tenant: Tenant, svc: Service, key: Key = None) -> JSONResponse:
    return success(await svc.register_batch(tenant, None, request.ids, key), 202)


@router.get("/spaces/{space_id}")
async def get_space(space_id: str, tenant: Tenant, svc: Service) -> JSONResponse:
    return success(public(await svc.space(tenant, space_id)))


@router.patch("/spaces/{space_id}")
async def update_space(space_id: str, request: UpdateSpace, tenant: Tenant, svc: Service, key: Key = None) -> JSONResponse:
    return success(await svc.update_space(tenant, space_id, request))


@router.delete("/spaces/{space_id}")
async def delete_space(space_id: str, tenant: Tenant, svc: Service, key: Key = None) -> JSONResponse:
    return success(await svc.register_action(tenant, space_id, "delete_space", key), 202)


@router.get("/spaces/{space_id}/skills")
async def list_skills(
    space_id: str,
    tenant: Tenant,
    svc: Service,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    keywords: str = "",
    sort: Literal["name", "create_time"] = "name",
    desc: bool = False,
) -> JSONResponse:
    return success(await svc.list_skills(tenant, space_id, page, page_size, keywords, sort, desc))


@router.post("/spaces/{space_id}/skills/delete")
async def delete_skills(space_id: str, request: DeleteMany, tenant: Tenant, svc: Service, key: Key = None) -> JSONResponse:
    return success(await svc.register_batch(tenant, space_id, request.ids, key), 202)


@router.get("/spaces/{space_id}/skills/{skill_id}")
async def get_skill(space_id: str, skill_id: str, tenant: Tenant, svc: Service) -> JSONResponse:
    return success(await svc.get_skill(tenant, space_id, skill_id))


@router.put("/spaces/{space_id}/skills/{skill_id}/active-version")
async def activate_version(space_id: str, skill_id: str, request: ActivateVersion, tenant: Tenant, svc: Service, key: Key = None) -> JSONResponse:
    return success(await svc.register_action(tenant, space_id, "activate", key, skill_id, request), 202)


@router.delete("/spaces/{space_id}/skills/{skill_id}")
async def delete_skill(space_id: str, skill_id: str, tenant: Tenant, svc: Service, key: Key = None) -> JSONResponse:
    return success(await svc.register_action(tenant, space_id, "delete_skill", key, skill_id), 202)


async def bounded_read(upload: UploadFile, maximum: int) -> bytes:
    data = bytearray()
    while chunk := await upload.read(min(65536, maximum + 1 - len(data))):
        data.extend(chunk)
        if len(data) > maximum:
            raise SkillError(413, "PACKAGE_TOO_LARGE", "Upload exceeds byte limits")
    return bytes(data)


@router.post("/spaces/{space_id}/versions")
async def install(space_id: str, request: Request, tenant: Tenant, svc: Service, key: Key = None) -> JSONResponse:
    await svc.space(tenant, space_id, write=True)
    await svc.db.rollback()
    # Starlette spools file parts; application reads enforce actual decoded limits.
    form = await request.form(max_files=1001, max_fields=4, max_part_size=MAX_TOTAL_SIZE)
    try:
        manifest_value = form.get("manifest")
        if not isinstance(manifest_value, str):
            raise SkillError(422, "INVALID_MANIFEST", "A manifest JSON field is required")
        manifest = UploadManifest.model_validate_json(manifest_value)
        archives = form.getlist("archive")
        uploads = form.getlist("file")
        if (bool(archives) == bool(uploads)) or len(archives) > 1:
            raise SkillError(422, "INVALID_UPLOAD", "Choose either directory files or one ZIP archive")
        if archives:
            if not isinstance(archives[0], UploadFile):
                raise SkillError(422, "INVALID_UPLOAD", "Archive must be a file part")
            inputs = await asyncio.to_thread(read_archive, await bounded_read(archives[0], MAX_TOTAL_SIZE))
        else:
            if manifest.files is None or len(manifest.files) != len(uploads):
                raise SkillError(422, "INVALID_MANIFEST", "Directory parts must match the manifest")
            inputs = []
            total = 0
            for entry, upload in zip(manifest.files, uploads, strict=True):
                if not isinstance(upload, UploadFile):
                    raise SkillError(422, "INVALID_UPLOAD", "File parts are required")
                data = await bounded_read(upload, MAX_FILE_SIZE)
                total += len(data)
                if total > MAX_TOTAL_SIZE:
                    raise SkillError(413, "PACKAGE_TOO_LARGE", "Upload exceeds total byte limit")
                inputs.append((entry.path, data))
        package = await asyncio.to_thread(validate_package, manifest, inputs)
        return success(await svc.install(tenant, space_id, package, manifest.activate, key), 202)
    finally:
        await form.close()


@router.delete("/spaces/{space_id}/versions/{version_id}")
async def delete_version(space_id: str, version_id: str, tenant: Tenant, svc: Service, key: Key = None) -> JSONResponse:
    return success(await svc.register_action(tenant, space_id, "delete_version", key, version_id), 202)


@router.get("/spaces/{space_id}/versions/{version_id}/files")
async def list_files(space_id: str, version_id: str, tenant: Tenant, svc: Service) -> JSONResponse:
    return success({"files": await svc.files(tenant, space_id, version_id)})


@router.get("/spaces/{space_id}/versions/{version_id}/file")
async def get_file(space_id: str, version_id: str, tenant: Tenant, svc: Service, path: str) -> Response:
    data, media_type = await svc.content(tenant, space_id, version_id, path)
    return Response(data, media_type=media_type, headers={"X-Content-Type-Options": "nosniff", "Content-Disposition": "attachment", "Cache-Control": "private, no-store"})


@router.get("/spaces/{space_id}/versions/{version_id}/download")
async def download(space_id: str, version_id: str, tenant: Tenant, svc: Service) -> Response:
    data, media_type = await svc.content(tenant, space_id, version_id)
    return Response(data, media_type=media_type, headers={"X-Content-Type-Options": "nosniff", "Content-Disposition": f'attachment; filename="{version_id}.zip"', "Cache-Control": "private, no-store"})


@router.get("/spaces/{space_id}/config")
async def config(space_id: str, tenant: Tenant, svc: Service) -> JSONResponse:
    await svc.space(tenant, space_id)
    return success(public(await svc.config(tenant, space_id)))


@router.patch("/spaces/{space_id}/config")
async def update_config(space_id: str, request: UpdateConfig, tenant: Tenant, svc: Service, key: Key = None) -> JSONResponse:
    return success(await svc.update_config(tenant, space_id, request))


@router.post("/spaces/{space_id}/reindex")
async def reindex(space_id: str, tenant: Tenant, svc: Service, key: Key = None) -> JSONResponse:
    return success(await svc.register_action(tenant, space_id, "reindex", key), 202)


@router.post("/spaces/{space_id}/search")
async def search(space_id: str, request: SearchRequest, tenant: Tenant, svc: Service) -> JSONResponse:
    space = await svc.space(tenant, space_id)
    where = [
        Skill.tenant_id == tenant,
        Skill.space_id == space_id,
        Skill.state == "active",
        SkillVersion.state == "installed",
        SkillVersion.tenant_id == tenant,
        Skill.active_version_id == SkillVersion.id,
    ]
    query = select(Skill, SkillVersion).join(SkillVersion, Skill.active_version_id == SkillVersion.id).where(*where)
    generation_id = space.active_generation_id
    if not request.query.strip():
        total = await svc.db.scalar(select(func.count()).select_from(query.subquery()))
        rows = (await svc.db.execute(query.order_by(Skill.name, Skill.id).offset((request.page - 1) * request.page_size).limit(request.page_size))).all()
        skills = [
            {"skill_id": skill.id, "version_id": version.id, "name": skill.name, "description": skill.description, "tags": skill.tags, "version": version.version, "score": 0.0}
            for skill, version in rows
        ]
        return success({"skills": skills, "total": total, "total_relation": "eq", "mode": request.mode, "generation_id": generation_id})
    generation = await svc.db.get(SkillIndexGeneration, generation_id) if generation_id else None
    if generation is None or generation.state != "active":
        raise SkillError(503, "INDEX_NOT_READY", "No active searchable index is available")
    snapshot = dict(generation.config)
    index_name = generation.index_name
    await svc.db.rollback()
    # A fixed candidate pool keeps reranked pages stable across page requests.
    hits = await svc.search.query(index_name, tenant, snapshot, request.query, request.mode, int(snapshot["top_k"]))
    # Re-authorize after slow index I/O. A concurrent deletion can never leak an asset.
    await svc.space(tenant, space_id)
    current = (await svc.db.execute(query.where(SkillVersion.id.in_([hit.version_id for hit in hits])))).all()
    allowed = {(version.id, skill.id): (skill, version) for skill, version in current}
    result = []
    for hit in hits:
        entry = allowed.get((hit.version_id, hit.skill_id))
        if entry:
            skill, version = entry
            result.append({"skill_id": skill.id, "version_id": version.id, "name": skill.name, "description": skill.description, "tags": skill.tags, "version": version.version, "score": hit.score})
    total = len(result)
    start = (request.page - 1) * request.page_size
    return success(
        {
            "skills": result[start : start + request.page_size],
            "total": total,
            "total_relation": "gte" if getattr(hits, "truncated", False) else "eq",
            "mode": request.mode,
            "generation_id": generation_id,
        }
    )


@router.get("/operations/{operation_id}")
async def operation(operation_id: str, tenant: Tenant, svc: Service) -> JSONResponse:
    return success(public(await svc.operation(tenant, operation_id)))


@router.post("/operations/{operation_id}/retry")
async def retry(operation_id: str, tenant: Tenant, svc: Service, key: Key = None) -> JSONResponse:
    from api.skills.schemas import idempotency_key

    idempotency_key(key)
    return success(await svc.retry(tenant, operation_id, key), 202)
