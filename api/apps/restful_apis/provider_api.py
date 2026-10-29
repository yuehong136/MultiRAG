"""Provider/instance management with tenant ownership and atomic writes."""

import re
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db.db_models import TenantModelInstance, TenantModelProvider, get_async_db
from api.db.model_contract import ConnectionCandidate, DefaultModelInput, DiscoveryInput, InstanceInput, InstancePatch, ModelRegistration, ProviderInput, StatusPatch, parse_model_id
from api.db.provider_catalog import provider_order, public_factories
from api.db.services import model_provider_service as service
from api.utils.api_utils import Principal, async_current_user


class ModelRoute(APIRoute):
    def get_route_handler(self) -> Callable[[Request], Awaitable[Any]]:
        handler = super().get_route_handler()

        async def safe_handler(request: Request) -> Any:
            try:
                response = await handler(request)
            except RequestValidationError as error:
                # FastAPI's default errors include the rejected input, which can
                # contain an entire provider credential envelope.
                response = JSONResponse(
                    status_code=422, content={"code": 422, "message": "Invalid model request", "data": {"errors": [{"loc": list(item["loc"]), "type": item["type"]} for item in error.errors()]}}
                )
            except HTTPException as error:
                if isinstance(error.detail, dict) and "code" in error.detail:
                    response = JSONResponse(status_code=error.status_code, content=error.detail)
                else:
                    raise
            if request.url.path.endswith("/models/discovery"):
                response.headers["Cache-Control"] = "no-store"
            return response

        return safe_handler


router = APIRouter(route_class=ModelRoute)


def result(data: Any = None) -> dict[str, Any]:
    return {"code": 0, "message": "success", "data": data}


def failure(status: int, message: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": status, "message": message})


def page(items: list[Any], request: Request) -> dict[str, Any]:
    values = {}
    for name, default, upper in (("page", "1", 2147483647), ("page_size", "100", 100)):
        raw = request.query_params.getlist(name)
        if len(raw) > 1:
            raise failure(422, "Duplicate pagination parameter")
        value = raw[0] if raw else default
        if not re.fullmatch(r"[1-9][0-9]*", value) or int(value) > upper:
            raise failure(422, "Invalid pagination parameter")
        values[name] = int(value)
    start = (values["page"] - 1) * values["page_size"]
    return result({"items": items[start : start + values["page_size"]], "total": len(items), **values})


def flag(request: Request, name: str) -> bool:
    values = request.query_params.getlist(name)
    if len(values) > 1 or (values and values[0] not in {"true", "false"}):
        raise failure(422, "Invalid boolean query parameter")
    return bool(values and values[0] == "true")


async def write(db: AsyncSession, operation: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
    try:
        data = await operation()
        await db.commit()
        return result(data)
    except service.ModelConflict as error:
        await db.rollback()
        raise failure(409, str(error)) from None
    except LookupError:
        await db.rollback()
        raise failure(404, "Requested model resource not found") from None
    except ValueError as error:
        await db.rollback()
        raise failure(400, str(error)) from None
    except IntegrityError:
        await db.rollback()
        raise failure(409, "Model resource already exists or is referenced") from None
    except SQLAlchemyError:
        await db.rollback()
        raise failure(500, "Model resource operation failed") from None


@router.get("/providers")
async def providers(request: Request, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    if flag(request, "available"):
        items = [
            {"name": factory["name"], "model_types": sorted({capability for model in service.catalog(factory["name"]) for capability in model.model_types}), "status": "active"}
            for factory in public_factories()
        ]
    else:
        rows = (await db.scalars(select(TenantModelProvider).where(TenantModelProvider.tenant_id == user.id).order_by(TenantModelProvider.provider_name))).all()
        known = {factory["name"] for factory in public_factories()}
        stored_types = await service.provider_capabilities_for_owner(db, user.id)
        items = []
        for row in rows:
            catalog_types = {capability for model in service.catalog(row.provider_name) for capability in model.model_types} if row.provider_name in known else set()
            items.append({"id": row.id, "name": row.provider_name, "status": row.status, "model_types": sorted(catalog_types | stored_types.get(row.provider_name, set()))})
    return page(sorted(items, key=lambda item: provider_order(item["name"])), request)


@router.put("/providers")
async def add_provider(body: ProviderInput, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    return await write(db, lambda: service.add_provider(db, user.id, body.provider_name))


@router.delete("/providers/{provider_name}")
async def delete_provider(provider_name: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    return await write(db, lambda: service.delete_provider(db, user.id, provider_name))


@router.get("/providers/{provider_name}/models")
async def catalog_models(provider_name: str, request: Request, _user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    try:
        if provider_name not in {factory["name"] for factory in public_factories()}:
            raise LookupError("Provider not found")
        regions = request.query_params.getlist("region")
        if len(regions) > 1:
            raise failure(422, "Duplicate region parameter")
        return page([model.model_dump() for model in service.catalog(provider_name, regions[0] if regions else "")], request)
    except ValueError:
        raise failure(422, "Invalid provider region") from None
    except LookupError:
        raise failure(404, "Provider not found") from None


@router.get("/providers/{provider_name}/instances")
async def instances(provider_name: str, request: Request, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    try:
        provider = await service.provider_for_owner(db, user.id, provider_name)
    except LookupError:
        raise failure(404, "Provider not found") from None
    rows = (await db.scalars(select(TenantModelInstance).where(TenantModelInstance.provider_id == provider.id).order_by(TenantModelInstance.instance_name))).all()
    return page([service.instance_projection(instance, provider) for instance in rows], request)


@router.post("/providers/{provider_name}/models/discovery")
async def discover_models(provider_name: str, body: DiscoveryInput, request: Request, response: Response, _user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    from core.llm.model_meta import DiscoveryError

    response.headers["Cache-Control"] = "no-store"
    # Validate pagination before any remote call. Credentials belong only in the body.
    if set(request.query_params) - {"page", "page_size"}:
        raise failure(422, "Unexpected discovery query parameter")
    page([], request)
    try:
        return page([model.model_dump() for model in await service.discover_models(provider_name, body)], request)
    except LookupError:
        raise failure(404, "Provider not found") from None
    except DiscoveryError as error:
        raise failure(502, str(error)) from None
    except ValueError:
        raise failure(400, "Invalid or unavailable provider model discovery") from None


@router.post("/providers/{provider_name}/connection")
async def connection(provider_name: str, body: ConnectionCandidate, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    try:
        return result(await service.verify_candidate(db, user.id, provider_name, body))
    except LookupError:
        raise failure(404, "Requested model resource not found") from None
    except ValueError as error:
        raise failure(400, str(error)) from None


@router.post("/providers/{provider_name}/instances")
async def create_instance(provider_name: str, body: InstanceInput, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    return await write(db, lambda: service.create_instance(db, user.id, provider_name, body))


@router.patch("/providers/{provider_name}/instances/{instance_name}")
async def patch_instance(provider_name: str, instance_name: str, body: InstancePatch, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    return await write(db, lambda: service.patch_instance(db, user.id, provider_name, instance_name, body))


@router.delete("/providers/{provider_name}/instances/{instance_name}")
async def delete_instance(provider_name: str, instance_name: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    return await write(db, lambda: service.delete_instance(db, user.id, provider_name, instance_name))


@router.get("/providers/{provider_name}/instances/{instance_name}/models")
async def instance_models(provider_name: str, instance_name: str, request: Request, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    try:
        _provider, instance = await service.instance_for_owner(db, user.id, provider_name, instance_name)
    except LookupError:
        raise failure(404, "Instance not found") from None
    if flag(request, "supported"):
        return page([model.model_dump() for model in service.catalog(provider_name, service.instance_projection(instance, _provider)["region"])], request)
    return page([service.model_projection(*row) for row in await service.models_for_owner(db, user.id) if row[2].id == instance.id], request)


@router.post("/providers/{provider_name}/instances/{instance_name}/models")
async def add_model(provider_name: str, instance_name: str, body: ModelRegistration, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    return await write(db, lambda: service.add_model(db, user.id, provider_name, instance_name, body))


async def target_model(db: AsyncSession, user_id: str, provider_name: str, instance_name: str, model_id: str) -> tuple[Any, ...]:
    identifier = parse_model_id(model_id)
    if identifier is None:
        raise ValueError("Invalid model ID")
    await service.lock_owner(db, user_id)
    _provider, instance = await service.instance_for_owner(db, user_id, provider_name, instance_name)
    for row in await service.models_for_owner(db, user_id):
        if row[0].id == identifier and row[2].id == instance.id:
            return row
    raise LookupError("Model not found")


@router.patch("/providers/{provider_name}/instances/{instance_name}/models/{model_id}")
async def patch_model(
    provider_name: str, instance_name: str, model_id: str, body: StatusPatch, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)
) -> dict[str, Any]:
    async def operation() -> dict[str, Any]:
        row = await target_model(db, user.id, provider_name, instance_name, model_id)
        row[0].status = "1" if body.status == "active" else "0"
        await db.flush()
        return service.model_projection(*row)

    return await write(db, operation)


@router.delete("/providers/{provider_name}/instances/{instance_name}/models/{model_id}")
async def delete_model(provider_name: str, instance_name: str, model_id: str, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    async def operation() -> None:
        row = await target_model(db, user.id, provider_name, instance_name, model_id)
        await service.delete_models(db, [row])

    return await write(db, operation)


@router.get("/models/default")
async def default_models(db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    try:
        return result(await service.defaults_for_owner(db, user.id))
    except LookupError:
        raise failure(404, "Tenant not found") from None


@router.patch("/models/default")
async def set_default(body: DefaultModelInput, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    return await write(db, lambda: service.set_default(db, user.id, body.model_type, body.model_id))


@router.get("/models")
async def models(request: Request, db: AsyncSession = Depends(get_async_db), user: Principal = Depends(async_current_user)) -> dict[str, Any]:
    active = flag(request, "active")
    capabilities = request.query_params.getlist("model_type")
    if len(capabilities) > 1 or (capabilities and capabilities[0] not in service.DEFAULT_MODEL_FIELDS):
        raise failure(422, "Invalid model capability")
    items = [service.model_projection(*row) for row in await service.models_for_owner(db, user.id)]
    return page([item for item in items if (not active or item["effective_status"] == "active") and (not capabilities or capabilities[0] in item["model_types"])], request)
